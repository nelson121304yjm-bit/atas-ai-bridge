"""Synthetic persisted-file fixtures only: no ATAS, private records or providers."""
import copy
import csv
from datetime import datetime
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from companion.app import Workbench
from companion.common import AppError, encode_json
from companion.native_journal_files import (MAX_FILE_BYTES, SCHEMAS, NativeFileJournalSource,
                                           parse_container)

SEC = "BTCUSDT@BinanceFutures"
NOW = datetime(2026, 11, 10, 12)


def container(entity, rows):
    version, columns = SCHEMAS[entity]
    stream = io.StringIO(newline="")
    stream.write(f'Version: "{version}"\nEntity: "{entity}"\nColumns: ')
    csv.writer(stream, quoting=csv.QUOTE_ALL, lineterminator="\n").writerow(columns)
    stream.write("StartData\n")
    writer = csv.writer(stream, quoting=csv.QUOTE_ALL, lineterminator="\n")
    for row in rows:
        writer.writerow([row.get(name, "") for name in columns])
    stream.write("EndData\n")
    return stream.getvalue().encode()


def portfolio(account="private-sim", real="False"):
    return {"AccountID": account, "IsRealAccount": real}


def history(**changes):
    row = {"Id": "private-row", "AccountID": "private-sim", "SecurityId": SEC,
           "OpenTime": "11/09/2026 10:00:00", "CloseTime": "11/09/2026 10:01:00",
           "OpenPrice": "100.00000000000000000001", "ClosePrice": "101.00000000000000000001",
           "OpenVolume": "0.000000000000000003", "CloseVolume": "0.000000000000000001",
           "PnL": "-0.00000000000000000017", "TicksPnL": "1", "PricePnL": "1",
           "Commission": "", "Comment": "private free text, secret", "Reviewed": "False",
           "EnterTradeId": "private-entry", "ExitTradeId": "private-exit", "MissingDataCase": "None"}
    return {**row, **changes}


def fill(**changes):
    return {"Route": "private-route", "AccountID": "private-sim", "SecurityId": SEC,
            "Id": "private-entry", "OrderId": "private-order", "OrderDirection": "Buy",
            "Price": "100.00000000000000000001", "Time": "11/09/2026 10:00:00",
            "Volume": "0.000000000000000003", "OpenVolume": "0", "Commission": "", "CommissionCurrency": "", **changes}


class FileJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tables = {"Portfolio": [portfolio()], "HistoryMyTrade": [history()], "MyTrade": [fill()]}
        self.write()
        self.source = NativeFileJournalSource(self.root, clock=lambda: NOW)

    def write(self):
        for entity, rows in self.tables.items():
            (self.root / (entity + ".cdb")).write_bytes(container(entity, rows))

    def error(self, call):
        with self.assertRaises(AppError) as caught:
            call()
        return caught.exception.code

    def test_strict_headers_schema_terminator_and_csv(self):
        original = container("HistoryMyTrade", [history()])
        self.assertEqual(len(parse_container(original.rstrip(b"\n"), "HistoryMyTrade")), 1)
        for changed in (original.replace(b'Version: "2"', b'Version: "1"'),
                        original.replace(b'Entity: "HistoryMyTrade"', b'Entity: "MyTrade"'),
                        original.replace(b'"Id",', b'"Unknown",', 1),
                        original.replace(b"StartData\n", b"StartData\nStartData\n"),
                        original.replace(b"EndData\n", b""), original + b"trailing\n",
                        original + b"EndData\n", original.replace(b'"private-row",', b'"private-row"x,', 1),
                        b"\xef\xbb\xbf" + original):
            with self.subTest(changed=changed[:50]):
                self.assertEqual(self.error(lambda: parse_container(changed, "HistoryMyTrade")), "native_files_invalid")

    def test_input_size_rows_and_unsafe_values(self):
        self.assertEqual(self.error(lambda: parse_container(b"x" * (MAX_FILE_BYTES + 1), "Portfolio")), "native_files_size")
        self.assertEqual(self.error(lambda: parse_container(container("Portfolio", [portfolio()] * 10001), "Portfolio")), "native_files_invalid")
        for name, value in (("OpenPrice", "NaN"), ("ClosePrice", "Infinity"), ("OpenPrice", "-1"),
                            ("CloseVolume", "1e-3"), ("PnL", "1,5"), ("CloseTime", "2026-11-09T10:01:00Z"),
                            ("MissingDataCase", "unsupported free text!")):
            with self.subTest(name=name, value=value):
                self.tables["HistoryMyTrade"] = [history(**{name: value})]; self.write()
                self.assertEqual(self.error(self.source.list_trades), "native_files_invalid")

    def test_one_simulated_account_partial_exit_precision_and_safe_facts(self):
        self.tables["Portfolio"].append(portfolio("private-real", "True"))
        self.tables["HistoryMyTrade"] += [history(Id="private-partial", ExitTradeId="private-exit-two", CloseVolume="0.000000000000000002"), history(Id="real-row", AccountID="private-real")]
        self.write()
        status = self.source.status(); self.assertTrue(status["connected"])
        value = self.source.list_trades(); self.assertEqual(value["origin_key"], status["origin_key"])
        self.assertEqual(value["returned"], 2)
        self.assertEqual(len(value["coverage"]["file_modified_at"]), 3)
        self.assertEqual(value["coverage"]["mode"], "local_persisted_snapshot")
        for row in value["trades"]:
            self.assertEqual(row["side"], "Buy"); self.assertIsNone(row["commission"])
            evidence = self.source.capture(row["trade_id"])
            self.assertEqual(evidence["identity"]["source_detail"], "atas_native_persisted_journal")
            facts = evidence["evidence"]["native_trade"]["data"]
            self.assertEqual(facts["open_price"], "100.00000000000000000001")
            self.assertEqual(facts["open_time"]["kind"], "Unspecified")
            self.assertIsNone(evidence["limits"]["net_pnl"])
            self.assertFalse(evidence["limits"]["current_flatness_verified"])
        raw = encode_json([status, value, evidence]).decode()
        for private in ("private-", "AccountID", "account_id", "native_id", "entry_ref", "exit_ref", "Comment", str(self.root)):
            self.assertNotIn(private, raw)

    def test_no_simulation_inference_from_names_and_exact_boolean(self):
        self.tables["Portfolio"] = [portfolio("SIM-not-sim", "True")]
        self.tables["HistoryMyTrade"] = [history(AccountID="SIM-not-sim")]
        self.tables["MyTrade"] = []; self.write()
        self.assertEqual(self.source.status()["reason"], "native_accounts_empty")
        self.tables["Portfolio"][0]["IsRealAccount"] = "false"; self.write()
        self.assertEqual(self.source.status()["reason"], "native_files_invalid")

    def test_multiple_accounts_require_opaque_explicit_selection(self):
        self.tables["Portfolio"].append(portfolio("private-second"))
        self.tables["HistoryMyTrade"].append(history(Id="private-other", AccountID="private-second")); self.write()
        status = self.source.status()
        self.assertTrue(status["selection_required"]); self.assertFalse(status["connected"])
        self.assertEqual(len(status["account_options"]), 2)
        self.assertEqual(self.error(self.source.list_trades), "native_account_selection_required")
        self.assertEqual(self.error(lambda: self.source.select_account("private-sim")), "native_account_selection")
        key = status["account_options"][0]["key"]
        self.assertTrue(self.source.select_account(key)["connected"])
        self.assertEqual(self.source.list_trades()["returned"], 1)
        self.assertEqual(self.error(lambda: NativeFileJournalSource(self.root).select_account(key)), "native_account_selection")

    def test_selected_account_missing_real_or_changed_source_never_falls_back(self):
        key = self.source.list_trades()["trades"][0]["trade_id"]
        self.tables["Portfolio"] = [portfolio("private-second")]
        self.tables["HistoryMyTrade"] = [history(AccountID="private-second")]; self.write()
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_account_changed")
        self.assertEqual(self.source._selection, {})
        self.assertEqual(self.error(lambda: self.source.select_account("old-stale-choice")), "native_account_selection")
        self.assertFalse(self.source.status()["connected"])
        status = self.source.status(); self.assertTrue(status["selection_required"])
        self.assertTrue(self.source.select_account(status["account_options"][0]["key"])["connected"])
        key = self.source.list_trades()["trades"][0]["trade_id"]
        self.tables["Portfolio"][0]["IsRealAccount"] = "True"; self.write()
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_account_changed")
        self.tables["Portfolio"][0]["IsRealAccount"] = "False"; self.write()
        self.source.select_account(status["account_options"][0]["key"])
        key = self.source.list_trades()["trades"][0]["trade_id"]
        other = self.root / "other"; other.mkdir()
        for entity, rows in self.tables.items(): (other / (entity + ".cdb")).write_bytes(container(entity, rows))
        self.source.directory = other
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_account_changed")

    def test_signed_native_long_short_and_partial_close_quantities_are_preserved(self):
        for side, opened, closed in (("Buy", "0.000000000000000003", "-0.000000000000000001"),
                                     ("Sell", "-0.000000000000000003", "0.000000000000000001")):
            self.tables["HistoryMyTrade"] = [history(OpenVolume=opened, CloseVolume=closed)]
            self.tables["MyTrade"] = [fill(OrderDirection=side)]; self.write()
            row = self.source.list_trades()["trades"][0]
            self.assertTrue(row["is_complete"]); self.assertEqual(row["side"], side)
            facts = self.source.capture(row["trade_id"])["evidence"]["native_trade"]["data"]
            self.assertEqual((facts["open_volume"], facts["close_volume"]), (opened, closed))

    def test_new_second_account_revokes_automatic_selection(self):
        self.source.list_trades()
        self.tables["Portfolio"].append(portfolio("private-second"))
        self.tables["MyTrade"].append(fill(AccountID="private-second")); self.write()
        self.assertTrue(self.source.status()["selection_required"])
        self.assertEqual(self.source._selection, {})

    def test_duplicate_ids_and_entry_direction_never_inferred(self):
        for entity in ("Portfolio", "HistoryMyTrade", "MyTrade"):
            original = copy.deepcopy(self.tables[entity]); self.tables[entity].append(copy.deepcopy(original[0])); self.write()
            self.assertEqual(self.error(self.source.list_trades), "native_files_invalid")
            self.tables[entity] = original
        for changed in ([], [fill(AccountID="other")], [fill(SecurityId="ETH")], [fill(Price="99")], [fill(Time="11/09/2026 09:59:00")], [fill(Volume="0")], [fill(OrderDirection="")]):
            self.tables["MyTrade"] = changed; self.write()
            listed = self.source.list_trades(); self.assertIsNone(listed["trades"][0]["side"])
            self.assertIn("entry_direction_missing", self.source.capture(listed["trades"][0]["trade_id"])["quality"])

    def test_stable_row_recapture_after_unrelated_append_and_changed_row_failure(self):
        listed = self.source.list_trades(); key = listed["trades"][0]["trade_id"]
        self.assertEqual(self.source.list_trades()["trades"][0]["trade_id"], key)
        self.tables["HistoryMyTrade"].append(history(Id="private-unrelated")); self.write()
        self.assertEqual(self.source.capture(key)["source"], "trade")
        self.tables["HistoryMyTrade"][0]["PnL"] = "2"; self.write()
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_journal_changed")
        self.assertEqual(self.source._selection, {})

    def test_bounds_incomplete_and_selected_row_outside_list_after_append(self):
        self.tables["HistoryMyTrade"] = [history(Id=str(index)) for index in range(101)]
        self.write(); listed = self.source.list_trades()
        self.assertEqual(listed["returned"], 100); self.assertTrue(listed["coverage"]["truncated"])
        key = listed["trades"][-1]["trade_id"]
        self.tables["HistoryMyTrade"].append(history(Id="zzzz")); self.write()
        self.assertEqual(self.source.capture(key)["source"], "trade")
        self.tables["HistoryMyTrade"] = [history(CloseTime="", CloseVolume="0")]; self.write()
        incomplete = self.source.list_trades()["trades"][0]
        self.assertFalse(incomplete["is_complete"])
        self.assertEqual(self.error(lambda: self.source.capture(incomplete["trade_id"])), "journal_open_trade")
        self.tables["HistoryMyTrade"] = [history(CloseTime="11/02/2026 10:00:00"), history(Id="future", CloseTime="11/11/2026 10:00:00")]; self.write()
        self.assertEqual(self.source.list_trades()["returned"], 0)

    def test_incomplete_native_flags_do_not_block_complete_history_or_establish_direction(self):
        self.tables["HistoryMyTrade"] = [history(Id="complete"), history(Id="incomplete", MissingDataCase="MissingEntry")]
        self.write(); listed = self.source.list_trades()
        self.assertEqual(listed["returned"], 2)
        incomplete = next(row for row in listed["trades"] if not row["is_complete"])
        self.assertIsNone(incomplete["side"])
        self.assertEqual(incomplete["missing_data_case"], "MissingEntry")
        self.assertEqual(listed["coverage"]["incomplete_rows"], 1)
        self.assertEqual(self.error(lambda: self.source.capture(incomplete["trade_id"])), "journal_open_trade")

    def test_partial_write_missing_file_and_symlink_clear_selection(self):
        key = self.source.list_trades()["trades"][0]["trade_id"]
        target = self.root / "HistoryMyTrade.cdb"
        target.write_bytes(target.read_bytes().replace(b"EndData\n", b""))
        self.assertEqual(self.error(self.source.list_trades), "native_files_invalid")
        self.assertEqual(self.error(lambda: self.source.capture(key)), "native_journal_selection")
        target.unlink()
        self.assertEqual(self.source.status()["reason"], "native_files_missing")
        target.symlink_to(self.root / "Portfolio.cdb")
        self.assertEqual(self.error(self.source.list_trades), "native_files_invalid")

    def test_snapshot_race_retries_once_and_persistent_race_fails(self):
        original = os.open
        changes, persistent = [], [False]
        def race(path, flags, *args, **kwargs):
            if Path(path).name == "Portfolio.cdb" and (not changes or persistent[0]):
                changes.append(1)
                self.tables["HistoryMyTrade"].append(history(Id="race-" + str(len(changes))))
                self.write()
            return original(path, flags, *args, **kwargs)
        with patch("companion.native_journal_files.os.open", race):
            self.assertEqual(self.source.list_trades()["returned"], 2)
        persistent[0] = True; changes.clear()
        with patch("companion.native_journal_files.os.open", race):
            self.assertEqual(self.error(self.source.list_trades), "native_files_changed")
        self.assertEqual(len(changes), 2); self.assertEqual(self.source._selection, {})

    def test_default_workbench_detail_without_market_provider_and_port_independence(self):
        class Offline:
            port = 1
            def status(self): return {"connected": False, "reason": "bridge_unavailable"}
            def capture(self): raise AssertionError("market must not be read")
        with patch("companion.native_journal_files.default_database_dir", return_value=self.root):
            app = Workbench(self.root / "state", market=Offline())
        self.addCleanup(app.close); app.journal._clock = lambda: NOW
        self.assertIsInstance(app.journal, NativeFileJournalSource)
        status = app.handle("GET", "/api/status", {})
        self.assertTrue(status["journal"]["connected"]); self.assertFalse(status["configured"])
        key = app.handle("GET", "/api/trades", {})["data"]["trades"][0]["trade_id"]
        source = app.journal; app.set_bridge_port(2)
        self.assertIs(app.journal, source); self.assertFalse(source._closed)
        self.assertEqual(app.handle("GET", f"/api/trades/{key}/evidence", {})["evidence"]["source"], "trade")
        self.assertEqual(self.error(lambda: app.handle("POST", "/api/preview", {"source": "trade", "trade_id": key, "question": "test"})), "provider_not_configured")

    def test_local_choice_route_and_mcp_allowlists(self):
        self.tables["Portfolio"].append(portfolio("private-second"))
        self.tables["HistoryMyTrade"].append(history(Id="private-second-row", AccountID="private-second")); self.write()
        app = Workbench(self.root / "state", journal=self.source); self.addCleanup(app.close)
        status = app.handle("GET", "/api/journal/accounts", {})["journal"]
        chosen = app.handle("POST", "/api/journal/account", {"account_key": status["account_options"][0]["key"]})
        self.assertTrue(chosen["journal"]["connected"])
        spec = importlib.util.spec_from_file_location("persisted_mcp_test", Path(__file__).resolve().parents[1] / "mcp/atas_client.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        client = module.AtasBridgeClient(port=1)
        with patch.object(client, "_persisted_request", lambda path: app.handle("GET", path, {})):
            listed = client.persisted_journal(); key = listed["trades"][0]["trade_id"]
            evidence = client.persisted_trade_evidence(key)
            with self.assertRaises(module.BridgeError): client.persisted_trade_evidence("b" * 64)
            self.assertEqual(evidence["evidence"]["native_trade"]["data"]["pnl"], listed["trades"][0]["pnl"])
        raw = json.dumps([listed, evidence])
        for private in ("private-", "account_options", "origin_key", "native_id", "entry_ref", "exit_ref", str(self.root), "control_token"):
            self.assertNotIn(private, raw)
        listed["trades"][0]["open_time"]["iso"] = "changed"
        self.assertNotEqual(self.source.capture(key)["evidence"]["native_trade"]["data"]["open_time"]["iso"], "changed")

    def test_account_switch_during_capture_cannot_publish_old_evidence(self):
        self.tables["Portfolio"].append(portfolio("private-second"))
        self.tables["HistoryMyTrade"].append(history(Id="other", AccountID="private-second")); self.write()
        options = self.source.status()["account_options"]
        self.source.select_account(options[0]["key"])
        app = Workbench(self.root / "state", journal=self.source); self.addCleanup(app.close)
        key = app._journal_read()["trades"][0]["trade_id"]
        entered, release = threading.Event(), threading.Event()
        original = self.source.capture
        def delayed(value):
            result = original(value); entered.set()
            self.assertTrue(release.wait(3)); return result
        results = []
        def run():
            try: results.append(app._journal_read(key))
            except AppError as exc: results.append(exc.code)
        with patch.object(self.source, "capture", delayed):
            thread = threading.Thread(target=run); thread.start()
            self.assertTrue(entered.wait(3))
            app.handle("POST", "/api/journal/account", {"account_key": options[1]["key"]})
            release.set(); thread.join(3)
        self.assertEqual(results, ["native_journal_changed"])

    def test_mcp_bootstrap_uses_only_existing_fixed_loopback_runtime_and_get_routes(self):
        import http.client
        spec = importlib.util.spec_from_file_location("persisted_mcp_runtime", Path(__file__).resolve().parents[1] / "mcp/atas_client.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        app = Workbench(self.root / "state", journal=self.source); self.addCleanup(app.close)
        runtime = self.root / "runtime.json"
        runtime.write_text(json.dumps({"port": 8770, "control_token": "t" * 43})); runtime.chmod(0o600)
        calls = []
        change_runtime = [False]
        class Response:
            status = 200
            def __init__(self, value, cookie=None): self.value, self.cookie = value, cookie
            def read(self, bound):
                if change_runtime[0]:
                    runtime.write_text(json.dumps({"port": 8771, "control_token": "u" * 43}))
                return encode_json(self.value)
            def getheader(self, name): return self.cookie
        class Connection:
            def __init__(self, host, port, timeout):
                self.assert_scope = (host, port, timeout)
                calls.append(self.assert_scope)
            def request(self, method, path, body=None, headers=None):
                calls.append((method, path, json.loads(body) if body else None, headers))
                if path == "/__launcher/issue":
                    assert method == "POST" and json.loads(body) == {} and headers["X-Launcher-Token"] == "t" * 43
                    self.response = Response({"token": "one-use-private-token"})
                elif path == "/api/bootstrap":
                    assert json.loads(body) == {"token": "one-use-private-token"}
                    self.response = Response({"ok": True}, "atas_review=private-cookie; HttpOnly; SameSite=Strict; Path=/")
                else:
                    assert method == "GET" and headers["Cookie"] == "atas_review=private-cookie"
                    self.response = Response(app.handle(method, path, {}))
            def getresponse(self): return self.response
            def close(self): pass
        client = module.AtasBridgeClient(port=1, host="evil.example")
        with patch.dict(os.environ, {"ATAS_REVIEW_STATE_DIR": str(self.root), "ATAS_AI_BRIDGE_HOST": "evil.example"}), patch.object(http.client, "HTTPConnection", Connection):
            value = client.persisted_journal(); key = value["trades"][0]["trade_id"]
            evidence = client.persisted_trade_evidence(key)
            self.assertEqual(evidence["source"], "trade")
            endpoints = [call[1] for call in calls if len(call) == 4]
            self.assertEqual(endpoints, ["/__launcher/issue", "/api/bootstrap", "/api/trades", f"/api/trades/{key}/evidence"])
            self.assertTrue(all(call == ("127.0.0.1", 8770, 5) for call in calls if len(call) == 3))
            change_runtime[0] = True
            with self.assertRaises(module.BridgeError): client.persisted_trade_evidence(key)
            self.assertEqual(client._persisted_keys, set())
        raw = json.dumps([value, evidence])
        for private in ("private-cookie", "one-use-private-token", "t" * 43, str(self.root), "private-"):
            self.assertNotIn(private, raw)

    def test_mcp_rejects_missing_unsafe_runtime_without_network_or_private_error(self):
        import http.client
        spec = importlib.util.spec_from_file_location("persisted_mcp_missing", Path(__file__).resolve().parents[1] / "mcp/atas_client.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        runtime = self.root / "runtime.json"
        with patch.dict(os.environ, {"ATAS_REVIEW_STATE_DIR": str(self.root)}), patch.object(http.client, "HTTPConnection") as connection:
            for data in (None, {"port": 1, "control_token": "s" * 43}, {"port": True, "control_token": "s" * 43},
                         {"port": 8770, "control_token": "private-secret\r\n" + "s" * 43}):
                if data is not None:
                    runtime.write_text(json.dumps(data)); runtime.chmod(0o600)
                with self.assertRaises(module.BridgeError) as caught:
                    module.AtasBridgeClient(port=1).persisted_journal()
                self.assertNotIn(str(self.root), str(caught.exception))
                self.assertNotIn("private-secret", str(caught.exception))
            connection.assert_not_called()


if __name__ == "__main__": unittest.main()
