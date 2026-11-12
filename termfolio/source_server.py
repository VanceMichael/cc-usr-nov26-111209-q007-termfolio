import argparse
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .hook import handle


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0):
        self.fixtures = Path(__file__).parent / 'fixtures'
        self.routes = json.loads((self.fixtures / 'routes.json').read_text())
        self.phase = 'initial'
        self.phase_lock = threading.Lock()
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *arguments):
        pass

    def do_GET(self):
        target = urllib.parse.urlsplit(self.path)
        handled = handle(self.server, self.path, self.headers)
        if handled is not None:
            self.respond(*handled)
            return
        if target.path == '/control/phase':
            phase = urllib.parse.parse_qs(target.query).get('value', ['initial'])[0]
            with self.server.phase_lock:
                self.server.phase = phase
            self.respond(200, 'application/json', json.dumps({'phase': phase}).encode())
            return
        with self.server.phase_lock:
            phase = self.server.phase
        entry = self.server.routes.get(phase + ':' + self.path, self.server.routes.get(self.path))
        if entry is None:
            self.respond(404, 'application/json', b'{"error":"unknown local route"}')
            return
        filename = (self.server.fixtures / entry['file']).resolve()
        if not filename.is_relative_to(self.server.fixtures.resolve()):
            self.respond(500, 'text/plain', b'invalid fixture path')
            return
        body = filename.read_bytes()
        if entry.get('encoding') == 'hex':
            body = bytes.fromhex(body.decode().removeprefix('HEX\n'))
        base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        body = body.replace(b'$BASE', base.encode())
        status = entry.get('status', 200)
        headers = dict(entry.get('headers', {}))
        byte_range = self.headers.get('Range')
        if byte_range:
            match = re.fullmatch(r'bytes=(\d+)-(\d+)', byte_range)
            if not match or int(match[1]) > int(match[2]) or int(match[2]) >= len(body):
                self.respond(416, 'text/plain', b'invalid range')
                return
            start, stop = int(match[1]), int(match[2])
            headers['Content-Range'] = f'bytes {start}-{stop}/{len(body)}'
            body, status = body[start:stop + 1], 206
        self.respond(status, entry.get('type', 'application/json'), body, headers)

    def respond(self, status, content_type, body, headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    arguments = parser.parse_args()
    with FixtureServer(arguments.port) as server:
        print(json.dumps({'base': 'http://127.0.0.1:' + str(server.server_address[1])}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
