"""Local fake provider protocol and injected credential storage."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from companion.common import AppError
from companion.credentials import CredentialVault
from companion.provider import ProviderClient, normalize_base_url


class FakeProvider(BaseHTTPRequestHandler):
    calls = []
    status = 200
    response = {"choices": [{"message": {"content": "review"}}],
                "usage": {"total_tokens": 12}, "id": "req-123"}
    redirect = None

    def do_POST(self):
        type(self).calls.append((self.path, dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(type(self).status)
        if type(self).redirect:
            self.send_header("Location", type(self).redirect)
        self.end_headers()
        body = type(self).response
        self.wfile.write(json.dumps(body).encode() if not isinstance(body, bytes) else body)

    def log_message(self, *_):
        pass


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/v1" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeProvider.calls = []
        FakeProvider.status = 200
        FakeProvider.response = {"choices": [{"message": {"content": "review"}}],
                                 "usage": {"total_tokens": 12}, "id": "req-123"}
        FakeProvider.redirect = None

    def test_url_rules_and_canonical_full_address(self):
        self.assertEqual(normalize_base_url("HTTPS://API.EXAMPLE/v1/"), "https://api.example/v1")
        self.assertEqual(normalize_base_url(self.base + "/"), self.base)
        for value in ("http://example.com/v1", "http://localhost:8000/v1",
                      "https://user:pass@example.com/v1", "https://example.com/v1?x=1",
                      "https://example.com/v1#x", "file:///tmp/x", "https://example.com:bad/v1"):
            with self.subTest(value=value), self.assertRaises(AppError):
                normalize_base_url(value)

    def test_posts_chat_completion_without_forced_parameters(self):
        messages = [{"role": "system", "content": "Be concise"},
                    {"role": "user", "content": "Review evidence"}]
        result = ProviderClient().complete(self.base, "test-model", "synthetic-key", messages)
        self.assertEqual(result, {"text": "review", "usage": {"total_tokens": 12},
                                  "request_id": "req-123"})
        self.assertEqual(len(FakeProvider.calls), 1)
        path, headers, body = FakeProvider.calls[0]
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer synthetic-key")
        self.assertEqual(json.loads(body), {"model": "test-model", "messages": messages})

    def test_explicit_timeout_is_sixty_seconds(self):
        from companion.provider import _open_no_redirect
        with patch("companion.provider._open_no_redirect", wraps=_open_no_redirect) as opened:
            ProviderClient().complete(self.base, "m", "synthetic-key", [])
        self.assertEqual(opened.call_args.args[1], 60)

    def test_status_errors_are_public_and_do_not_echo_secret(self):
        secret = "synthetic-do-not-echo"
        for status in (401, 429, 503):
            with self.subTest(status=status):
                FakeProvider.status = status
                FakeProvider.response = {"error": {"message": secret}}
                with self.assertRaises(AppError) as caught:
                    ProviderClient().complete(self.base, "m", secret, [])
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, caught.exception.message)
                self.assertEqual(len(FakeProvider.calls), 1)
                FakeProvider.calls.clear()

    def test_bad_json_bad_shape_oversize_and_redirect_are_rejected_without_retry(self):
        cases = [({"choices": []}, 200, None), (b"not-json", 200, None),
                 (b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":NaN}}',
                  200, None),
                 (b"a" * (2 * 1024 * 1024 + 1), 200, None),
                 ({}, 302, self.base + "/other")]
        for response, status, redirect in cases:
            with self.subTest(status=status, response_type=type(response).__name__):
                FakeProvider.calls.clear()
                FakeProvider.response = response
                FakeProvider.status = status
                FakeProvider.redirect = redirect
                with self.assertRaises(AppError):
                    ProviderClient().complete(self.base, "m", "synthetic-key", [])
                self.assertEqual(len(FakeProvider.calls), 1)

    def test_rejects_invalid_model_and_payload(self):
        for model, messages in (("x" * 129, []), ("m", "not-list")):
            with self.subTest(model=model[:5]), self.assertRaises(AppError):
                ProviderClient().complete(self.base, model, "synthetic-key", messages)
        self.assertEqual(FakeProvider.calls, [])

    def test_non_ascii_header_key_is_rejected_without_echo(self):
        secret = "synthetic-☃"
        with self.assertRaises(AppError) as caught:
            ProviderClient().complete(self.base, "m", secret, [])
        self.assertNotIn(secret, caught.exception.message)
        self.assertEqual(FakeProvider.calls, [])


class FakeKeychain:
    def __init__(self):
        self.data = {}
        self.fail = False

    def get(self, address):
        if self.fail:
            raise RuntimeError("keychain unavailable")
        return self.data.get(address)

    def set(self, address, key):
        if self.fail:
            raise RuntimeError("keychain unavailable")
        self.data[address] = key

    def delete(self, address):
        if self.fail:
            raise RuntimeError("keychain unavailable")
        self.data.pop(address, None)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.keychain = FakeKeychain()
        self.vault = CredentialVault(self.keychain)
        self.first = "https://provider.example/v1"
        self.second = "https://provider.example/v2"

    def test_session_and_persistent_keys_bind_to_full_address(self):
        self.vault.set(self.first + "/", "session-key")
        self.assertEqual(self.vault.get(self.first), "session-key")
        self.assertIsNone(self.vault.get(self.second))
        self.assertEqual(self.keychain.data, {})
        self.vault.set(self.second, "persistent-key", persist=True)
        self.assertEqual(self.keychain.data[self.second], "persistent-key")
        self.assertTrue(CredentialVault(self.keychain).has(self.second))
        self.vault.set(self.second, "new-session-key", persist=False)
        self.assertEqual(self.vault.get(self.second), "new-session-key")
        self.assertNotIn(self.second, self.keychain.data)
        self.assertFalse(CredentialVault(self.keychain).has(self.second))
        self.vault.delete(self.second)
        self.assertFalse(self.vault.has(self.second))

    def test_keychain_failure_is_explicit_and_does_not_fall_back_to_session(self):
        self.keychain.fail = True
        with self.assertRaises(AppError):
            self.vault.set(self.first, "synthetic-secret", persist=True)
        self.assertEqual(self.vault._session, {})
        with self.assertRaises(AppError):
            self.vault.get(self.first)

    def test_invalid_key_is_rejected_without_echo(self):
        for secret in ("bad\nkey", "synthetic-☃"):
            with self.subTest(secret=secret), self.assertRaises(AppError) as caught:
                self.vault.set(self.first, secret)
            self.assertNotIn(secret, caught.exception.message)


if __name__ == "__main__":
    unittest.main()
