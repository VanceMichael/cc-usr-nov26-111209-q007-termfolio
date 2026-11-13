import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from termfolio.collector import Collector, CollectorFailure
from termfolio.parser import Screen, inspect_document
from termfolio.terminal import (ALLOWED_KEYS, IllegalKey, PtyDriver,
                                ScreenState, parse_state)

LEDGER = [sys.executable, '-m', 'termfolio.ledger']


def ledger_argv(update_after_details):
    return LEDGER + ['--update-after-details', str(update_after_details)]


def load_export(run_dir):
    return json.loads((Path(run_dir) / 'export.json').read_text(encoding='utf-8'))


def load_state(run_dir):
    return json.loads((Path(run_dir) / 'state.json').read_text(encoding='utf-8'))


def journal_events(run_dir):
    return [json.loads(line) for line in
            (Path(run_dir) / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]


class ScreenStateTests(unittest.TestCase):
    def test_state_line_with_clock_noise_and_overwrite(self):
        # 时钟行变化不应影响结构状态解析；局部覆盖后以新单元格为准。
        screen = Screen()
        screen.feed('\x1b[2J\x1b[1;1H合作仓库装箱台账\x1b[2;1H时钟 10:00:00\x1b[K')
        screen.feed('\x1b[4;1H>箱001 摘要\x1b[22;1Hn下一页 p上一页 j下移 k上移 o详情 q退出')
        screen.feed('\x1b[24;1H版本v1 列表页1 焦点1 就绪')
        first = parse_state(screen)
        self.assertEqual((first.mode, first.version, first.page, first.focus),
                         ('列表', 'v1', 1, 1))
        self.assertEqual(first.list_rows[0].box_prefix, '箱001')
        self.assertTrue(first.list_rows[0].focused)
        self.assertTrue(first.ready)
        # 时钟局部刷新
        screen.feed('\x1b[2;1H时钟 10:00:01\x1b[K')
        second = parse_state(screen)
        self.assertEqual(second.version, 'v1')
        self.assertEqual(second.lines[3], '>箱001 摘要')

    def test_detail_state_distinguishes_empty_note_from_missing_field(self):
        screen = Screen()
        screen.feed('\x1b[2J\x1b[5;1H完整箱号 箱002完整号\x1b[7;1H备注 \x1b[24;1H版本v1 详情页1 焦点2 就绪')
        state = parse_state(screen)
        self.assertEqual(state.mode, '详情')
        self.assertEqual(state.detail_box, '箱002完整号')
        self.assertEqual(state.detail_note, '')  # 字段存在但为空：本来为空

    def test_modal_and_forbidden_detection(self):
        screen = Screen()
        screen.feed('\x1b[2J\x1b[10;1H台账已更新，空格确认重新读取\x1b[24;1H版本v1 提示页1 焦点1 就绪')
        self.assertTrue(parse_state(screen).modal)
        screen2 = Screen()
        screen2.feed('\x1b[2J\x1b[20;1H禁止访问写入操作\x1b[24;1H版本v1 列表页1 焦点1 就绪')
        self.assertTrue(parse_state(screen2).forbidden)


class FakeDriver:
    """按预制 ANSI 帧序列响应按键的假驱动，用于失败分支与白名单测试。"""

    def __init__(self, frames, transcript_path):
        self.frames = list(frames)
        self.transcript_path = transcript_path
        self.sent = []
        self.closed = False
        self.screen = Screen()
        self.frame_index = 0
        Path(transcript_path).parent.mkdir(parents=True, exist_ok=True)
        self.transcript = open(transcript_path, 'wb')
        self._advance()

    def _advance(self):
        if self.frame_index < len(self.frames):
            chunk = self.frames[self.frame_index].encode()
            self.frame_index += 1
            self.transcript.write(chunk)
            self.transcript.flush()
            self.screen.feed(chunk.decode())

    def start(self):
        return self

    def wait_stable(self):
        return parse_state(self.screen)

    def send_key(self, key):
        if key not in ALLOWED_KEYS:
            raise IllegalKey(f'按键不在采集白名单内: {key!r}')
        self.sent.append(key)
        self._advance()
        return {b'n': 'n', b'p': 'p', b'j': 'j', b'k': 'k',
                b'o': 'o', b'b': 'b', b' ': '空格', b'q': 'q'}[key]

    def wait_exit(self, timeout=5.0):
        return 0

    def close(self):
        self.closed = True
        self.transcript.close()


class CollectorPtyTests(unittest.TestCase):
    def test_full_stable_collection_with_overlap(self):
        with tempfile.TemporaryDirectory() as run_dir:
            result = Collector(run_dir, argv=ledger_argv(0)).run()
            self.assertEqual(result['status'], 'complete')
            export = load_export(run_dir)
            self.assertEqual(export['target_version'], 'v1')
            self.assertEqual(export['box_count'], 5)
            # 两页各 3 行、重叠 1 行：6 次观察，去重后 5 箱
            self.assertEqual(export['observation_count'], 6)
            self.assertEqual(export['overlap_duplicates'], 1)
            boxes = [row['box'] for row in export['boxes']]
            self.assertEqual(boxes, ['箱001完整号', '箱002完整号', '箱003完整号',
                                     '箱004完整号', '箱005完整号'])
            notes = {row['box']: row['note'] for row in export['boxes']}
            self.assertEqual(notes['箱002完整号'], '')           # 本来为空
            self.assertEqual(notes['箱003完整号'], '交接需复核')
            # 重叠行保留两次来源关联
            overlap = next(row for row in export['boxes'] if row['box'] == '箱003完整号')
            self.assertEqual(len(overlap['sources']), 2)
            self.assertEqual({s['page'] for s in overlap['sources']}, {1, 2})
            # 证据齐全且可重建
            events = journal_events(run_dir)
            self.assertIn('page_advanced', [e['event'] for e in events])
            self.assertIn('page_repeated', [e['event'] for e in events])
            for raw in (Path(run_dir) / 'raw').iterdir():
                replayed = inspect_document(raw.read_bytes())
                self.assertIn('就绪', replayed['lines'][23])
            key_logs = list((Path(run_dir) / 'keys').iterdir())
            pressed = {line.split()[2] for path in key_logs for line in path.read_text().splitlines()}
            self.assertTrue(pressed <= {'n', 'p', 'j', 'k', 'o', 'b', '空格', 'q'})
            self.assertNotIn('m', pressed)
            self.assertNotIn('s', pressed)

    def test_update_modal_invalidates_old_rows_and_recollects(self):
        with tempfile.TemporaryDirectory() as run_dir:
            result = Collector(run_dir, argv=ledger_argv(1)).run()
            self.assertEqual(result['status'], 'complete')
            export = load_export(run_dir)
            self.assertEqual(export['target_version'], 'v2')
            self.assertEqual(export['box_count'], 6)  # v2 新增箱006
            self.assertEqual({row['version'] for row in export['boxes']}, {'v2'})
            events = journal_events(run_dir)
            kinds = [e['event'] for e in events]
            self.assertIn('modal_observed', kinds)
            self.assertIn('version_reset', kinds)
            self.assertIn('foreign_observation_discarded', kinds)
            reset = next(e for e in events if e['event'] == 'version_reset')
            self.assertEqual(reset['old_version'], 'v1')
            self.assertEqual(reset['new_version'], 'v2')
            # 两个 PTY 会话各自保留原始字节流
            self.assertEqual(sorted(p.name for p in (Path(run_dir) / 'raw').iterdir()),
                             ['attempt-1.ansi', 'attempt-2.ansi'])

    def test_pause_and_resume_verifies_position(self):
        with tempfile.TemporaryDirectory() as run_dir:
            first = Collector(run_dir, argv=ledger_argv(0),
                              pause_after_page=1).run()
            self.assertEqual(first['status'], 'paused')
            self.assertEqual(first['next_page'], 2)
            paused = load_state(run_dir)
            self.assertEqual(paused['status'], 'paused')
            self.assertEqual(list(paused['entries']),
                             ['箱001完整号', '箱002完整号', '箱003完整号'])
            # 重入续采：新进程、新原始流，先定位核对再继续
            second = Collector(run_dir, argv=ledger_argv(0)).run()
            self.assertEqual(second['status'], 'complete')
            export = load_export(run_dir)
            self.assertEqual(export['box_count'], 5)
            self.assertEqual(export['observation_count'], 6)
            events = journal_events(run_dir)
            self.assertIn('resume_requested', [e['event'] for e in events])
            verify = [e for e in events if e['event'] == 'position_verified']
            self.assertEqual(verify[-1]['page'], 2)
            self.assertEqual(sorted(p.name for p in (Path(run_dir) / 'raw').iterdir()),
                             ['attempt-1.ansi', 'attempt-2.ansi'])

    def test_resume_then_update_still_produces_single_version_result(self):
        with tempfile.TemporaryDirectory() as run_dir:
            Collector(run_dir, argv=ledger_argv(0), pause_after_page=1).run()
            # 重入时换成会更新的台账程序：续采途中弹模态，最终结果只能是 v2
            result = Collector(run_dir, argv=ledger_argv(1)).run()
            self.assertEqual(result['status'], 'complete')
            export = load_export(run_dir)
            self.assertEqual(export['target_version'], 'v2')
            self.assertEqual(export['box_count'], 6)
            self.assertEqual({row['version'] for row in export['boxes']}, {'v2'})
            self.assertEqual(export['overlap_duplicates'], 2)

    def test_illegal_key_is_refused(self):
        with tempfile.TemporaryDirectory() as run_dir:
            driver = PtyDriver(ledger_argv(0), str(Path(run_dir) / 'raw' / 'x.ansi'))
            with self.assertRaises(IllegalKey):
                driver.send_key(b'm')
            with self.assertRaises(IllegalKey):
                driver.send_key(b's')
            with self.assertRaises(IllegalKey):
                driver.send_key(b'\x03')

    def test_failure_reports_visible_screen_state(self):
        # 详情屏缺少完整箱号：必须判定为详情失败并给出可见状态
        list_frame = ('\x1b[2J\x1b[4;1H>箱001 摘要'
                      '\x1b[22;1Hn下一页 p上一页 j下移 k上移 o详情 q退出'
                      '\x1b[24;1H版本v1 列表页1 焦点1 就绪')
        broken_detail = ('\x1b[2J\x1b[5;1H读取异常'
                         '\x1b[22;1Hb返回 q退出'
                         '\x1b[24;1H版本v1 详情页1 焦点1 就绪')
        with tempfile.TemporaryDirectory() as run_dir:
            frames = [list_frame, broken_detail]
            collector = Collector(
                run_dir,
                driver_factory=lambda path: FakeDriver(frames, path))
            with self.assertRaises(CollectorFailure) as caught:
                collector.run()
            visible = caught.exception.visible()
            self.assertIn('详情失败', visible['reason'])
            self.assertEqual(visible['mode'], '详情')
            self.assertIn('读取异常', visible['visible_lines'])
            state = load_state(run_dir)
            self.assertEqual(state['status'], 'failed')
            self.assertIn('visible', state)

    def test_failed_run_cannot_be_silently_resumed(self):
        list_frame = ('\x1b[2J\x1b[4;1H>箱001 摘要'
                      '\x1b[24;1H版本v1 列表页1 焦点1 就绪')
        broken_detail = ('\x1b[2J\x1b[5;1H读取异常'
                         '\x1b[24;1H版本v1 详情页1 焦点1 就绪')
        with tempfile.TemporaryDirectory() as run_dir:
            with self.assertRaises(CollectorFailure):
                Collector(run_dir,
                          driver_factory=lambda path: FakeDriver([list_frame, broken_detail], path)).run()
            with self.assertRaises(CollectorFailure):
                Collector(run_dir,
                          driver_factory=lambda path: FakeDriver([list_frame], path)).run()


class CliTests(unittest.TestCase):
    def test_collect_and_replay_cli(self):
        with tempfile.TemporaryDirectory() as run_dir:
            collect = subprocess.run(
                [sys.executable, '-m', 'termfolio', 'collect',
                 '--run-dir', run_dir,
                 '--program', ' '.join(ledger_argv(0))],
                capture_output=True, text=True)
            self.assertEqual(collect.returncode, 0, collect.stderr)
            result = json.loads(collect.stdout)
            self.assertEqual(result['box_count'], 5)
            raw = Path(run_dir) / 'raw' / 'attempt-1.ansi'
            replay = subprocess.run(
                [sys.executable, '-m', 'termfolio', 'replay', str(raw)],
                capture_output=True, text=True)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            replayed = json.loads(replay.stdout)
            self.assertIn('就绪', replayed['final_screen'][23])

    def test_failure_exit_code_and_visible_state_on_stderr(self):
        with tempfile.TemporaryDirectory() as run_dir:
            result = subprocess.run(
                [sys.executable, '-m', 'termfolio', 'collect',
                 '--run-dir', run_dir,
                 '--program', f'{sys.executable} -c "import time; time.sleep(30)"',
                 '--timeout', '1'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            payload = json.loads(result.stderr)
            self.assertEqual(payload['status'], 'failed')
            self.assertIn('reason', payload['visible'])


if __name__ == '__main__':
    unittest.main()
