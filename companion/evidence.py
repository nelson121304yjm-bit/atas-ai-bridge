"""Read-only, bounded evidence from one local ATAS indicator instance."""

from __future__ import annotations

import http.client
import importlib.util
import json
import math
from pathlib import Path

from .common import AppError, encode_json, utc_now


MAX_RAW = 3 * 1024 * 1024
MAX_EVIDENCE = 131072
SECURITY_ID = "BTCUSDT@BinanceFutures"
STATUS_IDENTITY = ("portfolio", "accountIsReal", "accountConnectionState", "securityId",
                   "instrument", "hostMode", "readOnly", "allowTrading", "tradingArmed",
                   "snapshotBars", "snapshotFootprintBars", "profileLookbackBars",
                   "profileValueAreaPercent", "defaultDomLevels", "tickSize", "chartType", "timeFrame")


def _discover_port() -> int:
    """Load only the existing discovery helper; never instantiate its trading client."""
    path = Path(__file__).resolve().parents[1] / "mcp" / "atas_client.py"
    spec = importlib.util.spec_from_file_location("_atas_port_discovery", path)
    if spec is None or spec.loader is None:
        raise AppError("bridge_unavailable", "ATAS bridge port discovery is unavailable.", 503)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.discover_port()


def _reject_nonfinite(_value):
    raise ValueError("Non-finite JSON number")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(raw: bytes):
    try:
        return json.loads(raw, parse_constant=_reject_nonfinite, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise AppError("bridge_invalid_response", "ATAS returned invalid JSON.", 502) from None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _numbers(source: dict, keys: tuple[str, ...]) -> dict:
    result = {}
    for key in keys:
        if key in source:
            value = source[key]
            if value is not None and not _number(value):
                raise AppError("bridge_invalid_response", "ATAS returned invalid market data.", 502)
            result[key] = value
    return result


def _objects(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise AppError("bridge_invalid_response", "ATAS returned invalid market data.", 502)
    return value


class MarketSource:
    def __init__(self, port: int | None = None):
        if port is not None and (type(port) is not int or not 1 <= port <= 65535):
            raise AppError("invalid_bridge_port", "Bridge port must be a valid number.")
        self.port = port if port is not None else _discover_port()

    def _get(self, path: str):
        # Fixed paths and numeric loopback avoid URL input, proxies, and redirects.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10 if path == "/api/journal" else 5)
        try:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                raise AppError("bridge_redirect", "ATAS redirect was rejected.", 502)
            if response.status != 200:
                raise AppError("bridge_http_error", "ATAS request failed.", 502)
            raw = response.read(MAX_RAW + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise AppError("bridge_unavailable", "ATAS bridge is unavailable.", 503) from None
        finally:
            connection.close()
        if len(raw) > MAX_RAW:
            raise AppError("bridge_response_too_large", "ATAS response exceeds 3 MiB.", 502)
        value = _decode(raw)
        if isinstance(value, dict) and value.get("ok") is False:
            if path == "/api/journal" and value.get("code") in {
                    "native_history_start_failed", "native_history_load_failed",
                    "native_history_snapshot_failed", "native_origin_changed"}:
                raise AppError(value["code"], "Native journal read failed at the reported stage; no alternate source was used.", 502)
            raise AppError("bridge_endpoint_error", "ATAS endpoint has no usable data.", 502)
        return value, utc_now()

    @staticmethod
    def _validate_status(value):
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise AppError("bridge_unsafe_status", "ATAS status is unknown or unsafe.", 409)
        required = {"hostMode": "indicator", "readOnly": True, "isActivated": True,
                    "allowTrading": False, "tradingArmed": False, "accountIsReal": False,
                    "accountConnectionState": "Connected", "instrument": "BTCUSDT",
                    "securityId": SECURITY_ID}
        if any(value.get(key) != expected or type(value.get(key)) is not type(expected)
               for key, expected in required.items()):
            raise AppError("bridge_unsafe_status", "ATAS status is unknown or unsafe.", 409)
        if not isinstance(value.get("portfolio"), str) or not value["portfolio"].strip():
            raise AppError("bridge_unsafe_status", "ATAS account status is unknown.", 409)
        if type(value.get("currentBar")) is not int or value["currentBar"] < 1:
            raise AppError("bridge_unsafe_status", "ATAS chart status is unknown.", 409)
        for key, low, high in (("snapshotBars", 1, 5000), ("snapshotFootprintBars", 1, 5000),
                               ("profileLookbackBars", 1, 5000), ("profileValueAreaPercent", 1, 100),
                               ("defaultDomLevels", 1, 200)):
            item = value.get(key)
            if type(item) is not int or not low <= item <= high:
                raise AppError("bridge_unsafe_status", "ATAS chart settings are unknown.", 409)
        if not _number(value.get("tickSize")) or value["tickSize"] <= 0:
            raise AppError("bridge_unsafe_status", "ATAS tick size is unknown.", 409)
        if any(not isinstance(value.get(key), str) or not value[key].strip()
               for key in ("chartType", "timeFrame")):
            raise AppError("bridge_chart_metadata_missing",
                           "ATAS chart metadata is unavailable; update the Mac bridge.", 409)
        freshness = value.get("freshness")
        if not isinstance(freshness, dict):
            raise AppError("bridge_stale", "ATAS freshness is unknown or stale.", 409)
        for age_key, stale_key in (("snapshotAgeMs", "snapshotStale"),
                                   ("marketEventAgeMs", "marketEventStale")):
            age = freshness.get(age_key)
            if type(age) is not int or not 0 <= age <= 5000 or freshness.get(stale_key) is not False:
                raise AppError("bridge_stale", "ATAS freshness is unknown or stale.", 409)

    @staticmethod
    def _safe_status(value):
        return {"host_mode": "indicator", "read_only": True, "connected": True,
                "security_id": value["securityId"], "instrument": value["instrument"],
                "account_is_real": False, "current_bar": value["currentBar"],
                "bridge_tick_size": value["tickSize"], "chart_type": value["chartType"],
                "time_frame": value["timeFrame"],
                "freshness": {key: value["freshness"].get(key) for key in
                              ("snapshotAgeMs", "marketEventAgeMs", "snapshotStale", "marketEventStale")}}

    def status(self) -> dict:
        try:
            value, _ = self._get("/api/status")
        except AppError as exc:
            return {"connected": False, "eligible": False, "reason": exc.code}
        bridge_version = {}
        if isinstance(value, dict):
            version = value.get("version")
            if (isinstance(version, str) and 1 <= len(version) <= 64
                    and all(33 <= ord(char) <= 126 for char in version)):
                bridge_version = {"bridge_version": version}
        try:
            self._validate_status(value)
        except AppError as exc:
            return {"connected": True, "eligible": False, "reason": exc.code,
                    "account": value.get("portfolio") if isinstance(value, dict) and value.get("accountIsReal") is False else None,
                    **bridge_version}
        return {"connected": True, "eligible": True, "account": value["portfolio"],
                **self._safe_status(value), **bridge_version}

    @staticmethod
    def _candles(value, current_bar):
        selected = []
        forming = None
        for item in _objects(value):
            index = item.get("i")
            if type(index) is not int or index < 0:
                raise AppError("bridge_invalid_response", "ATAS returned invalid candles.", 502)
            if not isinstance(item.get("t"), str) or any(not _number(item.get(key))
                                                        for key in ("o", "h", "l", "c", "v", "delta")):
                raise AppError("bridge_invalid_response", "ATAS returned invalid candles.", 502)
            row = {"i": index, "t": item["t"], **_numbers(item,
                   ("o", "h", "l", "c", "v", "delta"))}
            if index == current_bar - 1:
                forming = {**row, "is_closed": False}
            elif index < current_bar - 1:
                selected.append(row)
        selected.sort(key=lambda item: item["i"])
        if not selected:
            raise AppError("bridge_invalid_response", "ATAS returned no closed candles.", 502)
        return selected[-20:], forming

    @staticmethod
    def _cvd(value, current_bar):
        selected = []
        forming = None
        source_indices = []
        for item in _objects(value):
            index = item.get("i")
            if type(index) is not int or index < 0:
                raise AppError("bridge_invalid_response", "ATAS returned invalid CVD.", 502)
            if (not isinstance(item.get("t"), str) or type(item.get("sessionStart")) is not bool
                    or any(not _number(item.get(key)) for key in ("delta", "cvd"))):
                raise AppError("bridge_invalid_response", "ATAS returned invalid CVD.", 502)
            row = {"i": index, "t": item["t"], "sessionStart": item["sessionStart"],
                   **_numbers(item, ("delta", "cvd"))}
            source_indices.append(index)
            if index == current_bar - 1:
                forming = {**row, "is_closed": False, "basis": "window_relative"}
            elif index < current_bar - 1:
                selected.append(row)
        selected.sort(key=lambda item: item["i"])
        selected = selected[-20:]
        if not selected:
            raise AppError("bridge_invalid_response", "ATAS returned no CVD.", 502)
        source_start = min(source_indices)
        if forming is not None:
            forming["calculation_window_start_bar"] = source_start
            forming["origin_provenance"] = "first_observed_source_row"
        return {"basis": "window_relative", "full_session_cvd": False,
                "calculation_window_start_bar": source_start,
                "origin_provenance": "first_observed_source_row",
                "returned_window_start_bar": selected[0]["i"],
                "returned_window_end_bar": selected[-1]["i"],
                "session_reset_in_window": any(row["sessionStart"] for row in selected),
                "bars": selected}, forming

    @staticmethod
    def _dom(value):
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise AppError("bridge_invalid_response", "ATAS returned invalid DOM.", 502)
        result = _numbers(value, ("cumDomBids", "cumDomAsks"))
        for side in ("bids", "asks"):
            rows = _objects(value.get(side))
            if any(not _number(row.get("price")) or not _number(row.get("volume")) for row in rows):
                raise AppError("bridge_invalid_response", "ATAS returned invalid DOM.", 502)
            result[side] = [_numbers(row, ("price", "volume")) for row in rows[:15]]
            result[side + "_truncated"] = len(rows) > 15
        return result

    @staticmethod
    def _quote(value):
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise AppError("bridge_invalid_response", "ATAS returned invalid quote.", 502)
        return _numbers(value, ("last", "bestBid", "bidSize", "bestAsk", "askSize",
                                "spread", "cumDomBids", "cumDomAsks", "domImbalance", "tickSize"))

    @staticmethod
    def _profile(value):
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise AppError("bridge_invalid_response", "ATAS returned invalid profile.", 502)
        rows = _objects(value.get("levels"))
        if (type(value.get("levelCount")) is not int or value["levelCount"] < len(rows)
                or any(not _number(row.get("p")) or not _number(row.get("v")) for row in rows)):
            raise AppError("bridge_invalid_response", "ATAS returned invalid profile.", 502)
        selected = sorted(rows, key=lambda row: row["v"], reverse=True)[:20]
        result = _numbers(value, ("fromBar", "toBar", "usedBars", "valueAreaPercent",
                                  "poc", "vah", "val", "totalVolume", "valueAreaVolume"))
        result.update({"level_count": value.get("levelCount"), "returned_level_count": len(selected),
                       "truncated": value.get("truncated") is True or len(rows) > 20,
                       "selection": "top_volume", "requested_bars": 200,
                       "levels": [_numbers(row, ("p", "v", "bid", "ask")) for row in selected]})
        return result

    @staticmethod
    def _footprint(value, current_bar):
        if not isinstance(value, dict) or value.get("ok") is not True or not isinstance(value.get("bars"), dict):
            raise AppError("bridge_invalid_response", "ATAS returned invalid footprint.", 502)
        bars = []
        for key, raw in value["bars"].items():
            if not isinstance(key, str) or not key.isdecimal() or not isinstance(raw, str):
                raise AppError("bridge_invalid_response", "ATAS returned invalid footprint.", 502)
            index = int(key)
            if index < current_bar:
                bar = _decode(raw.encode("utf-8"))
                if (not isinstance(bar, dict) or bar.get("ok") is not True
                        or type(bar.get("bar")) is not int or bar["bar"] != index):
                    raise AppError("bridge_invalid_response", "ATAS returned invalid footprint.", 502)
                bars.append((index, bar))
        bars.sort(key=lambda entry: entry[0])
        output = {}
        for index, bar in bars[-2:]:
            rows = _objects(bar.get("levels"))
            if (type(bar.get("levelCount")) is not int or bar["levelCount"] < len(rows)
                    or not isinstance(bar.get("time"), (str, type(None)))
                    or any(not _number(row.get("p")) or not _number(row.get("v")) for row in rows)):
                raise AppError("bridge_invalid_response", "ATAS returned invalid footprint.", 502)
            selected = sorted(rows, key=lambda row: row["v"], reverse=True)[:20]
            data = _numbers(bar, ("o", "h", "l", "c", "volume", "delta", "minDelta",
                                  "maxDelta", "poc", "vah", "val", "totalVolume", "totalDelta"))
            data.update({"bar": index, "time": bar.get("time"), "is_closed": index < current_bar - 1,
                         "level_count": bar.get("levelCount"),
                         "returned_level_count": len(selected), "truncated": bar.get("truncated") is True or len(rows) > 20,
                         "selection": "top_volume", "levels": [_numbers(row,
                         ("p", "bid", "ask", "v", "d", "n")) for row in selected]})
            output[str(index)] = data
        return {"bars": output, "source_bar_count": len(bars), "bars_truncated": len(bars) > 2,
                "closed_bars_only": False,
                "forming_bar_index": current_bar - 1, "selection": "two_latest_available"}

    def capture(self) -> dict:
        start, start_at = self._get("/api/status")
        self._validate_status(start)
        evidence = {"status_start": {"retrieved_at": start_at, "data": self._safe_status(start)}}
        quality = ["non_atomic_endpoint_capture"]
        processors = (("quote", "/api/quote", self._quote),
                      ("candles", "/api/candles", lambda data: self._candles(data, start["currentBar"])),
                      ("cvd", "/api/cvd", lambda data: self._cvd(data, start["currentBar"])),
                      ("footprint", "/api/footprint", lambda data: self._footprint(data, start["currentBar"])),
                      ("dom", "/api/dom", self._dom), ("profile", "/api/profile?bars=200", self._profile))
        for name, path, process in processors:
            try:
                raw, retrieved_at = self._get(path)
            except AppError as exc:
                if name in ("candles", "cvd") or exc.code not in ("bridge_endpoint_error", "bridge_unavailable", "bridge_http_error"):
                    raise
                quality.append(name + "_unavailable")
                continue
            data = process(raw)
            if name in ("candles", "cvd"):
                data, forming = data
                if forming is not None:
                    forming_id = "forming_candle" if name == "candles" else "forming_cvd"
                    evidence[forming_id] = {"retrieved_at": retrieved_at, "data": forming}
            entry = {"retrieved_at": retrieved_at, "data": data}
            if name in ("candles", "cvd"):
                closed_count = sum(item["i"] < start["currentBar"] - 1 for item in raw)
                returned_count = len(data) if name == "candles" else len(data["bars"])
                entry["truncation"] = {"source_rows": len(raw), "closed_rows": closed_count,
                                       "returned_closed_rows": returned_count,
                                       "closed_rows_truncated": closed_count > returned_count,
                                       "forming_or_future_rows_excluded": len(raw) - closed_count}
            evidence[name] = entry
        end, end_at = self._get("/api/status")
        self._validate_status(end)
        if any(start.get(key) != end.get(key) for key in STATUS_IDENTITY) or end["currentBar"] < start["currentBar"]:
            raise AppError("bridge_status_changed", "ATAS chart or account changed during capture.", 409)
        if "profile" in evidence:
            profile = evidence["profile"]["data"]
            to_bar = profile.get("toBar")
            from_bar = profile.get("fromBar")
            used_bars = profile.get("usedBars")
            complete_range = (type(from_bar) is int and from_bar >= 0 and type(to_bar) is int
                              and from_bar <= to_bar and type(used_bars) is int
                              and used_bars == to_bar - from_bar + 1)
            if type(to_bar) is int and to_bar < start["currentBar"] - 1:
                profile["includes_forming_bar"] = False
            elif (complete_range and start["currentBar"] == end["currentBar"]
                  and to_bar == start["currentBar"] - 1):
                profile["includes_forming_bar"] = True
                quality.append("profile_includes_forming_bar")
            else:
                profile["includes_forming_bar"] = None
                quality.append("profile_forming_bar_unknown")
        evidence["status_end"] = {"retrieved_at": end_at, "data": self._safe_status(end)}
        result = {"schema_version": 1, "source": "market", "captured_at": utc_now(),
                  "identity": {"security_id": SECURITY_ID, "instrument": "BTCUSDT",
                               "account_is_real": False, "host_mode": "indicator",
                               "chart_type": start["chartType"], "time_frame": start["timeFrame"],
                               "bridge_tick_size": start["tickSize"]},
                  "quality": quality, "evidence": evidence,
                  "limits": {"raw_endpoint_bytes": MAX_RAW, "evidence_bytes": MAX_EVIDENCE,
                             "closed_candles": 20, "forming_candles": 1, "cvd_closed_bars": 20,
                             "forming_cvd_bars": 1, "footprint_bars": 2,
                             "footprint_levels_per_bar": 20, "dom_levels_per_side": 15,
                             "profile_bars": 200, "profile_levels": 20,
                             "endpoint_capture": "sequential_non_atomic",
                             "footprint_may_include_forming_bar": True,
                             "chart_aggregation_metadata": "actual_bridge_chart_type_and_time_frame",
                             "tick_size_source": "bridge_reported_not_exchange_verified"}}
        if len(encode_json(result)) > MAX_EVIDENCE:
            raise AppError("evidence_size", "Market evidence exceeds 131072 bytes.", 413)
        return result
