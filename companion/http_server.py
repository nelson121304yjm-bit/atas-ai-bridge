"""Loopback-only HTTP shell, session bootstrap, and request boundary."""

from __future__ import annotations

from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

from .common import AppError, encode_json, utc_now


MAX_BODY = 196608
REQUEST_IDLE_SECONDS = 2
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/bootstrap.js": ("bootstrap.js", "text/javascript; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
    "/analysis.js": ("analysis.js", "text/javascript; charset=utf-8"),
    "/report.js": ("report.js", "text/javascript; charset=utf-8"),
    "/journal.js": ("journal.js", "text/javascript; charset=utf-8"),
    "/system.js": ("system.js", "text/javascript; charset=utf-8"),
    "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
}


class Auth:
    """Single-use launch tokens and ephemeral, in-memory cookie sessions."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._tokens: dict[str, float] = {}
        self._sessions: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self.control_token = secrets.token_urlsafe(32)

    def issue_launch(self) -> str:
        with self._lock:
            now = self._clock()
            self._tokens = {key: expiry for key, expiry in self._tokens.items() if expiry > now}
            token = secrets.token_urlsafe(32)
            self._tokens[token] = now + 120
            return token

    def exchange(self, token: str) -> tuple[str, str]:
        with self._lock:
            expiry = self._tokens.pop(token, None)
            if expiry is None or expiry <= self._clock():
                raise AppError("invalid_bootstrap", "启动链接已失效，请重新打开工作台。", 403)
            session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._sessions[session] = (csrf, self._clock() + 8 * 3600)
            return session, csrf

    def session(self, cookie_header: str) -> tuple[str, str]:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            value = cookie["atas_review"].value
        except (KeyError, ValueError):
            raise AppError("unauthorized", "请从启动器打开工作台。", 401) from None
        with self._lock:
            record = self._sessions.get(value)
            if record is None or record[1] <= self._clock():
                self._sessions.pop(value, None)
                raise AppError("unauthorized", "会话已过期，请重新打开工作台。", 401)
            return value, record[0]


def make_server(handler, state_dir: Path, port: int = 0,
                on_bridge_port_change=None) -> ThreadingHTTPServer:
    """Bind loopback; the optional hook receives authenticated port changes."""
    state_dir = Path(state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state_dir, 0o700)
    assets = Path(__file__).resolve().parent / "static"
    auth = Auth()
    bridge_change_lock = threading.Lock()

    def record_internal_error(exc: Exception) -> None:
        # Never persist an exception message, request target, headers or body.
        line = f"{utc_now()} internal_error {type(exc).__name__}\n".encode("utf-8")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(state_dir / "errors.log", flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, line)
            finally:
                os.close(descriptor)
        except OSError:
            pass  # Diagnostic failure must not replace the API's generic 500.

    class RequestHandler(BaseHTTPRequestHandler):
        server_version = "ATASReview/1"
        sys_version = ""
        # StreamRequestHandler applies this only to socket I/O, not business/job
        # execution. Idle preconnects and incomplete bodies must not pin shutdown.
        timeout = REQUEST_IDLE_SECONDS

        def log_message(self, format, *args):
            pass  # URLs may carry private bootstrap material in fragments.

        def _headers(self, content_type, size, cookie=None):
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _json(self, status, value, cookie=None):
            body = encode_json(value)
            self.send_response(status)
            self._headers("application/json; charset=utf-8", len(body), cookie)
            self.wfile.write(body)

        def _error(self, error):
            self._json(error.status, {"ok": False, "error": {
                "code": error.code, "message": error.message}})

        def _boundary(self):
            expected = f"127.0.0.1:{self.server.server_address[1]}"
            if self.headers.get_all("Host") != [expected]:
                raise AppError("forbidden_host", "请求来源无效。", 403)
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + expected:
                raise AppError("forbidden_origin", "请求来源无效。", 403)
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                raise AppError("forbidden_site", "跨站请求已拒绝。", 403)

        def _payload(self):
            lengths = self.headers.get_all("Content-Length")
            if lengths is None or len(lengths) != 1 or not lengths[0].isdigit():
                raise AppError("invalid_length", "请求长度无效。", 400)
            length = int(lengths[0])
            if length > MAX_BODY:
                raise AppError("body_too_large", "请求内容过大。", 413)
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                raise AppError("invalid_content_type", "需要 JSON 请求。", 415)
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except TimeoutError:
                self.close_connection = True
                raise AppError("request_timeout", "本机请求接收超时；未执行该请求。", 408) from None
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise AppError("invalid_json", "JSON 格式无效。", 400) from None
            if not isinstance(value, dict):
                raise AppError("invalid_json", "JSON 必须是对象。", 400)
            return value

        def _serve(self):
            self._boundary()
            path = urlsplit(self.path).path
            method = self.command
            if method == "GET" and path in ASSETS:
                name, content_type = ASSETS[path]
                body = (assets / name).read_bytes()
                self.send_response(HTTPStatus.OK)
                self._headers(content_type, len(body))
                self.wfile.write(body)
                return
            if path == "/__launcher/issue" and method == "POST":
                if not secrets.compare_digest(self.headers.get("X-Launcher-Token", ""),
                                              auth.control_token):
                    raise AppError("unauthorized", "启动器认证失败。", 401)
                update = self._payload() if self.headers.get("Content-Length") else {}
                if update:
                    value = update.get("bridge_port")
                    if (set(update) != {"bridge_port"} or type(value) is not int
                            or not 1 <= value <= 65535):
                        raise AppError("invalid_bridge_port", "桥接端口无效。", 400)
                    with bridge_change_lock:
                        if value != self.server.bridge_port:
                            if on_bridge_port_change is not None:
                                on_bridge_port_change(value)
                            self.server.bridge_port = value
                self._json(200, {"token": auth.issue_launch()})
                return
            if path == "/api/bootstrap" and method == "POST":
                token = self._payload().get("token")
                if not isinstance(token, str):
                    raise AppError("invalid_bootstrap", "启动链接无效。", 403)
                session, csrf = auth.exchange(token)
                self._json(200, {"ok": True, "csrf": csrf},
                           f"atas_review={session}; HttpOnly; SameSite=Strict; Path=/")
                return
            if not path.startswith("/api/"):
                raise AppError("not_found", "未找到页面。", 404)
            _, csrf = auth.session(self.headers.get("Cookie", ""))
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf):
                    raise AppError("csrf_required", "请求验证失败。", 403)
                payload = self._payload()
            else:
                payload = {}
            if method == "GET" and path == "/api/session":
                self._json(200, {"ok": True, "csrf": csrf})
                return
            result = handler(method, path, payload)
            self._json(200, result)

        def _dispatch(self):
            try:
                self._serve()
            except AppError as error:
                self._error(error)
            except Exception as exc:
                record_internal_error(exc)
                self._error(AppError("internal_error", "服务暂时不可用。", 500))

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _dispatch

    server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    server.daemon_threads = True
    server.auth = auth
    server.state_dir = state_dir
    server.bridge_port = None
    return server
