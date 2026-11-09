import assert from "node:assert/strict";
import test from "node:test";
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
