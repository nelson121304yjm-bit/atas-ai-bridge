"""Inert answer validation and bounded asynchronous provider jobs."""

from collections import OrderedDict
import json
import re
import threading
import uuid

from .common import AppError, encode_json, utc_now
from .store import SYSTEM_FIELDS


JOB_HISTORY_LIMIT = 64
SAFE_JOB_ERRORS = {
    "provider_auth", "provider_quota", "provider_rate_limited", "provider_redirect",
    "provider_http_error", "provider_unavailable", "provider_status",
    "provider_response_too_large", "invalid_provider_response", "invalid_proposal",
    "metadata_too_large", "secret_metadata",
}


def detached(value):
    return json.loads(encode_json(value))


def answer(text, evidence):
    known = set(evidence["evidence"])
    references = sorted(set(re.findall(r"\[E:([^\]\r\n]*)\]", text)))
    valid = sorted(known.intersection(references))
    unknown = sorted(set(references) - known)
    warnings = []
    if unknown:
        warnings.append("unknown_evidence_citations")
    if not valid:
        warnings.append("missing_evidence_citations")
    return {"text": text, "format": "plain_text",
            "citations": {"valid": valid, "unknown": unknown, "missing": not bool(valid)},
            "warnings": warnings}


def parse_proposal(text):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError()
            result[name] = value
        return result

    def nonfinite(_):
        raise ValueError()

    try:
        value = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
        if not isinstance(value, dict) or set(value) != {"system", "prompt", "reason"}:
            raise ValueError()
        system = value["system"]
        if (not isinstance(system, dict) or set(system) != set(SYSTEM_FIELDS) or
                any(not isinstance(item, str) or len(item) > 4000 for item in system.values())):
            raise ValueError()
        if not isinstance(value["prompt"], str) or len(value["prompt"]) > 12000:
            raise ValueError()
        if not isinstance(value["reason"], str) or len(value["reason"]) > 4000:
            raise ValueError()
        return value
    except (ValueError, TypeError, RecursionError):
        raise AppError("invalid_proposal", "Provider proposal is not valid bounded profile JSON.", 502) from None


def provider_metadata(target, result, started_at, completed_at, demo=False):
    metadata = {"base_url": target["base_url"], "endpoint": target["endpoint"],
                "requested_model": target["model"], "actual_model": None,
                "started_at": started_at, "completed_at": completed_at, "demo": demo}
    # ProviderClient currently returns no model field. Preserve identity only if supplied.
    actual = result.get("model")
    if isinstance(actual, str) and 0 < len(actual) <= 256:
        metadata["actual_model"] = actual
    for field in ("usage", "request_id"):
        if field in result:
            metadata[field] = result[field]
    return metadata


class Jobs:
    """One active worker, zero waiting jobs, at most 64 pollable job records."""

    def __init__(self, lock):
        self.lock = lock
        self.history = OrderedDict()
        self.active = None
        self.thread = None
        self.closed = False

    def available(self):
        if self.closed:
            raise AppError("workbench_closed", "Workbench is shutting down.", 503)
        if self.active is not None:
            raise AppError("provider_busy", "A provider job is already active; no request was queued.", 409)

    def get(self, identifier):
        with self.lock:
            if identifier not in self.history:
                raise AppError("not_found", "Job is no longer in this process's bounded history.", 404)
            return detached(self.history[identifier])

    def submit(self, kind, operation, persist=None):
        # Caller holds the shared Workbench lock. Persist one-use consumption BEFORE dispatch.
        self.available()
        job = {"id": uuid.uuid4().hex, "kind": kind, "state": "queued", "created_at": utc_now(),
               "started_at": None, "finished_at": None, "result": None, "error": None}
        if persist is not None:
            persist(detached(job))
        self.history[job["id"]] = job
        self.active = job["id"]
        while len(self.history) > JOB_HISTORY_LIMIT:
            self.history.popitem(last=False)

        def run():
            with self.lock:
                job.update(state="running", started_at=utc_now())
            try:
                result = operation(job["started_at"])
                terminal = {"state": "succeeded", "result": result}
            except Exception as exc:
                code = exc.code if isinstance(exc, AppError) and exc.code in SAFE_JOB_ERRORS else "job_failed"
                terminal = {"state": "failed", "error": {"code": code,
                            "message": "Provider job failed. No automatic retry was made."}}
            with self.lock:
                job.update(**terminal, finished_at=utc_now())
                self.active = None

        self.thread = threading.Thread(target=run, daemon=True, name="review-provider-job")
        try:
            self.thread.start()
        except RuntimeError:
            self.thread = None
            job.update(state="failed", finished_at=utc_now(), error={
                "code": "job_failed", "message": "Provider job could not start. No automatic retry was made."})
            self.active = None
        return {"ok": True, "job": detached(job)}
