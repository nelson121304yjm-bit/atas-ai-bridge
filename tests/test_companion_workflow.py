"""Workflow contracts use synthetic sources, credentials and provider only."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from companion.common import AppError, encode_json
from companion.store import SYSTEM_FIELDS

try:
    from companion.app import Workbench
except ImportError:
    Workbench = None


def evidence(source="market"):
    return {"schema_version": 1, "source": source,
            "captured_at": "2026-11-09T00:00:00+00:00",
            "identity": {"security_id": "BTCUSDT@BinanceFutures", "simulation_only": True},
            "quality": ["synthetic_fixture", "non_atomic"],
            "evidence": {"candles" if source == "market" else "trade_context": {
                "retrieved_at": "2026-11-09T00:00:00+00:00", "data": {"close": 100}}},
            "limits": {"historical_only": source == "trade"}}


class FakeMarket:
    port = 34000

    def __init__(self):
        self.captures = 0
        self.value = evidence()

    def status(self):
        return {"connected": True, "eligible": True, "bridge_version": "synthetic"}

    def capture(self):
        self.captures += 1
        return copy.deepcopy(self.value)


class FakeJournal:
    def __init__(self):
        self.captures = []

    def status(self):
        return {"connected": True, "simulation_only": True}

    def list_trades(self):
        return {"trades": [{"trade_id": "synthetic-trade"}], "simulation_only": True}

    def capture(self, identifier):
        self.captures.append(identifier)
        return evidence("trade")


class FakeVault:
    def __init__(self):
        self.values = {}
        self.calls = []

    def set(self, address, key, persist=False):
        self.calls.append(("set", address, persist))
        self.values[address] = key

    def get(self, address):
        self.calls.append(("get", address))
        return self.values.get(address)

    def has(self, address):
        return self.get(address) is not None

    def delete(self, address):
        self.calls.append(("delete", address))
        self.values.pop(address, None)


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.result = {"text": "Observed synthetic close [E:candles].",
                       "usage": {"total_tokens": 12}, "request_id": "synthetic-request"}
        self.error = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def complete(self, base_url, model, key, messages):
        self.calls.append({"base_url": base_url, "model": model, "key": key,
                           "messages": copy.deepcopy(messages)})
        self.entered.set()
        self.release.wait(5)
        if self.error:
            raise self.error
        return copy.deepcopy(self.result)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Workbench, "Task 4 Workbench implementation is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.market, self.journal = FakeMarket(), FakeJournal()
        self.provider, self.vault = FakeProvider(), FakeVault()
        self.app = self.new_app()
        self.addCleanup(self.close_app)

    def new_app(self, **kwargs):
        return Workbench(self.directory, market=self.market, journal=self.journal,
                         provider=self.provider, vault=self.vault, **kwargs)

    def close_app(self):
        self.provider.release.set()
        self.app.close()

    def call(self, method, path, payload=None):
        payload = payload or {}
        if method == "POST" and path in ("/api/settings", "/api/settings/test") and "expected" not in payload:
            # Existing workflow fixtures act as a freshly loaded client. Stale/absent
            # identity behavior is tested separately without this convenience helper.
            payload = {**payload, "expected": self.app.store.settings()}
        return self.app.handle(method, path, payload)

    def configure(self, **values):
        return self.call("POST", "/api/settings", {
            "base_url": "https://synthetic.invalid/v1", "model": "synthetic-model",
            "key": "synthetic-credential-only", **values})

    def preview(self, **values):
        return self.call("POST", "/api/preview", {"source": "market", "question": "Review evidence", **values})["preview"]

    def finish(self, response):
        job_id = response["job"]["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.call("GET", "/api/jobs/" + job_id)["job"]
            if job["state"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.005)
        self.fail("Synthetic job did not complete")

    def report(self):
        self.configure()
        preview = self.preview()
        job = self.finish(self.call("POST", "/api/analyze", {"preview_id": preview["id"]}))
        self.assertEqual(job["state"], "succeeded", job)
        return self.call("GET", "/api/reports/" + job["result"]["report_id"])["report"]

    def error(self, code, method, path, payload=None):
        with self.assertRaises(AppError) as raised:
            self.call(method, path, payload)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_initial_prompt_optional_blank_system_and_no_provider_call(self):
        profile = self.call("GET", "/api/profile")["profile"]
        self.assertTrue(profile["prompt"]["data"])
        self.assertTrue(all(value == "" for value in profile["system"]["data"].values()))
        status = self.call("GET", "/api/status")
        self.assertEqual(status["service"], "running")
        self.assertEqual(status["bridgePort"], 34000)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.vault.calls, [])
        original = profile["prompt"]["id"]
        self.app.close()
        self.app = self.new_app()
        self.assertEqual(self.call("GET", "/api/profile")["profile"]["prompt"]["id"], original)

    def test_preview_is_complete_exact_persisted_and_sends_nothing(self):
        self.configure()
        preview = self.preview()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(preview["evidence"], self.market.value)
        self.assertEqual(preview["target"]["endpoint"], "https://synthetic.invalid/v1/chat/completions")
        self.assertEqual(preview["content_bytes"], len(encode_json(preview["request"])))
        self.assertIn("synthetic_fixture", preview["quality"])
        self.assertEqual(preview["request"]["model"], "synthetic-model")
        self.assertIn("candles", json.dumps(preview["request"]["messages"]))
        self.assertNotIn("synthetic-credential-only", json.dumps(preview))
        self.assertIsNone(preview["submission"])
        self.app.close()
        self.app = self.new_app()
        self.assertEqual(self.app.store.get("previews", preview["id"]), preview)

    def test_missing_model_or_key_blocks_analysis_without_consuming_preview(self):
        self.call("POST", "/api/settings", {"base_url": "https://synthetic.invalid/v1"})
        self.error("provider_not_configured", "POST", "/api/preview", {"source": "market", "question": "Review"})
        self.call("POST", "/api/settings", {"model": "synthetic-model"})
        preview = self.preview()
        self.error("key_required", "POST", "/api/analyze", {"preview_id": preview["id"]})
        self.assertIsNone(self.app.store.get("previews", preview["id"])["submission"])
        self.assertEqual(self.provider.calls, [])

    def test_changed_provider_invalidates_preview_but_profile_changes_do_not(self):
        self.configure()
        preview = self.preview()
        self.call("POST", "/api/settings", {"model": "different"})
        self.error("preview_invalidated", "POST", "/api/analyze", {"preview_id": preview["id"]})
        self.configure()
        self.call("POST", "/api/profile", {"system": {f: "new" for f in SYSTEM_FIELDS}, "prompt": "new prompt"})
        job = self.finish(self.call("POST", "/api/analyze", {"preview_id": preview["id"]}))
        report = self.call("GET", "/api/reports/" + job["result"]["report_id"])["report"]
        self.assertEqual(report["profile"], preview["profile"])
        self.assertEqual(report["evidence"], preview["evidence"])
        self.assertEqual(report["request"], preview["request"])
        self.assertEqual(report["execution_rules"], preview["execution_rules"])
        self.assertEqual(report["provider"]["requested_model"], "synthetic-model")
        self.assertIsNone(report["provider"]["actual_model"])
        self.assertEqual(report["provider"]["usage"], {"total_tokens": 12})
        self.assertNotIn("cost", report["provider"])

    def test_submission_is_one_use_even_after_failure_and_restart(self):
        self.configure()
        self.provider.error = AppError("provider_quota", "Do not leak private body", 402)
        preview = self.preview()
        job = self.finish(self.call("POST", "/api/analyze", {"preview_id": preview["id"]}))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["code"], "provider_quota")
        self.assertNotIn("private body", json.dumps(job))
        self.error("preview_consumed", "POST", "/api/analyze", {"preview_id": preview["id"]})
        self.app.close()
        self.app = self.new_app()
        self.error("preview_consumed", "POST", "/api/analyze", {"preview_id": preview["id"]})
        self.assertEqual(len(self.provider.calls), 1)

    def test_one_active_job_does_not_hold_status_or_capture_lock(self):
        self.configure()
        first = self.preview()
        self.provider.release.clear()
        response = self.call("POST", "/api/analyze", {"preview_id": first["id"]})
        self.assertTrue(self.provider.entered.wait(1))
        started = time.monotonic()
        status = self.call("GET", "/api/status")
        second = self.preview()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(status["active_job"]["id"], response["job"]["id"])
        self.error("provider_busy", "POST", "/api/analyze", {"preview_id": second["id"]})
        self.assertIsNone(self.app.store.get("previews", second["id"])["submission"])
        self.provider.release.set()
        self.assertEqual(self.finish(response)["state"], "succeeded")

    def test_settings_test_uses_only_fixed_synthetic_text_and_delete_key_is_explicit(self):
        self.configure()
        self.call("POST", "/api/profile", {"system": {f: "PRIVATE PROFILE" for f in SYSTEM_FIELDS}, "prompt": "PRIVATE PROMPT"})
        self.preview(question="PRIVATE QUESTION")
        job = self.finish(self.call("POST", "/api/settings/test"))
        self.assertEqual(job["state"], "succeeded")
        wire = json.dumps(self.provider.calls[0]["messages"])
        self.assertIn("synthetic", wire.lower())
        self.assertNotIn("PRIVATE", wire)
        self.assertNotIn("candles", wire)
        self.assertNotIn("text", job["result"])
        settings = self.call("POST", "/api/settings", {"delete_key": True})["settings"]
        self.assertFalse(settings["key_present"])
        self.assertNotIn("key", settings)
        self.error("key_required", "POST", "/api/settings/test")

    def test_trade_preview_preserves_historical_evidence_and_lists_trades(self):
        self.configure()
        self.assertEqual(self.call("GET", "/api/trades")["data"]["trades"][0]["trade_id"], "synthetic-trade")
        preview = self.preview(source="trade", trade_id="synthetic-trade")
        self.assertEqual(self.journal.captures, ["synthetic-trade"])
        self.assertTrue(preview["evidence"]["limits"]["historical_only"])
        self.assertEqual(self.market.captures, 0)

    def test_followups_keep_original_evidence_profile_and_all_prior_turns(self):
        report = self.report()
        self.market.value["evidence"]["candles"]["data"]["close"] = 999
        self.call("POST", "/api/profile", {"system": {f: "new" for f in SYSTEM_FIELDS}, "prompt": "new prompt"})
        for number in range(6):
            response = self.call("POST", "/api/reports/" + report["id"] + "/followup", {"question": "Follow up " + str(number)})
            self.assertEqual(self.finish(response)["state"], "succeeded")
        saved = self.call("GET", "/api/reports/" + report["id"])["report"]
        self.assertEqual(len(saved["followups"]), 6)
        self.assertEqual(saved["evidence"], report["evidence"])
        self.assertTrue(saved["followups"][-1]["fresh_capture_required_for_new_data"])
        self.assertEqual(self.market.captures, 1)
        last = self.provider.calls[-1]["messages"]
        self.assertEqual(last[:len(report["request"]["messages"])], report["request"]["messages"])
        self.assertIn("Follow up 0", json.dumps(last))
        self.assertNotIn("999", json.dumps(last))
        self.error("followup_limit", "POST", "/api/reports/" + report["id"] + "/followup", {"question": "seventh"})

    def test_citations_flag_unknown_and_missing_without_rendering_model_markup(self):
        self.provider.result = {"text": "<script>bad()</script> [E:invented]"}
        report = self.report()
        self.assertEqual(report["answer"]["text"], "<script>bad()</script> [E:invented]")
        self.assertEqual(report["answer"]["citations"]["unknown"], ["invented"])
        self.assertTrue(report["answer"]["citations"]["missing"])
        self.assertIn("unknown_evidence_citations", report["answer"]["warnings"])

    def proposal(self, report):
        self.provider.result = {"text": json.dumps({"system": {f: "proposed" for f in SYSTEM_FIELDS}, "prompt": "proposed prompt", "reason": "synthetic reason"})}
        job = self.finish(self.call("POST", "/api/reports/" + report["id"] + "/propose"))
        self.assertEqual(job["state"], "succeeded", job)
        return job["result"]["proposal"]

    def test_proposal_is_pending_and_rejection_does_not_change_versions(self):
        report = self.report()
        before = self.call("GET", "/api/profile")["profile"]
        proposal = self.proposal(report)
        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["old"]["system"], report["profile"]["system"]["data"])
        self.assertEqual(self.call("GET", "/api/profile")["profile"], before)
        rejected = self.call("POST", "/api/proposals/" + proposal["id"] + "/reject")["proposal"]
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(self.call("GET", "/api/profile")["profile"], before)
        self.error("proposal_resolved", "POST", "/api/proposals/" + proposal["id"] + "/accept", {"expected": proposal["expected"]})

    def test_accept_updates_both_versions_once_and_conflicts_if_original_changed(self):
        report = self.report()
        first = self.proposal(report)
        second = self.proposal(report)
        accepted = self.call("POST", "/api/proposals/" + first["id"] + "/accept", {"expected": first["expected"]})
        self.assertEqual(accepted["proposal"]["status"], "accepted")
        self.assertEqual(accepted["profile"]["prompt"]["data"], "proposed prompt")
        self.assertEqual(accepted["profile"]["system"]["data"]["name"], "proposed")
        self.error("version_conflict", "POST", "/api/proposals/" + second["id"] + "/accept", {"expected": second["expected"]})
        self.assertEqual(len(self.call("GET", "/api/profile")["profile"]["history"]), 2)

    def test_invalid_proposal_never_changes_profile(self):
        report = self.report()
        profile = self.call("GET", "/api/profile")["profile"]
        self.provider.result = {"text": '{"system":{},"prompt":123,"reason":"invalid"}'}
        job = self.finish(self.call("POST", "/api/reports/" + report["id"] + "/propose"))
        self.assertEqual(job["error"]["code"], "invalid_proposal")
        self.assertEqual(self.call("GET", "/api/profile")["profile"], profile)

    def test_demo_is_synthetic_deterministic_and_never_touches_sources_or_vault(self):
        first = self.call("POST", "/api/demo")["report"]
        second = self.call("POST", "/api/demo")["report"]
        self.assertTrue(first["demo"])
        self.assertTrue(first["evidence"]["demo"])
        self.assertTrue(first["provider"]["demo"])
        self.assertEqual(first["answer"], second["answer"])
        self.assertEqual(self.market.captures, 0)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.vault.calls, [])
        self.error("demo_read_only", "POST", "/api/reports/" + first["id"] + "/followup", {"question": "more"})
        self.error("demo_read_only", "POST", "/api/reports/" + first["id"] + "/propose")
        self.assertTrue(self.call("GET", "/api/reports")["reports"][0]["demo"])

    def test_exact_outbound_and_evidence_byte_limits_reject_without_truncation(self):
        self.configure()
        self.market.value["evidence"]["candles"]["data"] = {"value": "x" * 131072}
        self.error("evidence_size", "POST", "/api/preview", {"source": "market", "question": "Review"})
        self.market.value = evidence()
        self.market.value["evidence"]["candles"]["data"] = {"value": '"' * 60000}
        self.error("request_too_large", "POST", "/api/preview", {"source": "market", "question": "Review"})
        self.assertEqual(self.provider.calls, [])

    def test_legacy_journal_config_is_preserved_without_blocking_native_default(self):
        self.app.close()
        (self.directory / "journal.json").write_text('{"script_path":"/invalid"}')
        self.app = Workbench(self.directory, market=self.market, provider=self.provider, vault=self.vault)
        self.configure()
        self.assertIsNone(self.app._journal_error)
        self.assertEqual(self.app.journal.__class__.__name__, "NativeJournalSource")
        self.assertEqual(self.preview()["source"], "market")
        self.assertEqual((self.directory / "journal.json").read_text(), '{"script_path":"/invalid"}')
        self.error("invalid_settings", "POST", "/api/settings", {"script_path": "/unsafe"})

    def test_bridge_port_change_replaces_only_future_market_source(self):
        self.configure()
        preview = self.preview()
        replacement = FakeMarket()
        replacement.port = 34001
        with patch("companion.app.MarketSource", return_value=replacement) as factory:
            self.app.set_bridge_port(34001)
        factory.assert_called_once_with(port=34001)
        self.assertEqual(self.call("GET", "/api/status")["bridgePort"], 34001)
        self.assertEqual(self.app.store.get("previews", preview["id"]), preview)
        self.preview()
        self.assertEqual(replacement.captures, 1)

    def test_job_history_is_bounded_and_unknown_routes_fail_closed(self):
        self.configure()
        for _ in range(67):
            self.finish(self.call("POST", "/api/settings/test"))
        status = self.call("GET", "/api/status")
        self.assertLessEqual(status["job_history_count"], 64)
        self.assertEqual(status["job_queue_limit"], 0)
        self.error("not_found", "POST", "/api/orders", {})

    def test_server_uses_workbench_and_port_hook_preserves_runtime_metadata(self):
        from companion import server
        workbench = Mock()
        workbench.handle.return_value = {"ok": True, "test": True}
        workbench.set_bridge_port.side_effect = lambda port: None
        runtime_seen = []

        class FakeServer:
            server_address = ("127.0.0.1", 8770)
            auth = type("Auth", (), {"control_token": "synthetic-control"})()
            bridge_port = None

            def serve_forever(fake):
                try:
                    response = fake.handler("GET", "/api/profile", {})
                except AppError as exc:
                    response = {"code": exc.code}
                self.assertEqual(response, {"ok": True, "test": True})
                fake.on_change(34001)
                runtime_seen.append(json.loads((self.directory / "runtime.json").read_text()))

            def server_close(fake):
                pass

        def make_server(handler, directory, port, on_bridge_port_change):
            fake = FakeServer()
            fake.handler, fake.on_change = handler, on_bridge_port_change
            return fake

        with patch("companion.server.Workbench", return_value=workbench, create=True) as factory, \
                patch("companion.server.make_server", side_effect=make_server), \
                patch("companion.server.signal.signal"):
            server.serve(self.directory, 34000)
        self.assertEqual(factory.call_count, 1)
        self.assertIn(((34001,), {}), workbench.set_bridge_port.call_args_list)
        workbench.close.assert_called_once()
        self.assertEqual(runtime_seen[0]["bridge_port"], 34001)
        self.assertEqual(runtime_seen[0]["control_token"], "synthetic-control")

    def test_malformed_preview_identifier_is_a_safe_client_error(self):
        try:
            self.call("POST", "/api/analyze", {"preview_id": {"bad": "type"}})
        except Exception as exc:
            self.assertIsInstance(exc, AppError)
            self.assertEqual(exc.code, "invalid_preview_id")
        else:
            self.fail("Malformed preview ID was accepted")

    def test_legacy_null_config_is_retained_and_native_is_default(self):
        self.app.close()
        (self.directory / "journal.json").write_text("null")
        self.app = Workbench(self.directory, market=self.market, provider=self.provider, vault=self.vault)
        self.assertIsNone(self.app._journal_error)
        self.assertEqual(self.app.journal.__class__.__name__, "NativeJournalSource")
        self.assertEqual((self.directory / "journal.json").read_text(), "null")

    def test_followup_oversize_preserves_existing_history_and_does_not_call_provider(self):
        self.provider.result = {"text": "x" * 196608 + "[E:candles]"}
        report = self.report()
        self.error("request_too_large", "POST", "/api/reports/" + report["id"] + "/followup", {"question": "more"})
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.call("GET", "/api/reports/" + report["id"])["report"]["followups"], [])

    def test_failed_followup_keeps_history_and_key_echo_is_redacted(self):
        self.provider.result = {"text": "synthetic-credential-only [E:candles]"}
        report = self.report()
        self.assertNotIn("synthetic-credential-only", json.dumps(report))
        self.provider.error = RuntimeError("PRIVATE EXCEPTION synthetic-credential-only")
        job = self.finish(self.call("POST", "/api/reports/" + report["id"] + "/followup", {"question": "more"}))
        self.assertEqual(job["error"]["code"], "job_failed")
        self.assertNotIn("PRIVATE", json.dumps(job))
        self.assertEqual(self.call("GET", "/api/reports/" + report["id"])["report"]["followups"], [])

    def test_accept_requires_exact_expected_versions_and_never_accepts_implicitly(self):
        report = self.report()
        proposal = self.proposal(report)
        self.error("invalid_request", "POST", "/api/proposals/" + proposal["id"] + "/accept")
        self.error("version_conflict", "POST", "/api/proposals/" + proposal["id"] + "/accept", {"expected": {"system": None, "prompt": None}})
        self.assertEqual(self.call("GET", "/api/profile")["profile"]["system"]["id"], report["profile"]["system"]["id"])

    def test_pending_proposal_recovery_is_scoped_to_report_after_restart(self):
        report = self.report()
        proposal = self.proposal(report)
        for number in range(501):
            self.app.store.put("proposals", {"report_id": "another-report", "number": number})
        self.app.close()
        self.app = self.new_app()
        response = self.call("GET", "/api/reports/" + report["id"])
        self.assertEqual(response.get("proposals"), [proposal])
        self.assertEqual(response["proposal_history_limit"], 50)
        self.assertEqual(self.call("GET", "/api/proposals/" + proposal["id"])["proposal"], proposal)
        self.assertEqual(response["report"], report)

    def test_store_proposal_query_limits_apply_after_report_filter(self):
        query = getattr(self.app.store, "proposals_for_report", None)
        self.assertTrue(callable(query), "Store needs a report-scoped proposal query")
        first = self.app.store.put("proposals", {"report_id": "old", "position": 1})
        self.app.store.put("proposals", {"report_id": "other", "position": 2})
        self.assertEqual(query("old", limit=1), [first])
        copy_value = query("old")[0]
        copy_value["position"] = 999
        self.assertEqual(query("old")[0]["position"], 1)
        for limit in (0, 501, True):
            with self.assertRaises(AppError):
                query("old", limit=limit)

    def test_storage_mode_change_moves_existing_key_without_echo_or_provider_call(self):
        self.configure(persist_key=True)
        before = len(self.vault.calls)
        result = self.call("POST", "/api/settings", {"persist_key": False})
        self.assertIn(("set", "https://synthetic.invalid/v1", False), self.vault.calls[before:])
        self.assertTrue(result["settings"]["key_present"])
        self.assertFalse(result["settings"]["persist_key"])
        self.assertNotIn("synthetic-credential-only", json.dumps(result))
        self.assertNotIn(b"synthetic-credential-only", (self.directory / "workbench.sqlite3").read_bytes())
        self.assertEqual(self.provider.calls, [])

    def test_terminal_job_releases_active_slot_in_same_transition(self):
        # Pause exactly after the worker publishes terminal state to expose poll/submit races.
        self.configure()
        terminal_seen, release = threading.Event(), threading.Event()
        original_lock = self.app._lock
        jobs = self.app.jobs

        class ObserveLock:
            def __enter__(observed):
                return original_lock.__enter__()

            def __exit__(observed, *args):
                terminal = any(job["state"] in {"succeeded", "failed"} for job in jobs.history.values())
                original_lock.__exit__(*args)
                if threading.current_thread().name == "review-provider-job" and terminal and not terminal_seen.is_set():
                    terminal_seen.set()
                    release.wait(2)

        jobs.lock = ObserveLock()
        self.addCleanup(release.set)
        response = self.call("POST", "/api/settings/test")
        self.assertTrue(terminal_seen.wait(1))
        status = self.call("GET", "/api/status")
        job = self.call("GET", "/api/jobs/" + response["job"]["id"])["job"]
        self.assertIsNone(status["active_job"])
        self.assertIsNotNone(job["finished_at"])

    def test_provider_metadata_cannot_echo_key_in_a_json_field_name(self):
        self.provider.result = {"text": "Observed [E:candles]", "usage": {"synthetic-credential-only": 1}}
        report = self.report()
        self.assertNotIn("synthetic-credential-only", json.dumps(report))

    def test_concurrent_analysis_double_submit_dispatches_only_once(self):
        self.configure()
        preview = self.preview()
        self.provider.release.clear()
        outcomes = []
        barrier = threading.Barrier(3)

        def submit():
            barrier.wait()
            try:
                outcomes.append(self.call("POST", "/api/analyze", {"preview_id": preview["id"]}))
            except AppError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.provider.release.set()
        successful = [result for result in outcomes if isinstance(result, dict)]
        self.assertEqual(len(successful), 1)
        self.assertIn("preview_consumed", outcomes)
        self.finish(successful[0])
        self.assertEqual(len(self.provider.calls), 1)

    def test_escaped_credential_is_redacted_after_decoding_proposal_json(self):
        report = self.report()
        credential = "synthetic-credential-only"
        proposed = {"system": {field: credential for field in SYSTEM_FIELDS},
                    "prompt": credential, "reason": credential}
        escaped = "".join("\\u%04x" % ord(char) for char in credential)
        self.provider.result = {"text": json.dumps(proposed).replace(credential, escaped)}
        self.assertNotIn(credential, self.provider.result["text"])
        job = self.finish(self.call("POST", "/api/reports/" + report["id"] + "/propose"))
        self.assertEqual(job["state"], "succeeded", job)
        proposal_id = job["result"]["proposal_id"]
        reads = {
            "job": self.call("GET", "/api/jobs/" + job["id"]),
            "proposal": self.call("GET", "/api/proposals/" + proposal_id),
            "report": self.call("GET", "/api/reports/" + report["id"]),
        }
        for name, value in reads.items():
            with self.subTest(read=name):
                self.assertFalse(credential in json.dumps(value), "Decoded credential leaked into API data")
        with self.subTest(read="sqlite"):
            self.assertFalse(credential.encode() in (self.directory / "workbench.sqlite3").read_bytes(),
                             "Decoded credential leaked into SQLite")

    def test_explicit_key_mode_applies_to_reselected_endpoint_binding(self):
        from companion.credentials import CredentialVault

        class SyntheticKeychain:
            def __init__(keychain):
                keychain.values = {}

            def get(keychain, address):
                return keychain.values.get(address)

            def set(keychain, address, key):
                keychain.values[address] = key

            def delete(keychain, address):
                keychain.values.pop(address, None)

        keychain = SyntheticKeychain()
        self.app.vault = CredentialVault(keychain=keychain)
        address_a, address_b = "https://synthetic.invalid/a", "https://synthetic.invalid/b"
        self.configure(base_url=address_a, key="synthetic-a-credential", persist_key=True)
        self.configure(base_url=address_b, key="synthetic-b-credential", persist_key=False)
        self.assertIn(address_a, keychain.values)
        response = self.call("POST", "/api/settings", {"base_url": address_a, "persist_key": False})
        self.assertFalse(response["settings"]["persist_key"])
        self.assertTrue(response["settings"]["key_present"])
        self.assertNotIn(address_a, keychain.values)
        self.assertEqual(self.app.vault.get(address_a), "synthetic-a-credential")
        self.assertEqual(self.app.vault.get(address_b), "synthetic-b-credential")
        self.assertEqual(self.provider.calls, [])

    def test_worker_start_failure_still_closes_store_and_removes_runtime(self):
        from companion import server
        self.configure()
        preview = self.preview()
        observed = {}
        self.addCleanup(self.app.store.close)

        class FakeServer:
            server_address = ("127.0.0.1", 8770)
            auth = type("Auth", (), {"control_token": "synthetic-control"})()
            bridge_port = None

            def serve_forever(fake):
                with patch("companion.analysis.threading.Thread.start", side_effect=RuntimeError("synthetic failure")):
                    observed["job"] = self.call("POST", "/api/analyze", {"preview_id": preview["id"]})["job"]

            def server_close(fake):
                observed["server_closed"] = True

        failure = None
        with patch("companion.server.Workbench", return_value=self.app), \
                patch("companion.server.make_server", return_value=FakeServer()), \
                patch("companion.server.signal.signal"), \
                patch.object(self.app.store, "close", wraps=self.app.store.close) as close:
            try:
                server.serve(self.directory)
            except Exception as exc:
                failure = exc
            closed_count = close.call_count
        with self.subTest(check="clean_shutdown"):
            self.assertIsNone(failure)
        with self.subTest(check="store_closed"):
            self.assertEqual(closed_count, 1)
        with self.subTest(check="runtime_removed"):
            self.assertFalse((self.directory / "runtime.json").exists())
        self.assertTrue(observed["server_closed"])
        self.assertEqual(observed["job"]["state"], "failed")
        self.assertEqual(observed["job"]["error"]["code"], "job_failed")
        self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
