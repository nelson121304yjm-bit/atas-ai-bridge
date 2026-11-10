"""Minimal HTTP client for the ATAS AI Bridge (only the Python standard library).

The bridge runs as an ATAS chart strategy and listens on 127.0.0.1:<port>.
Port discovery order:
  1. ATAS_AI_BRIDGE_PORT environment variable
  2. ATAS/AiBridge.port under macOS Application Support or Windows APPDATA
  3. ATAS X/AiBridge.port under the same directory
  4. 8790 (first port the bridge tries)
"""

from __future__ import annotations

from copy import deepcopy
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 8790
DEFAULT_HOST = "127.0.0.1"


class BridgeError(Exception):
    """Raised when the bridge cannot be reached or returns an error."""


def _port_files() -> tuple[str, ...]:
    if sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("APPDATA")
    if not base or not os.path.isabs(base):
        return ()
    return tuple(os.path.join(base, name, "AiBridge.port") for name in ("ATAS", "ATAS X"))


def discover_port() -> int:
    env_port = os.environ.get("ATAS_AI_BRIDGE_PORT")
    if env_port:
        try:
            port = int(env_port.strip())
            if 1 <= port <= 65535:
                return port
        except ValueError:
            pass

    for port_file in _port_files():
        try:
            with open(port_file, "r", encoding="utf-8") as handle:
                port = int(handle.read().strip())
                if 1 <= port <= 65535:
                    return port
        except Exception:
            pass

    return DEFAULT_PORT


class AtasBridgeClient:
    def __init__(self, port: int | None = None, host: str | None = None):
        self._explicit_port = port
        self.port = port if port is not None else discover_port()
        self.host = host or os.environ.get("ATAS_AI_BRIDGE_HOST", DEFAULT_HOST)

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def rediscover(self) -> None:
        """Re-read the port file (ATAS may have restarted on another port)."""
        if self._explicit_port is None:
            self.port = discover_port()

    def _request(self, path: str, params: dict | None = None, payload: dict | None = None,
                 timeout: float = 10.0) -> object:
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise BridgeError(f"{url} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            self.rediscover()
            raise BridgeError(
                f"无法连接 ATAS AI Bridge（{url}）。请确认 ATAS 正在运行。"
                f"Mac：在图表指标列表添加 AI Bridge — read only 并应用；"
                f"Windows：添加 AI Bridge 策略，并按策略条目的控制按钮启动。"
                f"若仍失败，请检查图表的数据连接、标的和账户组合；"
                f"IsActivated 是只读运行状态，不是勾选项。({exc})"
            ) from exc

        if isinstance(result, dict) and result.get("ok") is False:
            raise BridgeError(str(result.get("error", "bridge returned an error")))
        return result

    # ---- read endpoints -------------------------------------------------
    def health(self) -> dict:
        return self._request("/api/health", timeout=5.0)

    def status(self) -> dict:
        return self._request("/api/status")

    def quote(self) -> dict:
        return self._request("/api/quote")

    def dom(self, levels: int | None = None) -> dict:
        return self._request("/api/dom", {"levels": levels})

    def candles(self, bars: int | None = None) -> object:
        return self._request("/api/candles", {"bars": bars})

    def cvd(self) -> object:
        return self._request("/api/cvd")

    def footprint(self, bar: int | None = None, top: int | None = None,
                  compact: bool | None = None) -> object:
        params: dict = {}
        if bar is not None:
            params["bar"] = bar
        if top is not None:
            params["top"] = top
        if compact is not None:
            params["compact"] = "1" if compact else "0"
        return self._request("/api/footprint", params, timeout=15.0)

    def profile(self, bars: int | None = None, percent: int | None = None,
                top: int | None = None) -> object:
        return self._request("/api/profile", {"bars": bars, "percent": percent, "top": top},
                             timeout=20.0)

    def log(self, lines: int | None = None) -> dict:
        return self._request("/api/log", {"lines": lines})

    def _native_source(self):
        if self.host != "127.0.0.1":
            raise BridgeError("Native journal requires the same numeric loopback bridge source.")
        # Same fixed port, no rediscovery/fallback during a journal read.
        import sys
        from pathlib import Path
        root = str(Path(__file__).resolve().parents[1])
        if root not in sys.path:
            sys.path.insert(0, root)
        from companion.evidence import MarketSource
        from companion.native_journal import NativeJournalSource
        existing = getattr(self, "_journal", None)
        if existing is None or existing.market.port != self.port:
            if existing is not None:
                existing.invalidate()
            self._journal = NativeJournalSource(MarketSource(port=self.port))
        return self._journal

    def _native_read(self, trade_id=None):
        source = self._native_source()
        result = source.list_trades() if trade_id is None else source.capture(trade_id)
        if source is not self._journal or source.market.port != self.port:
            source.invalidate()
            raise BridgeError("Native journal source changed; refresh the list.")
        return result

    def native_journal(self) -> dict:
        value = self._native_read()
        from companion.native_journal import FACTS
        # The authenticated workbench retains native IDs for local reconciliation.
        # MCP is model-facing: publish only selection keys and the same safe facts as evidence.
        fields = ("trade_id", "instrument", "status", "granularity", *FACTS)
        return deepcopy({
            "source": value["source"], "simulation_only": value["simulation_only"],
            "coverage": value["coverage"], "quality": value["quality"], "returned": value["returned"],
            "trades": [{key: row[key] for key in fields} for row in value["trades"]],
        })

    def native_trade_evidence(self, trade_id: str) -> dict:
        return self._native_read(trade_id)

    def _persisted_request(self, path):
        """Authenticate to an existing workbench only; never launch/reconfigure a bridge."""
        import http.client
        from http.cookies import SimpleCookie
        from pathlib import Path
        import re
        import stat
        state = Path(os.environ.get("ATAS_REVIEW_STATE_DIR", str(Path.home() / "Library" / "Application Support" / "ATAS AI Review")))
        unavailable = "Local persisted journal workbench is unavailable. Open the local workbench launcher; no chart indicator is required."
        try:
            runtime = state / "runtime.json"
            info = runtime.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o077 or info.st_uid != os.getuid():
                raise ValueError()
            descriptor = os.open(runtime, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError()
                metadata = json.loads(handle.read(4097))
            port, token = metadata["port"], metadata["control_token"]
            if type(port) is not int or not 8770 <= port <= 8780 or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
                raise ValueError()
            identity = (port, token)
            if getattr(self, "_persisted_identity", None) != identity:
                self._persisted_cookie = None
                self._persisted_keys = set()
                self._persisted_identity = identity
                if path != "/api/trades":
                    raise BridgeError("The local workbench session changed; list and select a trade again.")

            def request(method, route, payload=None, headers=None):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    body = json.dumps(payload) if payload is not None else None
                    connection.request(method, route, body=body, headers=headers or {})
                    response = connection.getresponse()
                    raw = response.read(196609)
                    cookie = response.getheader("Set-Cookie")
                    if len(raw) > 196608 or response.status != 200:
                        if response.status == 401:
                            self._persisted_cookie = None
                            self._persisted_keys = set()
                        raise BridgeError("Local persisted journal request failed; refresh the local workbench and select a simulated account if required.")
                    return json.loads(raw), cookie
                finally:
                    connection.close()

            if not getattr(self, "_persisted_cookie", None):
                issued, _ = request("POST", "/__launcher/issue", {}, {"Content-Type": "application/json", "X-Launcher-Token": token})
                _, cookie = request("POST", "/api/bootstrap", {"token": issued["token"]}, {"Content-Type": "application/json"})
                parsed = SimpleCookie(); parsed.load(cookie or "")
                self._persisted_cookie = "atas_review=" + parsed["atas_review"].value
            # Only these authenticated GET routes are exposed by the persisted MCP tools.
            value, _ = request("GET", path, headers={"Cookie": self._persisted_cookie})
            after = runtime.lstat()
            if (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size) != (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size):
                self._persisted_cookie = None
                self._persisted_keys = set()
                raise BridgeError("The local workbench changed during the read; list and select again.")
            return value
        except BridgeError:
            raise
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            self._persisted_cookie = None
            self._persisted_keys = set()
            raise BridgeError(unavailable) from None

    @staticmethod
    def _persisted_coverage(value):
        fields = ("mode", "from", "to", "days", "limit", "returned", "total_scope_rows", "truncated", "incomplete_rows", "server_history_complete", "live_account_state")
        result = {key: deepcopy(value[key]) for key in fields}
        result["file_modified_at"] = {key: value["file_modified_at"][key] for key in ("Portfolio", "HistoryMyTrade", "MyTrade")}
        return result

    def persisted_journal(self) -> dict:
        self._persisted_keys = set()
        value = self._persisted_request("/api/trades")["data"]
        if value.get("source") != "atas_native_persisted_journal" or value.get("simulation_only") is not True:
            raise BridgeError("The workbench must use its local persisted simulated journal source.")
        # Explicit model boundary: no account options, origins, IDs, refs, comments or paths.
        facts = ("open_price", "close_price", "open_volume", "close_volume", "pnl", "ticks_pnl", "price_pnl", "commission", "open_time", "close_time", "side", "is_complete", "missing_data_case")
        fields = ("trade_id", "instrument", "status", "granularity", *facts)
        trades = [{key: deepcopy(row[key]) for key in fields} for row in value["trades"]]
        self._persisted_keys = {row["trade_id"] for row in trades}
        return {"source": value["source"], "simulation_only": True, "coverage": self._persisted_coverage(value["coverage"]),
                "quality": deepcopy(value["quality"]), "returned": len(trades), "trades": trades}

    def persisted_trade_evidence(self, trade_id: str) -> dict:
        import re
        if not isinstance(trade_id, str) or not re.fullmatch(r"[a-f0-9]{64}", trade_id) or trade_id not in getattr(self, "_persisted_keys", set()):
            raise BridgeError("List the local persisted journal and select one of its opaque trade keys first.")
        value = self._persisted_request("/api/trades/" + trade_id + "/evidence")["evidence"]
        if value.get("identity", {}).get("source_detail") != "atas_native_persisted_journal":
            raise BridgeError("The selected evidence is not from the local persisted source.")
        facts = ("open_price", "close_price", "open_volume", "close_volume", "pnl", "ticks_pnl", "price_pnl", "commission", "open_time", "close_time", "side", "is_complete", "missing_data_case")
        identity = ("security_id", "simulation_only", "account_is_real", "source_detail", "granularity")
        limits = ("evidence_bytes", "historical_only", "current_flatness_verified", "net_pnl", "currency", "position_units", "historical_dom", "historical_delta", "initial_risk", "mae", "mfe")
        native, coverage = value["evidence"]["native_trade"], value["evidence"]["native_coverage"]
        return {"schema_version": value["schema_version"], "source": value["source"], "captured_at": value["captured_at"],
                "identity": {key: value["identity"][key] for key in identity}, "quality": deepcopy(value["quality"]),
                "limits": {key: value["limits"][key] for key in limits}, "evidence": {
                    "native_trade": {"retrieved_at": native["retrieved_at"], "data": {key: deepcopy(native["data"][key]) for key in facts}},
                    "native_coverage": {"retrieved_at": coverage["retrieved_at"], "data": self._persisted_coverage(coverage["data"])}}}

    # ---- settings (read + write) ----------------------------------------
    def settings(self) -> dict:
        return self._request("/api/settings")

    def update_settings(self, values: dict) -> dict:
        if not values:
            raise BridgeError("settings 不能为空：至少要给一个 {设置key: 新值}")
        return self._request("/api/settings", payload={"set": values}, timeout=15.0)

    def position(self) -> dict:
        return self._request("/api/position")

    def orders(self) -> object:
        return self._request("/api/orders")

    # ---- trading endpoints (bridge-side double lock must be armed) ------
    def place_order(self, direction: str, order_type: str, qty: float, confirm: str,
                    price: float | None = None, trigger_price: float | None = None,
                    comment: str | None = None) -> dict:
        payload: dict = {"direction": direction, "type": order_type, "qty": qty, "confirm": confirm}
        if price is not None:
            payload["price"] = price
        if trigger_price is not None:
            payload["triggerPrice"] = trigger_price
        if comment is not None:
            payload["comment"] = comment
        return self._request("/api/order", payload=payload, timeout=15.0)

    def cancel_order(self, order_id: str, confirm: str) -> dict:
        return self._request("/api/order/cancel", payload={"id": order_id, "confirm": confirm},
                             timeout=15.0)

    def close_position(self, confirm: str, volume: float | None = None) -> dict:
        payload: dict = {"confirm": confirm}
        if volume is not None:
            payload["volume"] = volume
        return self._request("/api/position/close", payload=payload, timeout=15.0)
