"use strict";

export const byId = (id) => document.getElementById(id);
export const setText = (id, value) => { byId(id).textContent = value ?? "—"; };
export const pretty = (value) => JSON.stringify(value, null, 2);
export const time = (value) => value ? new Date(value).toLocaleString("zh-CN") : "未知";
export const node = (tag, className, content) => {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = String(content);
  return element;
};

let csrf = null;
let activeJob = null;
const messages = {
  provider_not_configured: "请先在模型设置中保存服务地址和模型，然后重新生成预览。",
  key_required: "当前服务地址没有可用 Key。请在模型设置中单独保存 Key。",
  provider_auth: "认证失败。请核对当前服务地址的 Key。",
  provider_quota: "模型额度不足。请检查服务商账户。",
  provider_rate_limited: "模型请求被限流。稍后由你手动重试。",
  provider_unavailable: "模型服务不可用。请核对地址与网络。",
  provider_redirect: "服务尝试重定向，请检查 Base URL。",
  provider_http_error: "模型服务返回错误；没有自动重试。请检查服务端状态。",
  provider_response_too_large: "模型响应超出大小限制；没有保存报告。",
  provider_status: "模型服务状态异常；没有自动重试。",
  invalid_provider_response: "模型返回内容无效，请检查兼容协议。",
  invalid_proposal: "模型给出的修改提案格式无效，当前系统没有变化。",
  preview_consumed: "这份预览已经提交过。请查看报告历史；需要重试时重新生成预览。",
  preview_invalidated: "服务目标或模型已变化，请重新生成发送预览。",
  bridge_unsafe_status: "ATAS 图表、账户或只读状态无法核实；请使用受支持的模拟账户图表。",
  bridge_stale: "ATAS 行情已过期或新鲜度未知，请等待实时数据后重新生成预览。",
  bridge_chart_metadata_missing: "图表类型或周期信息缺失，请更新 Mac 桥。",
  journal_disconnected: "交易日志未连接。可继续进行行情分析。",
  journal_open_trade: "只能复盘已平仓的模拟交易。",
  evidence_size: "证据超出大小限制，未发送模型。",
  provider_changed: "服务目标或模型已变化。可恢复原设置，或生成新的预览。",
  provider_busy: "已有模型任务正在执行。完成后再发起新任务。",
  job_missing: "任务状态已不可查询。请检查报告历史或提案列表，不要重复提交同一预览。",
  version_conflict: "当前系统已有新版本。请重新查看当前版本并手动合并差异；旧报告不能直接重新生成提案来覆盖新版本。",
  followup_limit: "这份报告已达到 6 次追问上限。请基于新数据生成预览。",
  proposal_resolved: "这份提案已经处理。请刷新报告查看当前状态。",
  journal_config: "交易日志配置无效。行情分析仍可使用，请检查本机安装参数。",
  demo_read_only: "演示报告不可追问或生成规则提案。",
  request_too_large: "请求超出大小限制。未发送模型，请缩短问题或重新生成预览。",
  unauthorized: "会话已过期，请从 ATAS 图表或启动器重新打开工作台。",
  csrf_required: "会话验证失败，请重新从启动器打开工作台。",
};

export function errorText(error) {
  return messages[error?.code] || error?.message || "操作失败，请检查本机服务。";
}

export function showNotice(message) {
  const target = byId("notice");
  target.textContent = message;
  target.hidden = !message;
}

export async function connectSession() {
  const token = window.reviewBootstrap?.token;
  if (window.reviewBootstrap) window.reviewBootstrap.token = null;
  const response = token
    ? await fetch("/api/bootstrap", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({token})})
    : await fetch("/api/session");
  const data = await response.json();
  if (!response.ok || !data.csrf) throw data.error || {code: "unauthorized"};
  csrf = data.csrf;
}

export async function api(path, payload) {
  const options = payload === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf},
    body: JSON.stringify(payload),
  };
  let response;
  try { response = await fetch(path, options); }
  catch { throw {code: "network", message: "无法连接本机服务。"}; }
  let data;
  try { data = await response.json(); }
  catch { throw {code: "invalid_response", message: "本机服务响应无效。"}; }
  if (!response.ok || data.ok === false) throw data.error || {message: "请求失败。"};
  return data;
}

export function qualityText(record) {
  return pretty({quality: record?.quality ?? null, limits: record?.evidence?.limits ?? null,
    captured_at: record?.evidence?.captured_at ?? null,
    execution_rules: record?.execution_rules ?? null});
}

function jobView(job) {
  byId("job-panel").hidden = false;
  const kind = {analysis: "分析", settings_test: "合成连接测试", followup: "追问", proposal: "提案"}[job.kind] || "任务";
  const state = {queued: "等待执行", running: "正在执行", succeeded: "已完成", failed: "失败"}[job.state] || job.state;
  setText("job-status", `${kind}：${state}`);
  setText("job-help", job.state === "failed"
    ? `${errorText(job.error)} 未自动重试；请确认结果后由你决定下一步。`
    : "模型任务可能计费。刷新页面后会尝试恢复任务状态；若任务已丢失，请查看报告历史。");
}

export async function runJob(initial, onSuccess, onProgress = () => {}) {
  if (activeJob) throw {code: "provider_busy"};
  activeJob = initial.id;
  const buttons = [...document.querySelectorAll("[data-job-action]")];
  const priorDisabled = buttons.map(button => button.disabled);
  buttons.forEach(button => { button.disabled = true; });
  try {
    let job = initial;
    while (true) {
      jobView(job);
      onProgress(job);
      if (job.state === "succeeded") { await onSuccess(job); return; }
      if (job.state === "failed") throw job.error || {code: "job_failed"};
      await new Promise(resolve => setTimeout(resolve, 1200));
      try { job = (await api(`/api/jobs/${job.id}`)).job; }
      catch (error) {
        if (error.code === "not_found") {
          setText("job-help", "任务状态已不可查询。请检查报告历史或提案列表；不要重复提交同一预览。");
          throw {code: "job_missing"};
        }
        throw error;
      }
    }
  } finally {
    activeJob = null;
    buttons.forEach((button, index) => { button.disabled = priorDisabled[index]; });
  }
}

export const jobIsActive = () => activeJob !== null;
