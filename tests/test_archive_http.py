import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from termfolio.archive import Archive
from termfolio.parser import inspect_document
from termfolio.source_server import FixtureServer
from termfolio.transport import fetch


class ArchiveTests(unittest.TestCase):
    def test_binary_roundtrip_and_shared_object(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory) / 'raw.sqlite3', Path(directory) / 'objects')
            try:
                body = b'\x00\xff\r\n'
                first = archive.save('local:one', 200, 'application/octet-stream', body)
                second = archive.save('local:two', 200, 'application/octet-stream', body)
                self.assertNotEqual(first, second)
                self.assertEqual(archive.read(first), body)
                self.assertEqual(len(list(archive.objects.iterdir())), 1)
                self.assertEqual(len(archive.rows()), 2)
            finally:
                archive.close()

    def test_reopen_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            database, objects = Path(directory) / 'raw.sqlite3', Path(directory) / 'objects'
            archive = Archive(database, objects)
            capture_id = archive.save('local:item', 200, 'text/plain', b'original')
            archive.close()
            archive = Archive(database, objects)
            try:
                self.assertEqual(archive.read(capture_id), b'original')
                next(objects.iterdir()).write_bytes(b'changed')
                with self.assertRaises(ValueError):
                    archive.read(capture_id)
            finally:
                archive.close()

    def test_unknown_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(Path(directory) / 'raw.sqlite3', Path(directory) / 'objects')
            try:
                with self.assertRaises(KeyError):
                    archive.read(87)
            finally:
                archive.close()


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def test_real_http_source_and_parser(self):
        status, content_type, body = fetch(self.base + '/screen')
        self.assertEqual(status, 200)
        self.assertTrue(content_type)
        self.assertIsInstance(inspect_document(body), dict)

    def test_missing_source_preserves_status(self):
        status, content_type, body = fetch(self.base + '/missing')
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)['error'], 'unknown local route')

    def test_response_budget(self):
        with self.assertRaises(ValueError):
            fetch(self.base + '/screen', limit=2)

    def test_nonlocal_and_credentials_rejected(self):
        for url in ['https://example.com', 'http://name:password@localhost/a', 'file:///tmp/a']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetch(url)

    def test_cli_inspect(self):
        path = Path('termfolio/fixtures/screen.ansi')
        result = subprocess.run([sys.executable, '-m', 'termfolio', 'inspect', str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)


if __name__ == '__main__':
    unittest.main()
