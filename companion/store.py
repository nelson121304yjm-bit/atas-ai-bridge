"""Private SQLite storage with immutable evidence and profile revisions."""

import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid

from .common import AppError, encode_json, utc_now


SYSTEM_FIELDS = ("name", "timeframe", "setup", "entry", "exit", "risk", "goal")
RECORD_METADATA = {
    "previews": {"submission"},
    "reports": {"followups"},
    "proposals": {"status", "resolution"},
}
SECRET_FIELDS = {"key", "apikey", "apitoken", "accesskey", "accesstoken",
                 "refreshtoken", "authtoken", "bearertoken", "secretkey",
                 "clientsecret", "privatekey", "token", "secret", "password",
                 "authorization", "credential", "credentials", "bearer"}
METADATA_LIMIT = {"previews": 131072, "proposals": 131072,
                  "reports": 14 * 1024 * 1024}
DEFAULT_SETTINGS = {"base_url": "", "model": "", "persist_key": False}


def _json_copy(value):
    try:
        return json.loads(encode_json(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AppError("invalid_json", "Value must be finite JSON data.") from exc


def _reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if (isinstance(key, str) and
                    "".join(char.lower() for char in key if char.isalnum()) in SECRET_FIELDS):
                raise AppError("secret_metadata", "Credential fields are not accepted in local metadata.")
            _reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secrets(item)


def _profile_from_row(row):
    if row is None:
        return {"system": {"id": None, "data": {field: "" for field in SYSTEM_FIELDS},
                           "created_at": None},
                "prompt": {"id": None, "data": "", "created_at": None}}
    return {"system": {"id": row[1], "data": json.loads(row[2]), "created_at": row[5]},
            "prompt": {"id": row[3], "data": row[4], "created_at": row[5]}}


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise AppError("invalid_directory", "State directory cannot be a symlink.")
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "workbench.sqlite3"
        if self.path.is_symlink():
            raise AppError("invalid_database", "Database cannot be a symlink.")
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                   timeout=5)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=DELETE")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                system_id TEXT NOT NULL, system_json TEXT NOT NULL,
                prompt_id TEXT NOT NULL, prompt_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                kind TEXT NOT NULL, id TEXT NOT NULL, created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
                PRIMARY KEY(kind,id)
            );
            CREATE INDEX IF NOT EXISTS records_by_kind_time ON records(kind,created_at DESC);
        """)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def settings(self) -> dict:
        with self._lock:
            row = self._db.execute("SELECT data FROM settings WHERE id=1").fetchone()
        return json.loads(row[0]) if row else DEFAULT_SETTINGS.copy()

    def save_settings(self, value: dict) -> None:
        value = _json_copy(value)
        if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
            raise AppError("invalid_settings", "Only base_url, model and persist_key settings are accepted.")
        _reject_secrets(value)
        if "base_url" in value:
            from .provider import normalize_base_url
            url = value["base_url"]
            if not isinstance(url, str) or len(url) > 2048:
                raise AppError("invalid_base_url", "Base URL must be at most 2048 characters.")
            value["base_url"] = normalize_base_url(url) if url else ""
        if "model" in value:
            model = value["model"]
            if (not isinstance(model, str) or len(model) > 128 or
                    any(char in model for char in "\r\n\x00")):
                raise AppError("invalid_model", "Model must be at most 128 characters.")
        if "persist_key" in value and not isinstance(value["persist_key"], bool):
            raise AppError("invalid_settings", "persist_key must be a boolean.")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT data FROM settings WHERE id=1").fetchone()
                merged = json.loads(row[0]) if row else DEFAULT_SETTINGS.copy()
                merged.update(value)
                self._db.execute("INSERT INTO settings(id,data) VALUES(1,?) "
                                 "ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                                 (encode_json(merged).decode(),))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def profile(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT * FROM profiles ORDER BY seq DESC").fetchall()
        current = _profile_from_row(rows[0] if rows else None)
        current["history"] = [_profile_from_row(row) for row in reversed(rows)]
        return current

    def save_profile(self, system: dict, prompt: str, expected: dict | None = None) -> dict:
        system = _json_copy(system)
        if not isinstance(system, dict) or set(system) != set(SYSTEM_FIELDS):
            raise AppError("invalid_system", "System profile must contain the seven supported fields.")
        _reject_secrets(system)
        if any(not isinstance(value, str) or len(value) > 4000 for value in system.values()):
            raise AppError("invalid_system", "System fields must be at most 4000 characters each.")
        if not isinstance(prompt, str) or len(prompt) > 12000:
            raise AppError("invalid_prompt", "Prompt must be at most 12000 characters.")
        if expected is not None and (not isinstance(expected, dict) or
                                     set(expected) != {"system", "prompt"} or
                                     any(item is not None and not isinstance(item, str)
                                         for item in expected.values())):
            raise AppError("invalid_expected", "Expected profile IDs are invalid.")
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT * FROM profiles ORDER BY seq DESC LIMIT 1").fetchone()
                current = _profile_from_row(row)
                if expected is not None and (expected["system"] != current["system"]["id"] or
                                             expected["prompt"] != current["prompt"]["id"]):
                    raise AppError("version_conflict", "Profile was changed by another request.", 409)
                self._db.execute("INSERT INTO profiles(system_id,system_json,prompt_id,prompt_text,created_at) "
                                 "VALUES(?,?,?,?,?)",
                                 (uuid.uuid4().hex, encode_json(system).decode(), uuid.uuid4().hex,
                                  prompt, utc_now()))
                rows = self._db.execute("SELECT * FROM profiles ORDER BY seq").fetchall()
                saved = _profile_from_row(rows[-1])
                saved["history"] = [_profile_from_row(item) for item in rows]
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return saved

    @staticmethod
    def _kind(kind: str) -> None:
        if kind not in RECORD_METADATA:
            raise AppError("invalid_kind", "Unknown record kind.")

    @staticmethod
    def _metadata(kind: str, value: dict) -> dict:
        metadata = {key: value.pop(key) for key in list(value) if key in RECORD_METADATA[kind]}
        for key, item in metadata.items():
            if key in ("submission", "resolution") and item is not None and not isinstance(item, dict):
                raise AppError("invalid_metadata", f"{key} must be an object.")
            if key == "followups" and (not isinstance(item, list) or len(item) > 100 or
                                       any(not isinstance(entry, dict) for entry in item)):
                raise AppError("invalid_metadata", "followups must be a list of at most 100 objects.")
            if key == "status" and (not isinstance(item, str) or len(item) > 64):
                raise AppError("invalid_metadata", "status must be at most 64 characters.")
        if len(encode_json(metadata)) > METADATA_LIMIT[kind]:
            raise AppError("metadata_too_large", "Record metadata exceeds its size limit.")
        return metadata

    @staticmethod
    def _record(row) -> dict:
        return {**json.loads(row["payload_json"]), **json.loads(row["metadata_json"]),
                "id": row["id"], "created_at": row["created_at"]}

    def put(self, kind: str, value: dict) -> dict:
        self._kind(kind)
        value = _json_copy(value)
        if not isinstance(value, dict) or {"id", "created_at"} & set(value):
            raise AppError("invalid_record", "Record must be an object without id or created_at.")
        _reject_secrets(value)
        metadata = self._metadata(kind, value)
        identifier, created_at = uuid.uuid4().hex, utc_now()
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                self._db.execute("INSERT INTO records(kind,id,created_at,payload_json,metadata_json) "
                                 "VALUES(?,?,?,?,?)",
                                 (kind, identifier, created_at, encode_json(value).decode(),
                                  encode_json(metadata).decode()))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {**value, **metadata, "id": identifier, "created_at": created_at}

    def get(self, kind: str, identifier: str) -> dict:
        self._kind(kind)
        with self._lock:
            row = self._db.execute("SELECT * FROM records WHERE kind=? AND id=?",
                                   (kind, identifier)).fetchone()
        if row is None:
            raise AppError("not_found", "Record was not found.", 404)
        return self._record(row)

    def list(self, kind: str, limit: int = 50) -> list[dict]:
        self._kind(kind)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise AppError("invalid_limit", "Limit must be between 1 and 500.")
        with self._lock:
            rows = self._db.execute("SELECT * FROM records WHERE kind=? "
                                    "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                                    (kind, limit)).fetchall()
        return [self._record(row) for row in rows]

    def proposals_for_report(self, report_id: str, limit: int = 50) -> "list[dict]":
        """Newest proposals for this report; filter before the bounded history limit."""
        if not isinstance(report_id, str) or not report_id:
            raise AppError("invalid_report_id", "Report ID is invalid.")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise AppError("invalid_limit", "Limit must be between 1 and 500.")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM records WHERE kind='proposals' "
                "AND json_extract(payload_json,'$.report_id')=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (report_id, limit)).fetchall()
        return [self._record(row) for row in rows]

    def update(self, kind: str, identifier: str, value: dict) -> dict:
        self._kind(kind)
        value = _json_copy(value)
        if not isinstance(value, dict) or not value or set(value) - RECORD_METADATA[kind]:
            raise AppError("immutable_record", "Only supported follow-up metadata can be updated.")
        _reject_secrets(value)
        patch = self._metadata(kind, value)
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute("SELECT * FROM records WHERE kind=? AND id=?",
                                       (kind, identifier)).fetchone()
                if row is None:
                    raise AppError("not_found", "Record was not found.", 404)
                metadata = json.loads(row["metadata_json"])
                metadata.update(patch)
                if len(encode_json(metadata)) > METADATA_LIMIT[kind]:
                    raise AppError("metadata_too_large", "Record metadata exceeds its size limit.")
                self._db.execute("UPDATE records SET metadata_json=? WHERE kind=? AND id=?",
                                 (encode_json(metadata).decode(), kind, identifier))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return {**json.loads(row["payload_json"]), **metadata,
                "id": row["id"], "created_at": row["created_at"]}
