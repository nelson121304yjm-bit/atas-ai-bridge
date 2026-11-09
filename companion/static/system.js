"use strict";
import {api, byId, errorText, node, pretty, setText, showNotice, time} from "./ui.js";

const fields = ["name", "timeframe", "setup", "entry", "exit", "risk", "goal"];
let profile = null;

function readForm() {
  const system = {};
  for (const field of fields) system[field] = byId(`sys-${field}`).value;
  return {system, prompt: byId("sys-prompt").value};
}

function fillForm(value) {
  for (const field of fields) byId(`sys-${field}`).value = value.system?.data?.[field] || "";
  byId("sys-prompt").value = value.prompt?.data || "";
}

function renderHistory(value) {
  const list = byId("version-list"); list.replaceChildren();
  const history = [...(value.history || [])].reverse();
  if (!history.length) { list.append(node("p", "help", "还没有版本。")); return; }
  for (const item of history) {
    const details = node("details", "history-item");
    details.append(node("summary", "", `${time(item.system?.created_at)} · ${item.system?.data?.name || "未命名系统"}`));
    details.append(node("small", "", `系统 ${item.system?.id || "未知"} · 指令 ${item.prompt?.id || "未知"}`));
    details.append(node("pre", "payload", pretty({system: item.system?.data, prompt: item.prompt?.data})));
    list.append(details);
  }
}

export async function refreshProfile(fill = true) {
  const response = await api("/api/profile");
  profile = response.profile;
  if (fill) fillForm(profile);
  renderHistory(profile);
  return profile;
}

export function initSystem() {
  byId("profile-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button"); button.disabled = true;
    try {
      const value = readForm();
      const expected = {system: profile.system.id, prompt: profile.prompt.id};
      const response = await api("/api/profile", {...value, expected});
      profile = response.profile;
      renderHistory(profile);
      setText("profile-message", `新版本已保存：${time(profile.system.created_at)}`);
      showNotice("");
    } catch (error) {
      const message = errorText(error);
      setText("profile-message", message);
      showNotice(message);
      if (error.code === "version_conflict") {
        // Preserve unsaved form edits for manual merging.
        try { await refreshProfile(false); } catch { /* Keep the conflict message. */ }
      }
    } finally { button.disabled = false; }
  });
}
