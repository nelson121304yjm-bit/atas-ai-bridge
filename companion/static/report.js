"use strict";
import {api, byId, errorText, node, pretty, qualityText, runJob, setText, showNotice, time} from "./ui.js";

let current = null;
let profileApplied = () => {};
const names = {name: "名称", timeframe: "周期", setup: "Setup", entry: "入场", exit: "退出", risk: "风险", goal: "目标"};

function providerText(provider) {
  const requested = provider?.requested_model || "未知";
  const actual = provider?.actual_model || "未由服务返回";
  return `请求模型：${requested} · 实际模型：${actual} · 请求时间：${time(provider?.started_at)}`;
}

function citationText(answer) {
  const citations = answer?.citations || {};
  const valid = citations.valid?.length ? citations.valid.join("、") : "无";
  const unknown = citations.unknown?.length ? citations.unknown.join("、") : "无";
  return `可匹配证据引用：${valid}；未知引用：${unknown}；${citations.missing ? "没有有效证据引用，不能视为已核实。" : "引用仅验证 ID 存在，不保证逐句正确。"}`;
}

function renderFollowups(report) {
  const list = byId("followup-list");
  list.replaceChildren();
  for (const item of report.followups || []) {
    const section = node("section", "proposal");
    section.append(node("strong", "", `${time(item.created_at)} · ${item.question}`));
    section.append(node("pre", "answer", item.answer?.text || "（无文字）"));
    section.append(node("p", "help", `${citationText(item.answer)} 旧证据，无新行情。${providerText(item.provider)}`));
    list.append(section);
  }
  const readOnly = report.demo || (report.followups || []).length >= 6;
  byId("followup-form").hidden = readOnly;
  if (readOnly) list.append(node("p", "help", report.demo ? "本地演示为只读，不能追问。" : "已达到 6 次追问上限。"));
}

function diffText(value) {
  return Object.entries(names).map(([key, label]) => `${label}：${value?.system?.[key] || "（空）"}`).join("\n") +
    `\n\n复盘指令：\n${value?.prompt || "（空）"}`;
}

function renderProposal(item) {
  const section = node("section", "proposal");
  const status = {pending: "待处理", accepted: "已接受", rejected: "已拒绝"}[item.status] || item.status;
  section.append(node("h4", "", `修改提案 · ${status}`));
  section.append(node("p", "help", `${time(item.created_at)} · ${providerText(item.provider)}`));
  section.append(node("p", "", `理由：${item.reason || "（未给出）"}`));
  const diff = node("div", "diff");
  diff.append(node("pre", "", `原版本\n${diffText(item.old)}`));
  diff.append(node("pre", "", `建议版本\n${diffText(item.new)}`));
  section.append(diff);
  if (item.status === "pending") {
    const actions = node("div", "actions");
    const accept = node("button", "button secondary", "接受并生成新版本");
    const reject = node("button", "button danger", "拒绝提案");
    accept.type = reject.type = "button";
    accept.addEventListener("click", () => resolveProposal(item, "accept", section, accept, reject));
    reject.addEventListener("click", () => resolveProposal(item, "reject", section, accept, reject));
    actions.append(accept, reject);
    section.append(actions);
  }
  return section;
}

async function resolveProposal(item, action, section, accept, reject) {
  accept.disabled = reject.disabled = true;
  try {
    const body = action === "accept" ? {expected: item.expected} : {};
    await api(`/api/proposals/${item.id}/${action}`, body);
    if (action === "accept") await profileApplied();
    await openReport(item.report_id);
    showNotice("");
  } catch (error) {
    const message = errorText(error);
    section.append(node("p", "form-message", message));
    showNotice(message);
    accept.disabled = reject.disabled = false;
  }
}

export async function openReport(id) {
  const data = await api(`/api/reports/${id}`);
  const report = data.report;
  current = report;
  byId("report-panel").hidden = false;
  setText("report-title", report.demo ? "本地演示报告" : "分析报告");
  const badge = byId("report-badge");
  badge.textContent = report.demo ? "本地演示 · 非真实数据" : (report.source === "trade" ? "历史模拟交易" : "当前行情采集");
  badge.classList.toggle("demo", !!report.demo);
  setText("report-meta", `${time(report.created_at)} · ${report.question} · ${providerText(report.provider)} · 系统版 ${report.profile?.system?.id || "未知"} · 指令版 ${report.profile?.prompt?.id || "未知"}`);
  byId("report-quality").replaceChildren(node("strong", "", "证据质量、采集时间与固定边界"), node("pre", "payload", qualityText(report)));
  setText("report-answer", report.answer?.text || "（无文字）");
  setText("report-citations", citationText(report.answer));
  setText("report-evidence", pretty({evidence: report.evidence, request: report.request}));
  renderFollowups(report);
  byId("propose-button").disabled = !!report.demo;
  const proposals = byId("proposal-list");
  proposals.replaceChildren();
  if (report.demo) proposals.append(node("p", "help", "本地演示为只读，不能生成规则提案。"));
  else if (!data.proposals?.length) proposals.append(node("p", "help", "尚无提案。"));
  else data.proposals.forEach(item => proposals.append(renderProposal(item)));
  return report;
}

export function initReport(onProfileApplied) {
  profileApplied = onProfileApplied;
  byId("followup-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!current || current.demo) return;
    const question = byId("followup-question").value.trim();
    if (!question) return;
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      const response = await api(`/api/reports/${current.id}/followup`, {question});
      await runJob(response.job, async job => { await openReport(job.result.report_id); byId("followup-question").value = ""; });
      showNotice("");
    } catch (error) { showNotice(errorText(error)); }
    finally { button.disabled = !!current?.demo || (current?.followups?.length || 0) >= 6; }
  });
  byId("propose-button").addEventListener("click", async event => {
    if (!current || current.demo) return;
    const button = event.currentTarget;
    const reportId = current.id;
    button.disabled = true;
    try {
      const response = await api(`/api/reports/${reportId}/propose`, {});
      await runJob(response.job, async () => { await openReport(reportId); });
      showNotice("");
    } catch (error) { showNotice(errorText(error)); }
    finally { button.disabled = !!current?.demo; }
  });
}

export async function recoverReportJob(job) {
  if (job.kind === "analysis" || job.kind === "followup") await openReport(job.result.report_id);
  if (job.kind === "proposal") await openReport(job.result.proposal.report_id);
}
