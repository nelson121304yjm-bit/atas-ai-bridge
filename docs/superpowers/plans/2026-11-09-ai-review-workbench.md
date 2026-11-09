# AI Review Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver the approved local Chinese AI workbench, opened from ATAS, with BYOK, versioned rules/prompts, evidence-bound analysis and optional SIM journal review.

**Architecture:** Keep the existing indicator and GET bridge. A separate standard-library Python companion owns a loopback web UI, provider requests, private SQLite metadata/reports and a fixed read-only journal MCP adapter. The model never invokes trading or filesystem tools.

**Tech Stack:** Python 3.12+, standard library HTTP/SQLite/urllib, plain accessible HTML/CSS/JS, existing .NET 10 indicator SDK. No Node build step or vendor binaries in Git.

**Spec:** `docs/superpowers/specs/2026-11-09-ai-review-workbench-design.md` (approved route A).

## Global Constraints

- Work in the existing dedicated fork checkout `/Users/nelson/Documents/A股/atas-ai-bridge`, branch `codex/macos-support`; baseline `c095f8c`. Preserve the installed chart/recorder. Do not trade or change Binance auto-connect.
- All shell invocations start with `rtk`; use `rtk proxy` when necessary. Do not use injected Word/Office tools.
- No production API Key lookup or real provider billing during tests. Use synthetic fixtures and a local fake provider. No real trading data sent externally by the development workflow.
- Python companion binds only `127.0.0.1`; preferred ports 8770–8780. Existing bridge uses 8790–8810; collector uses 8766. Never bind publicly.
- Private state defaults to `~/Library/Application Support/ATAS AI Review`; directory 0700, private files 0600. Keys are session-only or macOS Keychain, never SQLite, argv, URL, logs, Git or browser persistent storage.
- Model protocol is text Chat Completions-compatible. Remote endpoints require HTTPS; HTTP only for explicit numeric loopback endpoints. Reject redirects. Keys are bound to the full normalized base URL.
- Live evidence requires indicator/read-only/connected/non-real BTCUSDT@BinanceFutures status and freshness <=5000 ms. Historical journal review is separate and never claims current flatness.
- Context JSON <=131072 UTF-8 bytes; no silent byte truncation. Closed candles 20, footprint 2 bars/top20 each, DOM15 each side, profile200/top20, trade max_samples60. All endpoint capture times and missing data are explicit.
- Reports bind immutable evidence, system and prompt versions. Model output is inert text. Unknown evidence citations are flagged; no fabricated fees, risk/R, exact excursions or psychology.
- Implementers never spawn other agents. Run focused tests while iterating, full relevant suite before committing. Keep report artifacts under this plan's `.superpowers/sdd/` directory; no production state there.

## Shared contracts

```python
# companion/common.py
class AppError(Exception):
    # public attributes: code: str, message: str, status: int
    def __init__(self, code: str, message: str, status: int = 400): ...
def utc_now() -> str: ...  # ISO8601 UTC with timezone
def encode_json(value: object) -> bytes: ...  # UTF-8, allow_nan=False

# HTTP layer invokes a business handler after auth:
# handler(method: str, path: str, payload: dict) -> dict | list
# AppError maps to {ok:false,error:{code,message}} and its HTTP status.

# companion/store.py: each method returns detached JSON-serializable data.
class Store:
    def __init__(self, directory: Path): ...
    def settings(self) -> dict: ...
    def save_settings(self, value: dict) -> None: ...  # allowlisted nonsecret metadata
    def profile(self) -> dict: ...  # {system:{id,data,created_at},prompt:{id,data,created_at},history:[...]}
    def save_profile(self, system: dict, prompt: str, expected: dict | None = None) -> dict: ...
    def put(self, kind: str, value: dict) -> dict: ...  # previews/reports/proposals, generates id+created_at
    def get(self, kind: str, identifier: str) -> dict: ...
    def list(self, kind: str, limit: int = 50) -> list[dict]: ...
    def update(self, kind: str, identifier: str, value: dict) -> dict: ...
```

## Task 1: Loopback shell, authenticated launcher and chart entry

**Files:** Create `companion/__init__.py`, `common.py`, `http_server.py`, `launch.py`, `server.py`, `static/index.html`, `static/app.js`, `static/app.css`, `install-workbench.sh`; modify `AiBridgeIndicatorHost.cs`, `.gitignore`; test `tests/test_companion_http.py`, `test_companion_launch.py`, existing host contract.

**Produces:** `make_server(handler, state_dir, port=0)` returns an HTTPServer-like object with `.server_address`, `.serve_forever()`, `.shutdown()`, `.server_close()` and `.auth` for bootstrap management. `Auth.issue_launch()->str`, `Auth.exchange(token)->(session_id,csrf)`. `python -m companion.launch --open [--state-dir DIR] [--bridge-port PORT]` starts/reuses backend. `--url-file PATH` writes the bootstrap URL privately instead of opening it (test/automation only).

- [x] Write HTTP integration tests using real loopback sockets: root loads, unauthenticated API rejected, one-use/120s bootstrap, HttpOnly SameSite=Strict cookie, CSRF on all mutations, reject foreign Origin/Host and Sec-Fetch-Site=cross-site, malformed/oversized JSON and no arbitrary static file reads. Test fresh session after page reload.

```python
with self.assertRaises(AppError):
    auth.exchange(token)  # token was already exchanged
self.assertEqual(response.status, 403)  # POST without CSRF must not reach handler
```

- [x] Run `rtk proxy mcp/.venv/bin/python -m unittest discover -s tests -p 'test_companion_http.py' -v`; record expected missing-feature failure before implementation.
- [x] Implement the local server and launcher. Assets use CSP `default-src 'self'`, no third-party resources, no inline scripts. API bodies limited to 196608 bytes. Use private runtime metadata/control token and locking to reuse exactly one instance; authenticated launcher endpoint issues a short-lived bootstrap URL. The browser removes the fragment before rendering. Never print the launch URL in normal operation.
- [x] Add a minimal Chinese working landing page showing service status and an explicit not-yet-configured state. No fake live data. Later tasks replace its presentation.
- [x] Add an ATAS chart button using installed SDK ProcessMouseDown/Up/GetCursor. Only consume clicks inside the button. Offload launch to Task.Run; read private installation metadata containing absolute Python and launcher paths. Use argument lists, never a shell command. Repeated clicks guarded. Missing installation shows a useful status, no throwing through ATAS callbacks.
- [x] Installer writes private installation metadata and a user launcher explicitly; build-only remains unchanged. It never restarts ATAS. Add ignored private state patterns. Compile Mac SDK and run contracts.
- [x] Verify bootstrap API with local requests, then commit only this slice. Report UI/native live checks still pending if controller has not performed them.

## Task 2: Provider credentials and versioned local storage

**Files:** Create `companion/store.py`, `provider.py`, `credentials.py`, `keychain.py`; tests `test_companion_store.py`, `test_companion_provider.py`.

**Produces:** Shared Store contract above; `normalize_base_url(str)->str`; `ProviderClient.complete(base_url,model,key,messages)->dict` returning `text`, optional `usage` and `request_id`; `CredentialVault(keychain=None).set(base_url,key,persist=False)`, `.get(base_url)`, `.delete(base_url)`, `.has(base_url)`; Keychain interface get/set/delete (no argv secrets).

- [x] Test immutable profile versions, expected-version conflict, detached JSON, secret metadata rejection, restrictive permissions, not overwriting prior reports. Use temporary directories.
- [x] Test a local fake HTTP provider: auth header destination, messages/model payload, response shape, bounded response, explicit timeout, 401/429/5xx, bad JSON, redirects rejected and no retries. Assert a secret echoed by the fake server never appears in AppError. No real remote provider calls.
- [x] Implement standard-library urllib protocol with 60s timeout and 2MiB response ceiling. Base URL disallows credentials/query/fragment; model length bounded. No temperature or tool parameters forced. Preserve usage when present, no invented costs.
- [x] Implement in-memory keys with full-address binding and optional macOS Security.framework Keychain via ctypes (not security CLI argv). Keychain failures are explicit and do not silently persist elsewhere. Provide injection for testing without touching the user's Keychain.
- [x] Implement SQLite transactions under a lock, with concrete allowlisted settings (`base_url`, `model`, `persist_key`) and version fields (`name`, `timeframe`, `setup`, `entry`, `exit`, `risk`, `goal`). Text limits: system fields 4000 chars each, prompt 12000, model128, URL2048. No key field accepted.
- [x] Run new suites plus prior suites; commit. Report contracts precisely.

## Task 3: Bounded live evidence and optional read-only journal

**Files:** Create `companion/evidence.py`, `journal.py`; tests `test_companion_evidence.py`, `test_companion_journal.py`.

**Consumes:** common AppError, existing ATAS port discovery (may import only discovery helpers; do not expose trading client methods).
**Produces:** `MarketSource(port=None).status()->dict`, `.capture()->dict`; `JournalSource(config=None).status()->dict`, `.list_trades()->dict`, `.capture(trade_id)->dict`. Evidence schema: `{schema_version:1,source:'market'|'trade',captured_at,identity,quality:[],evidence:{id:{retrieved_at,data}},limits:{...}}`. Known citations use `[E:<id>]`.

- [x] Tests fail closed for live accounts, unknown status, wrong instrument, stale/null ages, account/security/aggregation changes during capture, bad data and response oversize. Test active-bar exclusion and window-relative CVD metadata.
- [x] Implement GET-only loopback requests without proxies/redirects. Default cached candles/CVD/window footprint to reduce tick waits; trim structured arrays to documented sizes with explicit truncation metadata. Bound raw reads at 3MiB and final evidence at 131072 bytes. Record acquisition timestamps per endpoint; never claim an atomic snapshot.
- [x] Validate start/end status and keep quality flags for optional failures; do not substitute zeros. Sanitize account identifier out of provider evidence while retaining simulation classification. Status UI may show current simulated account locally.
- [x] Implement optional stdio journal client using installer-provided absolute Python/script/db paths only. Fixed allowlist list_trades/get_trade_context/get_session_summary/get_storage_stats, initialize handshake, bounded reads/timeouts, process cleanup. HTTP callers cannot supply commands or database paths. No config means explicit disconnected state.
- [x] Query selected trade with max_samples60, reject open/unverified/wrong-account/instrument entries as appropriate using actual journal contract. Preserve all quality/fees flags. Do not write the journal DB.
- [x] Run targeted tests and regressions, commit.

## Task 4: Preview, analysis, follow-up and rule proposal workflow

**Files:** Create `companion/app.py`, `analysis.py`, `prompts.py`; modify `server.py`; tests `test_companion_workflow.py`.

**Consumes:** Store, ProviderClient, CredentialVault, MarketSource, JournalSource.
**Produces:** `Workbench(state_dir, market=None, journal=None, provider=None, vault=None).handle(method,path,payload)` implementing these authenticated APIs:

```text
GET /api/status; GET/POST /api/settings; POST /api/settings/test
GET/POST /api/profile; GET /api/trades; GET /api/reports; GET /api/reports/<id>
POST /api/preview {source,trade_id?,question}; POST /api/analyze {preview_id}
GET /api/jobs/<id>; POST /api/reports/<id>/followup {question}
POST /api/reports/<id>/propose; POST /api/proposals/<id>/accept; POST /api/proposals/<id>/reject
POST /api/demo
```

- [x] Write workflow tests first: preview sends nothing, settings/test sends fixed synthetic text only, model key absent blocks paid analysis, provider config mismatch invalidates preview, report binds versions/evidence, followup uses old evidence, fake citations flagged, proposal rejected by default and acceptance optimistic-concurrency protected.
- [x] Implement one active provider job at a time with bounded queue/history and pollable states. Do not block HTTP status or chart capture. Failures carry generic safe categories. No automatic retry. One-use analyze submission prevents duplicate charges from double-clicking.
- [x] A preview persists full bounded evidence and current system/prompt/provider metadata. The API shows endpoint/model/content bytes/quality and full JSON preview. Keep exact message payload within 196608 bytes; reject oversize.
- [x] Provider results are inert text. Parse citation tokens and flag references not in evidence, including missing references. Record actual provider/model/time/usage, not invented costs. Followup capped at six turns and same immutable evidence; signal fresh capture is needed for new market data.
- [x] Proposals ask model for JSON `{system:{...},prompt:'...',reason:'...'}`; validate types/limits and show old/new. Accept requires expected original versions still current and applies both in one transaction; reject does not change versions. Never autoapply model output.
- [x] Demo uses explicitly synthetic fixtures and deterministic example response, no key/provider request. Mark report and all UI metadata as demo. Complete tests and commit.

## Task 5: Complete Chinese workbench UI and installation integration

**Files:** Replace `companion/static/index.html`, `app.css`, `app.js`; add focused JS modules as needed under static (each ~200 lines or less), modify installer/docs; tests `test_companion_install.py` and browser QA.

- [x] Implement four sections: 分析 / 交易日志 / 我的系统 / 模型设置, restrained dark-slate/teal palette, responsive layout, native controls, visible focus, semantic heading order and labels. No external assets or inline handlers.
- [x] Analysis shows connection/identity/age, source selector, question, generate-preview button, explicit provider destination and send action, job progress/errors, report history, inert result, followup and rule-proposal diff with accept/reject. Display evidence quality and demo badges persistently.
- [x] Settings show base URL/model, password input, session or Keychain option, separate Save/Test/Delete key actions. Never prefill keys or put them in storage/URLs. Clear password field after save. Changing address explains re-entry without silently reusing a key.
- [x] System form supports optional fields, editable prompt and previous version list. Journal lists real optional data and disconnected state, select closed trade for preview. Never show demo trades as actual journal entries.
- [x] Installer optional journal path arguments write private config only; discover no private keys. Provide a desktop-friendly launcher and uninstall/rollback instructions. Keep all private states out of public commits.
- [x] Use Ego Lite skill for real browser interactions with a temporary-state/fake-provider test server: bootstrap, save settings, connection test, version edit, preview→analyze→followup, proposal conflict/accept/reject, demo labels, errors and reload. Screenshot at 320,768,1024,1440 widths and inspect screenshots. No real external model requests.
- [ ] Compile/install updated indicator with backup, verify visible entry in ATAS and launch (hand off only exact clicks if automation cannot operate it). Verify local real status and journal list without sending records to a model. Commit.

## Task 6: Final review, documentation and delivery

**Files:** `README.md`, `docs/macos.md`, new `docs/workbench.md`, plan checkboxes and validation record.

- [ ] Review aggregate diff against approved spec, resolve substantive findings with tests. Do not claim commercial-grade or full supplier support from mocks.
- [ ] Run `rtk proxy mcp/.venv/bin/python -m unittest discover -s tests -v`, Mac Release build and host contract, legacy conditional build and contract, JS syntax checks. Verify no vendor DLL, key, state DB, account record or screenshot is staged.
- [x] Document launch/setup, provider data destination, Keychain/session behavior, analysis limitations, optional journal integration, recovery, tested versions and unverified real provider calls. Update plan outcomes.
- [ ] Push source/tests/docs to the already-authorized existing fork branch and verify `git ls-remote` matches HEAD. Leave installed workbench usable at model settings; user enters their own key when ready.

截至 2026-11-09：Task 1–4 的代码／契约与 Task 5 的 UI、安装器及合成浏览器流程已完成。
默认安装的 0.4.0-macos.1 指标尚未在新图表完成入口与实时采集验收；
Task 5 最后一项及 Task 6 的最终复核、发布检查和推送继续保持未完成。
真实付费模型未验证。详见 `docs/validation/2026-11-09-workbench.md`。
