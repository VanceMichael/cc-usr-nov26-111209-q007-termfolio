"""基于 PTY 屏幕的台账采集状态机。

采集纪律：
* 只发送白名单按键（翻页 n/p、定位 j/k、详情 o、返回 b、确认提示空格、退出 q），
  绝不触发 m/s 等写入或发运入口；
* 每一次按键都依据“刷新完成并稳定后的屏幕”决定下一步，不使用固定 sleep，
  也不在原始输出流里搜索关键字；
* 一份结果只来自同一台账版本：遇到“台账已更新”模态，确认后旧版本已抄行全部作废，
  新版本从首页重新采集；
* 重叠页靠完整箱号去重，并保留每次出现的屏幕证据与来源关联；
* 详情缺少箱号属于详情失败，备注字段存在但内容为空属于本来为空，二者明确区分；
* 所有原始字节流、屏幕快照、按键与事件都落盘，可重建、可审计；
* 中途退出后重入，先核对版本并逐页核对定位，再从下一个未完成页继续。
"""

import json
import os
import tempfile
import time
from pathlib import Path

from .terminal import IllegalKey, PtyDriver, TerminalTimeout


class CollectorFailure(Exception):
    """采集无法继续；携带运营可见的屏幕状态，便于人工接手。"""

    def __init__(self, reason, state=None):
        super().__init__(reason)
        self.reason = reason
        self.state = state

    def visible(self):
        if self.state is None:
            return {'reason': self.reason, 'visible_lines': []}
        return {
            'reason': self.reason,
            'mode': self.state.mode,
            'version': self.state.version,
            'page': self.state.page,
            'focus': self.state.focus,
            'visible_lines': list(self.state.lines),
        }


class VersionReset(Exception):
    """确认更新提示后台账版本发生变化，本轮结果必须作废重来。"""

    def __init__(self, old_version, new_version):
        super().__init__(f'台账版本由 {old_version} 变为 {new_version}')
        self.old_version = old_version
        self.new_version = new_version


class RunStore:
    """一次采集运行的证据仓库（追加写、原子状态、内容寻址屏幕）。"""

    def __init__(self, directory):
        self.directory = Path(directory)
        for sub in ('raw', 'screens', 'keys'):
            (self.directory / sub).mkdir(parents=True, exist_ok=True)
        self.journal_path = self.directory / 'journal.jsonl'
        self.state_path = self.directory / 'state.json'
        self.export_path = self.directory / 'export.json'
        self.sequence = 0
        if self.journal_path.exists():
            with self.journal_path.open(encoding='utf-8') as stream:
                self.sequence = sum(1 for _ in stream)
        self.journal = self.journal_path.open('a', encoding='utf-8')

    def event(self, event_type, **payload):
        self.sequence += 1
        record = {'seq': self.sequence, 'time': time.time(),
                  'event': event_type, **payload}
        self.journal.write(json.dumps(record, ensure_ascii=False) + '\n')
        self.journal.flush()
        return self.sequence

    def save_snapshot(self, state, label, attempt):
        """保存刷新后整屏的文本证据与结构化状态，返回可追溯引用。"""
        seq = self.event('screen_snapshot', label=label, attempt=attempt,
                         mode=state.mode, version=state.version,
                         page=state.page, focus=state.focus)
        path = self.directory / 'screens' / f'{seq:04d}-{label}.txt'
        body = '\n'.join(state.lines) + '\n'
        path.write_text(body, encoding='utf-8')
        return {'seq': seq, 'path': str(path.relative_to(self.directory)),
                'mode': state.mode, 'version': state.version,
                'page': state.page, 'focus': state.focus,
                'state_line': state.lines[23] if len(state.lines) > 23 else ''}

    def save_key(self, key_name, attempt):
        path = self.directory / 'keys' / f'attempt-{attempt}.log'
        with path.open('a', encoding='utf-8') as stream:
            stream.write(f'{self.sequence + 1:04d} {time.time():.3f} {key_name}\n')

    def save_state(self, document):
        descriptor, temporary = tempfile.mkstemp(prefix='.state-', dir=self.directory)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, self.state_path)

    def close(self):
        self.journal.close()


def default_driver_factory(argv, settle_seconds, timeout):
    def factory(transcript_path):
        return PtyDriver(argv, transcript_path,
                         settle_seconds=settle_seconds, timeout=timeout)
    return factory


class Collector:
    def __init__(self, run_dir, argv=None, driver_factory=None,
                 settle_seconds=0.25, timeout=10.0, pause_after_page=None):
        self.store = RunStore(run_dir)
        self.argv = argv
        self.driver_factory = driver_factory or default_driver_factory(
            argv, settle_seconds, timeout)
        self.pause_after_page = pause_after_page
        self.attempt = 0
        self.driver = None
        self.state = None
        self.target_version = None
        self.start_page = 1
        self.entries = {}        # 完整箱号 -> 规范条目（含 sources 来源关联）
        self.observations = []   # 每次详情读取，重叠行每次出现都留痕
        self.completed_pages = []
        self.paused = False
        self.failure = None
        self.expect_startup_modal = False

    # ---------- 对外入口 ----------

    def run(self):
        try:
            self._load_resume()
            try:
                self._run_attempt()
            except VersionReset as reset:
                self._reset_after_update(reset)
                # 新会话必须从新版本首页重采，续采页码不再适用。
                self.start_page = 1
                self._run_attempt()
        except CollectorFailure as failure:
            self.failure = failure
            self._persist_failure(failure)
            self.store.close()
            raise
        if not self.paused:
            self._finalize()
        self.store.close()
        return {
            'status': 'paused' if self.paused else 'complete',
            'run_dir': str(self.store.directory),
            'target_version': self.target_version,
            'box_count': len(self.entries),
            'next_page': (self.completed_pages[-1] + 1) if self.paused else None,
            'export': str(self.store.export_path) if not self.paused else None,
        }

    # ---------- 断点续采 ----------

    def _load_resume(self):
        if not self.store.state_path.exists():
            self.store.event('run_start', mode='fresh')
            return
        document = json.loads(self.store.state_path.read_text(encoding='utf-8'))
        status = document.get('status')
        if status == 'complete':
            raise CollectorFailure('该采集运行已完成，如需重采请新建运行目录')
        if status == 'failed':
            raise CollectorFailure(
                '该采集运行处于失败状态，请按 state.json 中的可见状态人工接手后新建运行')
        if status != 'paused':
            raise CollectorFailure(f'未知的运行状态: {status}')
        self.target_version = document['target_version']
        self.start_page = document['next_page']
        self.entries = document['entries']
        self.observations = document.get('observations', [])
        self.completed_pages = document['completed_pages']
        self.attempt = document.get('attempt', 0)
        self.store.event('resume_requested', target_version=self.target_version,
                         next_page=self.start_page,
                         known_boxes=list(self.entries))

    def _reset_after_update(self, reset):
        self.store.event('version_reset', old_version=reset.old_version,
                         new_version=reset.new_version,
                         discarded_observations=len(self.observations),
                         discarded_boxes=list(self.entries))
        # 旧版本已抄行一律不得混入新结果：清空内存结果，证据仍保留在日志中。
        self.entries = {}
        self.observations = []
        self.completed_pages = []
        self.start_page = 1
        self.target_version = reset.new_version
        # 新进程仍从旧版本号启动，它的首个更新模态是进入新版本的预期路径。
        self.expect_startup_modal = True

    # ---------- 单轮程序会话 ----------

    def _run_attempt(self):
        self.attempt += 1
        transcript = self.store.directory / 'raw' / f'attempt-{self.attempt}.ansi'
        self.driver = self.driver_factory(str(transcript))
        self.driver.start()
        try:
            self.state = self._stable('启动后首屏')
            self._guard(self.state)
            if self.state.modal:
                self._confirm_update_modal()
            if self.target_version is None:
                self.target_version = self.state.version
                self.store.event('version_locked', version=self.target_version,
                                 attempt=self.attempt)
            elif self.expect_startup_modal:
                # 新进程从旧版本号启动，预期首个详情后会弹出更新模态把它带到目标版本。
                if self.state.version == self.target_version:
                    self.expect_startup_modal = False
                else:
                    self.store.event('awaiting_startup_update',
                                     screen_version=self.state.version,
                                     target_version=self.target_version,
                                     attempt=self.attempt)
            elif self.state.version != self.target_version:
                raise CollectorFailure(
                    f'重入后版本为 {self.state.version}，与上次运行的 {self.target_version} 不一致',
                    self.state)
            self._persist_running()
            self._navigate_to(self.start_page)
            page = self.start_page
            while page <= 10000:
                self._expect(self.state, mode='列表', page=page)
                restart = self._collect_page(page)
                if restart:
                    # 启动更新把屏幕带回新版本首页，本会话从首页重新采集。
                    self.start_page = 1
                    self.entries = {box: entry for box, entry in self.entries.items()
                                    if entry['version'] == self.target_version}
                    self.observations = [item for item in self.observations
                                         if item['version'] == self.target_version]
                    self.completed_pages = []
                    page = 1
                    self._expect(self.state, mode='列表', page=1, focus=1)
                    continue
                if self.paused:
                    return
                advanced = self._advance_page(page)
                if not advanced:
                    self.store.event('ledger_end', page=page,
                                     version=self.target_version)
                    return
                page += 1
            raise CollectorFailure('翻页步数超过安全上限，屏幕状态不可信', self.state)
        except TerminalTimeout as error:
            raise CollectorFailure(str(error),
                                   _LinesState(error.lines)) from error
        finally:
            self._close_driver()

    def _close_driver(self):
        if self.driver is None:
            return
        try:
            self.driver.close()
        finally:
            self.driver = None

    def _quit_program(self, purpose):
        name = self.driver.send_key(b'q')
        self.store.save_key(name, self.attempt)
        self.store.event('key', key=name, purpose=purpose, attempt=self.attempt)
        code = self.driver.wait_exit()
        self.store.event('program_exit', returncode=code, attempt=self.attempt)

    # ---------- 屏幕与按键原语 ----------

    def _stable(self, label):
        state = self.driver.wait_stable()
        self.store.save_snapshot(state, label, self.attempt)
        return state

    def _guard(self, state):
        if state.forbidden:
            raise CollectorFailure('台账程序报告禁止访问写入操作，采集立即停止', state)
        if state.mode == '未知' or not state.ready:
            raise CollectorFailure('屏幕状态行无法识别，无法安全决定下一步按键', state)

    def _expect(self, state, mode=None, page=None, focus=None):
        if mode is not None and state.mode != mode:
            raise CollectorFailure(
                f'期望{mode}屏幕，实际为{state.mode}（页{state.page} 焦点{state.focus}）', state)
        if page is not None and state.page != page:
            raise CollectorFailure(f'期望第{page}页，实际第{state.page}页', state)
        if focus is not None and state.focus != focus:
            raise CollectorFailure(f'期望焦点{focus}，实际焦点{state.focus}', state)
        self._guard(state)
        return state

    def _press(self, key, purpose):
        """发送一个白名单按键，等待整屏刷新稳定，处理可能插入的模态。"""
        before = self.store.save_snapshot(self.state, purpose + '-按键前', self.attempt)
        name = self.driver.send_key(key)
        self.store.save_key(name, self.attempt)
        self.store.event('key', key=name, purpose=purpose, attempt=self.attempt,
                         before_snapshot=before)
        state = self.driver.wait_stable()
        after = self.store.save_snapshot(state, purpose + '-按键后', self.attempt)
        self.state = state
        self._guard(state)
        if state.modal:
            self._confirm_update_modal()
            # 模态确认已经重绘屏幕，按键后的证据以确认后的屏幕为准。
            state = self.state
            after = self.store.save_snapshot(state, purpose + '-模态确认后', self.attempt)
        return state, after

    def _confirm_update_modal(self):
        state = self.state
        if not state.modal:
            return
        self.store.event('modal_observed',
                         snapshot=self.store.save_snapshot(
                             state, '更新模态', self.attempt),
                         attempt=self.attempt)
        screen_version = state.version
        name = self.driver.send_key(b' ')
        self.store.save_key(name, self.attempt)
        self.store.event('key', key=name, purpose='确认更新提示',
                         attempt=self.attempt)
        confirmed = self.driver.wait_stable()
        self.store.save_snapshot(confirmed, '更新确认后', self.attempt)
        self.state = confirmed
        self._guard(confirmed)
        if confirmed.modal:
            raise CollectorFailure('确认后更新模态仍未消失', confirmed)
        if self.expect_startup_modal:
            # 上一轮确认更新后新起的进程仍从旧版本号启动，首个模态把它带到
            # 本次运行真正要采集的新版本：这是预期的启动路径，不是再次更新。
            self.expect_startup_modal = False
            self.store.event('startup_update_cleared',
                             old_version=screen_version, new_version=confirmed.version,
                             attempt=self.attempt)
            if self.target_version != confirmed.version:
                raise CollectorFailure(
                    f'启动更新后版本 {confirmed.version} 与目标版本 {self.target_version} 不一致',
                    confirmed)
            return
        if self.target_version is not None and confirmed.version == self.target_version:
            # 重入的进程首屏即处于目标版本，确认提示不改变版本边界。
            self.store.event('startup_modal_cleared',
                             version=confirmed.version, attempt=self.attempt)
            return
        if confirmed.version == screen_version:
            raise CollectorFailure('确认更新后版本号没有变化，无法判断台账边界', confirmed)
        raise VersionReset(screen_version, confirmed.version)

    # ---------- 定位与翻页 ----------

    def _navigate_to(self, target_page):
        """重入后逐页核对定位；每一跳都以刷新后的状态行为准。"""
        state = self.state
        guard = 0
        while state.page != target_page:
            guard += 1
            if guard > 100:
                raise CollectorFailure('重入定位超过安全步数', state)
            if state.page < target_page:
                state, _ = self._press(b'n', f'重入定位第{target_page}页')
            else:
                state, _ = self._press(b'p', f'重入定位第{target_page}页')
            self._expect(state, mode='列表')
            if not state.list_rows:
                raise CollectorFailure('定位途中列表页没有可读行', state)
        self._expect(state, mode='列表', page=target_page, focus=1)
        self.store.event('position_verified', page=target_page,
                         version=state.version, attempt=self.attempt)

    def _advance_page(self, page):
        state, _ = self._press(b'n', f'第{page}页向后翻页')
        self._expect(state, mode='列表')
        if state.page == page:
            # 页号未变：这是重复页，说明已经到末页，而不是翻页成功。
            self.store.event('page_repeated', page=page,
                             version=self.target_version, attempt=self.attempt)
            return False
        if state.page != page + 1:
            raise CollectorFailure(
                f'翻页后页号由{page}跳到{state.page}，屏幕状态不可信', state)
        self.store.event('page_advanced', page=page, next_page=state.page,
                         version=self.target_version, attempt=self.attempt)
        return True

    # ---------- 逐行详情采集 ----------

    def _collect_page(self, page):
        state = self._expect(self.state, mode='列表', page=page)
        row_count = len(state.list_rows)
        if row_count == 0:
            raise CollectorFailure(f'第{page}页没有任何列表行', state)
        self.store.event('page_start', page=page, row_count=row_count,
                         version=self.target_version, attempt=self.attempt)
        restarted = False
        for position in range(row_count):
            wanted_focus = position + 1
            # 详情返回后焦点会回到第一行，这里依据实时屏幕重新定位，不做假设。
            while state.focus < wanted_focus:
                state, _ = self._press(b'j', f'第{page}页定位第{wanted_focus}行')
                self._expect(state, mode='列表', page=page)
            self._expect(state, mode='列表', page=page, focus=wanted_focus)
            list_prefix = state.list_rows[position].box_prefix
            detail, detail_ref = self._press(b'o', f'第{page}页第{wanted_focus}行打开详情')
            self._expect(detail, mode='详情', page=page)
            if not detail.detail_box:
                raise CollectorFailure(
                    f'第{page}页第{wanted_focus}行详情打开后读不到完整箱号（详情失败）', detail)
            if not detail.detail_box.startswith(list_prefix):
                raise CollectorFailure(
                    f'详情箱号 {detail.detail_box} 与列表摘要 {list_prefix} 对不上，焦点关联可疑',
                    detail)
            if detail.detail_note is None:
                # 备注字段本身缺失：读取失败，不能当作空备注导出。
                raise CollectorFailure(
                    f'第{page}页第{wanted_focus}行详情缺少备注字段，无法区分空备注与读取失败',
                    detail)
            note = detail.detail_note  # '' 即备注本来为空，属于正常结果
            returned, return_ref = self._press(b'b', f'第{page}页第{wanted_focus}行详情返回')
            self._expect(returned, mode='列表', page=page)
            if detail.version != self.target_version or returned.version != self.target_version:
                # 启动更新期间详情屏或返回屏属于旧版本：只留痕不入结果，
                # 整个会话稍后在新版本上从首页重新读取。
                self.store.event('foreign_observation_discarded',
                                 box=detail.detail_box,
                                 detail_version=detail.version,
                                 returned_version=returned.version,
                                 target_version=self.target_version,
                                 attempt=self.attempt)
                restarted = True
                break
            self._merge_observation({
                'version': detail.version,
                'page': page,
                'focus': wanted_focus,
                'box': detail.detail_box,
                'note': note,
                'detail_snapshot': detail_ref,
                'return_snapshot': return_ref,
            })
        if restarted:
            return True
        self.completed_pages.append(page)
        self.store.event('page_complete', page=page,
                         unique_boxes=len(self.entries),
                         version=self.target_version, attempt=self.attempt)
        self._persist_running()
        if self.pause_after_page == page:
            self.paused = True
            self._quit_program(f'已完成第{page}页，按计划中途退出')
            self.store.save_state(self._state_document('paused'))
            self.store.event('run_paused', next_page=page + 1,
                             version=self.target_version)
        return False

    def _merge_observation(self, observation):
        self.observations.append(observation)
        box = observation['box']
        source = {k: observation[k] for k in
                  ('version', 'page', 'focus', 'detail_snapshot', 'return_snapshot')}
        if box in self.entries:
            existing = self.entries[box]
            if existing['version'] != observation['version'] or existing['note'] != observation['note']:
                raise CollectorFailure(
                    f'同一箱号 {box} 在不同屏幕读到不一致内容，采集停止以避免抄错',
                    self.state)
            existing['sources'].append(source)
            self.store.event('overlap_duplicate', box=box, page=observation['page'],
                             version=observation['version'], attempt=self.attempt)
            return
        self.entries[box] = {
            'box': box,
            'note': observation['note'],
            'version': observation['version'],
            'first_seen_page': observation['page'],
            'sources': [source],
        }
        self.store.event('row_collected', box=box, page=observation['page'],
                         note_empty=observation['note'] == '',
                         version=observation['version'], attempt=self.attempt)

    # ---------- 状态持久化与导出 ----------

    def _state_document(self, status, extra=None):
        document = {
            'status': status,
            'target_version': self.target_version,
            'completed_pages': list(self.completed_pages),
            'next_page': (self.completed_pages[-1] + 1) if self.completed_pages else 1,
            'entries': self.entries,
            'observations': self.observations,
            'attempt': self.attempt,
        }
        if extra:
            document.update(extra)
        return document

    def _persist_running(self):
        self.store.save_state(self._state_document('running'))

    def _persist_failure(self, failure):
        document = self._state_document('failed', {'visible': failure.visible()})
        self.store.save_state(document)
        self.store.event('collection_failed', reason=failure.reason,
                         visible=failure.visible())

    def _finalize(self):
        if not self.entries:
            raise CollectorFailure('没有采集到任何装箱条目', self.state)
        versions = {entry['version'] for entry in self.entries.values()}
        if versions != {self.target_version}:
            raise CollectorFailure(
                f'结果混入了其他台账版本: {sorted(v for v in versions if v != self.target_version)}',
                self.state)
        ordered_boxes = []
        for observation in self.observations:
            if observation['box'] not in ordered_boxes:
                ordered_boxes.append(observation['box'])
        document = {
            'status': 'complete',
            'target_version': self.target_version,
            'box_count': len(ordered_boxes),
            'observation_count': len(self.observations),
            'overlap_duplicates': len(self.observations) - len(ordered_boxes),
            'completed_pages': list(self.completed_pages),
            'boxes': [self.entries[box] for box in ordered_boxes],
            'evidence': {
                'journal': 'journal.jsonl',
                'state': 'state.json',
                'raw_streams': 'raw/',
                'screen_snapshots': 'screens/',
                'key_logs': 'keys/',
            },
        }
        descriptor, temporary = tempfile.mkstemp(prefix='.export-', dir=self.store.directory)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, self.store.export_path)
        self.store.save_state(self._state_document('complete'))
        self.store.event('export_written', path=str(self.store.export_path),
                         box_count=len(ordered_boxes),
                         version=self.target_version)


class _LinesState:
    """超时时只有文本行时的最小状态壳，供失败落盘使用。"""

    def __init__(self, lines):
        self.mode = '未知'
        self.version = None
        self.page = None
        self.focus = None
        self.lines = lines
