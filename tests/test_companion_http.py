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
        self.calls = []

        def handler(method, path, payload):
            self.calls.append((method, path, payload))
            if path == "/api/error":
                raise AppError("sample", "expected failure", 409)
            return {"method": method, "path": path, "payload": payload}

        self.server = make_server(handler, Path(temporary.name), port=0)
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


if __name__ == "__main__":
    unittest.main()
