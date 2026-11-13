DESCRIPTION = '字符终端的屏幕投影工具及可交互的本地台账程序。'
DEMO_PATH = '/screen'

import argparse
import json
import shlex
import sys
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .collector import Collector, CollectorFailure
from .parser import Screen, inspect_document
from .source_server import FixtureServer
from .transport import fetch


def replay_transcript(path):
    """把采集保存的 PTY 原始字节流重新投影成整屏（回放能力继续可用）。"""
    screen = Screen()
    screen.feed(Path(path).read_bytes().decode('utf-8'))
    result = inspect_document(Path(path).read_bytes())
    return {'path': str(path), 'final_screen': result['lines'],
            'cursor': result['cursor'], 'incomplete_control': result['incomplete_control']}


def run_collect(arguments):
    argv = shlex.split(arguments.program)
    collector = Collector(
        arguments.run_dir, argv=argv,
        settle_seconds=arguments.settle, timeout=arguments.timeout,
        pause_after_page=arguments.pause_after_page,
    )
    try:
        return collector.run()
    except CollectorFailure as failure:
        print(json.dumps({'status': 'failed', 'run_dir': arguments.run_dir,
                          'visible': failure.visible()}, ensure_ascii=False, indent=2),
              file=sys.stderr)
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('path')
    capture = commands.add_parser('capture')
    capture.add_argument('url')
    capture.add_argument('--db', default='captures.sqlite3')
    capture.add_argument('--objects', default='objects')
    show = commands.add_parser('show')
    show.add_argument('--db', default='captures.sqlite3')
    show.add_argument('--objects', default='objects')
    commands.add_parser('demo')
    replay = commands.add_parser('replay')
    replay.add_argument('path', help='采集保存的 PTY 原始字节流（raw/attempt-*.ansi）')
    collect = commands.add_parser('collect', help='通过 PTY 驱动本地终端程序采集台账')
    collect.add_argument('--run-dir', required=True, help='证据与导出目录，可重入续采')
    collect.add_argument('--program',
                         default=f'{sys.executable} -m termfolio.ledger',
                         help='本地终端程序启动命令（仅可由本进程在 PTY 中启动）')
    collect.add_argument('--pause-after-page', type=int, default=None,
                         help='完成指定页后主动退出程序，供演练中途退出与重入续采')
    collect.add_argument('--settle', type=float, default=0.25,
                         help='结构画面静止判定秒数，不是固定 sleep')
    collect.add_argument('--timeout', type=float, default=10.0)
    arguments = parser.parse_args()
    if arguments.command == 'inspect':
        result = inspect_document(Path(arguments.path).read_bytes())
    elif arguments.command in {'capture', 'show'}:
        archive = Archive(arguments.db, arguments.objects)
        try:
            if arguments.command == 'capture':
                status, content_type, body = fetch(arguments.url)
                result = {'capture_id': archive.save(arguments.url, status, content_type, body), 'http_status': status}
            else:
                result = archive.rows()
        finally:
            archive.close()
    elif arguments.command == 'replay':
        result = replay_transcript(arguments.path)
    elif arguments.command == 'collect':
        result = run_collect(arguments)
    else:
        with FixtureServer() as server, tempfile.TemporaryDirectory() as directory:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_address[1]) + DEMO_PATH
                status, content_type, body = fetch(url)
                archive = Archive(Path(directory) / 'demo.sqlite3', Path(directory) / 'objects')
                try:
                    capture_id = archive.save(url, status, content_type, body)
                    result = {'capture_id': capture_id, 'http_status': status, 'observation': inspect_document(archive.read(capture_id))}
                finally:
                    archive.close()
            finally:
                server.shutdown()
                thread.join(timeout=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
