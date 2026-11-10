"""Synthetic native paired-row contracts and source races; no host/database/provider calls."""
import copy
import json
import importlib.util
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from companion.app import Workbench
from companion.common import AppError, encode_json
from companion.native_journal import NativeJournalSource, QUALITY

SEC = "BTCUSDT@BinanceFutures"


def nt(iso="2026-11-09T10:00:00.1234567", kind="Unspecified"):
    return {"iso": iso, "kind": kind}


def row(**changes):
    value = {"native_id": "1", "account_id": "SYNTHETIC_SIM", "security_id": SEC,
             "entry_ref": "private-entry", "exit_ref": "private-exit", "open_time": nt(),
             "close_time": nt("2026-11-09T10:01:00.1234567"), "open_price": "12345.123456789012345678",
             "close_price": "12346.123456789012345678", "open_volume": "0.000000000000000123",
             "close_volume": "0.000000000000000100", "pnl": "-0.00000000000000000123",
             "ticks_pnl": "1", "price_pnl": "1.000000000000000000", "commission": None,
             "side": None, "is_complete": True, "missing_data_case": "None"}
    value.update(changes)
    return value


class NativeMarket:
    port = 34000
    def __init__(self):
        self.origin = {"instance_id": "a" * 32, "scope_id": "b" * 32, "port": self.port,
                       "account_id": "SYNTHETIC_SIM", "security_id": SEC, "account_is_real": False,
                       "host_mode": "indicator"}
        self.rows = [row(), row(native_id="2", exit_ref="private-exit-2", commission="0.00000000000000000017")]
        self.hook = None
        self.failure = None
        self.change = lambda value: value
        self.calls = []
    def status(self):
        return {"connected": True, "eligible": True}
    def _get(self, path):
        self.calls.append(path)
        if path == "/api/status":
            value = {"ok": True, "readOnly": True, "isActivated": True, "allowTrading": False,
                     "tradingArmed": False, "hostMode": "indicator", "accountIsReal": False,
                     "portfolio": self.origin["account_id"], "securityId": self.origin["security_id"],
                     "journalInstanceId": self.origin["instance_id"], "journalScopeId": self.origin["scope_id"]}
        else:
            assert path == "/api/journal", path
            if self.failure:
                raise self.failure
            value = {"ok": True, "schema_version": 1, "source": "atas_native_journal",
                     "origin": copy.deepcopy(self.origin), "coverage": {"mode": "load_history", "days": 7,
                        "from": nt("2026-11-03T12:00:00+08:00", "Local"),
                        "to": nt("2026-11-10T12:00:00+08:00", "Local"), "limit": 100,
                        "returned": len(self.rows), "total_scope_rows": len(self.rows), "truncated": False,
                        "incomplete_rows": sum(not item["is_complete"] for item in self.rows),
                        "excluded_rows": 0, "duplicate_rows": 0},
                     "trades": copy.deepcopy(self.rows), "quality": QUALITY.copy()}
            value = self.change(value)
            if self.hook:
                self.hook()
        return copy.deepcopy(value), "2026-11-10T12:00:00Z"


class NativeJournalTests(unittest.TestCase):
    def setUp(self):
        self.market = NativeMarket()
        self.source = NativeJournalSource(self.market)
    def error(self, call):
        with self.assertRaises(AppError) as caught:
            call()
        return caught.exception.code
    def test_partial_exits_precision_nullable_commission_and_unknown_direction_time(self):
        listed = self.source.list_trades()
        self.assertEqual(len(listed["trades"]), 2)
        first, second = listed["trades"]
        evidence = self.source.capture(first["trade_id"])
        facts = evidence["evidence"]["native_trade"]["data"]
        self.assertEqual(facts["pnl"], "-0.00000000000000000123")
        self.assertEqual(facts["open_volume"], "0.000000000000000123")
        self.assertEqual(facts["close_volume"], "0.000000000000000100")
        self.assertIsNone(facts["commission"])
        self.assertIsNone(facts["side"])
        self.assertEqual(facts["open_time"], nt())
        self.assertFalse(facts["open_time"]["iso"].endswith("Z"))
        self.assertIn("commission_missing_net_pnl_unknown", evidence["quality"])
        self.assertIn("entry_direction_missing", evidence["quality"])
        self.assertIsNone(evidence["limits"]["net_pnl"])
        self.assertEqual(self.source.capture(second["trade_id"])["evidence"]["native_trade"]["data"]["commission"], "0.00000000000000000017")
        raw = encode_json(evidence).decode()
        for private in ("SYNTHETIC_SIM", "private-entry", "private-exit", "native_id", first["trade_id"]):
            self.assertNotIn(private, raw)
        evidence["quality"].append("mutated")
        self.assertNotIn("mutated", self.source.capture(first["trade_id"])["quality"])
    def test_selection_must_come_from_same_list_and_source(self):
        key = self.source.list_trades()["trades"][0]["trade_id"]
        self.assertEqual(self.error(lambda: NativeJournalSource(self.market).capture(key)), "native_journal_selection")
        self.source.invalidate()
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_journal_changed")
    def test_account_or_instance_scope_changes_cannot_retrieve_old_row(self):
        for name in ("account_id", "scope_id", "instance_id"):
            with self.subTest(name=name):
                self.setUp()
                key = self.source.list_trades()["trades"][0]["trade_id"]
                self.market.origin[name] = "c" * 32
                self.assertIn(self.error(lambda: self.source.capture(key)), ("native_journal_changed", "native_journal_scope"))
                self.assertEqual(self.source._selection, {})
    def test_origin_changes_during_load_fail_closed(self):
        self.market.hook = lambda: self.market.origin.update(scope_id="c" * 32)
        self.assertEqual(self.error(self.source.list_trades), "native_journal_changed")
    def test_failed_refresh_clears_old_selection(self):
        key = self.source.list_trades()["trades"][0]["trade_id"]
        self.market.failure = AppError("bridge_unavailable", "synthetic timeout", 503)
        self.error(self.source.list_trades)
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_journal_selection")
    def test_wrong_row_account_security_and_changed_row_rejected(self):
        for change in ({"account_id": "OTHER"}, {"security_id": "ETHUSDT@BinanceFutures"}):
            self.market.rows = [row(**change)]
            self.assertEqual(self.error(self.source.list_trades), "native_journal_scope")
        self.market.rows = [row()]
        key = self.source.list_trades()["trades"][0]["trade_id"]
        self.market.rows[0]["pnl"] = "1"
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_journal_changed")
    def test_local_fall_back_range_uses_wall_clock_days_and_preserves_raw_times(self):
        start = nt("2026-10-27T12:00:00-04:00", "Local")
        end = nt("2026-11-03T12:00:00-05:00", "Local")
        self.market.rows = [row(open_time=nt("2026-11-03T10:00:00-05:00", "Local"),
                                close_time=nt("2026-11-03T10:01:00-05:00", "Local"))]
        def coverage(value):
            value["coverage"].update({"from": start, "to": end})
            return value
        self.market.change = coverage
        listed = self.source.list_trades()
        evidence = self.source.capture(listed["trades"][0]["trade_id"])
        self.assertEqual(evidence["evidence"]["native_coverage"]["data"]["from"], start)
        self.assertEqual(evidence["evidence"]["native_coverage"]["data"]["to"], end)
        self.assertEqual(evidence["evidence"]["native_trade"]["data"]["open_time"], self.market.rows[0]["open_time"])
        for invalid_end in ("2026-11-03T12:00:01-05:00", "2026-10-27T12:00:00-05:00"):
            with self.subTest(invalid_end=invalid_end):
                end["iso"] = invalid_end
                self.assertEqual(self.error(self.source.list_trades), "native_journal_invalid")

    def test_malformed_rows_are_rejected_without_coercion(self):
        for change in ({"pnl": 1.1}, {"commission": "NaN"}, {"pnl": "1e-8"}, {"open_volume": "-1"},
                       {"open_time": nt("2026-11-09T10:00:00Z")}, {"side": "Long"},
                       {"is_complete": 1}, {"comment": "private"}, {"close_time": nt("2000-01-01T00:00:00")},
                       {"open_time": nt(None)}):
            with self.subTest(change=change):
                self.market.rows = [row(**change)]
                self.assertEqual(self.error(self.source.list_trades), "native_journal_invalid")
    def test_duplicates_bounds_coverage_source_and_replay_rejected(self):
        mutations = [lambda v: v.update(source="replay"), lambda v: v.update(schema_version=True),
                     lambda v: v["coverage"].update(days=8), lambda v: v["coverage"].update(account_id="PRIVATE"), lambda v: v["coverage"].update(limit=101),
                     lambda v: v["coverage"].update(truncated=True), lambda v: v["coverage"].update(returned=True),
                     lambda v: v["coverage"].update(to=nt("2026-11-11T12:00:00+08:00", "Local")),
                     lambda v: v["origin"].update(account_is_real=0)]
        for change in mutations:
            with self.subTest(change=change):
                self.market.change = lambda v, change=change: (change(v), v)[1]
                self.assertEqual(self.error(self.source.list_trades), "native_journal_invalid")
        self.market.change = lambda v: v
        self.market.rows = [row(), row()]
        self.assertEqual(self.error(self.source.list_trades), "native_journal_invalid")
    def test_incomplete_rows_are_listed_but_cannot_be_evidence(self):
        self.market.rows = [row(is_complete=False, close_time=nt(None), close_price="0", close_volume="0")]
        item = self.source.list_trades()["trades"][0]
        self.assertEqual(item["status"], "incomplete")
        self.assertEqual(self.error(lambda: self.source.capture(item["trade_id"])), "journal_open_trade")
    def test_workbench_default_ignores_and_preserves_legacy_config_and_detail_needs_no_model(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "journal.json"
            config.write_text('{"legacy":"retained"}')
            with patch("companion.app.ProviderClient") as provider:
                app = Workbench(directory, market=self.market)
                try:
                    listed = app.handle("GET", "/api/trades", {})["data"]
                    key = listed["trades"][0]["trade_id"]
                    result = app.handle("GET", "/api/trades/" + key + "/evidence", {})
                    self.assertEqual(result["evidence"]["source"], "trade")
                    self.assertEqual(config.read_text(), '{"legacy":"retained"}')
                    provider.return_value.complete.assert_not_called()
                    old = app.journal
                    replacement = NativeMarket(); replacement.port = 34001
                    replacement.origin["port"] = 34001
                    with patch("companion.app.MarketSource", return_value=replacement):
                        app.set_bridge_port(34001)
                    self.assertTrue(old._closed)
                    self.assertEqual(self.error(lambda: app._journal_read(key)), "native_journal_selection")
                finally:
                    app.close()
    def test_mcp_uses_same_scope_and_rejects_midflight_port_change(self):
        spec = importlib.util.spec_from_file_location("_native_test_client", Path(__file__).resolve().parents[1] / "mcp/atas_client.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        client = module.AtasBridgeClient(port=self.market.port, host="127.0.0.1")
        with patch("companion.evidence.MarketSource", return_value=self.market):
            key = client.native_journal()["trades"][0]["trade_id"]
            self.assertEqual(client.native_trade_evidence(key)["source"], "trade")
            source = client._journal
            captured = source.capture(key)
            def switched(_key):
                client.port = 34001
                return captured
            with patch.object(source, "capture", switched), self.assertRaises(module.BridgeError):
                client.native_trade_evidence(key)
            self.assertTrue(source._closed)
        remote = module.AtasBridgeClient(port=34000, host="example.invalid")
        with self.assertRaises(module.BridgeError): remote.native_journal()

    def test_port_switch_during_inflight_capture_cannot_publish_old_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Workbench(directory, market=self.market)
            try:
                key = app._journal_read()["trades"][0]["trade_id"]
                old = app.journal
                entered, release = threading.Event(), threading.Event()
                original = old.capture
                def delayed(value):
                    evidence = original(value)
                    entered.set()
                    self.assertTrue(release.wait(3))
                    return evidence
                results = []
                def run():
                    try: results.append(app._journal_read(key))
                    except AppError as exc: results.append(exc.code)
                with patch.object(old, "capture", delayed):
                    thread = threading.Thread(target=run); thread.start()
                    self.assertTrue(entered.wait(3))
                    replacement = NativeMarket(); replacement.port = 34001
                    with patch("companion.app.MarketSource", return_value=replacement): app.set_bridge_port(34001)
                    release.set(); thread.join(3)
                self.assertEqual(results, ["native_journal_changed"])
            finally: app.close()

if __name__ == "__main__": unittest.main()
