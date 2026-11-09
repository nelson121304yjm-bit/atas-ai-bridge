"use strict";
import {api, byId, errorText, jobIsActive, runJob, setText, showNotice} from "./ui.js";

let saved = null;
let loading = false;
let busy = false;
let conflicted = false;
let needsConfirmation = false;

function unavailable() { return !saved || loading || busy || conflicted || needsConfirmation; }

function expected() {
  return {base_url: saved.base_url, model: saved.model, persist_key: saved.persist_key};
}

function failed(error) {
  if (error?.code === "settings_conflict") conflicted = true;
  setText("settings-message", errorText(error));
  showNotice(errorText(error));
}

function targetChanged(event) {
  const address = !!saved && byId("base-url").value.trim() !== saved.base_url;
  if (address && event?.target === byId("base-url")) byId("key").value = "";
  const dirty = address || (!!saved && byId("model").value.trim() !== saved.model);
  const blocked = unavailable();
  for (const id of ["base-url", "model", "key", "persist-key"])
    byId(id).disabled = !saved || loading || busy;
  byId("settings-submit").disabled = blocked;
  byId("settings-refresh").disabled = loading || busy;
  byId("settings-confirm").hidden = !needsConfirmation;
  byId("settings-confirm").disabled = loading || busy || conflicted;
  byId("address-warning").hidden = !address;
  byId("save-key").disabled = blocked || dirty;
  byId("test-key").disabled = blocked || dirty || !saved?.key_present || jobIsActive();
  byId("delete-key").disabled = blocked || dirty || !saved?.key_present;
  if (!blocked && dirty && !address) setText("settings-message", "模型名称尚未保存。先保存服务目标，再执行 Key 或测试操作。");
  return blocked || dirty;
}

function render(value, fill = true) {
  saved = value;
  if (fill) {
    byId("base-url").value = value.base_url || "";
    byId("model").value = value.model || "";
    byId("persist-key").checked = !!value.persist_key;
  }
  setText("key-state", value.key_present
    ? `当前地址已有 Key（${value.persist_key ? "macOS Keychain" : "本次后台会话"}）；密钥不会回显。`
    : "当前地址没有 Key。保存地址后单独输入并保存。密钥不会回显。");
  targetChanged();
}

function values() {
  return {base_url: byId("base-url").value.trim(), model: byId("model").value.trim(),
    persist_key: byId("persist-key").checked};
}

export async function refreshSettings() {
  if (loading || busy) return;
  loading = true;
  targetChanged();
  try {
    const response = await api("/api/settings");
    const changed = saved && (saved.base_url !== response.settings.base_url || saved.model !== response.settings.model);
    needsConfirmation = needsConfirmation || conflicted || (!!byId("key").value && !!changed);
    conflicted = false;
    render(response.settings);
    setText("settings-message", needsConfirmation
      ? "已载入最新服务目标，未保存的 Key 仍保留。请核对上方地址、模型和保存模式，并确认此 Key 属于该地址后继续。"
      : "模型设置已载入。Key 不会回显。");
  } catch (error) {
    conflicted = true;
    failed(error);
    throw error;
  } finally { loading = false; targetChanged(); }
}

export function initSettings() {
  targetChanged();
  byId("settings-refresh").addEventListener("click", () => refreshSettings().catch(() => {}));
  byId("settings-confirm").addEventListener("click", () => {
    if (!saved || loading || busy || conflicted || !needsConfirmation) return;
    needsConfirmation = false;
    setText("settings-message", "已确认当前服务目标。请按需保存 Key 或手动测试；没有自动发送。");
    showNotice("");
    targetChanged();
  });
  byId("base-url").addEventListener("input", targetChanged);
  byId("model").addEventListener("input", targetChanged);
  byId("settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (unavailable()) return;
    const payload = {...values(), expected: expected()};
    busy = true;
    targetChanged();
    try {
      const response = await api("/api/settings", payload);
      render(response.settings);
      setText("settings-message", "服务目标与保存模式已更新。地址改变时，请重新输入此地址的 Key。");
      showNotice("");
    } catch (error) { failed(error); }
    finally { busy = false; targetChanged(); }
  });
  byId("save-key").addEventListener("click", async event => {
    if (targetChanged()) return;
    const key = byId("key").value;
    if (!key) { setText("settings-message", "请先输入 Key。未保存空密钥。"); return; }
    const payload = {key, persist_key: byId("persist-key").checked, expected: expected()};
    busy = true;
    targetChanged();
    try {
      const response = await api("/api/settings", payload);
      render(response.settings);
      byId("key").value = "";
      setText("settings-message", "当前服务地址的 Key 已保存，输入框已清空。");
      showNotice("");
    } catch (error) {
      failed(error);
    } finally { busy = false; targetChanged(); }
  });
  byId("delete-key").addEventListener("click", async event => {
    if (targetChanged()) return;
    const payload = {delete_key: true, expected: expected()};
    busy = true;
    targetChanged();
    try {
      const response = await api("/api/settings", payload);
      render(response.settings);
      setText("settings-message", "当前地址的 Key 已删除。"); showNotice("");
    } catch (error) { failed(error); }
    finally { busy = false; targetChanged(); }
  });
  byId("test-key").addEventListener("click", async event => {
    if (targetChanged() || jobIsActive()) return;
    const payload = {expected: expected()};
    busy = true;
    targetChanged();
    try {
      const response = await api("/api/settings/test", payload);
      await runJob(response.job, async job => {
        const provider = job.result?.provider || {};
        setText("settings-message", `合成文本连接测试完成。请求模型：${provider.requested_model || "未知"}；实际模型：${provider.actual_model || "服务未返回"}。此结果不验证行情或真实分析质量。`);
      }, job => {
        if (job.state === "queued" || job.state === "running")
          setText("settings-message", "正在测试连接；只发送固定合成文本，请等待结果。不会自动重试。");
      });
      showNotice("");
    } catch (error) { failed(error); }
    finally { busy = false; targetChanged(); }
  });
}

export function recoveredSettingsTest() {
  setText("settings-message", "合成文本连接测试已完成；未发送交易数据。");
}
