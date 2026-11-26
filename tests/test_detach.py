"""--detach: the editor keeps running as a process of its own after the command returns."""
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


class Detach(unittest.TestCase):
    def run_detached(self, *extra):
        home = tempfile.mkdtemp()
        env = dict(os.environ, CLAUDE_CONFIG_DIR=home, HOME=home, USERPROFILE=home)
        env.pop('CTXEDIT_DETACHED', None)
        return subprocess.run([sys.executable, os.path.join(ROOT, 'ctxedit.py'), '--detach', '--no-browser'] + list(extra),
                              capture_output=True, text=True, env=env, timeout=60)

    def test_returns_with_url_and_leaves_a_server_running(self):
        p = self.run_detached()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('running in the background', p.stdout)
        url = re.search(r'http://\S+', p.stdout).group(0)
        pid = int(re.search(r'pid (\d+)', p.stdout).group(1))
        log = re.search(r'log: (.+)', p.stdout).group(1).strip()
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                self.assertEqual(r.status, 200)
            if os.name != 'nt':
                self.assertNotEqual(os.getsid(pid), os.getsid(0))  # cut loose from us
        finally:
            os.kill(pid, signal.SIGTERM)
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
                time.sleep(0.1)
            os.unlink(log)

    def test_reports_a_failed_start(self):
        p = self.run_detached('00000000-0000-0000-0000-000000000000')
        self.assertEqual(p.returncode, 1)
        self.assertIn('failed to start', p.stdout)
        self.assertIn('no session', p.stdout)


if __name__ == '__main__':
    unittest.main()
