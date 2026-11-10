# Native Trade Journal Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement and independently review this scoped integration. The user already approved the journal-first design in the preceding exchange.

**Goal:** Read persisted BTC simulation records from the existing ATAS journal into the local workbench without chart indicators, and reconcile existing matched trades.

**User amendment (2026-11-10):** No indicator dependency. Task 2 runtime work is superseded by Task 3 below; Task 4 delivers the independent local-file source. The amended spec controls over the earlier API-only/source-scope defaults.

**Architecture:** A bounded read-only native journal adapter uses public TradingStatisticsProvider APIs. A dedicated companion source validates origin/scope and creates immutable evidence for existing preview/analysis routes. The old recorder remains intact but is no longer the default trade source.

**Tech Stack:** installed ATAS X .NET10 Mac SDK, C#, Python stdlib, vanilla JS, existing MCP server.

## Global Constraints

- Follow `docs/superpowers/specs/2026-11-10-native-trade-journal-design.md`.
- Public APIs only, no global statistics filter changes, no trades or real model calls.
- Exact current simulation account and BTCUSDT@BinanceFutures; preserve origin through every async stage. No fallback to unrelated account/port or replay.
- Preserve decimal precision, nullable fees, native pairing and raw time semantics. No inferred net PnL or historical DOM/Delta.
- No native dock work, app restart, new indicator, account/feed changes, runtime files/vendor DLLs/private records in Git. Standard native-file reads supersede the API-only source constraint for Task3/4; never infer current/live account state from persisted history.
- Commands start rtk. Existing checkout / branch codex/macos-support. Push to own fork already authorized, no upstream PR.

### Task 1: Native journal through the existing workbench

**Files:** Create `src/ATAS.AiBridge/NativeJournal.cs`, `companion/native_journal.py`, focused tests. Modify indicator lifecycle/request dispatch, `companion/app.py`, journal UI and existing MCP client/server as needed. Update user documentation and validation record. Keep changes limited to this data path.

**Interfaces:**
- Read-only `GET /api/journal` returns one bounded scope-verified list with schema/source, origin identity, coverage, native rows and quality metadata. Optional parameters must be strictly validated and bounded. No write endpoint.
- Existing Workbench journal interface remains `status()`, `list_trades()`, `capture(trade_id)`; add an authenticated evidence read route for inspection before configuring a model.
- Preserve `source=trade` immutable evidence format expected by analysis.py, `identity`, `quality`, `evidence`, `limits`. Source detail must distinguish native paired rows from recorder round trips.

- [ ] Inspect actual public SDK and pin DTO contract in implementation report before consumer code; use synthetic HistoryMyTrade fixtures with partial exits, null commission, two accounts and unknown DateTime kind.
- [ ] Implement bounded native read and async/lifetime/source checks. Use public APIs on documented host threads; no HTTP thread enumeration of live host collections. Only immutable sanitized results leave the host boundary.
- [ ] Implement native source validation/default wiring, list/detail UI and readonly MCP tools. Explicit source changes invalidate stale selection; caller-selected keys cannot retrieve another account's cached record.
- [ ] Run meaningful focused tests and regressions; build actual Mac and legacy outputs separately; update validation honestly and commit source-only. Do not install; controller validates after review.

### Task 2: Installed-host data reconciliation and delivery (superseded by user; host acceptance not claimed)

- [ ] Independently review Task1; resolve source defects through implementer and scoped review.
- [ ] Back up existing DLL / workbench state as needed, install reviewed artifact; no ATAS restart or new indicators. Confirm actual runtime version.
- [ ] Read existing closed simulation row and independently compare with ATAS UI or read-only native database; no new test trade needed.
- [ ] Verify workbench list/detail and immutable evidence. No configured provider needed for local evidence; zero external model calls.
- [ ] Record boundaries, perform final source/privacy audit, commit and push existing fork branch, verify remote SHA. If host genuinely blocks journal access, report the exact blocker without claiming acceptance.

### Task 3: Zero-indicator native persisted journal source (complete at44be584; scoped review passed)

**Files:** Add `companion/native_journal_files.py` and focused tests. Modify workbench default/source selection, existing journal UI, MCP list/evidence boundary and documentation only as required. Reuse native paired-row fact/evidence shapes; avoid duplicating existing analysis/provider workflow. No C# work, native UI, ATAS changes, install, or external model calls.

- Read the amended spec first. Implement bounded, strict read-only CSV container parsing for observed schemas. Require correct Version/Entity, exact expected Columns, StartData and EndData; reject malformed rows, ambiguous duplicate IDs, nonfinite numbers/negative prices, and unsupported time/decimal forms. Native paired volumes are signed and must remain signed; compare magnitudes only for fill-reference consistency, never infer side from signs. Fix shared API Python validation accordingly. Read a stable three-file snapshot (stat before/after, retry once at most), with no stale-data fallback on a failed refresh. Unit tests use synthetic files only.
- Discover simulated BTC accounts using Portfolio.IsRealAccount exactly false. One eligible account selects automatically; zero gives actionable empty/source status; multiple require explicit account selection through authenticated local API/UI. Return opaque account option keys, not private IDs, for UI selection. Validate selection on every read; changed or missing account fails closed. Do not infer simulation from an account-name prefix. Keep private account IDs solely inside source scope.
- Preserve native pairing/partial closes, decimal strings, null commission, raw Unspecified times and direction only from matching explicit entry fill. Evidence must not infer units, fee treatment, net PnL, current account state or historical Delta/DOM. Local snapshot coverage and three file modification times remain explicit. Bound 7-day/100-row result and 8MiB/10000-row inputs; failures never return another account or the old recorder.
- Make persisted source the default for normal workbench startup, independent of missing bridge. Changing market bridge port must not replace or invalidate the independent persisted source. Retain public API source as explicitly named optional tooling, no silent fallback. Local trade detail/preview must work without market connection; provider only required for actual send preview/analyze as existing contract.
- Expose safe readonly MCP persisted-list and selected-evidence tools with opaque keys and fact allowlists (no paths, private IDs, free-text comments, or credentials to models). Existing native API MCP names may stay for their documented explicit bridge source.
- Tests cover strict schema/version/terminator, partial write/race, size/row bound, one/multiple/real-only accounts, selected source change, partial exits, missing fees/timezone/direction, invalid/mismatched refs, list/evidence privacy, repeated reads and unchanged-row recapture after unrelated append, and local evidence route with market disconnected/provider absent. Run covering Python/JS tests and final regressions once, update report, commit source only. No private actual files in fixtures or Git.

### Task 4: Persisted-source acceptance and fork delivery

- Independently review Task 3, resolve source issues through scoped fix/re-review.
- Restore local companion process with existing state, new default source and no bridge requirement. Reconcile existing recent paired rows against a separate controller parser of the same ATAS records, disclose this is persisted-source agreement rather than public API runtime validation.
- Confirm authenticated list/detail/evidence with zero configured provider; ensure simulated account and private-ID redaction. Inspect browser UI if task browser is available; otherwise document actual boundary, do not claim visual acceptance.
- Record schema/version support, missing historical market context, unverified native module/dock and public API host route. Update source/privacy audit, commit, push own existing fork branch, verify remote SHA. Preserve user workspaces and existing indicators/config/data.

## Final acceptance status

Task1 source/contract review complete; its native host runtime remains unaccepted. Task2 was superseded by the user request for zero indicators. Task3 complete with171 initial Python regression,37 final native checks and16 final JS checks. Task4 authenticated HTTP/MCP private reconciliation passed on44be584 without external model calls. No private trading counts, directional distributions, field values or records are published. Browser list/detail data accepted on443366b; post-fix reveal is test/review verified, final visual rerun unavailable after browser space disappeared. See validation record for boundaries and final Git delivery.
