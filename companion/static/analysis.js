"use strict";
import {api, byId, errorText, node, pretty, qualityText, runJob, setText, showNotice, time} from "./ui.js";
import {openReport} from "./report.js";

let preview = null;
let marketEligible = false;
let journalConnected = false;
let closedTradeCount = 0;

function sourceChanged() {
  const isTrade = byId("source").value === "trade";
  byId("trade-choice").hidden = !isTrade;
  const enabled = isTrade ? journalConnected && closedTradeCount > 0 : marketEligible;
  byId("preview-button").disabled = !enabled;
  byId("preview-button").title = enabled ? "" : isTrade ? "日志无可用的已平仓模拟交易" : "ATAS 行情连接或模拟账户资格未通过";
}

export function setTradeAvailability(connected, count) {
  journalConnected = connected;
  closedTradeCount = count;
  sourceChanged();
}

export function selectTrade(id) {
  byId("source").value = "trade";
  byId("trade-id").value = id;
  sourceChanged();
}

export function updateStatus(status) {
  const market = status.market || {};
  marketEligible = market.eligible === true;
  setText("service", status.configured ? "本机服务已连接 · 模型地址已配置（未必已验证）" : "本机服务已连接 · 待配置模型地址");
  setText("market-status", !market.connected ? "未连接" : marketEligible ? "已连接 · 可采集" : "已连接 · 不可采集");
  setText("market-detail", `${market.bridge_version || "桥版本未知"}${market.reason ? ` · ${market.reason}` : " · 只读"}`);
  setText("market-identity", marketEligible ? `${market.instrument || market.security_id} · ${market.account || "模拟账户"}` : "模拟账户未确认");
  const age = market.freshness;
  setText("market-age", marketEligible ? `${market.chart_type || "图表类型未知"} / ${market.time_frame || "周期未知"} · 快照 ${age?.snapshotAgeMs ?? "?"} ms · 行情事件 ${age?.marketEventAgeMs ?? "?"} ms` : "需检查 ATAS 图表、账户与数据新鲜度");
  const journal = status.journal || {};
  setText("journal-status", journal.connected ? "已连接 · 只读" : "未连接");
  setText("journal-detail", journal.connected ? "仅模拟交易" : (journal.reason || "未配置"));
  journalConnected = journal.connected === true;
  sourceChanged();
}

function renderPreview(value) {
  preview = value;
  byId("preview-panel").hidden = false;
  setText("preview-badge", value.submission ? "已提交" : "未发送");
  setText("preview-target", `接收地址：${value.target?.endpoint || "未配置"} · 请求模型：${value.target?.model || "未配置"}`);
  const facts = byId("preview-meta");
  facts.replaceChildren();
  const items = [
    ["证据来源", value.source === "trade" ? "已平仓模拟交易" : "当前行情"],
    ["采集时间", time(value.evidence?.captured_at)],
    ["请求大小", `${value.content_bytes} 字节`],
    ["系统 / 指令版本", `${value.profile?.system?.id || "未知"} / ${value.profile?.prompt?.id || "未知"}`],
  ];
  for (const [label, val] of items) {
    const row = node("div"); row.append(node("dt", "", label), node("dd", "", val)); facts.append(row);
  }
  byId("preview-quality").replaceChildren(node("pre", "payload", qualityText(value)));
  setText("preview-payload", pretty({evidence: value.evidence, request: value.request}));
  const targetReady = !!value.target?.endpoint && !!value.target?.model;
  byId("analyze-button").disabled = !!value.submission || !targetReady;
  byId("analyze-button").title = targetReady ? "" : "先在模型设置中配置服务目标，再生成新的预览";
}

export async function refreshReports() {
  const list = byId("report-list");
  try {
    const response = await api("/api/reports");
    list.replaceChildren();
    if (!response.reports?.length) { list.append(node("p", "help", "还没有报告。先生成发送预览，或打开本地演示。")); return; }
    for (const item of response.reports) {
      const button = node("button", "history-item"); button.type = "button";
      const row = node("span", "row");
      row.append(node("strong", "", item.question || "未命名分析"));
      if (item.demo) row.append(node("span", "badge demo", "本地演示"));
      button.append(row, node("small", "", `${time(item.created_at)} · ${item.source === "trade" ? "交易日志" : "当前行情"} · 请求模型 ${item.provider?.requested_model || "未知"}`));
      button.addEventListener("click", async () => {
        try { await openReport(item.id); showNotice(""); }
        catch (error) { showNotice(errorText(error)); }
      });
      list.append(button);
    }
  } catch (error) { list.textContent = errorText(error); }
}

export function initAnalysis() {
  byId("source").addEventListener("change", sourceChanged);
  byId("refresh-reports").addEventListener("click", refreshReports);
  byId("preview-form").addEventListener("submit", async event => {
    event.preventDefault();
    const button = byId("preview-button"); button.disabled = true;
    const source = byId("source").value;
    const payload = {source, question: byId("question").value.trim()};
    if (source === "trade") payload.trade_id = byId("trade-id").value;
    try {
      const response = await api("/api/preview", payload);
      renderPreview(response.preview);
      showNotice("");
    } catch (error) { showNotice(errorText(error)); }
    finally { sourceChanged(); }
  });
  byId("analyze-button").addEventListener("click", async () => {
    if (!preview || preview.submission || !preview.target?.endpoint || !preview.target?.model) return;
    byId("analyze-button").disabled = true;
    try {
      const response = await api("/api/analyze", {preview_id: preview.id});
      preview.submission = {consumed: true, job_id: response.job.id};
      setText("preview-badge", "已提交");
      await runJob(response.job, async job => { await openReport(job.result.report_id); await refreshReports(); });
      showNotice("");
    } catch (error) {
      showNotice(errorText(error));
      if (!preview.submission) byId("analyze-button").disabled = !preview.target?.endpoint || !preview.target?.model;
    }
  });
  byId("demo-button").addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    try {
      const response = await api("/api/demo", {});
      await openReport(response.report.id);
      await refreshReports(); showNotice("");
    } catch (error) { showNotice(errorText(error)); }
    finally { event.currentTarget.disabled = false; }
  });
  sourceChanged();
}
