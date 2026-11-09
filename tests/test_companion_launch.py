"""Launcher reuse and private installation metadata."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        self.urls = Path(temporary.name) / "urls"

    def launch(self, destination):
        return subprocess.run([sys.executable, "-m", "companion.launch", "--state-dir",
                               str(self.state), "--url-file", str(destination)], cwd=ROOT,
                              capture_output=True, text=True, timeout=10)

    def test_launch_reuses_one_server_and_writes_private_fresh_urls(self):
        first = self.launch(self.urls)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("#", first.stdout + first.stderr)
        metadata = json.loads((self.state / "runtime.json").read_text())
        self.addCleanup(lambda: self.stop(metadata["pid"]))
        first_url = self.urls.read_text().strip()
        self.assertEqual(urlsplit(first_url).hostname, "127.0.0.1")
        self.assertTrue(urlsplit(first_url).fragment)
        second = self.launch(self.urls)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads((self.state / "runtime.json").read_text())["pid"], metadata["pid"])
        self.assertNotEqual(self.urls.read_text().strip(), first_url)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        for path in (self.state / "runtime.json", self.urls):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    @staticmethod
    def stop(pid):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
