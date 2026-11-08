"""ATAS AI Bridge —— MCP server (stdio / sse / streamable-http).

把 ATAS 图表上的订单流数据（逐价买卖量 footprint、区间成交量分布 profile、
累计 delta、委托簿）暴露给 AI 助手。

运行：
    python server.py                      # stdio（桌面客户端默认）
    python server.py --transport sse --port 8000

下单类工具默认【不注册】：只有设置了环境变量 ATAS_TRADE_TOKEN 才会出现，
并且 ATAS 策略侧还要同时打开「允许下单」并填写同一个口令，双重闸门。

设置类工具（0.2.0）：atas_get_settings / atas_set_settings 让 AI 在运行中调整桥的
参数（快照根数、profile 口径、DOM 档数等），改动立即生效、只存内存、逐条写审计日志。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer

from atas_client import AtasBridgeClient, BridgeError

VERSION = "0.2.0"

server = MCPServer(name="ATAS", version=VERSION)
_client = AtasBridgeClient()
_trade_token = os.environ.get("ATAS_TRADE_TOKEN", "").strip()


def _guard(call):
    try:
        return call()
    except BridgeError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - never crash the MCP process
        return {"ok": False, "error": f"unexpected error: {exc}"}


# ---------------------------------------------------------------------------
# 状态与盘口
# ---------------------------------------------------------------------------

@server.tool(
    description="ATAS 桥连通状态：品种、tick size、当前 bar、账户、策略是否激活、"
                "下单是否已武装。Mac 指标还提供 hostMode、readOnly 和 freshness；"
                "分析前先核对品种、模拟账户与数据年龄。本地接收时间不代表交易所延迟或行情完整性。"
)
def atas_status() -> dict[str, Any]:
    return _guard(_client.status)


@server.tool(
    description="图表报价快照：last 来自当前 K 线 Close，买一/卖一及挂单量来自图表 DOM。"
                "数据受 ATAS 图表价格聚合设置影响，tickSize 不一定是交易所最小报价单位。"
                "另含价差、盘口累计买量/卖量与失衡度（(累计买-累计卖)/(累计买+累计卖)）。"
)
def atas_quote() -> dict[str, Any]:
    return _guard(_client.quote)


@server.tool(
    description="挂单簿逐档明细（默认每侧 15 档）：买盘由高到低、卖盘由低到高，逐档价格与挂单量。"
                "这是图表聚合后的单次快照，不能单凭一次查询推断撤单、补单、吸收或完整交易所深度。"
)
def atas_dom(levels: int = 15) -> dict[str, Any]:
    return _guard(lambda: _client.dom(levels))


# ---------------------------------------------------------------------------
# 订单流核心
# ---------------------------------------------------------------------------

@server.tool(
    description="单根 K 线的 footprint（逐价成交量）：每个价位的 bid 量、ask 量、总量、delta(ask-bid)、"
                "成交笔数，外加该根 POC（最大量价位）、VAH/VAL（价值区上下沿）、整根 volume/delta/minDelta/maxDelta。"
                "bar 用负数从当前往回数：-1=正在形成的那根，-2=上一根。top>0 时只返回成交量最大的前 top 档。"
                "这是判断吸收、失衡、被动/主动成交的主力工具。"
)
def atas_footprint(bar: int = -1, top: int = 0, compact: bool = True) -> Any:
    return _guard(lambda: _client.footprint(bar=bar, top=top, compact=compact))


@server.tool(
    description="快照窗口内最近几根 K 线的 footprint（一次拿多根，省往返）。"
                "窗口大小由 ATAS 策略设置里的「快照 footprint 根数」决定。"
)
def atas_footprint_recent() -> Any:
    return _guard(lambda: _client.footprint())


@server.tool(
    description="区间成交量分布（volume profile）：把最近 bars 根 K 线的逐价成交量合并，"
                "给出 POC（最大量价位）、VAH/VAL（价值区上下沿，按 percent 从 POC 向两侧扩展）、"
                "总成交量，以及逐价明细（含该价位的累计 bid/ask 量）。"
                "bars=0 表示用策略设置里的默认回看根数；top>0 只返回量最大的前 top 档。"
                "用来看筹码密集区、支撑阻力、价值区边界。"
)
def atas_profile(bars: int = 0, percent: int = 70, top: int = 0) -> Any:
    return _guard(lambda: _client.profile(bars=bars or None, percent=percent, top=top or None))


@server.tool(
    description="累计成交量 delta（CVD）序列：每根 K 线的 delta 与累计值，"
                "从返回窗口起点开始累加，在 sessionStart=true 时归零。"
                "若窗口从会话中途开始，首次会话重置前是局部累计值，不能冒充完整会话 CVD。"
)
def atas_cvd() -> Any:
    return _guard(_client.cvd)


@server.tool(
    description="K 线序列（默认快照窗口，最大 5000 根）：OHLC + volume + 每根 delta。"
                "需要更长的历史时传 bars。"
)
def atas_candles(bars: int = 0) -> Any:
    return _guard(lambda: _client.candles(bars=bars or None))


@server.tool(
    description="桥内诊断日志（最近 lines 行）：策略启动、绑端口、下单动作、设置变更（SETTINGS 行）等。"
                "桥行为异常时看这个。"
)
def atas_log(lines: int = 80) -> dict[str, Any]:
    return _guard(lambda: _client.log(lines))


# ---------------------------------------------------------------------------
# 设置（读写；0.2.0 新增）
# ---------------------------------------------------------------------------

@server.tool(
    description="读取 ATAS 桥的全部设置项：当前值、取值范围、是否支持运行中修改、说明。"
                "改设置前先调这个拿可用 key（key 为 camelCase，如 snapshotBars）。"
                "口令类设置（tradeToken）不回显原值，只给 isSet 与 length。"
)
def atas_get_settings() -> dict[str, Any]:
    return _guard(_client.settings)


@server.tool(
    description="【改设置】运行中修改 ATAS 桥的设置并立即生效。参数 settings 是 {设置key: 新值} 字典，"
                "key 见 atas_get_settings。数据类：snapshotBars(50-5000)、snapshotFootprintBars(0-20)、"
                "profileLookbackBars(20-5000)、profileValueAreaPercent(1-100)、defaultDomLevels(1-200)。"
                "风控类：allowTrading(bool)、tradeToken(str，清空传空串)。"
                "整批原子：任一项未知/类型错/越界则整批拒绝、一个都不改。"
                "改动只在内存生效（ATAS 重启后回到属性面板里的值），且每次都写入桥日志审计。"
)
def atas_set_settings(settings: dict[str, Any]) -> dict[str, Any]:
    return _guard(lambda: _client.update_settings(settings))


# ---------------------------------------------------------------------------
# 交易（默认不注册；需 ATAS_TRADE_TOKEN + ATAS 策略侧同时开启）
# ---------------------------------------------------------------------------

if _trade_token:

    @server.tool(
        description="【真实下单】通过 ATAS 下单。direction: buy/sell；"
                    "order_type: limit/stop/market/stoplimit；qty 为手数；"
                    "limit/stoplimit 需 price，stop/stoplimit 需 trigger_price。价格自动按 tick 对齐。"
    )
    def atas_place_order(direction: str, order_type: str, qty: float,
                         price: float | None = None, trigger_price: float | None = None,
                         comment: str | None = None) -> dict[str, Any]:
        return _guard(lambda: _client.place_order(
            direction=direction, order_type=order_type, qty=qty, confirm=_trade_token,
            price=price, trigger_price=trigger_price, comment=comment))

    @server.tool(description="【真实撤单】按 order_id 撤销挂单。")
    def atas_cancel_order(order_id: str) -> dict[str, Any]:
        return _guard(lambda: _client.cancel_order(order_id, _trade_token))

    @server.tool(description="【真实平仓】市价平掉当前持仓；volume 省略则全平。")
    def atas_close_position(volume: float | None = None) -> dict[str, Any]:
        return _guard(lambda: _client.close_position(_trade_token, volume=volume))


def main() -> None:
    parser = argparse.ArgumentParser(description="ATAS AI Bridge MCP server")
    parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"],
                        default=os.environ.get("MCP_TRANSPORT", "stdio"))
    parser.add_argument("--host", default=os.environ.get("MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8000")))
    parser.add_argument("--atas-port", type=int, default=None, help="ATAS 桥端口（默认自动发现）")
    args = parser.parse_args()

    global _client
    if args.atas_port is not None:
        _client = AtasBridgeClient(port=args.atas_port)

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
