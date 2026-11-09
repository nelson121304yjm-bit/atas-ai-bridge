"""Loopback HTTP security and bootstrap behavior using real sockets."""

import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from companion.common import AppError
from companion.http_server import Auth, make_server


class HttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)
        self.calls = []

        def handler(method, path, payload):
            self.calls.append((method, path, payload))
            if path == "/api/error":
                raise AppError("sample", "expected failure", 409)
            if path == "/api/crash":
                raise RuntimeError("sensitive-value-must-not-be-logged")
            return {"method": method, "path": path, "payload": payload}

        self.server = make_server(handler, self.state_dir, port=0)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def bootstrap(self):
        token = self.server.auth.issue_launch()
        status, headers, body = self.request(
            "POST", "/api/bootstrap", json.dumps({"token": token}),
            {"Content-Type": "application/json"})
        self.assertEqual(status, 200, body)
        return token, headers, json.loads(body)

    def test_root_loads_with_restrictive_csp_and_no_inline_script(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertIn(b"app.js", body)
        self.assertNotIn(b"<script>", body)
        self.assertNotIn(b"defer", body, "bootstrap fragment must be removed before body render")
        self.assertLess(body.index(b"/bootstrap.js"), body.index(b"<body>"))
        self.assertNotIn(b"onclick=", body)

    def test_workbench_modules_are_explicitly_allowlisted(self):
        for path in ("/bootstrap.js", "/app.js", "/ui.js", "/analysis.js",
                     "/report.js", "/journal.js", "/system.js", "/settings.js", "/app.css"):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.request("GET", "/settings.js.map")[0], 404)

    def test_api_requires_session_and_bootstrap_is_one_use(self):
        self.assertEqual(self.request("GET", "/api/status")[0], 401)
        token, headers, payload = self.bootstrap()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertTrue(payload["csrf"])
        self.assertEqual(self.request("POST", "/api/bootstrap", json.dumps({"token": token}),
                                      {"Content-Type": "application/json"})[0], 403)
        with self.assertRaises(AppError):
            self.server.auth.exchange(token)

    def test_bootstrap_expires_after_120_seconds(self):
        now = [1000.0]
        auth = Auth(clock=lambda: now[0])
        token = auth.issue_launch()
        now[0] += 121
        with self.assertRaises(AppError):
            auth.exchange(token)

    def test_cookie_session_survives_reload_without_browser_storage(self):
        _, headers, payload = self.bootstrap()
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("GET", "/api/session", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["csrf"], payload["csrf"])
        self.assertEqual(self.request("GET", "/api/status", headers={"Cookie": cookie})[0], 200)

    def test_all_mutations_require_csrf_before_handler(self):
        _, headers, payload = self.bootstrap()
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                before = len(self.calls)
                status, _, _ = self.request(method, "/api/echo", "{}",
                                            {"Cookie": cookie, "Content-Type": "application/json"})
                self.assertEqual(status, 403)
                self.assertEqual(len(self.calls), before)
                status, _, _ = self.request(method, "/api/echo", "{}",
                                            {"Cookie": cookie, "Content-Type": "application/json",
                                             "X-CSRF-Token": payload["csrf"]})
                self.assertEqual(status, 200)

    def test_rejects_foreign_host_origin_and_cross_site_fetch(self):
        _, headers, payload = self.bootstrap()
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        for extra in ({"Host": "evil.example"},
                      {"Origin": "https://evil.example"},
                      {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(extra=extra):
                status, _, _ = self.request("GET", "/api/status", headers={"Cookie": cookie, **extra})
                self.assertEqual(status, 403)
        self.assertEqual(self.request("POST", "/api/echo", "{}", {
            "Cookie": cookie, "Content-Type": "application/json", "X-CSRF-Token": payload["csrf"],
            "Origin": "http://127.0.0.1:%d" % self.port})[0], 200)

    def test_rejects_malformed_and_oversized_json_before_handler(self):
        _, headers, payload = self.bootstrap()
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        request_headers = {"Cookie": cookie, "Content-Type": "application/json",
                           "X-CSRF-Token": payload["csrf"]}
        before = len(self.calls)
        self.assertEqual(self.request("POST", "/api/echo", "{", request_headers)[0], 400)
        self.assertEqual(self.request("POST", "/api/echo", "[]", request_headers)[0], 400)
        # Advertise the oversized body without transmitting it: the server must
        # reject from Content-Length before reading or invoking the handler.
        self.assertEqual(self.request("POST", "/api/echo", headers={
            **request_headers, "Content-Length": "196609"})[0], 413)
        self.assertEqual(len(self.calls), before)

    def test_static_allowlist_prevents_arbitrary_file_reads(self):
        for path in ("/../README.md", "/%2e%2e/README.md", "/README.md", "/static/../README.md"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_app_error_maps_to_public_json(self):
        _, headers, _ = self.bootstrap()
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("GET", "/api/error", headers={"Cookie": cookie})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"ok": False, "error": {
            "code": "sample", "message": "expected failure"}})

    def test_unexpected_handler_error_writes_private_redacted_diagnostic(self):
        _, headers, _ = self.bootstrap()
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("GET", "/api/crash?secret=path-value",
                                       headers={"Cookie": cookie})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"]["code"], "internal_error")
        diagnostic = self.state_dir / "errors.log"
        self.assertEqual(diagnostic.stat().st_mode & 0o777, 0o600)
        content = diagnostic.read_text()
        self.assertIn("RuntimeError", content)
        self.assertNotIn("sensitive-value-must-not-be-logged", content)
        self.assertNotIn("path-value", content)
        self.assertNotIn("/api/crash", content)

    def test_launcher_port_update_is_authenticated_and_validated(self):
        updates = []
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        server = make_server(lambda *_: {}, Path(temporary.name), port=0,
                             on_bridge_port_change=updates.append)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        port = server.server_address[1]

        def issue(value, token):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("POST", "/__launcher/issue",
                               body=json.dumps(value), headers={
                                   "Content-Type": "application/json",
                                   "X-Launcher-Token": token})
            response = connection.getresponse()
            status, body = response.status, response.read()
            connection.close()
            return status, body

        self.assertEqual(issue({"bridge_port": 34000}, "wrong")[0], 401)
        self.assertEqual(updates, [])
        for invalid in (0, 65536, True, "34000"):
            with self.subTest(invalid=invalid):
                self.assertEqual(issue({"bridge_port": invalid},
                                       server.auth.control_token)[0], 400)
        self.assertEqual(updates, [])
        status, body = issue({"bridge_port": 34000}, server.auth.control_token)
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["token"])
        self.assertEqual(server.bridge_port, 34000)
        self.assertEqual(updates, [34000])
        self.assertEqual(issue({"bridge_port": 34000}, server.auth.control_token)[0], 200)
        self.assertEqual(issue({}, server.auth.control_token)[0], 200)
        self.assertEqual(updates, [34000])


if __name__ == "__main__":
    unittest.main()
