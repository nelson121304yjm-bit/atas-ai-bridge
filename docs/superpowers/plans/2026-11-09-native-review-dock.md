# Native Review Dock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Display the Chinese workbench in a genuine ATAS dockable tab without leaking credentials into workspace settings.
**Architecture:** Use public host WebPages factory/dock APIs. A bounded adapter owns only its marked panel. Python remains the auth/business boundary; deliver one-use bootstrap through a private launcher pipe and host native web message after checking the exact local URL.
**Tech Stack:** .NET 10, installed ATAS X/Avalonia, Python stdlib companion, existing browser modules.
**Spec:** `docs/superpowers/specs/2026-11-09-native-review-window-design.md`

## Global Constraints

- Must deliver an ATAS dockable sidebar or tab; a floating window or browser is not a substitute.
- Use public installed host APIs only; honor CanOpenWidget, replay and brand restrictions. Never patch host DLLs or access private host fields.
- Preserve other charts, indicators, recorder, collector, account settings, Binance auto-connect state, and provider settings.
- Never trade, activate a trial, upgrade a subscription, submit real model requests, or publish private runtime files/vendor DLLs.
- No bootstrap tokens, keys or cookies in persisted URLs, layout settings, titles, logs or command arguments.
- Only local root URLs with exact host 127.0.0.1 and port 8770 through 8780 are allowed by the adapter.
- Native messages only bootstrap the existing authenticated session; no arbitrary script execution, generic RPC or trading tools.
- Hide chart overlay entry and status by default; provide a usable indicator-settings entry. Do not assign a default keyboard shortcut.
- No silent external-browser fallback. Keep the existing browser launcher as a manual alternative.
- Use `rtk` before every shell command. Work in existing `codex/macos-support` checkout. No upstream PR.

---

### Task 1: Native dock adapter, secure bootstrap and entry

**Files:**
- Create `src/ATAS.AiBridge/ReviewDockHost.cs` for public host integration/ownership.
- Create if useful `src/ATAS.AiBridge/ReviewLaunch.cs` for private launcher IPC/validation.
- Modify `src/ATAS.AiBridge/AiBridgeIndicatorHost.cs`, `src/ATAS.AiBridge/ATAS.AiBridge.csproj`.
- Modify `companion/launch.py`, `companion/static/bootstrap.js`, `companion/static/ui.js`, `companion/static/app.js` only for native readiness/auth.
- Test `tests/host_contract/Program.cs`, `tests/test_companion_launch.py`, `tests/workbench_ui.test.mjs` or focused native test.
- Modify `README.md`; add `docs/validation/2026-11-09-native-review-dock.md` with truthful automated/manual status.

**Interfaces:**
- Consume `issue_url(directory: Path, bridge_port: int | None) -> str` and existing `/api/bootstrap`.
- Introduce mutually exclusive launcher mode `--native-json`: one JSON object with credential-free root `url` and one-use `token`, stdout only. Never log output. Preserve --open/--url-file.
- Host code under `#if MAC_INDICATOR`; legacy strategy unchanged.
- SDK evidence `/private/tmp/atas-dock-public-api.txt`, probe `/private/tmp/atas-dock-inspect/Program.cs`; SDK `/Applications/ATAS X.app/Contents/MonoBundle`.
- Dotnet `/private/tmp/atas-dotnet/dotnet`, `DOTNET_CLI_HOME=/private/tmp/atas-dotnet-home`, `DOTNET_CLI_TELEMETRY_OPTOUT=1`; Python `mcp/.venv/bin/python`.

- [ ] **Step 1: Pin auth/ownership behavior with tests.**

Extend existing real server fixtures to assert clean native URL, token exchange then replay rejection. Example contract:
```python
native = native_launch_result(issue_url(state, bridge_port=8790))
assert native["url"].startswith("http://127.0.0.1:")
assert native["url"].endswith("/")
assert "#" not in native["url"] and "?" not in native["url"]
assert native["token"]
```
Use actual test fixtures, not placeholder assertions. Test URL rejection (userinfo/localhost/remote host/wrong port/path/query/fragment), malformed stdout/timeouts, duplicate native event, bootstrap arriving before/after app startup. Re-auth must not double-register handlers or issue provider calls.

- [ ] **Step 2: Implement bounded private launcher IPC and native adapter.**

Verified host creation outline (not proof of unverified host behavior):
```csharp
var factory = PanelDockFactory.Instance;
if (!factory.CanOpenWidget(WorkspaceLayoutType.WebPages))
    throw new InvalidOperationException("ATAS does not allow the WebPages panel.");
```
Supply only clean WebPagesSettings to CreateWidget; stable plugin ID; CreateLayout then MainViewModel.AddTab. Locate only its own native WebViewControl. Check exact root URL and messaging support before delivering token. Verify public signatures and native event format with metadata/read-only implementation inspection. If essential behavior unavailable, report specific evidence before changing authentication/UI architecture. No private reflection or permission bypass. Keep capture running on failure.

- [ ] **Step 3: Wire usable settings action and lifecycle.**

Provide `Open AI review tab` using host-supported settings action/property. Must not fire on settings deserialization. Default `ShowReviewButton=false`, `ShowBridgeStatus=false`; opt-in button still uses bounded hit testing. Serialize launch requests; heavy work off GUI thread, UI changes on GUI thread. Reuse/re-auth marked pane only. Clean event disposal; no default shortcut.

- [ ] **Step 4: Focused/regression checks, self-review and commit.**

```sh
rtk proxy mcp/.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -q
rtk proxy node --test tests/workbench_ui.test.mjs
rtk proxy env DOTNET=/private/tmp/atas-dotnet/dotnet DOTNET_CLI_HOME=/private/tmp/atas-dotnet-home DOTNET_CLI_TELEMETRY_OPTOUT=1 bash build.sh
```
Run actual compiled Mac and legacy host contracts. Report commands/counts, behavior covered, native UI still unverified and compatibility limits. Commit only source/tests/docs. Do not install or manipulate ATAS; controller owns runtime validation.

### Task 2: Controller-owned installed-host validation and delivery

**Files:** validation doc and this plan; code repairs return to implementer with independent re-review.
**Interfaces:** reviewed Task 1 DLL, private workbench installation and native ATAS window.

- [ ] Install using existing backup-preserving script after build/contract success; record hashes privately.
- [ ] Inspect current workspace; open new native tab through settings; no trading controls.
- [ ] Verify Chinese page and four sections, empty model configuration, no provider calls.
- [ ] Verify reopen reuses tab, close/reopen, backend reuse, re-auth. Inspect own persisted settings for absence of secrets without printing unrelated workspace/account data.
- [ ] Confirm chart preserved and default overlay gone. Document observed docking/restore behavior; don't imply untested full restart persistence.
- [ ] Resolve findings through implementer/scoped review, run covering checks, complete native-scope broad final review, commit/push to existing fork and verify remote HEAD.

If automation cannot click, first make the product concrete, then hand off one precise action. Do not claim native acceptance until observed.
