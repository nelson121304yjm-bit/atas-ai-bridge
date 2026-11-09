"""Synthetic stdio journal tests; no production database is opened."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

from companion.common import AppError, encode_json
from companion.journal import JournalSource


FAKE_SERVER = '''import json, sys
from pathlib import Path
data = json.loads(Path(sys.argv[2]).read_text())
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "notifications/initialized":
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/call":
        name = request["params"]["name"]
        args = request["params"]["arguments"]
        if name == "get_trade_context" and args != {"trade_id": "t1", "max_samples": 60}:
            result = {"isError": True, "structuredContent": {"error": "wrong arguments"}}
        elif name == "list_trades" and args != {"limit": 50}:
            result = {"isError": True, "structuredContent": {"error": "wrong arguments"}}
        else:
            result = {"isError": False, "structuredContent": data[name]}
    else:
        result = {"error": "unknown method"}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}) + "\\n")
    sys.stdout.flush()
'''


def context(**trade_changes):
    trade = {"trade_id": "t1", "account_id": "SIM", "instrument": "BTCUSDT@BinanceFutures",
             "status": "closed", "opened_at_ms": 1000, "closed_at_ms": 2000,
             "fees_complete": False, "fees": None, "net_pnl": None,
             "baseline_provenance": {"baseline_source": "operator_confirmed_flat",
                                     "units_verification": "native"}}
    trade.update(trade_changes)
    return {"trade": trade, "executions": [], "execution_coverage": {"returned": 0, "total": 0,
            "truncated": False}, "snapshots": [], "snapshot_coverage": {"returned": 0, "total": 0,
            "truncated": False}, "excursions": {"r_multiple": None},
            "quality": {"missing_delta": 2}, "capture_integrity": {"ledger_quarantined": False,
            "coverage_incomplete": True, "gap_count": 2, "scope": "whole_capture_including_events_after_last_snapshot",
            "baseline_provenance": trade["baseline_provenance"]},
            "warnings": ["Fees incomplete", "Coverage incomplete"], "simulation_only": True}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.script = root / "fake_mcp.py"
        self.script.write_text(FAKE_SERVER)
        self.db = root / "synthetic.json"
        self.data = {"get_storage_stats": {"rows": {"trades": 1}},
                     "list_trades": {"trades": [context()["trade"]], "returned": 1,
                         "total": 1, "truncated": False, "order": "opened_at_ms_desc",
                         "simulation_only": True, "capture_integrity": context()["capture_integrity"],
                         "warnings": ["Coverage incomplete"]},
                     "get_trade_context": context()}
        self.save()
        self.config = {"python_path": sys.executable, "script_path": str(self.script),
                       "db_path": str(self.db), "account_id": "SIM"}

    def save(self):
        self.db.write_text(json.dumps(self.data))

    def test_no_config_is_explicitly_disconnected(self):
        self.assertEqual(JournalSource().status()["connected"], False)
        with self.assertRaises(AppError) as caught:
            JournalSource().list_trades()
        self.assertEqual(caught.exception.code, "journal_disconnected")

    def test_fixed_handshake_list_and_historical_capture(self):
        source = JournalSource(self.config)
        status = source.status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["storage"]["rows"]["trades"], 1)
        listed = source.list_trades()
        self.assertEqual([t["trade_id"] for t in listed["trades"]], ["t1"])
        captured = source.capture("t1")
        self.assertEqual(captured["schema_version"], 1)
        self.assertEqual(captured["source"], "trade")
        self.assertEqual(captured["identity"]["trade_id"], "t1")
        self.assertEqual(captured["evidence"]["trade_context"]["data"]["trade"]["net_pnl"], None)
        self.assertEqual(captured["evidence"]["trade_context"]["data"]["quality"]["missing_delta"], 2)
        self.assertIn("operator_confirmed_flat", encode_json(captured).decode())
        self.assertNotIn('"account_id"', encode_json(captured).decode())
        self.assertTrue(any("operator" in q.lower() for q in captured["quality"]))
        self.assertTrue(any("coverage" in q.lower() for q in captured["quality"]))
        self.assertNotIn("db_path", encode_json(captured).decode())
        self.assertLessEqual(len(encode_json(captured)), 131072)

    def test_rejects_open_wrong_account_instrument_and_unverified_trade(self):
        cases = ({"status": "open", "closed_at_ms": None}, {"account_id": "OTHER"},
                 {"instrument": "ETHUSDT@BinanceFutures"}, {"baseline_provenance": None})
        for change in cases:
            with self.subTest(change=change):
                self.data["get_trade_context"] = context(**change)
                self.save()
                with self.assertRaises(AppError):
                    JournalSource(self.config).capture("t1")

    def test_rejects_unverified_simulation_and_bad_config(self):
        self.data["get_trade_context"] = context()
        self.data["get_trade_context"]["simulation_only"] = None
        self.save()
        with self.assertRaises(AppError):
            JournalSource(self.config).capture("t1")
        for change in ({"python_path": "python3"}, {"db_path": "relative.db"},
                       {"script_path": "relative.py"}, {"account_id": ""}):
            with self.subTest(change=change), self.assertRaises(AppError):
                JournalSource(dict(self.config, **change))

    def test_rejects_oversize_context_without_silent_truncation(self):
        self.data["get_trade_context"]["warnings"] = ["x" * 140000]
        self.save()
        with self.assertRaises(AppError) as caught:
            JournalSource(self.config).capture("t1")
        self.assertIn("size", caught.exception.code)

    def test_rejects_more_than_sixty_returned_snapshots(self):
        self.data["get_trade_context"]["snapshots"] = [{"snapshot_key": str(i)} for i in range(61)]
        self.save()
        with self.assertRaises(AppError):
            JournalSource(self.config).capture("t1")

    def test_rejects_raw_response_over_three_mebibytes(self):
        self.data["list_trades"]["warnings"] = ["x" * (3 * 1024 * 1024)]
        self.save()
        with self.assertRaises(AppError) as caught:
            JournalSource(self.config).list_trades()
        self.assertEqual(caught.exception.code, "journal_response_too_large")

    def test_list_filters_out_other_accounts_and_instruments(self):
        self.data["list_trades"]["trades"].extend([
            context(account_id="OTHER", trade_id="t2")["trade"],
            context(instrument="ETHUSDT@BinanceFutures", trade_id="t3")["trade"]])
        self.save()
        listed = JournalSource(self.config).list_trades()
        self.assertEqual([t["trade_id"] for t in listed["trades"]], ["t1"])
        self.assertEqual(listed["excluded_count"], 2)


if __name__ == "__main__":
    unittest.main()
