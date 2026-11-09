"use strict";
import {api, byId, errorText, node, setText, time} from "./ui.js";
import {selectTrade, setTradeAvailability} from "./analysis.js";

let connected = false;
let openAnalysis = () => {};

export function updateJournalStatus(status) {
  connected = status?.connected === true;
  setText("journal-state", connected
    ? "模拟交易日志已连接。列表只包含配置账户和 BTCUSDT 标的；分析仅接受已平仓交易。"
    : `交易日志未连接（${status?.reason || "未配置"}）。行情分析仍可使用。`);
  if (!connected) {
    byId("trade-list").replaceChildren();
    byId("trade-id").replaceChildren();
    setTradeAvailability(false, 0);
  }
}

export async function refreshTrades() {
  const list = byId("trade-list");
  if (!connected) { setTradeAvailability(false, 0); return; }
  list.textContent = "正在读取本机日志…";
  try {
    const response = await api("/api/trades");
    const data = response.data || {};
    if (data.simulation_only !== true) throw {message: "日志未确认仅含模拟交易。"};
    const trades = (data.trades || []).filter(item => item.status === "closed" && Number.isInteger(item.closed_at_ms));
    list.replaceChildren();
    const choices = byId("trade-id"); choices.replaceChildren();
    if (!trades.length) list.append(node("p", "help", "目前没有可复盘的已平仓模拟交易。"));
    for (const trade of trades) {
      const label = `${trade.instrument || "BTCUSDT"} · ${trade.trade_id}`;
      const option = node("option", "", label); option.value = trade.trade_id; choices.append(option);
      const row = node("article", "trade-row");
      const info = node("div");
      info.append(node("strong", "", label), node("small", "", `已平仓 · ${time(trade.closed_at_ms)} · ${trade.side || trade.direction || "方向未记录"}`));
      const button = node("button", "button secondary", "用此交易生成预览"); button.type = "button";
      button.addEventListener("click", () => { selectTrade(trade.trade_id); openAnalysis(); });
      row.append(info, button); list.append(row);
    }
    setTradeAvailability(true, trades.length);
  } catch (error) {
    list.textContent = errorText(error);
    setTradeAvailability(true, 0);
  }
}

export function initJournal(navigateAnalysis) {
  openAnalysis = navigateAnalysis;
  byId("refresh-trades").addEventListener("click", refreshTrades);
}
