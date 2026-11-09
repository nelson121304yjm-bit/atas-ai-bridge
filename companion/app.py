"""Authenticated business routes for the local, read-only review workbench."""

import json
from pathlib import Path
import re
import threading
import uuid

from .analysis import Jobs, answer, detached, parse_proposal, provider_metadata
from .common import AppError, encode_json, utc_now
from .credentials import CredentialVault
from .evidence import MarketSource
from .journal import JournalSource
from .prompts import (DEFAULT_PROMPT, EXECUTION_RULES, MAX_EVIDENCE_BYTES, TEST_MESSAGES,
                      checked_request, conversation, initial_messages, proposal_messages)
from .provider import ProviderClient, _validate_key, normalize_base_url
from .store import Store


def _fields(value, allowed, required=(), code="invalid_request"):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise AppError(code, "Request fields are invalid.")


def _question(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise AppError("invalid_question", "Question must contain 1 to 4000 characters.")
    return value


def _versions(profile):
    return {"system": profile["system"]["id"], "prompt": profile["prompt"]["id"]}


def _profile_snapshot(profile):
    return {"system": profile["system"], "prompt": profile["prompt"]}


def _target(settings):
    if not settings["base_url"] or not settings["model"].strip():
        raise AppError("provider_not_configured", "Configure a provider base URL and model first.", 409)
    return {"base_url": settings["base_url"], "model": settings["model"],
            "endpoint": settings["base_url"] + "/chat/completions"}


class Workbench:
    def __init__(self, state_dir, market=None, journal=None, provider=None, vault=None):
        self._lock = threading.RLock()
        self.store = Store(Path(state_dir))
        self.market = market if market is not None else MarketSource()
        self.bridge_port = getattr(market, "port", None)
        self.provider = provider if provider is not None else ProviderClient()
        self.vault = vault if vault is not None else CredentialVault()
        self._journal_error = None
        self.journal = journal if journal is not None else self._load_journal(Path(state_dir))
        self.jobs = Jobs(self._lock)
        self._closed = False
        profile = self.store.profile()
        if profile["prompt"]["id"] is None:
            try:
                self.store.save_profile(profile["system"]["data"], DEFAULT_PROMPT, expected=_versions(profile))
            except AppError as exc:
                if exc.code != "version_conflict":
                    raise

    def _load_journal(self, directory):
        path = directory / "journal.json"
        if not path.exists():
            return JournalSource()
        try:
            if path.is_symlink():
                raise ValueError()
            with path.open("rb") as file:
                raw = file.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            config = json.loads(raw)
            if not isinstance(config, dict):
                raise ValueError()
            return JournalSource(config)
        except (AppError, OSError, ValueError, TypeError):
            self._journal_error = "journal_config"
            return JournalSource()

    def set_bridge_port(self, port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise AppError("invalid_bridge_port", "Bridge port is invalid.")
        replacement = MarketSource(port=port)
        with self._lock:
            self.market = replacement
            self.bridge_port = port

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.jobs.closed = True
            thread = self.jobs.thread
        if thread is not None:
            thread.join()
        self.store.close()

    def _settings(self):
        settings = self.store.settings()
        present = bool(settings["base_url"] and self.vault.has(settings["base_url"]))
        return {**settings, "key_present": present}

    def _save_settings(self, payload):
        _fields(payload, {"base_url", "model", "persist_key", "key", "delete_key"}, code="invalid_settings")
        with self._lock:
            current = self.store.settings()
            changed = {key: value for key, value in payload.items() if key in current}
            merged = {**current, **changed}
            url = merged["base_url"]
            if not isinstance(url, str):
                raise AppError("invalid_base_url", "Base URL is invalid.")
            merged["base_url"] = normalize_base_url(url) if url else ""
            model = merged["model"]
            if not isinstance(model, str) or len(model) > 128 or any(c in model for c in "\r\n\x00"):
                raise AppError("invalid_model", "Model must be at most 128 safe characters.")
            if type(merged["persist_key"]) is not bool or type(payload.get("delete_key", False)) is not bool:
                raise AppError("invalid_settings", "Key persistence and deletion must be boolean.")
            if "key" in payload and payload.get("delete_key"):
                raise AppError("invalid_settings", "Saving and deleting a key are separate actions.")
            if ("key" in payload or payload.get("delete_key")) and not merged["base_url"]:
                raise AppError("provider_not_configured", "Configure a provider base URL first.", 409)
            if "key" in payload:
                _validate_key(payload["key"])
                self.vault.set(merged["base_url"], payload["key"], persist=merged["persist_key"])
            elif payload.get("delete_key"):
                self.vault.delete(merged["base_url"])
            elif ("persist_key" in payload and merged["persist_key"] != current["persist_key"]
                  and merged["base_url"]):
                existing = self.vault.get(merged["base_url"])
                if existing:
                    self.vault.set(merged["base_url"], existing, persist=merged["persist_key"])
            self.store.save_settings(merged)
            return {"ok": True, "settings": self._settings()}

    def _status(self):
        with self._lock:
            market, journal_error = self.market, self._journal_error
            bridge_port = self.bridge_port
            job = self.jobs.get(self.jobs.active) if self.jobs.active else None
            count = len(self.jobs.history)
            settings = self.store.settings()
        return {"ok": True, "service": "running", "configured": bool(settings["base_url"] and settings["model"].strip()),
                "bridgePort": bridge_port, "market": market.status(),
                "journal": {"connected": False, "reason": journal_error} if journal_error else self.journal.status(),
                "active_job": job, "job_history_count": count, "job_history_limit": 64,
                "job_queue_limit": 0, "followup_limit": 6, "demo_available": True}

    def _preview(self, payload):
        _fields(payload, {"source", "trade_id", "question"}, {"source", "question"})
        question = _question(payload["question"])
        source = payload["source"]
        if source not in ("market", "trade") or (source == "market" and "trade_id" in payload):
            raise AppError("invalid_source", "Preview source must be market or a selected trade.")
        if source == "trade" and (not isinstance(payload.get("trade_id"), str) or not payload["trade_id"]):
            raise AppError("invalid_trade_id", "Select one historical trade.")
        with self._lock:
            target = _target(self.store.settings())
            profile = _profile_snapshot(self.store.profile())
            market = self.market
        if source == "trade":
            self._require_journal()
            evidence = self.journal.capture(payload["trade_id"])
        else:
            evidence = market.capture()
        evidence = detached(evidence)
        if len(encode_json(evidence)) > MAX_EVIDENCE_BYTES:
            raise AppError("evidence_size", "Evidence exceeds 131072 bytes; no content was sent.", 413)
        request, size = checked_request(target["model"], initial_messages(profile, evidence, question))
        preview = {"source": source, "question": question, "demo": False, "target": target,
                   "profile": profile, "execution_rules": EXECUTION_RULES, "evidence": evidence,
                   "quality": evidence["quality"], "request": request, "content_bytes": size, "submission": None}
        return {"ok": True, "preview": self.store.put("previews", preview)}

    def _credentials(self, target):
        self.jobs.available()
        key = self.vault.get(target["base_url"])
        if not key:
            raise AppError("key_required", "A provider key is required before sending this request.", 409)
        return key

    def _complete(self, target, request, key, started_at):
        result = self.provider.complete(target["base_url"], target["model"], key, request["messages"])
        # Keys must not enter report strings even if a nonconforming provider echoes one.
        def redact(value):
            if isinstance(value, str):
                return value.replace(key, "[credential redacted]")
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {redact(name): redact(item) for name, item in value.items()}
            return value
        result = redact(result)
        if not isinstance(result, dict) or not isinstance(result.get("text"), str):
            raise AppError("invalid_provider_response", "Provider response is invalid.", 502)
        return result, provider_metadata(target, result, started_at, utc_now())

    def _analyze(self, payload):
        _fields(payload, {"preview_id"}, {"preview_id"})
        if not isinstance(payload["preview_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", payload["preview_id"]):
            raise AppError("invalid_preview_id", "Preview ID is invalid.")
        with self._lock:
            preview = self.store.get("previews", payload["preview_id"])
            if preview["submission"] is not None:
                raise AppError("preview_consumed", "This preview was already submitted; create a new preview to send again.", 409)
            target = _target(self.store.settings())
            if target != preview["target"]:
                raise AppError("preview_invalidated", "Provider destination or model changed; create a new preview.", 409)
            key = self._credentials(target)

            def consume(job):
                self.store.update("previews", preview["id"], {"submission": {
                    "job_id": job["id"], "submitted_at": job["created_at"], "consumed": True}})

            def operation(started_at):
                result, metadata = self._complete(target, preview["request"], key, started_at)
                report = self.store.put("reports", {
                    **{name: preview[name] for name in ("source", "question", "demo", "profile", "execution_rules",
                                                       "evidence", "quality", "target", "request", "content_bytes")},
                    "preview_id": preview["id"], "provider": metadata,
                    "answer": answer(result["text"], preview["evidence"]), "followups": []})
                return {"report_id": report["id"]}

            return self.jobs.submit("analysis", operation, persist=consume)

    def _test(self, payload):
        _fields(payload, set())
        with self._lock:
            target = _target(self.store.settings())
            key = self._credentials(target)
            request, _ = checked_request(target["model"], detached(TEST_MESSAGES))

            def operation(started_at):
                _, metadata = self._complete(target, request, key, started_at)
                return {"synthetic": True, "provider": metadata}
            return self.jobs.submit("settings_test", operation)

    def _report(self, identifier):
        return self.store.get("reports", identifier)

    def _continuation(self, identifier, payload, propose=False):
        _fields(payload, set() if propose else {"question"}, () if propose else {"question"})
        question = None if propose else _question(payload["question"])
        with self._lock:
            report = self._report(identifier)
            if report["demo"]:
                raise AppError("demo_read_only", "Demo reports are synthetic read-only examples.", 409)
            if not propose and len(report["followups"]) >= 6:
                raise AppError("followup_limit", "This report has reached six follow-up turns.", 409)
            target = _target(self.store.settings())
            if target != report["target"]:
                raise AppError("provider_changed", "Restore this report's provider destination and requested model to continue.", 409)
            messages = proposal_messages(report) if propose else conversation(report) + [{"role": "user", "content": question}]
            request, size = checked_request(target["model"], messages)
            key = self._credentials(target)

            def operation(started_at):
                result, metadata = self._complete(target, request, key, started_at)
                with self._lock:
                    if propose:
                        proposed = parse_proposal(result["text"])
                        proposal = self.store.put("proposals", {
                            "report_id": identifier, "demo": False, "expected": _versions(report["profile"]),
                            "old": {"system": report["profile"]["system"]["data"], "prompt": report["profile"]["prompt"]["data"]},
                            "new": {"system": proposed["system"], "prompt": proposed["prompt"]},
                            "reason": proposed["reason"], "provider": metadata, "content_bytes": size,
                            "status": "pending", "resolution": None})
                        return {"proposal_id": proposal["id"], "proposal": proposal}
                    current = self._report(identifier)
                    turn = {"id": uuid.uuid4().hex, "question": question, "created_at": utc_now(), "demo": False,
                            "answer": answer(result["text"], report["evidence"]), "provider": metadata,
                            "content_bytes": size, "fresh_capture_required_for_new_data": True}
                    self.store.update("reports", identifier, {"followups": current["followups"] + [turn]})
                    return {"report_id": identifier, "followup_id": turn["id"]}
            return self.jobs.submit("proposal" if propose else "followup", operation)

    def _resolve(self, identifier, payload, accept):
        _fields(payload, {"expected"} if accept else set(), {"expected"} if accept else ())
        with self._lock:
            proposal = self.store.get("proposals", identifier)
            if proposal["status"] != "pending":
                raise AppError("proposal_resolved", "This proposal has already been resolved.", 409)
            if accept:
                if payload["expected"] != proposal["expected"]:
                    raise AppError("version_conflict", "Expected original profile versions do not match this proposal.", 409)
                profile = self.store.save_profile(proposal["new"]["system"], proposal["new"]["prompt"], expected=proposal["expected"])
                resolution = {"resolved_at": utc_now(), "versions": _versions(profile)}
            else:
                profile = None
                resolution = {"resolved_at": utc_now()}
            updated = self.store.update("proposals", identifier, {"status": "accepted" if accept else "rejected", "resolution": resolution})
            response = {"ok": True, "proposal": updated}
            if profile is not None:
                response["profile"] = profile
            return response

    def _demo(self, payload):
        _fields(payload, set())
        timestamp = "2000-01-01T00:00:00+00:00"
        evidence = {"schema_version": 1, "source": "demo", "demo": True, "captured_at": timestamp,
                    "identity": {"label": "SYNTHETIC DEMO", "simulation_only": True},
                    "quality": ["synthetic_demo_not_market_data"],
                    "evidence": {"demo_candles": {"retrieved_at": timestamp, "data": [{"close": 100}, {"close": 101}]}},
                    "limits": {"synthetic_only": True, "current_flatness_verified": False}}
        profile = _profile_snapshot(self.store.profile())
        target = {"base_url": "", "endpoint": "", "model": "deterministic-synthetic-example"}
        request, size = checked_request(target["model"], initial_messages(profile, evidence, "Explain the synthetic example."))
        report = self.store.put("reports", {
            "source": "demo", "demo": True, "question": "Explain the synthetic example.", "preview_id": None,
            "profile": profile, "execution_rules": EXECUTION_RULES, "evidence": evidence, "quality": evidence["quality"],
            "target": target, "request": request, "content_bytes": size,
            "provider": provider_metadata(target, {}, None, None, demo=True),
            "answer": answer("SYNTHETIC DEMO: example closes are 100 and 101 [E:demo_candles]. "
                             "These are invented teaching values, not market data. Fees, risk, R and psychology are unknown.", evidence),
            "followups": []})
        return {"ok": True, "report": report}

    def _require_journal(self):
        if self._journal_error:
            raise AppError("journal_config", "Optional journal installation configuration is invalid.", 503)

    def handle(self, method, path, payload):
        if not isinstance(payload, dict):
            raise AppError("invalid_request", "Request must be an object.")
        if method == "GET":
            if path == "/api/status":
                return self._status()
            if path == "/api/settings":
                with self._lock:
                    return {"ok": True, "settings": self._settings()}
            if path == "/api/profile":
                return {"ok": True, "profile": self.store.profile()}
            if path == "/api/trades":
                self._require_journal()
                return {"ok": True, "data": self.journal.list_trades()}
            if path == "/api/reports":
                reports = [{name: item[name] for name in ("id", "created_at", "source", "question", "demo", "provider")}
                           for item in self.store.list("reports")]
                return {"ok": True, "reports": reports}
            matched = re.fullmatch(r"/api/(reports|jobs|proposals)/([a-f0-9]{32})", path)
            if matched:
                kind, identifier = matched.groups()
                if kind == "reports":
                    with self._lock:
                        return {"ok": True, "report": self._report(identifier),
                                "proposals": self.store.proposals_for_report(identifier), "proposal_history_limit": 50}
                if kind == "proposals":
                    return {"ok": True, "proposal": self.store.get("proposals", identifier)}
                return {"ok": True, "job": self.jobs.get(identifier)}
        if method == "POST":
            routes = {"/api/settings": self._save_settings, "/api/settings/test": self._test,
                      "/api/preview": self._preview, "/api/analyze": self._analyze, "/api/demo": self._demo}
            if path in routes:
                return routes[path](payload)
            if path == "/api/profile":
                _fields(payload, {"system", "prompt", "expected"}, {"system", "prompt"})
                with self._lock:
                    return {"ok": True, "profile": self.store.save_profile(payload["system"], payload["prompt"], payload.get("expected"))}
            matched = re.fullmatch(r"/api/reports/([a-f0-9]{32})/(followup|propose)", path)
            if matched:
                return self._continuation(matched[1], payload, propose=matched[2] == "propose")
            matched = re.fullmatch(r"/api/proposals/([a-f0-9]{32})/(accept|reject)", path)
            if matched:
                return self._resolve(matched[1], payload, accept=matched[2] == "accept")
        raise AppError("not_found", "Unknown workbench route.", 404)
