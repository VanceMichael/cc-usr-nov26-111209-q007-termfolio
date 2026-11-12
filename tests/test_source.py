import base64
import json
import threading
import unittest
import urllib.parse
import urllib.request

from termfolio.source_server import FixtureServer
from termfolio.transport import fetch


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.server = FixtureServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()

    def get(self, path):
        return fetch(self.base + path)

    def phase(self, value):
        self.assertEqual(self.get('/control/phase?value=' + value)[0], 200)

    def test_screen_response_is_real_control_stream(self):
        body = self.get('/screen')[2]
        self.assertTrue(body.startswith(b'\x1b[2J'))
        self.assertIn('就绪'.encode(), body)
