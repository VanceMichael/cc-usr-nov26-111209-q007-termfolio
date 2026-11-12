import unittest
from pathlib import Path
from termfolio.parser import inspect_document

from termfolio.parser import Screen

class ParserTests(unittest.TestCase):
    def test_partial_refresh_overwrites(self):
        result = inspect_document(b'abcdef\x1b[1;1HXY\x1b[K')
        self.assertEqual(result['lines'][0], 'XY')

    def test_control_split_across_feeds(self):
        screen = Screen()
        screen.feed('abc\x1b[2;')
        screen.feed('3HX')
        self.assertEqual(screen.lines()[1], '  X')

    def test_clear_and_chinese(self):
        result = inspect_document(Path('termfolio/fixtures/screen.ansi').read_bytes())
        self.assertIn('就绪', result['lines'][23])

    def test_unsupported_control(self):
        with self.assertRaises(ValueError):
            inspect_document(b'\x1b[1A')
