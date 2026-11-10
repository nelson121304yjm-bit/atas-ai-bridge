import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import {initAnalysis, updateStatus} from "../companion/static/analysis.js";
import {initSettings, refreshSettings} from "../companion/static/settings.js";
import {initSystem, refreshProfile} from "../companion/static/system.js";

function fakeDocument() {
  const elements = new Map();
  class Element {
    constructor(tag = "div") {
      this.tag = tag;
      this.children = [];
      this.listeners = new Map();
      this.classList = {toggle() {}};
      this.value = "";
      this.textContent = "";
      this.hidden = false;
      this.disabled = false;
    }
    set id(value) { this._id = value; elements.set(value, this); }
    get id() { return this._id; }
    addEventListener(type, listener) {
      const listeners = this.listeners.get(type) || [];
      listeners.push(listener);
      this.listeners.set(type, listeners);
    }
    async emit(type, extra = {}) {
      const event = {target: this, currentTarget: this, preventDefault() {}, ...extra};
      for (const listener of this.listeners.get(type) || []) await listener(event);
    }
    append(...children) { this.children.push(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute() {}
    querySelector(selector) {
      return selector === "button[type=submit]" ? get("profile-save") : null;
    }
  }
  function get(id) {
    if (!elements.has(id)) { const element = new Element(); element.id = id; }
    return elements.get(id);
  }
  globalThis.document = {getElementById: get, createElement: tag => new Element(tag),
    querySelectorAll: () => []};
  return get;
}

const reply = (data, status = 200) => ({ok: status < 400, json: async () => data});

function nativeBootstrap() {
  const listeners = new Map();
  const nativeListeners = new Map();
  const acknowledgements = [];
  const location = {hash: "", pathname: "/", search: "", hostname: "127.0.0.1", port: "8770",
    protocol: "http:", origin: "http://127.0.0.1:8770"};
  const window = {
    location,
    addEventListener(type, callback) {
      const callbacks = listeners.get(type) || []; callbacks.push(callback); listeners.set(type, callbacks);
    },
    dispatchEvent(event) { for (const callback of listeners.get(event.type) || []) callback(event); },
    chrome: {webview: {
      addEventListener(type, callback) { nativeListeners.set(type, callback); },
      postMessage(data) { acknowledgements.push(data); },
    }},
  };
  window.top = window;
  vm.runInNewContext(fs.readFileSync(new URL("../companion/static/bootstrap.js", import.meta.url), "utf8"),
    {window, location, history: {replaceState() {}}, URLSearchParams, Event});
  return {window, acknowledgements,
    receive(data) { nativeListeners.get("message")?.({data}); }};
}

test("native bootstrap queues early and late messages once on the verified host channel", async () => {
  const native = nativeBootstrap();
  const token = "a".repeat(32), requestId = "1".repeat(32);
  const message = {type: "atas-ai-review.bootstrap", version: 1, token, requestId};
  native.receive(message);
  assert.equal(native.window.reviewBootstrap.hasToken(), true);
  const value = native.window.reviewBootstrap.take();
  assert.equal(value.token, token);
  assert.equal(value.requestId, requestId);
  assert.equal(native.window.reviewBootstrap.hasToken(), false);
  let changes = 0;
  native.window.addEventListener("review-bootstrap", () => { changes += 1; });
  native.receive(message);
  assert.equal(changes, 0);
  native.receive({...message, token: "b".repeat(32), requestId: "2".repeat(32)});
  assert.equal(changes, 1);
  assert.equal(native.window.reviewBootstrap.take().token, "b".repeat(32));
  native.receive({type: "arbitrary-rpc", token, requestId});
  native.receive({...message, token: "x"});
  assert.equal(native.window.reviewBootstrap.hasToken(), false);
  for (const [field, bad] of [["protocol", "https:"], ["hostname", "localhost"], ["port", "8781"],
    ["pathname", "/other"], ["search", "?token=bad"], ["hash", "#bad"]]) {
    const original = native.window.location[field];
    native.window.location[field] = bad;
    native.receive({...message, token: "e".repeat(32), requestId: "5".repeat(32)});
    assert.equal(native.window.reviewBootstrap.hasToken(), false, field);
    native.window.location[field] = original;
  }
});

test("rejected native bootstrap reports failure once without requesting a provider", async () => {
  const native = nativeBootstrap();
  globalThis.window = native.window;
  const {connectSession} = await import("../companion/static/ui.js");
  let exchanges = 0;
  globalThis.fetch = async path => {
    assert.equal(path, "/api/bootstrap"); exchanges += 1;
    return reply({error: {code: "unauthorized"}}, 403);
  };
  const message = {type: "atas-ai-review.bootstrap", version: 1, token: "f".repeat(32), requestId: "6".repeat(32)};
  native.receive(message);
  await assert.rejects(connectSession(), error => error.code === "unauthorized");
  native.receive(message);
  assert.equal(exchanges, 1);
  assert.equal(native.acknowledgements.length, 1);
  assert.equal(native.acknowledgements[0].ok, false);
  assert.equal(native.window.reviewBootstrap.hasToken(), false);
});

test("native reauthentication before, during and after app startup installs handlers once", async t => {
  for (const timing of ["before", "during", "after"]) await t.test(timing, async () => {
    const get = fakeDocument();
    const native = nativeBootstrap();
    globalThis.window = native.window;
    const originalInterval = globalThis.setInterval;
    let intervals = 0;
    globalThis.setInterval = () => { intervals += 1; return intervals; };
    const calls = [];
    let finishSession;
    const delayedSession = new Promise(resolve => { finishSession = resolve; });
    globalThis.fetch = async (path, options = {}) => {
      calls.push({path, options});
      if (path === "/api/session") return timing === "during" ? delayedSession : reply({error: {code: "unauthorized"}}, 401);
      if (path === "/api/bootstrap") return reply({csrf: "synthetic-csrf"});
      if (path === "/api/status") return reply({market: {}, journal: {connected: false}, configured: false});
      if (path === "/api/settings") return reply({settings: {base_url: "", model: "", persist_key: false}});
      if (path === "/api/profile") return reply({profile: profile("s", "p", "", "")});
      if (path === "/api/reports") return reply({reports: []});
      throw new Error(`Unexpected automatic request ${path}`);
    };
    const message = {type: "atas-ai-review.bootstrap", version: 1, token: "c".repeat(32), requestId: "3".repeat(32)};
    try {
      if (timing === "before") native.receive(message);
      const app = await import(`../companion/static/app.js?native-start-${timing}`);
      if (timing === "after") await app.start();
      if (timing !== "before") native.receive(message);
      if (timing === "during") finishSession(reply({error: {code: "unauthorized"}}, 401));
      await app.start();
      await app.start();
      assert.equal(native.acknowledgements.length, 1);
      assert.equal(native.acknowledgements[0].ok, true);
      assert.equal(native.acknowledgements[0].requestId, message.requestId);
      native.receive(message); // duplicate message must not exchange a consumed token
      native.receive({...message, token: "d".repeat(32), requestId: "4".repeat(32)});
      await app.start();
      assert.equal(calls.filter(value => value.path === "/api/bootstrap").length, 2);
      assert.equal(native.acknowledgements.length, 2);
      assert.equal(get("preview-form").listeners.get("submit").length, 1);
      assert.equal(get("settings-form").listeners.get("submit").length, 1);
      assert.equal(get("refresh-status").listeners.get("click").length, 1);
      assert.equal(intervals, 1);
      assert.ok(calls.every(value => value.path === "/api/bootstrap" || !value.options.body),
        "reauthentication issues no model/provider mutations");
    } finally { globalThis.setInterval = originalInterval; }
  });
});
const profile = (systemId, promptId, name, promptText) => ({
  system: {id: systemId, data: {name, timeframe: "", setup: "", entry: "", exit: "", risk: "", goal: ""}, created_at: "2026-11-09T00:00:00Z"},
  prompt: {id: promptId, data: promptText, created_at: "2026-11-09T00:00:00Z"},
  history: [],
});

test("workbench UI regressions", async t => {
  await t.test("question, source and trade changes invalidate an old preview", async () => {
    const get = fakeDocument();
    get("source").value = "market";
    get("question").value = "First question";
    let analyzeCalls = 0;
    globalThis.fetch = async (path, options = {}) => {
      if (path === "/api/preview") {
        const payload = JSON.parse(options.body);
        return reply({ok: true, preview: {id: "preview", source: payload.source, question: payload.question,
          target: {endpoint: "https://example.invalid/v1/chat/completions", model: "test-model"},
          profile: profile("s1", "p1", "", ""), evidence: {captured_at: "2026-11-09T00:00:00Z", limits: {}},
          quality: [], execution_rules: {}, request: {}, content_bytes: 100, submission: null}});
      }
      if (path === "/api/analyze") { analyzeCalls += 1; return reply({ok: true}); }
      throw new Error(`Unexpected request: ${path}`);
    };
    initAnalysis();
    updateStatus({market: {connected: true, eligible: true}, journal: {connected: true}, configured: true});
    await get("preview-form").emit("submit");
    assert.equal(get("analyze-button").disabled, false);
    get("question").value = "Changed question";
    await get("question").emit("input");
    assert.equal(get("analyze-button").disabled, true);
    assert.match(get("preview-badge").textContent, /需重建/);
    await get("analyze-button").emit("click");
    assert.equal(analyzeCalls, 0);
    await get("preview-form").emit("submit");
    assert.equal(get("analyze-button").disabled, false);
    get("source").value = "trade";
    await get("source").emit("change");
    assert.equal(get("analyze-button").disabled, true);
    get("trade-id").value = "trade-1";
    await get("preview-form").emit("submit");
    assert.equal(get("analyze-button").disabled, false);
    get("trade-id").value = "trade-2";
    await get("trade-id").emit("change");
    assert.equal(get("analyze-button").disabled, true);
  });

  await t.test("target save and failed Key save retain typed Key", async () => {
    const get = fakeDocument();
    let saved = {base_url: "https://example.invalid/v1", model: "m1", persist_key: false, key_present: false};
    let failKey = true;
    globalThis.fetch = async (path, options = {}) => {
      assert.equal(path, "/api/settings");
      if (!options.body) return reply({ok: true, settings: saved});
      const payload = JSON.parse(options.body);
      if (payload.key && failKey) { failKey = false; return reply({ok: false, error: {code: "invalid_settings", message: "Rejected"}}, 400); }
      saved = {...saved, ...payload, key_present: !!payload.key || saved.key_present};
      delete saved.key;
      return reply({ok: true, settings: saved});
    };
    initSettings();
    await refreshSettings();
    get("key").value = "unsaved-secret";
    get("persist-key").checked = true;
    await get("settings-form").emit("submit", {submitter: get("settings-submit")});
    assert.equal(get("key").value, "unsaved-secret");
    await get("save-key").emit("click");
    assert.equal(get("key").value, "unsaved-secret");
    await get("save-key").emit("click");
    assert.equal(get("key").value, "");
    get("key").value = "other-secret";
    get("base-url").value = "https://other.invalid/v1";
    await get("base-url").emit("input");
    assert.equal(get("key").value, "");
  });

  await t.test("settings load gates every action before any configuration is displayed", async () => {
    const get = fakeDocument();
    const {initSettings: initFresh, refreshSettings: refreshFresh} =
      await import("../companion/static/settings.js?initial-settings-load");
    let finishLoad;
    const delayed = new Promise(resolve => { finishLoad = resolve; });
    const posts = [];
    const settings = {base_url: "https://a.invalid/v1", model: "alias", persist_key: false, key_present: true};
    globalThis.fetch = async (_path, options = {}) => {
      if (!options.body) return delayed;
      posts.push(JSON.parse(options.body));
      return reply({ok: true, settings});
    };
    initFresh();
    const loading = refreshFresh();
    for (const id of ["base-url", "model", "key", "persist-key", "settings-submit", "save-key", "delete-key", "test-key"])
      assert.equal(get(id).disabled, true, id);
    get("key").value = "unsaved-key";
    await get("settings-form").emit("submit", {submitter: get("settings-submit")});
    for (const id of ["save-key", "delete-key", "test-key"]) await get(id).emit("click");
    assert.equal(posts.length, 0);
    finishLoad(reply({ok: true, settings}));
    await loading;
    assert.equal(get("save-key").disabled, false);
    assert.equal(get("settings-submit").disabled, false);
  });

  await t.test("stale settings retain Key and require explicit refresh then target confirmation", async () => {
    const get = fakeDocument();
    const {initSettings: initFresh, refreshSettings: refreshFresh} =
      await import("../companion/static/settings.js?stale-settings");
    const a = {base_url: "https://a.invalid/v1", model: "alias-a", persist_key: false};
    const b = {base_url: "https://b.invalid/v1", model: "alias-b", persist_key: true};
    let current = a;
    const posts = [];
    globalThis.fetch = async (path, options = {}) => {
      if (!options.body) return reply({ok: true, settings: {...current, key_present: true}});
      const payload = JSON.parse(options.body);
      posts.push({path, payload});
      if (JSON.stringify(payload.expected) !== JSON.stringify(current))
        return reply({ok: false, error: {code: "settings_conflict"}}, 409);
      if (path === "/api/settings/test")
        return reply({ok: true, job: {id: "synthetic", state: "succeeded", result: {provider: {}}}});
      return reply({ok: true, settings: {...current, key_present: true}});
    };
    initFresh();
    await refreshFresh();
    current = b;
    get("key").value = "key-intended-for-a";
    await get("save-key").emit("click");
    assert.deepEqual(posts[0].payload.expected, a);
    assert.equal(get("key").value, "key-intended-for-a");
    assert.equal(get("base-url").value, a.base_url);
    assert.equal(get("settings-submit").disabled, true);
    for (const id of ["save-key", "delete-key", "test-key"]) await get(id).emit("click");
    assert.equal(posts.length, 1);
    await get("settings-refresh").emit("click");
    assert.equal(get("base-url").value, b.base_url);
    assert.equal(get("key").value, "key-intended-for-a");
    assert.equal(get("settings-confirm").hidden, false);
    assert.equal(get("save-key").disabled, true);
    await get("save-key").emit("click");
    assert.equal(posts.length, 1);
    await get("settings-confirm").emit("click");
    assert.equal(get("save-key").disabled, false);
    await get("save-key").emit("click");
    assert.deepEqual(posts[1].payload.expected, b);
    assert.equal(get("key").value, "");
    await get("delete-key").emit("click");
    await get("test-key").emit("click");
    await get("settings-form").emit("submit", {submitter: get("settings-submit")});
    for (const post of posts.slice(1)) assert.deepEqual(post.payload.expected, b);
    assert.equal(posts.length, 5);
  });

  await t.test("form stays unavailable until the first complete profile arrives", async () => {
    const get = fakeDocument();
    const {initSystem: initFresh, refreshProfile: refreshFresh} =
      await import("../companion/static/system.js?initial-load-test");
    let finishLoad;
    const delayed = new Promise(resolve => { finishLoad = resolve; });
    const posts = [];
    const initial = profile("initial-system", "initial-prompt", "Saved system", "Saved prompt");
    initial.system.data.entry = "Existing entry rule";
    globalThis.fetch = async (path, options = {}) => {
      assert.equal(path, "/api/profile");
      if (!options.body) return delayed;
      posts.push(JSON.parse(options.body));
      return reply({ok: true, profile: initial});
    };
    initFresh();
    const loading = refreshFresh();
    assert.equal(get("sys-name").disabled, true);
    assert.equal(get("sys-entry").disabled, true);
    assert.equal(get("sys-prompt").disabled, true);
    assert.equal(get("profile-save").disabled, true);
    await get("profile-form").emit("submit");
    assert.equal(posts.length, 0);
    finishLoad(reply({ok: true, profile: initial}));
    await loading;
    assert.equal(get("sys-name").value, "Saved system");
    assert.equal(get("sys-entry").value, "Existing entry rule");
    assert.equal(get("sys-prompt").value, "Saved prompt");
    assert.equal(get("sys-entry").disabled, false);
    assert.equal(get("profile-save").disabled, false);
    assert.match(get("profile-message").textContent, /已载入/);
    await get("profile-form").emit("submit");
    assert.equal(posts[0].system.entry, "Existing entry rule");
    assert.deepEqual(posts[0].expected, {system: "initial-system", prompt: "initial-prompt"});
  });

  await t.test("accepted profile refresh preserves old draft baseline until explicit merge", async () => {
    const get = fakeDocument();
    let current = profile("old-system", "old-prompt", "Old", "Old prompt");
    const posts = [];
    globalThis.fetch = async (path, options = {}) => {
      assert.equal(path, "/api/profile");
      if (!options.body) return reply({ok: true, profile: current});
      const payload = JSON.parse(options.body);
      posts.push(payload);
      current = profile("saved-system", "saved-prompt", payload.system.name, payload.prompt);
      return reply({ok: true, profile: current});
    };
    initSystem();
    await refreshProfile();
    assert.equal(get("sys-name").value, "Old");
    get("sys-entry").value = "My unfinished rule";
    get("sys-prompt").value = "My unfinished prompt";
    await get("sys-entry").emit("input");
    await get("sys-prompt").emit("input");
    current = profile("accepted-system", "accepted-prompt", "Accepted", "Accepted prompt");
    await refreshProfile(false);
    assert.equal(get("sys-entry").value, "My unfinished rule");
    assert.equal(get("sys-prompt").value, "My unfinished prompt");
    assert.equal(get("profile-recovery").hidden, false);
    assert.equal(get("profile-save").disabled, true);
    await get("profile-form").emit("submit");
    assert.equal(posts.length, 0);
    await get("profile-confirm-merge").emit("click");
    assert.equal(get("profile-save").disabled, false);
    await get("profile-form").emit("submit");
    assert.deepEqual(posts[0].expected, {system: "accepted-system", prompt: "accepted-prompt"});
    assert.equal(posts[0].system.entry, "My unfinished rule");
    get("sys-entry").value = "Another draft";
    await get("sys-entry").emit("input");
    current = profile("latest-system", "latest-prompt", "Latest", "Latest prompt");
    await refreshProfile(false);
    await get("profile-load-current").emit("click");
    assert.equal(get("sys-name").value, "Latest");
    assert.equal(get("sys-prompt").value, "Latest prompt");
    assert.equal(get("profile-save").disabled, false);
  });
});


test("native journal local detail needs no model and source changes discard pending UI results", async () => {
  const {initJournal, updateJournalStatus, refreshTrades} = await import("../companion/static/journal.js");
  const get = fakeDocument();
  get("source").value = "trade";
  initJournal(() => {});
  updateJournalStatus({connected: false});
  updateJournalStatus({connected: true, origin_key: "origin-a"});
  const item = {trade_id: "a".repeat(64), native_id: "7", instrument: "BTCUSDT@BinanceFutures",
    is_complete: true, side: null, open_time: {iso: "2026-11-09T10:00:00", kind: "Unspecified"},
    close_time: {iso: "2026-11-09T10:01:00", kind: "Unspecified"}, open_price: "12345.123456789012345678",
    close_price: "12346.123456789012345678", open_volume: "0.000000000000000123", close_volume: "0.000000000000000100",
    pnl: "-0.00000000000000000123", commission: null};
  const listed = {data: {simulation_only: true, origin_key: "origin-a", trades: [item], returned: 1,
    coverage: {from: item.open_time, to: item.close_time, incomplete_rows: 0}}};
  const requests = [];
  globalThis.fetch = async path => {
    requests.push(path);
    if (path === "/api/trades") return reply(listed);
    assert.equal(path, `/api/trades/${item.trade_id}/evidence`);
    return reply({evidence: {source: "trade", quality: ["native_timezone_unknown"], evidence: {pnl: item.pnl}}});
  };
  await refreshTrades();
  assert.equal(get("trade-id").children.length, 1);
  const row = get("trade-list").children.find(child => child.tag === "article");
  const text = row.children[0].children.map(child => child.textContent).join(" ");
  assert.match(text, /12345\.123456789012345678/);
  assert.match(text, /-0\.00000000000000000123/);
  assert.match(text, /手续费 缺失/);
  assert.match(text, /Unspecified/);
  await row.children[1].children[0].emit("click");
  assert.match(get("trade-evidence").textContent, /native_timezone_unknown/);
  assert.equal(requests.length, 2);
  assert.equal(requests.some(path => /preview|analyze|settings/.test(path)), false);
  let release;
  globalThis.fetch = async () => new Promise(resolve => { release = resolve; });
  const pending = refreshTrades();
  updateJournalStatus({connected: true, origin_key: "origin-b"});
  release(reply(listed)); await pending;
  assert.equal(get("trade-id").children.length, 0);
  assert.equal(get("trade-list").children.length, 0);
  assert.equal(get("trade-evidence").textContent, "");
  assert.equal(get("preview-button").disabled, true);
});
