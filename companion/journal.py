"""Optional, fixed-command stdio client for the local read-only trade journal."""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import time

from .common import AppError, encode_json, utc_now


MAX_RAW = 3 * 1024 * 1024
MAX_EVIDENCE = 131072
TIMEOUT_SECONDS = 15
SECURITY_ID = "BTCUSDT@BinanceFutures"
TOOL_NAMES = frozenset({"list_trades", "get_trade_context", "get_session_summary", "get_storage_stats"})
BASELINE_SOURCES = frozenset({"operator_confirmed_flat", "platform_position"})
UNITS_VERIFICATIONS = frozenset({"native", "official_exchange_info_profile"})


def _reject_nonfinite(_value):
    raise ValueError("Non-finite JSON number")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _read_line(process, deadline: float) -> dict:
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    raw = bytearray()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AppError("journal_timeout", "Journal request timed out.", 504)
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                raise AppError("journal_unavailable", "Journal process closed unexpectedly.", 503)
            raw.extend(chunk)
            if len(raw) > MAX_RAW:
                raise AppError("journal_response_too_large", "Journal response exceeds 3 MiB.", 502)
            if b"\n" in raw:
                line, _, extra = raw.partition(b"\n")
                if extra.strip():
                    raise AppError("journal_invalid_response", "Journal returned extra output.", 502)
                try:
                    value = json.loads(line, parse_constant=_reject_nonfinite,
                                       object_pairs_hook=_unique_pairs)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    raise AppError("journal_invalid_response", "Journal returned invalid JSON.", 502) from None
                if not isinstance(value, dict):
                    raise AppError("journal_invalid_response", "Journal returned invalid data.", 502)
                return value
    finally:
        selector.close()


def _send(process, message: dict):
    try:
        process.stdin.write(encode_json(message) + b"\n")
        process.stdin.flush()
    except (OSError, BrokenPipeError):
        raise AppError("journal_unavailable", "Journal process is unavailable.", 503) from None


def _redact(value, account_id: str):
    if isinstance(value, dict):
        return {key: _redact(item, account_id) for key, item in value.items()
                if key not in ("account_id", "accountId", "portfolio", "account")}
    if isinstance(value, list):
        return [_redact(item, account_id) for item in value]
    if isinstance(value, str):
        return value.replace(account_id, "[account redacted]")
    return value


class JournalSource:
    """Config is installer-owned: absolute python_path, script_path, db_path, account_id."""

    def __init__(self, config: dict | None = None):
        if config is None:
            self.config = None
            return
        if not isinstance(config, dict) or set(config) != {"python_path", "script_path", "db_path", "account_id"}:
            raise AppError("journal_config", "Journal installation config is invalid.")
        for key in ("python_path", "script_path", "db_path"):
            value = config[key]
            if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
                raise AppError("journal_config", "Journal installation paths must be absolute existing files.")
        if not isinstance(config["account_id"], str) or not config["account_id"].strip() or len(config["account_id"]) > 256:
            raise AppError("journal_config", "Journal account identifier is invalid.")
        self.config = dict(config)

    def _call(self, name: str, arguments: dict) -> tuple[dict, str]:
        if self.config is None:
            raise AppError("journal_disconnected", "No journal installation is configured.", 503)
        if name not in TOOL_NAMES:
            raise AppError("journal_tool", "Journal tool is not allowed.")
        argv = [self.config["python_path"], self.config["script_path"], "--db", self.config["db_path"]]
        try:
            process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, bufsize=0, close_fds=True)
        except OSError:
            raise AppError("journal_unavailable", "Journal process could not start.", 503) from None
        try:
            deadline = time.monotonic() + TIMEOUT_SECONDS
            _send(process, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                       "clientInfo": {"name": "atas-ai-review", "version": "1"}}})
            initialized = _read_line(process, deadline)
            result = initialized.get("result")
            if initialized.get("id") != 1 or not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), str):
                raise AppError("journal_handshake", "Journal initialization failed.", 502)
            _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": name, "arguments": arguments}})
            response = _read_line(process, deadline)
            wrapper = response.get("result")
            if response.get("id") != 2 or not isinstance(wrapper, dict):
                raise AppError("journal_invalid_response", "Journal returned invalid data.", 502)
            if wrapper.get("isError") is True:
                raise AppError("journal_query", "Journal could not answer this read-only query.", 502)
            value = wrapper.get("structuredContent")
            if not isinstance(value, dict):
                raise AppError("journal_invalid_response", "Journal returned invalid data.", 502)
            return value, utc_now()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    stream.close()

    def status(self) -> dict:
        if self.config is None:
            return {"connected": False, "reason": "not_configured"}
        try:
            storage, _ = self._call("get_storage_stats", {})
        except AppError as exc:
            return {"connected": False, "reason": exc.code}
        if not isinstance(storage.get("rows"), dict):
            return {"connected": False, "reason": "journal_invalid_response"}
        return {"connected": True, "simulation_only": True,
                "account": self.config["account_id"], "storage": storage}

    def list_trades(self) -> dict:
        value, _ = self._call("list_trades", {"limit": 50})
        trades = value.get("trades")
        if value.get("simulation_only") is not True or not isinstance(trades, list) or any(not isinstance(t, dict) for t in trades):
            raise AppError("journal_invalid_response", "Journal trade list is invalid.", 502)
        selected = [trade for trade in trades if trade.get("account_id") == self.config["account_id"]
                    and trade.get("instrument") == SECURITY_ID]
        result = {**value, "trades": selected, "returned": len(selected),
                  "source_returned": len(trades), "excluded_count": len(trades) - len(selected),
                  "total_scope": "all_accounts_and_instruments", "filter_scope": "configured_sim_account_and_instrument"}
        if len(encode_json(result)) > MAX_EVIDENCE:
            raise AppError("journal_list_size", "Journal trade list exceeds 131072 bytes.", 413)
        return result

    def capture(self, trade_id: str) -> dict:
        if not isinstance(trade_id, str) or not 1 <= len(trade_id) <= 256 or any(ord(c) < 32 for c in trade_id):
            raise AppError("invalid_trade_id", "Trade ID is invalid.")
        value, retrieved_at = self._call("get_trade_context", {"trade_id": trade_id, "max_samples": 60})
        trade = value.get("trade")
        if value.get("simulation_only") is not True or not isinstance(trade, dict) or trade.get("trade_id") != trade_id:
            raise AppError("journal_unverified", "Selected trade cannot be verified.", 409)
        if trade.get("account_id") != self.config["account_id"] or trade.get("instrument") != SECURITY_ID:
            raise AppError("journal_scope", "Selected trade is outside the configured simulation scope.", 409)
        if trade.get("status") != "closed" or type(trade.get("closed_at_ms")) is not int:
            raise AppError("journal_open_trade", "Only closed trades can be reviewed.", 409)
        provenance = trade.get("baseline_provenance")
        if (not isinstance(provenance, dict) or provenance.get("baseline_source") not in BASELINE_SOURCES
                or provenance.get("units_verification") not in UNITS_VERIFICATIONS):
            raise AppError("journal_unverified", "Selected trade provenance is unverified.", 409)
        if trade.get("fees_complete") is not True and trade.get("net_pnl") is not None:
            raise AppError("journal_invalid_response", "Journal fee completeness is inconsistent.", 502)
        for key in ("quality", "capture_integrity"):
            if not isinstance(value.get(key), dict):
                raise AppError("journal_invalid_response", "Journal quality metadata is invalid.", 502)
        warnings = value.get("warnings")
        if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
            raise AppError("journal_invalid_response", "Journal warnings are invalid.", 502)
        snapshots = value.get("snapshots")
        if not isinstance(snapshots, list) or len(snapshots) > 60 or any(not isinstance(row, dict) for row in snapshots):
            raise AppError("journal_invalid_response", "Journal snapshot sample limit was exceeded.", 502)
        integrity = value["capture_integrity"]
        quality = ["historical_trade_not_current_flatness"]
        if provenance["baseline_source"] == "operator_confirmed_flat":
            quality.append("operator_confirmed_flat_not_platform_verified")
        if trade.get("fees_complete") is not True:
            quality.append("fees_incomplete_net_pnl_unknown")
        if integrity.get("ledger_quarantined") is True:
            quality.append("ledger_quarantined")
        if integrity.get("coverage_incomplete") is True:
            quality.append("coverage_incomplete")
        if type(integrity.get("gap_count")) is int and integrity["gap_count"] > 0:
            quality.append("capture_gaps_reported")
        quality.extend(warnings)
        safe = _redact(value, self.config["account_id"])
        result = {"schema_version": 1, "source": "trade", "captured_at": utc_now(),
                  "identity": {"trade_id": _redact(trade_id, self.config["account_id"]),
                               "security_id": SECURITY_ID, "simulation_only": True,
                               "account_is_real": False},
                  "quality": _redact(quality, self.config["account_id"]),
                  "evidence": {"trade_context": {"retrieved_at": retrieved_at, "data": safe}},
                  "limits": {"raw_response_bytes": MAX_RAW, "evidence_bytes": MAX_EVIDENCE,
                             "max_samples": 60, "historical_only": True,
                             "current_flatness_verified": False}}
        if len(encode_json(result)) > MAX_EVIDENCE:
            raise AppError("evidence_size", "Trade evidence exceeds 131072 bytes.", 413)
        return result
