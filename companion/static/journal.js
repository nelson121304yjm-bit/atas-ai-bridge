"use strict";
import {api, byId, errorText, node, pretty, setText, time} from "./ui.js";
import {invalidateTradeSelection, selectTrade, setTradeAvailability} from "./analysis.js";

let connected = false;
let origin = null;
let generation = 0;
let openAnalysis = () => {};

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
  setText("journal-state", connected
    ? "ATAS 原生模拟交易日志 · 同一图表桥。原生配对记录可能是部分平仓，不代表整个持仓已结束。"
    : `交易日志未连接（${status?.reason || "未配置"}）。行情分析仍可使用。`);
  return changed;
}

export async function refreshTrades() {
  const list = byId("trade-list");
  if (!connected) { setTradeAvailability(false, 0); return; }
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
    list.append(node("p", "help", `范围：${coverage.from?.iso || "未知"} 至 ${coverage.to?.iso || "未知"}（宿主时间，时区不推断）；返回 ${data.returned ?? trades.length} 条${coverage.truncated ? " · 已截断" : ""}；未完成 ${coverage.incomplete_rows ?? "未知"} 条。`));
    if (!trades.length) list.append(node("p", "help", "目前没有原生配对记录。"));
    for (const trade of trades) {
      const label = `${trade.instrument || "BTCUSDT"} · 原生配对记录 ${trade.native_id || trade.trade_id}`;
      const eligible = trade.is_complete === true || (trade.status === "closed" && Number.isInteger(trade.closed_at_ms));
      if (eligible) { const option = node("option", "", label); option.value = trade.trade_id; choices.append(option); complete++; }
      const row = node("article", "trade-row");
      const info = node("div");
      const rawTime = value => value ? `${value.iso || "缺失"} (${value.kind || "未知"})` : "缺失";
      info.append(node("strong", "", label), node("small", "", `${eligible ? "原生配对完整" : "未完成"} · ${trade.side || "开仓方向缺失"}`),
        node("small", "", `开仓 ${rawTime(trade.open_time)} · 价格 ${trade.open_price ?? "未知"} · 数量 ${trade.open_volume ?? "未知"}`),
        node("small", "", `平仓 ${rawTime(trade.close_time)} · 价格 ${trade.close_price ?? "未知"} · 数量 ${trade.close_volume ?? "未知"}`),
        node("small", "", `ATAS 原生盈亏 ${trade.pnl ?? "未知"} · 手续费 ${trade.commission ?? "缺失"} · 净收益/币种/单位未推断`));
      const actions = node("div", "actions");
      const detail = node("button", "button secondary", "查看本地证据"); detail.type = "button"; detail.disabled = !eligible;
      detail.addEventListener("click", async () => {
        detail.disabled = true;
        try {
          const response = await api(`/api/trades/${encodeURIComponent(trade.trade_id)}/evidence`);
          if (request === generation) setText("trade-evidence", pretty(response.evidence));
        } catch (error) { if (request === generation) setText("trade-evidence", errorText(error)); }
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
}
