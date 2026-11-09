"""Versioned private storage and nonsecret metadata."""

from pathlib import Path
import tempfile
import unittest

from companion.common import AppError
from companion.store import Store


SYSTEM = {name: "" for name in (
    "name", "timeframe", "setup", "entry", "exit", "risk", "goal")}


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "private"
        self.store = Store(self.directory)
        self.addCleanup(self.store.close)

    def test_profile_versions_are_immutable_and_conflicts_are_detected(self):
        first = self.store.save_profile({**SYSTEM, "name": "First"}, "prompt one")
        second = self.store.save_profile({**SYSTEM, "name": "Second"}, "prompt two", {
            "system": first["system"]["id"], "prompt": first["prompt"]["id"]})
        self.assertNotEqual(first["system"]["id"], second["system"]["id"])
        self.assertNotEqual(first["prompt"]["id"], second["prompt"]["id"])
        self.assertEqual(self.store.profile()["system"]["data"]["name"], "Second")
        self.assertEqual(self.store.profile()["history"][0]["system"]["data"]["name"], "First")
        with self.assertRaises(AppError) as caught:
            self.store.save_profile(SYSTEM, "stale", {
                "system": first["system"]["id"], "prompt": first["prompt"]["id"]})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store.profile()["history"]), 2)

    def test_save_profile_returns_own_revision_when_another_writer_commits_afterward(self):
        competitor = Store(self.directory)
        self.addCleanup(competitor.close)
        connection = self.store._db
        interleaved = []

        class CommitInterleaver:
            def execute(self, statement, *arguments):
                result = connection.execute(statement, *arguments)
                if statement == "COMMIT":
                    interleaved.append(competitor.save_profile({**SYSTEM, "name": "Competing"},
                                                              "other prompt"))
                return result

            def close(self):
                connection.close()

        self.store._db = CommitInterleaver()
        saved = self.store.save_profile({**SYSTEM, "name": "Mine"}, "my prompt")
        self.assertEqual(len(interleaved), 1)
        self.assertEqual(saved["system"]["data"]["name"], "Mine")
        self.assertEqual(saved["prompt"]["data"], "my prompt")
        self.assertNotEqual(saved["system"]["id"], interleaved[0]["system"]["id"])
        self.assertEqual(competitor.profile()["system"]["data"]["name"], "Competing")

    def test_profile_and_records_return_detached_json(self):
        profile = self.store.save_profile(SYSTEM, "original")
        profile["system"]["data"]["name"] = "mutated"
        self.assertEqual(self.store.profile()["system"]["data"]["name"], "")
        source = {"evidence": {"bar": [1]}, "text": "result"}
        saved = self.store.put("reports", source)
        source["evidence"]["bar"].append(2)
        saved["evidence"]["bar"].append(3)
        self.assertEqual(self.store.get("reports", saved["id"])["evidence"], {"bar": [1]})

    def test_report_update_can_add_followups_but_not_rewrite_evidence(self):
        saved = self.store.put("reports", {"evidence": {"id": "snapshot"},
                                            "system_version": "s1", "prompt_version": "p1",
                                            "text": "original"})
        changed = self.store.update("reports", saved["id"], {"followups": [{"text": "reviewed"}]})
        self.assertEqual(changed["followups"], [{"text": "reviewed"}])
        self.assertEqual(changed["evidence"], {"id": "snapshot"})
        with self.assertRaises(AppError):
            self.store.update("reports", saved["id"], {"evidence": {"id": "replacement"}})
        self.assertEqual(self.store.get("reports", saved["id"])["text"], "original")
        self.assertEqual(self.store.list("reports")[0]["id"], saved["id"])

    def test_report_metadata_fits_six_bounded_answers_and_is_still_bounded(self):
        saved = self.store.put("reports", {"evidence": {"id": "immutable"}})
        followup = {"question": "q" * 12000, "text": "a" * (2 * 1024 * 1024 - 100)}
        six = [followup for _ in range(6)]
        result = self.store.update("reports", saved["id"], {"followups": six})
        self.assertEqual(len(result["followups"]), 6)
        self.assertEqual(result["evidence"], {"id": "immutable"})
        with self.assertRaises(AppError) as caught:
            self.store.update("reports", saved["id"], {"followups": six + [followup]})
        self.assertEqual(caught.exception.code, "metadata_too_large")
        self.assertEqual(len(self.store.get("reports", saved["id"])["followups"]), 6)

    def test_preview_submission_and_proposal_resolution_are_mutable_metadata(self):
        preview = self.store.put("previews", {"evidence": {"id": "snap"}})
        self.assertEqual(self.store.update("previews", preview["id"], {
            "submission": {"status": "consumed", "job_id": "j1"}})["submission"]["job_id"], "j1")
        proposal = self.store.put("proposals", {"draft": {"name": "Idea"}})
        changed = self.store.update("proposals", proposal["id"], {
            "status": "accepted", "resolution": {"system_version": "s1"}})
        self.assertEqual(changed["status"], "accepted")
        self.assertEqual(changed["resolution"]["system_version"], "s1")
        for kind, item, value in (("previews", preview, {"evidence": {"id": "new"}}),
                                  ("reports", self.store.put("reports", {"text": "fixed"}),
                                   {"text": "changed"}),
                                  ("proposals", proposal, {"draft": {"name": "Other"}})):
            with self.subTest(kind=kind), self.assertRaises(AppError):
                self.store.update(kind, item["id"], value)

    def test_settings_are_allowlisted_and_secret_fields_are_rejected(self):
        self.store.save_settings({"base_url": "https://api.example/v1", "model": "m", "persist_key": False})
        self.assertEqual(self.store.settings(), {
            "base_url": "https://api.example/v1", "model": "m", "persist_key": False})
        for value in ({"api_key": "secret"}, {"key": "secret"},
                      {"model": "m", "extra": "x"}, {"persist_key": "false"},
                      {"model": "bad\nname"}):
            with self.subTest(value=value), self.assertRaises(AppError):
                self.store.save_settings(value)
        self.assertEqual(self.store.settings()["model"], "m")
        for value in ({"metadata": {"access_token": "secret"}}, {"authorization": "secret"}):
            with self.subTest(value=value), self.assertRaises(AppError):
                self.store.put("reports", value)

    def test_secret_field_variants_are_rejected_without_blocking_usage_counts(self):
        for name in ("accessToken", "secretKey", "api-key", "API_Key", "clientSecret",
                     "refreshToken", "authToken"):
            with self.subTest(name=name), self.assertRaises(AppError) as caught:
                self.store.put("reports", {"metadata": {name: "synthetic-secret"}})
            self.assertEqual(caught.exception.code, "secret_metadata")
        saved = self.store.put("reports", {"usage": {"prompt_tokens": 3,
                                                       "completion_tokens": 5,
                                                       "total_tokens": 8}})
        self.assertEqual(saved["usage"]["total_tokens"], 8)

    def test_validation_and_failed_write_do_not_change_profile(self):
        before = self.store.save_profile(SYSTEM, "valid")
        for system, prompt in (({**SYSTEM, "name": "x" * 4001}, "ok"),
                               ({**SYSTEM, "unknown": "x"}, "ok"),
                               (SYSTEM, "x" * 12001)):
            with self.subTest(system=system, prompt_length=len(prompt)), self.assertRaises(AppError):
                self.store.save_profile(system, prompt)
        self.assertEqual(self.store.profile(), before)
        with self.assertRaises(AppError):
            self.store.put("other", {"text": "bad kind"})
        with self.assertRaises(AppError):
            self.store.put("reports", {"number": float("nan")})

    def test_directory_and_database_permissions_are_restrictive(self):
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / "workbench.sqlite3").stat().st_mode & 0o777, 0o600)
        self.store.put("previews", {"text": "ok"})
        for file in self.directory.iterdir():
            if file.is_file():
                self.assertEqual(file.stat().st_mode & 0o777, 0o600, file.name)

    def test_reopen_preserves_versions_and_records(self):
        self.store.save_profile(SYSTEM, "version one")
        report = self.store.put("reports", {"text": "original"})
        reopened = Store(self.directory)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.profile()["prompt"]["data"], "version one")
        self.assertEqual(reopened.get("reports", report["id"])["text"], "original")
        preview = self.store.put("previews", {"evidence": {"id": "snap"}})
        self.store.update("previews", preview["id"], {
            "submission": {"status": "consumed", "job_id": "j1"}})
        self.assertEqual(reopened.get("previews", preview["id"])["submission"]["job_id"], "j1")


if __name__ == "__main__":
    unittest.main()
