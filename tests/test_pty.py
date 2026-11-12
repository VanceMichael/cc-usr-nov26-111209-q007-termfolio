import errno
import os
import pty
import select
import subprocess
import sys
import time
import unittest


class PtyTests(unittest.TestCase):
    def test_real_terminal_detail_and_update(self):
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, '-m', 'termfolio.ledger', '--update-after-details', '1'], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)

        def until(marker):
            body, deadline = b'', time.monotonic() + 4
            while marker not in body and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], max(0, deadline - time.monotonic()))
                if ready:
                    body += os.read(master, 8192)
            self.assertIn(marker, body)
            return body

        try:
            until('就绪'.encode())
            os.write(master, b'o')
            until('完整箱号'.encode())
            os.write(master, b'b')
            until('台账已更新'.encode())
            os.write(master, b' ')
            until(b'v2')
            os.write(master, b'q')
            self.assertEqual(process.wait(timeout=3), 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
