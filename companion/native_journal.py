"""Bounded native paired-row journal from the exact same read-only bridge as market data."""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import re
import threading

from .common import AppError, encode_json, utc_now
from .evidence import MAX_EVIDENCE, SECURITY_ID

SOURCE = "atas_native_journal"
DECIMALS = ("open_price", "close_price", "open_volume", "close_volume", "pnl", "ticks_pnl", "price_pnl", "commission")
FACTS = (*DECIMALS, "open_time", "close_time", "side", "is_complete", "missing_data_case")
QUALITY = ["native_paired_rows_not_roundtrips", "native_pnl_fee_semantics_unverified",
           "native_wall_clock_range_timezone_not_inferred", "historical_delta_dom_risk_mae_mfe_missing"]


def invalid(message="Native journal response is invalid."):
    return AppError("native_journal_invalid", message, 502)


def text(value, maximum=256, nullable=False):
    if nullable and value is None:
        return value
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise invalid()
    return value


def decimal_string(value, nullable=False):
    if nullable and value is None:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"-?\d{1,29}(?:\.\d{1,28})?", value):
        raise invalid()
    try:
        if not Decimal(value).is_finite():
            raise invalid()
    except InvalidOperation:
        raise invalid() from None
    return value


def native_time(value):
    if not isinstance(value, dict) or set(value) != {"iso", "kind"} or value["kind"] not in ("Utc", "Local", "Unspecified"):
        raise invalid()
    iso = value["iso"]
    if iso is not None:
        text(iso, 64)
        try:
            parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except ValueError:
            raise invalid() from None
        aware = parsed.utcoffset() is not None
        if (value["kind"] == "Unspecified" and aware) or (value["kind"] != "Unspecified" and not aware):
            raise invalid()
        if value["kind"] == "Utc" and parsed.utcoffset().total_seconds() != 0:
            raise invalid()
    return copy.deepcopy(value)


class NativeJournalSource:
    def __init__(self, market):
        self.market = market
        self._lock = threading.RLock()
        self._closed = False
        self._selection = {}
        self._origin = None

    def invalidate(self):
        with self._lock:
            self._closed = True
            self._selection.clear()
            self._origin = None

    def _status(self):
        value, _ = self.market._get("/api/status")
        if (not isinstance(value, dict) or value.get("ok") is not True or
            any(value.get(key) is not expected for key, expected in
                (("readOnly", True), ("isActivated", True), ("allowTrading", False), ("tradingArmed", False), ("accountIsReal", False))) or
            value.get("hostMode") != "indicator" or value.get("securityId") != SECURITY_ID):
            raise AppError("native_journal_scope", "Native journal requires the originating BTC simulation indicator.", 409)
        for key in ("journalInstanceId", "journalScopeId"):
            if not isinstance(value.get(key), str) or not re.fullmatch(r"[a-f0-9]{32}", value[key]):
                raise invalid()
        return {"instance_id": value["journalInstanceId"],
                "scope_id": value["journalScopeId"], "port": self.market.port,
                "account_id": text(value.get("portfolio")), "security_id": SECURITY_ID,
                "account_is_real": False, "host_mode": "indicator"}

    def _alive(self):
        if self._closed:
            raise AppError("native_journal_changed", "Journal source changed; refresh the list.", 409)

    def status(self):
        try:
            origin = self._status()
            with self._lock:
                self._alive()
                if self._origin is not None and origin != self._origin:
                    self._selection.clear()
                # Origin identity is local only and is not put into model evidence.
                return {"connected": True, "simulation_only": True, "source": SOURCE,
                        "account": origin["account_id"], "origin_key": self._key(origin)}
        except AppError as exc:
            with self._lock:
                self._selection.clear()
            return {"connected": False, "source": SOURCE, "reason": exc.code}

    @staticmethod
    def _key(value):
        return hashlib.sha256(encode_json(value)).hexdigest()

    def _read(self):
        self._alive()
        before = self._status()
        value, retrieved = self.market._get("/api/journal")
        after = self._status()
        self._alive()
        if before != after or not isinstance(value, dict) or value.get("origin") != before:
            raise AppError("native_journal_changed", "Native journal origin changed; refresh the list.", 409)
        origin = value["origin"]
        if (type(origin.get("port")) is not int or type(origin.get("account_is_real")) is not bool or
                value.get("ok") is not True or type(value.get("schema_version")) is not int or
                value.get("schema_version") != 1 or value.get("source") != SOURCE):
            raise invalid()
        coverage = value.get("coverage")
        if (not isinstance(coverage, dict) or set(coverage) != {
                "mode", "from", "to", "days", "limit", "returned", "total_scope_rows", "truncated",
                "incomplete_rows", "excluded_rows", "duplicate_rows"} or coverage.get("mode") != "load_history"):
            raise invalid()
        for key, maximum in (("days", 7), ("limit", 100), ("returned", 100), ("total_scope_rows", 10000),
                             ("incomplete_rows", 10000), ("excluded_rows", 10000), ("duplicate_rows", 10000)):
            item = coverage.get(key)
            if type(item) is not int or not (1 if key in ("days", "limit") else 0) <= item <= maximum:
                raise invalid()
        if type(coverage.get("truncated")) is not bool:
            raise invalid()
        start, end = native_time(coverage.get("from")), native_time(coverage.get("to"))
        if start["iso"] is None or end["iso"] is None:
            raise invalid()
        begin = datetime.fromisoformat(start["iso"].replace("Z", "+00:00"))
        finish = datetime.fromisoformat(end["iso"].replace("Z", "+00:00"))
        # Producer uses DateTime.AddDays and row bounds compare native wall-clock ticks.
        # Preserve ISO offsets/Kind, but do not reinterpret a DST offset change as an extra hour of scope.
        wall_duration = finish.replace(tzinfo=None) - begin.replace(tzinfo=None)
        if start["kind"] != end["kind"] or not 0 < wall_duration.total_seconds() <= 7 * 86400:
            raise invalid()
        rows = value.get("trades")
        if not isinstance(rows, list) or len(rows) != coverage["returned"] or len(rows) > coverage["limit"]:
            raise invalid()
        if (coverage["total_scope_rows"] < len(rows) or
            coverage["truncated"] != (coverage["total_scope_rows"] > len(rows)) or
            coverage["incomplete_rows"] > coverage["total_scope_rows"]):
            raise invalid()
        clean = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or row.get("account_id") != before["account_id"] or row.get("security_id") != SECURITY_ID:
                raise AppError("native_journal_scope", "Native row is outside the selected simulation scope.", 409)
            allowed = {"native_id", "account_id", "security_id", "entry_ref", "exit_ref", *FACTS}
            if set(row) != allowed or type(row.get("is_complete")) is not bool or row.get("side") not in (None, "Buy", "Sell"):
                raise invalid()
            text(row["native_id"])
            text(row["entry_ref"], nullable=True); text(row["exit_ref"], nullable=True)
            text(row["missing_data_case"], 128)
            for key in DECIMALS:
                decimal_string(row[key], nullable=key == "commission")
            native_time(row["open_time"]); native_time(row["close_time"])
            if any(Decimal(row[key]) < 0 for key in ("open_price", "close_price")):
                raise invalid()
            time_value = row["close_time"]["iso"] or row["open_time"]["iso"]
            if time_value is None:
                raise invalid()
            # Scope bounds use the host's wall clock; unknown Kind is never converted or suffixed.
            wall = datetime.fromisoformat(time_value.replace("Z", "+00:00")).replace(tzinfo=None)
            if not begin.replace(tzinfo=None) <= wall <= finish.replace(tzinfo=None):
                raise invalid()
            if row["is_complete"] and (not row["open_time"]["iso"] or not row["close_time"]["iso"]):
                raise invalid()
            fingerprint = self._key(row)
            if fingerprint in seen:
                raise invalid("Native journal returned duplicate rows.")
            seen.add(fingerprint)
            clean.append(copy.deepcopy(row))
        if value.get("quality") != QUALITY:
            raise invalid()
        return before, copy.deepcopy(coverage), clean, retrieved

    def list_trades(self):
        # Serializes reads and atomically replaces all keys; a failed refresh cannot leave stale records selectable.
        with self._lock:
            self._alive()
            self._selection.clear()
            origin, coverage, rows, _ = self._read()
            self._origin = origin
            trades = []
            for row in rows:
                key = self._key({"origin": origin, "row": row})
                self._selection[key] = row
                trades.append({**row, "trade_id": key, "instrument": SECURITY_ID,
                               "status": "closed" if row["is_complete"] else "incomplete",
                               "granularity": "native_paired_row"})
            result = {"source": SOURCE, "simulation_only": True, "origin_key": self._key(origin),
                      "coverage": coverage, "quality": QUALITY, "trades": trades, "returned": len(trades)}
            if len(encode_json(result)) > MAX_EVIDENCE:
                self._selection.clear()
                raise AppError("journal_list_size", "Native journal list exceeds 131072 bytes.", 413)
            return result

    def capture(self, trade_id):
        with self._lock:
            self._alive()
            if not isinstance(trade_id, str) or not re.fullmatch(r"[a-f0-9]{64}", trade_id) or trade_id not in self._selection:
                raise AppError("native_journal_selection", "Refresh and select a native paired row first.", 409)
            selected = copy.deepcopy(self._selection[trade_id])
            try:
                origin, coverage, rows, retrieved = self._read()
                if origin != self._origin or selected not in rows:
                    raise AppError("native_journal_changed", "Selected native row or origin changed; refresh the list.", 409)
                if not selected["is_complete"]:
                    raise AppError("journal_open_trade", "Only complete native paired rows can be reviewed.", 409)
            except AppError:
                self._selection.clear()
                raise
            # Explicit allowlist: no account IDs, row IDs, fill references, free text or current snapshots.
            facts = {key: selected[key] for key in FACTS}
            quality = [*QUALITY, "historical_trade_not_current_flatness"]
            if facts["commission"] is None:
                quality.append("commission_missing_net_pnl_unknown")
            if facts["side"] is None:
                quality.append("entry_direction_missing")
            if any(facts[key]["kind"] == "Unspecified" for key in ("open_time", "close_time")):
                quality.append("native_timezone_unknown_no_exact_market_alignment")
            result = {"schema_version": 1, "source": "trade", "captured_at": utc_now(),
                      "identity": {"security_id": SECURITY_ID, "simulation_only": True, "account_is_real": False,
                                   "source_detail": SOURCE, "granularity": "native_paired_row"},
                      "quality": quality,
                      "evidence": {"native_trade": {"retrieved_at": retrieved, "data": facts},
                                   "native_coverage": {"retrieved_at": retrieved, "data": coverage}},
                      "limits": {"evidence_bytes": MAX_EVIDENCE, "historical_only": True,
                                 "current_flatness_verified": False, "net_pnl": None, "currency": None,
                                 "position_units": None, "historical_dom": None, "historical_delta": None,
                                 "initial_risk": None, "mae": None, "mfe": None}}
            if len(encode_json(result)) > MAX_EVIDENCE:
                raise AppError("evidence_size", "Native trade evidence exceeds 131072 bytes.", 413)
            return copy.deepcopy(result)
