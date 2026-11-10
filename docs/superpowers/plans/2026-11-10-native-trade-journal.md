# Native Trade Journal Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement and independently review this scoped integration. The user already approved the journal-first design in the preceding exchange.

**Goal:** Read the current BTC simulation account's ATAS journal into the existing workbench and verify one existing matched trade.

**Architecture:** A bounded read-only native journal adapter uses public TradingStatisticsProvider APIs. A dedicated companion source validates origin/scope and creates immutable evidence for existing preview/analysis routes. The old recorder remains intact but is no longer the default trade source.

**Tech Stack:** installed ATAS X .NET10 Mac SDK, C#, Python stdlib, vanilla JS, existing MCP server.

## Global Constraints

- Follow `docs/superpowers/specs/2026-11-10-native-trade-journal-design.md`.
- Public APIs only, no global statistics filter changes, no trades or real model calls.
- Exact current simulation account and BTCUSDT@BinanceFutures; preserve origin through every async stage. No fallback to unrelated account/port or replay.
- Preserve decimal precision, nullable fees, native pairing and raw time semantics. No inferred net PnL or historical DOM/Delta.
- No native dock work, app restart, new indicator, account/feed changes, runtime files/vendor DLLs/private records in Git.
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

### Task 2: Installed-host data reconciliation and delivery

- [ ] Independently review Task1; resolve source defects through implementer and scoped review.
- [ ] Back up existing DLL / workbench state as needed, install reviewed artifact; no ATAS restart or new indicators. Confirm actual runtime version.
- [ ] Read existing closed simulation row and independently compare with ATAS UI or read-only native database; no new test trade needed.
- [ ] Verify workbench list/detail and immutable evidence. No configured provider needed for local evidence; zero external model calls.
- [ ] Record boundaries, perform final source/privacy audit, commit and push existing fork branch, verify remote SHA. If host genuinely blocks journal access, report the exact blocker without claiming acceptance.
