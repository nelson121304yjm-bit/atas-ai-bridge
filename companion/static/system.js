"use strict";
import {api, byId, errorText, node, pretty, setText, showNotice, time} from "./ui.js";

const fields = ["name", "timeframe", "setup", "entry", "exit", "risk", "goal"];
let profile = null;
let draftBase = null;
let draftDirty = false;
let recovery = null;
let recoveryMessage = null;
let mergeButton = null;
let saveButton = null;

const versionIds = value => ({system: value.system.id, prompt: value.prompt.id});

function currentVersionChanged() {
  return !!profile && !!draftBase &&
    (draftBase.system !== profile.system.id || draftBase.prompt !== profile.prompt.id);
}

function updateRecovery() {
  const changed = currentVersionChanged();
  recovery.hidden = !changed;
  saveButton.disabled = !profile || changed;
  if (changed) {
    recoveryMessage.textContent = draftDirty
      ? "当前版本已变化。未保存草稿仍基于旧版本；请展开版本记录核对并手动合并，或明确载入当前版本。"
      : "当前版本已变化。表单仍显示旧版本；请明确载入当前版本。";
    mergeButton.hidden = !draftDirty;
    setText("profile-message", recoveryMessage.textContent);
  }
}

function readForm() {
  const system = {};
  for (const field of fields) system[field] = byId(`sys-${field}`).value;
  return {system, prompt: byId("sys-prompt").value};
}

function fillForm(value) {
  for (const field of fields) byId(`sys-${field}`).value = value.system?.data?.[field] || "";
  byId("sys-prompt").value = value.prompt?.data || "";
  draftBase = versionIds(value);
  draftDirty = false;
  updateRecovery();
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
  if (fill && !draftDirty) fillForm(profile);
  else if (!draftBase) draftBase = versionIds(profile);
  renderHistory(profile);
  updateRecovery();
  return profile;
}

export function initSystem() {
  const form = byId("profile-form");
  saveButton = form.querySelector("button[type=submit]");
  saveButton.disabled = true;
  recovery = node("div", "callout"); recovery.hidden = true;
  recovery.id = "profile-recovery";
  recoveryMessage = node("p", "");
  recoveryMessage.id = "profile-recovery-message";
  recoveryMessage.setAttribute("role", "status");
  const actions = node("div", "actions");
  const loadButton = node("button", "button quiet", "载入当前版本（替换草稿）"); loadButton.type = "button";
  mergeButton = node("button", "button secondary", "确认已手动合并，以当前版本为基准"); mergeButton.type = "button";
  loadButton.id = "profile-load-current";
  mergeButton.id = "profile-confirm-merge";
  actions.append(loadButton, mergeButton);
  recovery.append(recoveryMessage, actions);
  form.append(recovery);
  for (const field of [...fields.map(name => `sys-${name}`), "sys-prompt"]) {
    byId(field).addEventListener("input", () => { draftDirty = true; updateRecovery(); });
  }
  loadButton.addEventListener("click", () => {
    fillForm(profile);
    setText("profile-message", "已明确载入当前系统与指令版本，原草稿已替换。");
  });
  mergeButton.addEventListener("click", () => {
    draftBase = versionIds(profile);
    updateRecovery();
    setText("profile-message", "已确认手动合并并更新编辑基准。请核对草稿后再保存新版本。");
  });
  byId("profile-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (currentVersionChanged()) { updateRecovery(); return; }
    const button = saveButton; button.disabled = true;
    try {
      const value = readForm();
      const expected = draftBase;
      const response = await api("/api/profile", {...value, expected});
      profile = response.profile;
      draftBase = versionIds(profile);
      draftDirty = JSON.stringify(readForm()) !== JSON.stringify(value);
      renderHistory(profile);
      updateRecovery();
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
    } finally { button.disabled = currentVersionChanged(); }
  });
}
