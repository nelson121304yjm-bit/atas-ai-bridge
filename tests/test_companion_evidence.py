"""Bounded read-only market evidence from a synthetic loopback bridge."""

import copy
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from companion.common import AppError, encode_json
from companion.evidence import MarketSource


def status(**changes):
    value = {"ok": True, "port": 0, "version": "0.4.0-macos.1",
             "hostMode": "indicator", "readOnly": True,
             "isActivated": True, "allowTrading": False, "tradingArmed": False,
             "instrument": "BTCUSDT", "securityId": "BTCUSDT@BinanceFutures",
             "portfolio": "PRIVATE-SIM-ACCOUNT", "accountIsReal": False,
             "accountConnectionState": "Connected", "tickSize": 0.1,
             "currentBar": 26, "snapshotBars": 30, "snapshotFootprintBars": 3,
             "profileLookbackBars": 60, "profileValueAreaPercent": 70,
             "defaultDomLevels": 20, "chartType": "Candles", "timeFrame": "1m",
             "freshness": {"snapshotAgeMs": 100, "marketEventAgeMs": 200,
                           "snapshotStale": False, "marketEventStale": False}}
    value.update(changes)
    return value


def default_payloads():
    candles = [{"i": i, "t": "2026-01-01T00:00:00Z", "o": 1, "h": 2, "l": 1,
                "c": 2, "v": 3, "delta": 1} for i in range(0, 27)]
    cvd = [{"i": i, "t": "2026-01-01T00:00:00Z", "delta": 1,
            "cvd": i - 4, "sessionStart": i == 5} for i in range(0, 27)]
    footprint = {str(i): json.dumps({"ok": True, "bar": i, "levels":
                 [{"p": j, "bid": 1, "ask": 2, "v": 3, "d": 1, "n": 1}
                  for j in range(30)], "levelCount": 30, "totalVolume": 90})
                 for i in (23, 24, 25, 26)}
    return {"/api/quote": {"ok": True, "last": 2, "bestBid": 1.9,
                            "bestAsk": 2.1, "bidSize": 1, "askSize": 2,
                            "privateMemo": "PRIVATE-SIM-ACCOUNT"},
            "/api/candles": candles, "/api/cvd": cvd,
            "/api/footprint": {"ok": True, "currentBar": 26, "bars": footprint},
            "/api/dom": {"ok": True, "bids": [{"price": i, "volume": 1} for i in range(20)],
                         "asks": [{"price": i, "volume": 1} for i in range(20)]},
            "/api/profile": {"ok": True, "fromBar": 0, "toBar": 25,
                             "levels": [{"p": i, "v": 1} for i in range(230)],
                             "levelCount": 230, "usedBars": 26}}


class FakeBridge(BaseHTTPRequestHandler):
    payloads = {}
    calls = []
    statuses = []
    raw = {}
    redirects = {}

    def do_GET(self):
        cls = type(self)
        cls.calls.append(self.path)
        path = self.path.split("?", 1)[0]
        if path in cls.redirects:
            self.send_response(302)
            self.send_header("Location", cls.redirects[path])
            self.end_headers()
            return
        value = cls.statuses.pop(0) if path == "/api/status" and cls.statuses else cls.payloads.get(path)
        raw = cls.raw.get(path, json.dumps(value).encode())
        self.send_response(200)
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        pass


class MarketEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeBridge.payloads = default_payloads()
        FakeBridge.payloads["/api/status"] = status(port=self.port)
        FakeBridge.statuses = []
        FakeBridge.calls = []
        FakeBridge.raw = {}
        FakeBridge.redirects = {}

    def test_capture_bounded_closed_bars_and_sanitized_account(self):
        value = MarketSource(self.port).capture()
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["source"], "market")
        self.assertEqual(value["identity"]["security_id"], "BTCUSDT@BinanceFutures")
        self.assertEqual(value["identity"]["account_is_real"], False)
        self.assertNotIn("PRIVATE-SIM-ACCOUNT", encode_json(value).decode())
        self.assertLessEqual(len(encode_json(value)), 131072)
        self.assertEqual([x["i"] for x in value["evidence"]["candles"]["data"]], list(range(5, 25)))
        self.assertEqual(len(value["evidence"]["dom"]["data"]["bids"]), 15)
        self.assertEqual(len(value["evidence"]["profile"]["data"]["levels"]), 20)
        self.assertEqual(list(value["evidence"]["footprint"]["data"]["bars"]), ["24", "25"])
        self.assertEqual(len(value["evidence"]["footprint"]["data"]["bars"]["24"]["levels"]), 20)
        self.assertTrue(value["evidence"]["footprint"]["data"]["bars"]["24"]["is_closed"])
        self.assertFalse(value["evidence"]["footprint"]["data"]["bars"]["25"]["is_closed"])
        self.assertEqual(value["evidence"]["forming_candle"]["data"]["i"], 25)
        self.assertFalse(value["evidence"]["forming_candle"]["data"]["is_closed"])
        self.assertEqual(value["evidence"]["forming_cvd"]["data"]["i"], 25)
        self.assertFalse(value["evidence"]["forming_cvd"]["data"]["is_closed"])
        self.assertEqual(value["evidence"]["cvd"]["data"]["basis"], "window_relative")
        self.assertEqual(value["evidence"]["cvd"]["data"]["calculation_window_start_bar"], 0)
        self.assertEqual(value["evidence"]["cvd"]["data"]["returned_window_start_bar"], 5)
        self.assertEqual(value["evidence"]["cvd"]["data"]["bars"][-1]["i"], 24)
        self.assertTrue(value["evidence"]["candles"]["truncation"]["closed_rows_truncated"])
        self.assertTrue(value["evidence"]["cvd"]["truncation"]["closed_rows_truncated"])
        self.assertTrue(value["evidence"]["footprint"]["data"]["bars_truncated"])
        self.assertTrue(value["evidence"]["profile"]["data"]["includes_forming_bar"])
        self.assertIn("profile_includes_forming_bar", value["quality"])
        self.assertIn("/api/candles", FakeBridge.calls)
        self.assertIn("/api/cvd", FakeBridge.calls)
        self.assertIn("/api/footprint", FakeBridge.calls)
        self.assertIn("/api/profile?bars=200", FakeBridge.calls)
        self.assertNotIn("/api/profile", FakeBridge.calls)
        self.assertNotIn("/api/position", FakeBridge.calls)
        self.assertEqual(FakeBridge.calls.count("/api/status"), 2)
        for item in value["evidence"].values():
            self.assertIn("retrieved_at", item)

    def test_status_shows_sim_account_only_locally(self):
        value = MarketSource(self.port).status()
        self.assertTrue(value["eligible"])
        self.assertEqual(value["account"], "PRIVATE-SIM-ACCOUNT")
        self.assertEqual(value["bridge_version"], "0.4.0-macos.1")

    def test_status_keeps_actual_version_when_capture_ineligible(self):
        FakeBridge.payloads["/api/status"] = status(port=self.port, accountIsReal=True,
                                                    version="0.4.1-macos.2")
        value = MarketSource(self.port).status()
        self.assertFalse(value["eligible"])
        self.assertEqual(value["bridge_version"], "0.4.1-macos.2")

    def test_status_omits_missing_or_invalid_version(self):
        for version in (None, "", "x" * 65, "bad\nversion", 123):
            with self.subTest(version=version):
                FakeBridge.payloads["/api/status"] = status(port=self.port, version=version)
                self.assertNotIn("bridge_version", MarketSource(self.port).status())

    def test_malformed_status_is_reported_as_ineligible(self):
        FakeBridge.payloads["/api/status"] = []
        self.assertFalse(MarketSource(self.port).status()["eligible"])

    def test_rejects_unsafe_or_unknown_status(self):
        cases = [{"accountIsReal": True}, {"accountIsReal": None},
                 {"accountConnectionState": "Disconnected"}, {"accountConnectionState": None},
                 {"securityId": "ETHUSDT@BinanceFutures"}, {"hostMode": "strategy"},
                 {"readOnly": False}, {"tradingArmed": True}, {"portfolio": None},
                 {"chartType": None}, {"timeFrame": ""}, {"snapshotBars": None},
                 {"freshness": {"snapshotAgeMs": None, "marketEventAgeMs": 1}},
                 {"freshness": {"snapshotAgeMs": 5001, "marketEventAgeMs": 1}},
                 {"freshness": {"snapshotAgeMs": 1, "marketEventAgeMs": 5001}}]
        for changes in cases:
            with self.subTest(changes=changes):
                FakeBridge.payloads["/api/status"] = status(port=self.port, **changes)
                with self.assertRaises(AppError):
                    MarketSource(self.port).capture()
                self.assertEqual(FakeBridge.calls[-1], "/api/status")

    def test_rechecks_account_security_and_aggregation(self):
        for change in ({"portfolio": "OTHER"}, {"accountIsReal": True},
                       {"securityId": "ETHUSDT@BinanceFutures"}, {"timeFrame": "5m"},
                       {"chartType": "Clusters"},
                       {"snapshotBars": 40}):
            with self.subTest(change=change):
                FakeBridge.calls = []
                FakeBridge.statuses = [status(port=self.port), status(port=self.port, **change)]
                with self.assertRaises(AppError):
                    MarketSource(self.port).capture()
                self.assertEqual(FakeBridge.calls[-1], "/api/status")

    def test_optional_failure_is_flagged_without_zero_substitution(self):
        FakeBridge.payloads["/api/quote"] = {"ok": False, "error": "unavailable"}
        value = MarketSource(self.port).capture()
        self.assertNotIn("quote", value["evidence"])
        self.assertTrue(any("quote" in q for q in value["quality"]))

    def test_missing_profile_does_not_claim_forming_profile(self):
        FakeBridge.payloads["/api/profile"] = {"ok": False, "error": "unavailable"}
        value = MarketSource(self.port).capture()
        self.assertNotIn("profile", value["evidence"])
        self.assertNotIn("profile_includes_forming_bar", value["quality"])

    def test_profile_forming_marker_uses_returned_bar_range(self):
        FakeBridge.payloads["/api/profile"]["toBar"] = 24
        lagged = MarketSource(self.port).capture()
        self.assertFalse(lagged["evidence"]["profile"]["data"]["includes_forming_bar"])
        self.assertNotIn("profile_includes_forming_bar", lagged["quality"])
        FakeBridge.payloads["/api/profile"]["toBar"] = 26
        ahead = MarketSource(self.port).capture()
        self.assertIsNone(ahead["evidence"]["profile"]["data"]["includes_forming_bar"])
        self.assertIn("profile_forming_bar_unknown", ahead["quality"])

    def test_matching_profile_end_with_skipped_bar_is_unknown(self):
        FakeBridge.payloads["/api/profile"]["usedBars"] = 25
        value = MarketSource(self.port).capture()
        self.assertEqual(value["evidence"]["profile"]["data"]["toBar"], 25)
        self.assertIsNone(value["evidence"]["profile"]["data"]["includes_forming_bar"])
        self.assertIn("profile_forming_bar_unknown", value["quality"])
        self.assertNotIn("profile_includes_forming_bar", value["quality"])

    def test_cvd_origin_uses_observed_rows_even_if_cache_lags_settings(self):
        FakeBridge.payloads["/api/cvd"] = default_payloads()["/api/cvd"][12:23]
        lagged = MarketSource(self.port).capture()
        self.assertEqual(lagged["evidence"]["cvd"]["data"]["calculation_window_start_bar"], 12)
        self.assertNotIn("forming_cvd", lagged["evidence"])
        FakeBridge.payloads["/api/status"] = status(port=self.port, snapshotBars=7)
        FakeBridge.payloads["/api/cvd"] = default_payloads()["/api/cvd"][10:26]
        changed_setting = MarketSource(self.port).capture()
        self.assertEqual(changed_setting["evidence"]["cvd"]["data"]["calculation_window_start_bar"], 10)
        self.assertEqual(changed_setting["evidence"]["forming_cvd"]["data"]["calculation_window_start_bar"], 10)

    def test_bad_data_raw_oversize_redirect_and_final_oversize(self):
        for raw in (b"not-json", b'{"ok":NaN}', b" " * (3 * 1024 * 1024 + 1)):
            with self.subTest(raw=raw[:10]):
                FakeBridge.raw["/api/status"] = raw
                with self.assertRaises(AppError):
                    MarketSource(self.port).capture()
        FakeBridge.raw = {}
        FakeBridge.redirects["/api/status"] = "http://example.invalid/"
        with self.assertRaises(AppError):
            MarketSource(self.port).capture()

    def test_missing_required_series_fields_and_invalid_aggregate_fail(self):
        for path, mutation in (("/api/candles", lambda data: data[0].pop("o")),
                               ("/api/cvd", lambda data: data[0].pop("cvd")),
                               ("/api/profile", lambda data: data["levels"][0].pop("v")),
                               ("/api/dom", lambda data: data["bids"][0].pop("price")),
                               ("/api/footprint", lambda data: data["bars"].update({"24": "not-json"}))):
            with self.subTest(path=path):
                FakeBridge.payloads = default_payloads()
                FakeBridge.payloads["/api/status"] = status(port=self.port)
                mutation(FakeBridge.payloads[path])
                with self.assertRaises(AppError):
                    MarketSource(self.port).capture()
        FakeBridge.redirects = {}
        FakeBridge.payloads["/api/candles"] = [dict(default_payloads()["/api/candles"][0],
                                                   t="x" * 140000)]
        with self.assertRaises(AppError):
            MarketSource(self.port).capture()

    def test_bypasses_proxy(self):
        with patch.dict(os.environ, {"http_proxy": "http://127.0.0.1:1", "HTTP_PROXY": "http://127.0.0.1:1",
                                     "no_proxy": "", "NO_PROXY": ""}), \
                patch("urllib.request.proxy_bypass", return_value=False):
            self.assertTrue(MarketSource(self.port).status()["eligible"])


if __name__ == "__main__":
    unittest.main()
