"use strict";
import {api, byId, errorText, runJob, setText, showNotice} from "./ui.js";

let saved = null;

function targetChanged(event) {
  const address = !!saved && byId("base-url").value.trim() !== saved.base_url;
  if (address && event?.target === byId("base-url")) byId("key").value = "";
  const dirty = address || (!!saved && byId("model").value.trim() !== saved.model);
  byId("address-warning").hidden = !address;
  byId("save-key").disabled = dirty;
  byId("test-key").disabled = dirty || !saved?.key_present;
  byId("delete-key").disabled = dirty || !saved?.key_present;
  if (dirty && !address) setText("settings-message", "模型名称尚未保存。先保存服务目标，再执行 Key 或测试操作。");
  return dirty;
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
  const response = await api("/api/settings");
  render(response.settings);
}

export function initSettings() {
  byId("base-url").addEventListener("input", targetChanged);
  byId("model").addEventListener("input", targetChanged);
  byId("settings-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = event.submitter || event.currentTarget.querySelector("[type=submit]");
    button.disabled = true;
    try {
      const response = await api("/api/settings", values());
      render(response.settings);
      setText("settings-message", "服务目标与保存模式已更新。地址改变时，请重新输入此地址的 Key。");
      showNotice("");
    } catch (error) { setText("settings-message", errorText(error)); showNotice(errorText(error)); }
    finally { button.disabled = false; }
  });
  byId("save-key").addEventListener("click", async event => {
    const key = byId("key").value;
    if (!key) { setText("settings-message", "请先输入 Key。未保存空密钥。"); return; }
    if (targetChanged()) return;
    event.currentTarget.disabled = true;
    try {
      const response = await api("/api/settings", {key, persist_key: byId("persist-key").checked});
      render(response.settings);
      byId("key").value = "";
      setText("settings-message", "当前服务地址的 Key 已保存，输入框已清空。");
      showNotice("");
    } catch (error) {
      setText("settings-message", errorText(error)); showNotice(errorText(error));
    } finally { targetChanged(); }
  });
  byId("delete-key").addEventListener("click", async event => {
    if (targetChanged()) return;
    event.currentTarget.disabled = true;
    try {
      const response = await api("/api/settings", {delete_key: true});
      render(response.settings);
      setText("settings-message", "当前地址的 Key 已删除。"); showNotice("");
    } catch (error) { setText("settings-message", errorText(error)); showNotice(errorText(error)); }
    finally { targetChanged(); }
  });
  byId("test-key").addEventListener("click", async event => {
    if (targetChanged()) return;
    event.currentTarget.disabled = true;
    try {
      const response = await api("/api/settings/test", {});
      await runJob(response.job, async job => {
        const provider = job.result?.provider || {};
        setText("settings-message", `合成文本连接测试完成。请求模型：${provider.requested_model || "未知"}；实际模型：${provider.actual_model || "服务未返回"}。此结果不验证行情或真实分析质量。`);
      }, job => {
        if (job.state === "queued" || job.state === "running")
          setText("settings-message", "正在测试连接；只发送固定合成文本，请等待结果。不会自动重试。");
      });
      showNotice("");
    } catch (error) { setText("settings-message", errorText(error)); showNotice(errorText(error)); }
    finally { targetChanged(); }
  });
}

export function recoveredSettingsTest() {
  setText("settings-message", "合成文本连接测试已完成；未发送交易数据。");
}
