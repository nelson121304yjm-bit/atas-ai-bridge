"""Strict, read-only local ATAS CSV snapshots; independent of charts and bridge ports."""
from __future__ import annotations

import copy
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import io
import os
from pathlib import Path
import re
import secrets
import stat
import sys

from .common import AppError, encode_json, utc_now
from .evidence import MAX_EVIDENCE, SECURITY_ID
from .native_journal import FACTS, NativeJournalSource, QUALITY, decimal_string, text

SOURCE = "atas_native_persisted_journal"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ROWS = 10000
SCHEMAS = {
    "Portfolio": ("1", "AccountID,Currency,Balance,BlockedMargin,Leverage,BalancePower,OpenPnL,ClosedPnL,TotalClosedPnL,MaxEquityValue,IsLocked,IsSuspended,TradingOptionsId,CommissionState,ClosedPnlDate,IsRealAccount,UserId,ProcessedTradeTime".split(",")),
    "HistoryMyTrade": ("2", "Id,AccountID,SecurityId,OpenTime,OpenPrice,OpenVolume,CloseTime,ClosePrice,CloseVolume,PnL,TicksPnL,PricePnL,Commission,Comment,Reviewed,EnterTradeId,ExitTradeId,MissingDataCase".split(",")),
    "MyTrade": ("1", "Route,AccountID,SecurityId,Id,OrderId,OrderDirection,Price,Time,Volume,OpenVolume,Commission,CommissionCurrency".split(",")),
}
FILE_QUALITY = [*QUALITY, "local_persisted_snapshot_not_server_history_or_live_account_state"]


def failure(code="native_files_invalid", message="ATAS native files are invalid or unsupported; refresh after ATAS finishes saving.", status=409):
    return AppError(code, message, status)


def default_database_dir():
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ATAS" / "Database"
    base = os.environ.get("APPDATA")
    return Path(base) / "ATAS" / "Database" if base and os.path.isabs(base) else None


def parse_container(raw, entity):
    """Parse only the observed header/schema, bounded rows and one terminal EndData."""
    version, columns = SCHEMAS[entity]
    if len(raw) > MAX_FILE_BYTES:
        raise failure("native_files_size", "ATAS native file exceeds 8 MiB.", 413)
    try:
        if raw.rstrip(b"\r\n").rsplit(b"\n", 1)[-1] != b"EndData":
            raise ValueError()
        stream = io.StringIO(raw.decode("utf-8"), newline="")
        if stream.readline().rstrip("\r\n") != f'Version: "{version}"':
            raise ValueError()
        if stream.readline().rstrip("\r\n") != f'Entity: "{entity}"':
            raise ValueError()
        header = stream.readline().rstrip("\r\n")
        if not header.startswith("Columns: ") or next(csv.reader([header[9:]], strict=True, skipinitialspace=True)) != columns:
            raise ValueError()
        if stream.readline().rstrip("\r\n") != "StartData":
            raise ValueError()
        rows = []
        for values in csv.reader(stream, strict=True, skipinitialspace=True):
            if values == ["EndData"]:
                if stream.read() != "":
                    raise ValueError()
                return rows
            if len(values) != len(columns) or len(rows) >= MAX_ROWS:
                raise ValueError()
            if any(len(value) > 4096 or any(ord(c) < 32 for c in value) for value in values):
                raise ValueError()
            rows.append(dict(zip(columns, values)))
        raise ValueError()
    except (ValueError, UnicodeError, csv.Error, StopIteration):
        raise failure() from None


def wall_time(value):
    if not value:
        return {"iso": None, "kind": "Unspecified"}
    if not re.fullmatch(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}", value):
        raise failure()
    try:
        result = datetime.strptime(value, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        raise failure() from None
    return {"iso": result.isoformat(), "kind": "Unspecified"}


def number(value, *, quantity=False, nullable=False):
    try:
        result = decimal_string(None if nullable and value == "" else value, nullable=nullable)
    except AppError:
        raise failure() from None
    if quantity and Decimal(result) < 0:
        raise failure()
    return result


def signature(value):
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


class NativeFileJournalSource(NativeJournalSource):
    def __init__(self, directory=None, *, clock=None):
        super().__init__(None)
        self.directory = Path(directory) if directory is not None else default_database_dir()
        self._clock = clock or datetime.now
        self._salt = secrets.token_bytes(32)
        self._selected = None
        self._explicit = False
        self._selected_origin = None
        self._available_options = {}
        self._requires_selection = False

    def _key(self, value):
        return hmac.new(self._salt, encode_json(value), hashlib.sha256).hexdigest()

    def _snapshot(self):
        self._alive()
        if self.directory is None:
            raise failure("native_files_missing", "Set up the standard local ATAS Database directory; no bridge or indicator is required.", 503)
        paths = {entity: self.directory / (entity + ".cdb") for entity in SCHEMAS}
        for attempt in range(2):
            try:
                directory_before = self.directory.stat()
                before = {entity: path.lstat() for entity, path in paths.items()}
                if any(not stat.S_ISREG(value.st_mode) for value in before.values()):
                    raise failure()
                if any(value.st_size > MAX_FILE_BYTES for value in before.values()):
                    raise failure("native_files_size", "ATAS native file exceeds 8 MiB.", 413)
                raw, changed = {}, False
                for entity, path in paths.items():
                    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(descriptor, "rb") as handle:
                        changed |= signature(os.fstat(handle.fileno())) != signature(before[entity])
                        raw[entity] = handle.read(MAX_FILE_BYTES + 1)
                        changed |= signature(os.fstat(handle.fileno())) != signature(before[entity])
                after = {entity: path.lstat() for entity, path in paths.items()}
                directory_after = self.directory.stat()
                changed |= (directory_before.st_dev, directory_before.st_ino) != (directory_after.st_dev, directory_after.st_ino)
                changed |= any(signature(before[key]) != signature(after[key]) for key in paths)
                if changed:
                    if attempt == 0:
                        continue
                    raise failure("native_files_changed", "ATAS native files changed during both reads; refresh after saving completes.")
                tables = {entity: parse_container(raw[entity], entity) for entity in paths}
                modified = {key: datetime.fromtimestamp(value.st_mtime, timezone.utc).isoformat() for key, value in after.items()}
                origin = {"directory": str(self.directory.resolve()), "device": directory_after.st_dev, "inode": directory_after.st_ino}
                return tables, modified, origin
            except OSError:
                raise failure("native_files_missing", "The three local ATAS native journal files are unavailable; check the standard Database directory and refresh. No indicator is required.", 503) from None
        raise failure("native_files_changed")

    def _accounts(self, tables, origin):
        portfolios = {}
        for row in tables["Portfolio"]:
            try:
                account = text(row["AccountID"])
            except AppError:
                raise failure() from None
            if row["IsRealAccount"] not in ("False", "True") or account in portfolios:
                raise failure()
            portfolios[account] = row["IsRealAccount"] == "False"
        eligible = sorted({row["AccountID"] for row in tables["HistoryMyTrade"] + tables["MyTrade"]
                           if row["SecurityId"] == SECURITY_ID and portfolios.get(row["AccountID"]) is True})
        options = {self._key({"origin": origin, "account": account}): account for account in eligible}
        self._available_options = options
        if self._selected is not None:
            if self._selected not in eligible or self._selected_origin != origin:
                self._requires_selection = True
                raise failure("native_account_changed", "The selected simulated account or source changed; select it again locally.")
            if len(eligible) > 1 and not self._explicit:
                self._selected = None
                self._requires_selection = True
        if self._selected is None and len(eligible) == 1 and not self._requires_selection:
            self._selected = eligible[0]
            self._selected_origin = origin
        return options

    @staticmethod
    def _options(options):
        return [{"key": key, "label": f"模拟账户 {index + 1} · BTCUSDT"} for index, key in enumerate(options)]

    def select_account(self, key):
        with self._lock:
            self._selection.clear()
            self._selected = None
            self._selected_origin = None
            self._explicit = False
            self._requires_selection = True
            tables, _, origin = self._snapshot()
            options = self._accounts(tables, origin)
            if not isinstance(key, str) or key not in options:
                self._selected = None
                raise failure("native_account_selection", "Refresh the local account choices and explicitly select one.")
            self._selected = options[key]
            self._selected_origin = origin
            self._explicit = True
            self._requires_selection = False
            return self.status()

    def status(self):
        with self._lock:
            try:
                tables, _, origin = self._snapshot()
                options = self._accounts(tables, origin)
                if not options:
                    raise failure("native_accounts_empty", "No account is explicitly marked simulated with BTCUSDT native journal records.")
                if self._selected is None:
                    self._selection.clear()
                    return {"connected": False, "source": SOURCE, "reason": "native_account_selection_required",
                            "selection_required": True, "account_options": self._options(options)}
                return {"connected": True, "source": SOURCE, "simulation_only": True,
                        "origin_key": self._key({**origin, "account": self._selected}),
                        "account_options": self._options(options),
                        "selected_account_key": self._key({"origin": origin, "account": self._selected})}
            except AppError as exc:
                self._selection.clear()
                return {"connected": False, "source": SOURCE, "reason": exc.code,
                        "selection_required": exc.code == "native_account_changed",
                        "account_options": self._options(self._available_options) if exc.code == "native_account_changed" else []}

    def _read(self):
        tables, modified, origin = self._snapshot()
        options = self._accounts(tables, origin)
        if not options:
            raise failure("native_accounts_empty", "No explicitly simulated BTCUSDT native journal account exists.")
        if self._selected is None:
            raise failure("native_account_selection_required", "Choose a simulated account in the authenticated local workbench first.")
        origin = {**origin, "account": self._selected}
        fills = {}
        for fill in tables["MyTrade"]:
            key = (text(fill["AccountID"]), text(fill["SecurityId"]), text(fill["Id"]))
            if key in fills:
                raise failure()
            for name in ("Price", "Volume", "OpenVolume"):
                number(fill[name], quantity=True)
            number(fill["Commission"], nullable=True)
            wall_time(fill["Time"])
            if fill["OrderDirection"] not in ("Buy", "Sell", ""):
                raise failure()
            fills[key] = fill
        finish = self._clock()
        if not isinstance(finish, datetime) or finish.tzinfo is not None:
            raise failure()
        begin = finish - timedelta(days=7)
        rows, seen = [], set()
        for value in tables["HistoryMyTrade"]:
            identity = (text(value["AccountID"]), text(value["SecurityId"]), text(value["Id"]))
            if identity in seen:
                raise failure()
            seen.add(identity)
            opened, closed = wall_time(value["OpenTime"]), wall_time(value["CloseTime"])
            # ATAS paired quantities carry native signs; never abs/rewrite stored facts.
            facts = {key: number(value[column], quantity=key in ("open_price", "close_price"), nullable=key == "commission")
                     for key, column in (("open_price", "OpenPrice"), ("close_price", "ClosePrice"), ("open_volume", "OpenVolume"), ("close_volume", "CloseVolume"), ("pnl", "PnL"), ("ticks_pnl", "TicksPnL"), ("price_pnl", "PricePnL"), ("commission", "Commission"))}
            missing = value["MissingDataCase"]
            # Native incomplete flags remain visible; they never establish a complete pair.
            # Constrain this enum-shaped field so free-text content cannot enter model facts.
            if missing and not re.fullmatch(r"[A-Za-z0-9_]+(?:[ ,|]+[A-Za-z0-9_]+)*", missing):
                raise failure()
            if len(missing) > 128:
                raise failure()
            if identity[:2] != (self._selected, SECURITY_ID):
                continue
            side = None
            ref = value["EnterTradeId"]
            fill = fills.get((identity[0], identity[1], ref)) if ref else None
            if (missing == "None" and fill and fill["Time"] == value["OpenTime"] and Decimal(fill["Price"]) == Decimal(value["OpenPrice"])
                    and abs(Decimal(fill["Volume"])) >= abs(Decimal(value["OpenVolume"]))):
                side = fill["OrderDirection"] or None
            at = closed["iso"] or opened["iso"]
            if at is None:
                raise failure()
            if not begin <= datetime.fromisoformat(at) <= finish:
                continue
            rows.append({"native_id": identity[2], "account_id": identity[0], "security_id": identity[1],
                         "entry_ref": ref or None, "exit_ref": value["ExitTradeId"] or None,
                         **facts, "open_time": opened, "close_time": closed, "side": side,
                         "is_complete": bool(opened["iso"] and closed["iso"] and Decimal(facts["open_volume"]) != 0 and Decimal(facts["close_volume"]) != 0 and missing == "None"),
                         "missing_data_case": missing or "Unknown"})
        rows.sort(key=lambda row: (row["close_time"]["iso"] or row["open_time"]["iso"], row["native_id"]), reverse=True)
        coverage = {"mode": "local_persisted_snapshot", "from": {"iso": begin.isoformat(), "kind": "Unspecified"},
                    "to": {"iso": finish.isoformat(), "kind": "Unspecified"}, "days": 7, "limit": 100,
                    "returned": min(100, len(rows)), "total_scope_rows": len(rows), "truncated": len(rows) > 100,
                    "incomplete_rows": sum(not row["is_complete"] for row in rows),
                    "file_modified_at": modified, "server_history_complete": False, "live_account_state": False}
        return origin, coverage, rows, utc_now()

    def list_trades(self):
        with self._lock:
            self._selection.clear()
            origin, coverage, rows, _ = self._read()
            self._origin = origin
            trades = []
            for row in rows[:100]:
                key = self._key({"origin": origin, "row": row})
                self._selection[key] = copy.deepcopy(row)
                trades.append({"trade_id": key, "instrument": SECURITY_ID,
                               "status": "closed" if row["is_complete"] else "incomplete",
                               "granularity": "native_paired_row", **{name: copy.deepcopy(row[name]) for name in FACTS}})
            result = {"source": SOURCE, "simulation_only": True, "origin_key": self._key(origin),
                      "coverage": coverage, "quality": FILE_QUALITY.copy(), "trades": trades, "returned": len(trades)}
            if len(encode_json(result)) > MAX_EVIDENCE:
                self._selection.clear()
                raise failure("journal_list_size", "Native journal list exceeds 131072 bytes.", 413)
            return result

    def capture(self, trade_id):
        # Reuse native facts/evidence and revalidate selected rows, not whole-file hashes.
        result = super().capture(trade_id)
        result["identity"]["source_detail"] = SOURCE
        result["quality"] = [*result["quality"], FILE_QUALITY[-1]]
        return result
