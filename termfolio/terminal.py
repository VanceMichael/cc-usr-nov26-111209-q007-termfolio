"""PTY 终端驱动与整屏状态解释。

本模块只负责：
* 通过伪终端启动本地终端程序、收发按键与输出字节；
* 用屏幕投影器（parser.Screen）把字节流还原成完整屏幕；
* 等待“结构画面稳定”后再解释屏幕，而不是按固定时长等待，
  也不在原始输出流里搜索业务关键字；
* 解释刷新完成后的屏幕，给出版本、页号、焦点、模态与详情等状态。

采集器只能看到渲染后的屏幕与允许发送的按键，看不到台账程序的内部数据。
"""

import codecs
import errno
import fcntl
import os
import pty
import select
import struct
import subprocess
import termios
import time
from dataclasses import dataclass, field

from .parser import Screen

# 只允许翻页、定位、打开详情、返回、确认屏幕提示与退出。
# m / s 等写入入口永远不在允许集合内。
ALLOWED_KEYS = frozenset([b'n', b'p', b'j', b'k', b'o', b'b', b' ', b'q'])
KEY_NAMES = {b'n': 'n', b'p': 'p', b'j': 'j', b'k': 'k',
             b'o': 'o', b'b': 'b', b' ': '空格', b'q': 'q'}

STATE_LINE_MARKER = '版本'
CLOCK_ROW = 1  # 屏幕第 2 行是持续局部刷新的时钟，静止判断时屏蔽该行
STATE_ROW = 23  # 屏幕第 24 行是版本、页号与就绪状态
MODAL_MARKER = '台账已更新'
FORBIDDEN_MARKER = '禁止访问'


class TerminalTimeout(Exception):
    """结构画面在限定时间内没有稳定下来。"""

    def __init__(self, message, lines=None):
        super().__init__(message)
        self.lines = lines or []


class IllegalKey(ValueError):
    """请求发送的按键不在采集白名单内。"""


@dataclass
class ListRow:
    position: int          # 列表内位置，从 0 开始
    focused: bool
    box_prefix: str        # 列表只显示摘要中的箱号前缀


@dataclass
class ScreenState:
    mode: str                          # 列表 / 详情 / 提示 / 未知
    version: str | None
    page: int | None                   # 1 起始
    focus: int | None                  # 1 起始
    list_rows: list = field(default_factory=list)
    detail_box: str | None = None
    detail_note: str | None = None    # '' 表示备注本来为空；None 表示未读到备注字段
    modal: bool = False
    forbidden: bool = False
    ready: bool = False
    lines: list = field(default_factory=list)
    cursor: tuple = (1, 1)


def parse_state(screen):
    """把投影完成的整屏解释成 ScreenState。

    只依据刷新后的屏幕单元格内容，不接触原始字节流。
    """
    lines = screen.lines()
    state_line = lines[STATE_ROW] if len(lines) > STATE_ROW else ''
    version = page_text = focus_text = None
    mode = '未知'
    ready = False
    if state_line.startswith(STATE_LINE_MARKER):
        parts = state_line.split()
        for part in parts:
            if part.startswith('版本'):
                version = part[2:]
            elif part.startswith('焦点'):
                focus_text = part[2:]
            else:
                for candidate in ('列表', '详情', '提示'):
                    if part.startswith(candidate):
                        mode = candidate
                        if '页' in part:
                            page_text = part.split('页', 1)[1]
        ready = state_line.rstrip().endswith('就绪')
    page = int(page_text) if page_text and page_text.isdigit() else None
    focus = int(focus_text) if focus_text and focus_text.isdigit() else None

    list_rows = []
    if mode == '列表':
        for position in range(3):
            line = lines[position + 3] if len(lines) > position + 3 else ''
            if line[:1] in {'>', ' '} and len(line) >= 5:
                list_rows.append(ListRow(position, line[0] == '>', line[1:5]))

    detail_box = detail_note = None
    if mode == '详情':
        box_line = lines[4] if len(lines) > 4 else ''
        note_line = lines[6] if len(lines) > 6 else ''
        if box_line.startswith('完整箱号 '):
            detail_box = box_line[len('完整箱号 '):].strip()
        if note_line.startswith('备注'):
            # 屏幕上存在“备注”字段即详情渲染成功；冒号后没有内容属于本来为空。
            detail_note = note_line[len('备注'):].lstrip()
            if detail_note.startswith(' '):
                detail_note = detail_note[1:]

    modal = any(MODAL_MARKER in line for line in lines)
    forbidden = any(FORBIDDEN_MARKER in line for line in lines)
    return ScreenState(mode, version, page, focus, list_rows,
                       detail_box, detail_note, modal, forbidden, ready,
                       lines, (screen.row + 1, screen.column + 1))


def structural_snapshot(screen):
    """屏蔽时钟行后的整屏快照，用于静止判断。"""
    lines = list(screen.lines())
    if len(lines) > CLOCK_ROW:
        lines[CLOCK_ROW] = ''
    return tuple(lines)


class PtyDriver:
    """以 PTY 驱动一个本地终端程序。"""

    def __init__(self, argv, transcript_path, rows=24, columns=80,
                 settle_seconds=0.25, poll_seconds=0.05, timeout=10.0, env=None):
        self.argv = list(argv)
        self.transcript_path = transcript_path
        self.rows = rows
        self.columns = columns
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.timeout = timeout
        self.env = env
        self.master = None
        self.slave = None
        self.process = None
        self.screen = Screen(rows, columns)
        self.decoder = codecs.getincrementaldecoder('utf-8')()
        self.transcript = None
        self.eof = False

    def start(self):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ,
                    struct.pack('HHHH', self.rows, self.columns, 0, 0))
        environment = dict(os.environ if self.env is None else self.env)
        environment.setdefault('TERM', 'xterm')
        environment['PYTHONIOENCODING'] = 'utf-8'
        os.makedirs(os.path.dirname(self.transcript_path), exist_ok=True)
        self.transcript = open(self.transcript_path, 'wb')
        self.process = subprocess.Popen(
            self.argv, stdin=self.slave, stdout=self.slave, stderr=self.slave,
            close_fds=True, env=environment,
        )
        os.close(self.slave)
        self.slave = None
        return self

    def _drain(self):
        """读一次当前可读字节，写入原始证据并投影到屏幕。返回是否读到数据。"""
        if self.eof:
            return False
        ready, _, _ = select.select([self.master], [], [], self.poll_seconds)
        if not ready:
            return False
        try:
            chunk = os.read(self.master, 8192)
        except OSError as error:
            if error.errno == errno.EIO:
                self.eof = True
                return False
            raise
        if not chunk:
            self.eof = True
            return False
        self.transcript.write(chunk)
        self.transcript.flush()
        self.screen.feed(self.decoder.decode(chunk))
        return True

    def wait_stable(self):
        """等到结构画面（屏蔽时钟后）连续稳定再解释屏幕。

        时钟每 0.2 秒局部刷新但不改变结构快照；台账真正重绘会立刻改变
        结构快照并重新计时，因此这里不使用固定 sleep，也不搜索原始流。
        """
        deadline = time.monotonic() + self.timeout
        baseline = structural_snapshot(self.screen)
        stable_since = None
        while time.monotonic() < deadline:
            self._drain()
            current = structural_snapshot(self.screen)
            now = time.monotonic()
            if current != baseline:
                baseline = current
                stable_since = now
            elif stable_since is None and any(line.strip() for line in current):
                stable_since = now
            state = parse_state(self.screen)
            if (stable_since is not None and now - stable_since >= self.settle_seconds
                    and state.ready and not self.eof):
                return state
        if self.eof:
            raise TerminalTimeout('终端程序已退出，未取得稳定屏幕', self.screen.lines())
        raise TerminalTimeout('等待屏幕稳定超时', self.screen.lines())

    def send_key(self, key):
        if not isinstance(key, bytes) or key not in ALLOWED_KEYS:
            raise IllegalKey(f'按键不在采集白名单内: {key!r}')
        if self.eof:
            raise TerminalTimeout('终端程序已退出，无法发送按键', self.screen.lines())
        os.write(self.master, key)
        return KEY_NAMES[key]

    def wait_exit(self, timeout=5.0):
        if self.process is None:
            return -1
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            return self.process.wait(timeout=timeout)

    def close(self):
        if self.process is None:
            return
        if self.process.poll() is None:
            try:
                self.send_key(b'q')
            except (IllegalKey, TerminalTimeout, OSError):
                self.process.kill()
        code = self.wait_exit()
        if self.transcript is not None:
            self.transcript.close()
            self.transcript = None
        if self.master is not None:
            os.close(self.master)
            self.master = None
        self.process = None
        return code

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False
