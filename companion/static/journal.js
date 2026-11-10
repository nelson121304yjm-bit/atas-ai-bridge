"use strict";
import {api, byId, errorText, node, pretty, setText, time} from "./ui.js";
import {invalidateTradeSelection, selectTrade, setTradeAvailability} from "./analysis.js";

let connected = false;
let origin = null;
let generation = 0;
let openAnalysis = () => {};
let detailGeneration = 0;

function revealEvidence(content) {
  const result = byId("trade-evidence");
  result.textContent = content;
  result.scrollIntoView({block: "start"});
  result.focus({preventScroll: true});
}

export function updateJournalStatus(status) {
  const next = status?.origin_key || null;
  const changed = next !== origin || connected !== (status?.connected === true);
  connected = status?.connected === true;
  if (changed) {
    generation++;
    origin = next;
    byId("trade-list").replaceChildren();
    byId("trade-id").replaceChildren();
    byId("trade-evidence").textContent = "";
    invalidateTradeSelection();
    setTradeAvailability(connected, 0);
  }
  const options = status?.account_options || [];
  const chooser = byId("journal-account");
  const draftAccount = chooser.value;
  chooser.replaceChildren();
  const placeholder = node("option", "", "请选择本机模拟账户"); placeholder.value = ""; chooser.append(placeholder);
  for (const item of options) { const option = node("option", "", item.label); option.value = item.key; chooser.append(option); }
  chooser.value = options.some(item => item.key === draftAccount) ? draftAccount : (status?.selected_account_key || "");
  byId("journal-account-form").hidden = options.length < 2 && !status?.selection_required;
  const reasons = {
    native_account_selection_required: "检测到多个模拟账户，请在此明确选择一个账户。",
    native_account_changed: "所选账户或文件来源已变化，请重新明确选择；没有切换到其他账户。",
    native_accounts_empty: "没有明确标记为模拟且含 BTCUSDT 原生日志的账户。请核对本机已保存的日志。",
    native_files_missing: "本机 ATAS Database 的三个日志文件暂不可读。请核对本机数据库并刷新，无需添加图表指标。",
  };
  setText("journal-state", connected
    ? "ATAS 本机落盘模拟日志 · 独立于图表和行情桥。历史配对行可能是部分平仓，不代表当前账户或持仓状态。"
    : (reasons[status?.reason] || `本机日志暂不可用（${status?.reason || "未配置"}），请刷新本机来源状态。`));
  return changed;
}

export async function refreshTrades() {
  const list = byId("trade-list");
  if (!connected) {
    try { updateJournalStatus((await api("/api/journal/accounts")).journal); }
    catch (error) { list.textContent = errorText(error); }
    if (!connected) { setTradeAvailability(false, 0); return; }
  }
  const request = ++generation;
  invalidateTradeSelection();
  setTradeAvailability(true, 0);
  byId("trade-id").replaceChildren();
  byId("trade-evidence").textContent = "";
  list.textContent = "正在读取 ATAS 原生日志…";
  try {
    const response = await api("/api/trades");
    if (request !== generation) return;
    const data = response.data || {};
    if (data.simulation_only !== true || (origin && data.origin_key !== origin)) throw {message: "日志来源已变化，请刷新连接状态。"};
    const trades = data.trades || [];
    list.replaceChildren();
    const choices = byId("trade-id"); choices.replaceChildren();
    let complete = 0;
    const coverage = data.coverage || {};
    list.append(node("p", "help", `范围：${coverage.from?.iso || "未知"} 至 ${coverage.to?.iso || "未知"}（本机墙钟范围，时区不推断）；返回 ${data.returned ?? trades.length} 条${coverage.truncated ? " · 已截断" : ""}；未完成 ${coverage.incomplete_rows ?? "未知"} 条。`));
    if (coverage.file_modified_at) list.append(node("p", "help", `本机文件更新时间：${Object.entries(coverage.file_modified_at).map(([name, value]) => `${name} ${value}`).join(" · ")}。本地快照，不保证服务器历史完整或实时同步。`));
    if (!trades.length) list.append(node("p", "help", "目前没有原生配对记录。"));
    for (const trade of trades) {
      const label = `${trade.instrument || "BTCUSDT"} · 原生配对记录 ${trade.native_id || trade.trade_id}`;
      const eligible = trade.is_complete === true || (trade.status === "closed" && Number.isInteger(trade.closed_at_ms));
      if (eligible) { const option = node("option", "", label); option.value = trade.trade_id; choices.append(option); complete++; }
      const row = node("article", "trade-row");
      const info = node("div");
      const rawTime = value => value ? `${value.iso || "缺失"} (${value.kind || "未知"})` : "缺失";
      info.append(node("strong", "", label), node("small", "", `${eligible ? "原生配对完整" : "未完成"} · ${trade.side || "开仓方向缺失"}`),
        node("small", "", `开仓 ${rawTime(trade.open_time)} · 价格 ${trade.open_price ?? "未知"} · 原生有符号数量 ${trade.open_volume ?? "未知"}`),
        node("small", "", `平仓 ${rawTime(trade.close_time)} · 价格 ${trade.close_price ?? "未知"} · 原生有符号数量 ${trade.close_volume ?? "未知"}`),
        node("small", "", `ATAS 原生盈亏 ${trade.pnl ?? "未知"} · 手续费 ${trade.commission ?? "缺失"} · 净收益/币种/单位未推断`));
      const actions = node("div", "actions");
      const detail = node("button", "button secondary", "查看本地证据"); detail.type = "button"; detail.disabled = !eligible;
      detail.addEventListener("click", async () => {
        const detailRequest = ++detailGeneration;
        detail.disabled = true;
        try {
          const response = await api(`/api/trades/${encodeURIComponent(trade.trade_id)}/evidence`);
          if (request === generation && detailRequest === detailGeneration) revealEvidence(pretty(response.evidence));
        } catch (error) { if (request === generation && detailRequest === detailGeneration) revealEvidence(errorText(error)); }
        finally { detail.disabled = !eligible; }
      });
      const button = node("button", "button secondary", "用此记录生成预览"); button.type = "button"; button.disabled = !eligible;
      button.addEventListener("click", () => { if (request === generation) { selectTrade(trade.trade_id); openAnalysis(); } });
      actions.append(detail, button); row.append(info, actions); list.append(row);
    }
    setTradeAvailability(true, complete);
  } catch (error) {
    if (request !== generation) return;
    list.textContent = errorText(error);
    setTradeAvailability(true, 0);
  }
}

export function initJournal(navigateAnalysis) {
  openAnalysis = navigateAnalysis;
  byId("refresh-trades").addEventListener("click", refreshTrades);
  byId("journal-account-form").addEventListener("submit", async event => {
    event.preventDefault();
    const key = byId("journal-account").value;
    if (!key) { setText("journal-state", "请明确选择一个模拟账户。"); return; }
    const selectionRequest = ++generation;
    invalidateTradeSelection();
    byId("trade-id").replaceChildren();
    byId("trade-list").replaceChildren();
    byId("trade-evidence").textContent = "";
    setTradeAvailability(false, 0);
    try {
      const response = await api("/api/journal/account", {account_key: key});
      if (selectionRequest !== generation) return;
      updateJournalStatus(response.journal);
      await refreshTrades();
    } catch (error) {
      if (selectionRequest !== generation) return;
      connected = false;
      setText("journal-state", errorText(error));
    }
  });
}
