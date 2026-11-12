DESCRIPTION = '字符终端的屏幕投影工具及可交互的本地台账程序。'
DEMO_PATH = '/screen'

import argparse
import json
import tempfile
import threading
from pathlib import Path

from .archive import Archive
from .parser import inspect_document
from .source_server import FixtureServer
from .transport import fetch


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
