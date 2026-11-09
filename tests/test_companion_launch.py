"""Launcher reuse and private installation metadata."""

import json
import http.client
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class LaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        self.urls = Path(temporary.name) / "urls"

    def launch(self, destination, bridge_port=None):
        command = [sys.executable, "-m", "companion.launch", "--state-dir",
                   str(self.state), "--url-file", str(destination)]
        if bridge_port is not None:
            command.extend(["--bridge-port", str(bridge_port)])
        return subprocess.run(command, cwd=ROOT,
                              capture_output=True, text=True, timeout=10)

    def status_from_url(self, url):
        parsed = urlsplit(url)
        token = parse_qs(parsed.fragment)["token"][0]
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        connection.request("POST", "/api/bootstrap", body=json.dumps({"token": token}),
                           headers={"Content-Type": "application/json"})
        exchange = connection.getresponse()
        self.assertEqual(exchange.status, 200)
        cookie = exchange.getheader("Set-Cookie").split(";", 1)[0]
        exchange.read()
        connection.request("GET", "/api/status", headers={"Cookie": cookie})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        result = json.loads(response.read())
        connection.close()
        return result

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

    def test_reused_server_applies_new_bridge_port_without_new_process(self):
        first = self.launch(self.urls)
        self.assertEqual(first.returncode, 0, first.stderr)
        metadata = json.loads((self.state / "runtime.json").read_text())
        self.addCleanup(lambda: self.stop(metadata["pid"]))
        self.assertIsNone(self.status_from_url(self.urls.read_text().strip())["bridgePort"])
        for bridge_port in (34000, 34001):
            with self.subTest(bridge_port=bridge_port):
                result = self.launch(self.urls, bridge_port)
                self.assertEqual(result.returncode, 0, result.stderr)
                current = json.loads((self.state / "runtime.json").read_text())
                self.assertEqual(current["pid"], metadata["pid"])
                self.assertEqual(current["bridge_port"], bridge_port)
                self.assertEqual(self.status_from_url(self.urls.read_text().strip())["bridgePort"],
                                 bridge_port)
        self.assertEqual(self.launch(self.urls).returncode, 0)
        self.assertEqual(self.status_from_url(self.urls.read_text().strip())["bridgePort"], 34001)

    @staticmethod
    def stop(pid):
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        time.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
