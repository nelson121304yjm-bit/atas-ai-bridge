# ATAS AI Bridge

把 **ATAS 图表上的订单流数据**（逐价买卖量 footprint、区间成交量分布 profile、累计 delta、
委托簿）通过 **MCP** 交给 AI 助手。

> **上游致谢**：本项目基于 `Ax3lsk3r3/atas-mcp`（https://github.com/Ax3lsk3r3/atas-mcp）
> 为「学习订单流」**重写**，不是原仓库的补丁版。原仓库的整体结构（ATAS 策略 + 本地
> HTTP 桥 + Python MCP server）是起点，下文「为什么重写」列的是相对原版的改动点。

## 为什么重写

原版 `Ax3lsk3r3/atas-mcp` 有四个问题：

| 问题 | 本项目的处理 |
|---|---|
| 12 个工具里**没有任何订单流数据**（只有 quote/DOM/OHLCV+delta），学订单流用不上 | 核心是 footprint / profile / CVD 三个订单流工具 |
| 用 `HttpListener`（http.sys），非管理员绑端口会 `Network Access Denied`，要 `netsh urlacl` 加权限 | 改用 `TcpListener` 自实现最小 HTTP/1.1，**免管理员、免预留**，只绑 127.0.0.1 |
| 宣传里说"自带止盈止损/回撤风控"，但工具签名里没有任何风控参数 | 不吹这个；改成**双闸门**（见下） |
| 下单工具默认可用，HTTP 无鉴权 | 下单默认**不存在**，且策略侧还有第二道锁 |

## 架构

```
ATAS 进程（.NET 10）
  └─ AiBridgeStrategy : ChartStrategy          ← 在图表线程 OnCalculate 内取数
       ├─ 快照：status / quote / DOM / candles / CVD / footprint(近N根) / profile(节流2s)
       └─ BridgeServer（TcpListener, 127.0.0.1:8790..8810）
                    │  HTTP/1.1 + JSON
                    ▼
        mcp/server.py（Python, mcp 2.x, stdio）  ← 11 个只读工具（+3 个下单工具，默认隐藏）
                    │  MCP
                    ▼
              AI 客户端（DSH / Claude Code / Cursor / …）
```

**线程模型**：ATAS 的 API 不是线程安全的。所有取数都在 `OnCalculate`（图表线程）里做；
HTTP 线程只读内存里的 JSON 快照。只有「指定历史 bar 的按需查询」和「下单」会排队到
图表线程执行，并带超时（无行情时 `OnCalculate` 不触发，此时返回明确错误而不是挂死）。
**唯一例外是设置写入**：设置项是普通 CLR 属性（显式字段 + `volatile`），不碰 ATAS 内部
集合，所以 HTTP 线程直接写、不排队——理由见下文「设置读写」第 4 条。

## 目录

```
atas-ai-bridge/
├─ build.ps1                       # 编译 + 部署到 %APPDATA%\ATAS\Strategies
├─ src/ATAS.AiBridge/
│   ├─ ATAS.AiBridge.csproj        # net10.0-windows，ATAS_BASE 默认 C 盘官方路径
│   ├─ AiBridgeStrategy.cs         # 策略主体：取数、快照、端点、下单闸门
│   └─ BridgeServer.cs             # TcpListener 版最小 HTTP 服务器
└─ mcp/
    ├─ server.py                   # MCP server（11 只读工具；下单工具按环境变量注册）
    ├─ atas_client.py              # 纯标准库 HTTP 客户端 + 端口自动发现
    ├─ requirements.txt            # mcp>=2.0.0
    └─ setup-venv.ps1              # 建独立 venv 并安装依赖
```

## 构建与部署

```powershell
# 编译 + 部署（产物只落 %APPDATA%\ATAS\Strategies，安装目录零改动）
.\build.ps1
.\build.ps1 -NoDeploy              # 只编译
```

前置：

- **.NET 10 SDK**。ATAS 8.x 跑在 .NET 10
  （`OFT.Platform.runtimeconfig.json` 里 `tfm=net10.0`），所以 `TargetFramework` 必须是
  `net10.0-windows`，不能用官方默认的 net8。
- ATAS 常见装在 `C:\Program Files (x86)\ATAS Platform`，也可能装在别处（如 D 盘）。
  `build.ps1` 会**自动探测**常见安装路径；探测不到或要指定别的路径时，显式传参：
  `.\build.ps1 -AtasBase 'D:\Program Files (x86)\ATAS Platform'`
  （底层等价于 MSBuild 的 `-p:ATAS_BASE=...`；直接 `dotnet build` 时才需要自己加）。
- csproj 里所有 ATAS 引用都是 `Private=false`：**只读引用**，不会把 ATAS 的 DLL 拷进产物。

## 在 ATAS 里启用

> 🔴 **2026-09-24 纠正（原文写错了，导致用户找不到开关）**：
> 原文第 3 步写"属性面板里勾选 **IsActivated**"——**这是错的**。
> ATAS 官方 API 文档里 `ChartStrategy.IsActivated` 的签名是 **`bool IsActivated { get; }`**
> （见 <https://docs.atas.net/en/classATAS_1_1Strategies_1_1Chart_1_1ChartStrategy.html>），
> **只有 getter、是只读属性**，它回读的是"策略当前是否在运行"这个**状态**，
> 不是给你设的开关。属性面板里既不会出现它，也点不动它。
> 真正的启用动作是**按策略条目上的控制按钮**（官方原文："you can enable / disable
> the strategy by pressing the control button"）。

1. 启动 ATAS，打开你要分析的图表（品种/周期随意，策略挂在哪个图就只看得见那个图）。
2. **刷新策略清单**：若刚部署完 dll，点 ATAS 里那个**闪烁的刷新按钮**，
   让新增策略进入列表（官方原文："Click on the blinking button for the changes in the
   list of strategies to take effect"）。
3. **右键图表 → 点「策略列表」图标（不是指标列表）** → 在窗口里选中 **AI Bridge** → 点 **Add**。
4. **按该策略条目上的「控制按钮」启动它**（▶ 启动 / ■ 停止）——**这一步才是真正的"激活"**。
   不启动则 `OnStarted` 不触发，端口不会监听。
5. 验证：浏览器或 curl 打开 `http://127.0.0.1:8790/api/health`
   预期 `{"ok":true,"bridge":"atas-ai-bridge","version":"0.2.0",...}`。
   **`version` 是判断"ATAS 里到底加载了哪个 DLL"的唯一可靠信号**：覆盖磁盘上的 dll
   不会让已运行的 ATAS 重新加载它（重启 ATAS 或删掉策略重新 Add 才生效）。

**排查**：若第 4 步启动不了，看 ATAS 是否配了交易连接（`ChartStrategy` 带交易功能，
可能需要 Security/Portfolio/Connector 就绪）。桥本身只读，但基类可能仍要求这些就绪。

### 🔴 排障：桥"昨天还能用，今天连不上"（2026-09-25 实际踩到）

**桥的可用性 ≡ 图表数据连接的可用性**。`ChartStrategy` 在掉数据连接时会被 ATAS 主动
停止，重连后才自动恢复——所以桥会随时间**时有时无**，看起来像"随机坏掉"。

ATAS 自己的日志（`%APPDATA%\ATAS\Logs\app_YYYYMMDD.log`）里认这三个签名：

| 日志签名 | 含义 |
|---|---|
| `Notification ... 'Chart strategy stopped due to connection loss. Will resume after reconnect.'` | 正常：掉线 → 策略被停 → 重连后自动恢复。**不是桥的 bug** |
| `Notification ... '该策略尚未启动，请检查与数据的连接和图表设置中选定的组合。'` | 启动被 ATAS 拒绝：**(Portfolio, Security) 组合没通过**（见下） |
| `ERROR ... [SearchSecuritiesResult] Searching security error` + `Search securities request rejected: Security X not found.` | 该 security 在提供该 portfolio 的连接上不存在 → 组合无效 |

**判断"桥自己有没有问题"的最快办法**（别猜）：
1. 看 `%APPDATA%\ATAS\AiBridge.port` 的**时间戳**。它是成功绑端口时写的；若时间戳很旧，
   说明本次 ATAS 启动**从未成功绑过端口** → `OnStarted` 压根没被执行 → 问题在 ATAS 启不启动
   策略这一层，与桥的代码无关。
2. 在同一个图表上试着启动 ATAS **自带的** `Sample SMA strategy`。若它同样报"尚未启动"，
   就是平台级的就绪问题，与任何自定义策略无关。
3. 搜日志里**有没有 `Starting with portfolio ... security ...` 这一行**：
   **有** = 组合没问题、是连接掉了（会自愈）；**没有** = 组合就没通过，跟网络无关。

**根因（2026-09-25，实测日志）**：不是网络、不是桥的代码，是
**(Portfolio, Security) 组合不匹配**。

- 账户（如 `CRYPTOxxxxx`）来自对应服务器的**模拟连接**，启动时注册的能力决定了
  它能看到哪些品种（例如只仿真期货、没有现货）。
  `Auth: Server Emulation name Crypto Sim types: [Type:CryptoFutures Exchanges:[BinanceFutures]]`
  —— **只仿真 `BinanceFutures`，没有现货**。
- 于是图表挂在**现货** `ETHUSDT@Binance` / `BTCUSDT@Binance` 上时：
  ```
  Crypto Sim: Search request Code:ETHUSDT Exchange:Binance ...        ← 现货
  Crypto Sim: Search securities request rejected: Security ETHUSDT@Binance not found.
  Crypto Sim: Search request ... found 0 results.
  ```
  → 组合无效 → 策略启动被拒，报"请检查与数据的连接**和图表设置中选定的组合**"。
- 对照：**每一次成功启动**都是同一行，用的是**期货** security：
  ```
  Starting with portfolio <你的模拟账户> security BTCUSDT@BinanceFutures...
  Started.
  ```
- **修法**：把挂 AI Bridge 的图表**品种换成 `@BinanceFutures`**
  （`BTCUSDT@BinanceFutures` / `ETHUSDT@BinanceFutures` / `XAUUSDT@BinanceFutures`），
  与提供该模拟账户的连接对齐。

> ⚠️ **别把这个误判成网络问题**（2026-09-25 第一版就误判成系统代理了，错）：
> 同一天的日志里真实 `Binance` 连接器是好的（`Binance: Search request 044ABE2E found 1 results`
> → `Subscribing ETHUSDT@Binance`，连的是 `wss://stream.binance.com:9443`）。
> 排障顺序：**先看有没有 `Starting with portfolio ...` 那一行**，再谈网络。

**这类连接会持续抖**：`Crypto Sim` 实测每 20–30 秒掉一次，每次掉线 ATAS 都停策略、
重连后再启（所以端口文件时间戳会反复刷新）。**桥跟着一起抖是设计使然**，不是 bug。

> 想让桥**不受行情连接影响**（连接掉线也能读）需要把桥从 `ChartStrategy` 改成
> `Indicator` 挂载——Indicator 不要求交易连接就绪。代价是失去下单能力（Indicator
> 没有 `OpenOrder`），这是个设计取舍，未实现。

端口：默认 8790，被占用则顺延到 8810；实际端口写入 `%APPDATA%\ATAS\AiBridge.port`，
Python 侧会自动读取，无需手工配置。

### 策略设置项

「运行中可改」= 能不能通过 `POST /api/settings`（即 MCP 的 `atas_set_settings`）在运行中改。
改动**只存内存**：ATAS 重启 / 重新加载策略后回到属性面板里保存的值（ATAS 没有公开 API
让策略触发工作区设置落盘）。

| 设置 key | 属性面板名 | 默认 | 运行中可改 | 作用 |
|---|---|---|---|---|
| `bridgeEnabled` | 启用 AI 桥 | 开 | ❌ 只在启动时读 | 关掉则不监听端口 |
| `snapshotBars` | 快照 K 线根数 | 300 | ✅ 50–5000 | `/api/candles`、`/api/cvd` 的快照长度与默认值 |
| `snapshotFootprintBars` | 快照 footprint 根数 | 2 | ✅ 0–20 | 快照里保留最近几根的逐价数据 |
| `profileLookbackBars` | 区间 profile 回看根数 | 200 | ✅ 20–5000 | `/api/profile` 默认回看多少根 |
| `profileValueAreaPercent` | profile 价值区百分比 | 70 | ✅ 1–100 | `/api/profile` 默认价值区百分比 |
| `defaultDomLevels` | 默认 DOM 档数 | 15 | ✅ 1–200 | `/api/dom` 默认档数（也是快照的档数） |
| `allowTrading` | 允许下单 | **关** | ✅ | 第一道闸门 |
| `tradeToken` | 下单确认口令 | 空 | ✅ | 第二道闸门（留空 = 永久禁止下单） |

> ⚠️ `bridgeEnabled` 故意**不支持**运行中修改：关掉它等于把 HTTP 通道自己掐断，
> 之后就再也调不了 `atas_set_settings` 把它打开。要起停请在 ATAS 属性面板操作。

### 设置读写（0.2.0 新增，AI 可调）

| 端点 | 说明 |
|---|---|
| `GET /api/settings` | 列出全部设置项：当前值、取值范围、是否可运行中修改、说明 |
| `POST /api/settings` | `{"set":{"snapshotBars":500,"defaultDomLevels":30}}`，立即生效 |

设计要点（都是踩过的坑，别退回去）：

1. **整批原子**：任一项未知 key / 类型错 / 越界 → 整批拒绝，一个都不改。避免"改了一半"
   而调用方以为成功。
2. **口令脱敏**：`tradeToken` 的 `value` 永远是 `null`，只回 `isSet` + `length`。
   明文既不回显给 AI，也不写进日志。
3. **审计**：每次变更都往桥日志写一行 `SETTINGS <key> <旧> -> <新>`（口令只记长度），
   用 `/api/log` 可查。
4. **不排队到 ATAS 线程**：设置项是普通 CLR 属性（显式字段 + `volatile`），
   `int`/`bool`/`string` 引用的读写本身原子、`volatile` 保证图表线程可见，所以 HTTP
   线程直接写。若排队，收盘后 `OnCalculate` 不触发会让"调设置"直接超时——而收盘后
   恰恰是最想调设置的时候。
5. **快照缓存按"构建参数"命中**：快照记录自己是"用哪组参数构建的"，命中判断比对这些
   值而不是比对"当前设置值"。否则改完设置后第一次请求会命中旧快照，返回与参数不符的数据。
   改设置还会置位 `_refreshAllRequested`，让下一根 tick 立刻重建全部快照。

## HTTP 端点

| 端点 | 参数 | 说明 |
|---|---|---|
| `GET /api/health` | — | 存活 + 版本 + 端口 |
| `GET /api/status` | — | 品种、tick size、当前 bar、账户、是否激活、下单是否武装 |
| `GET /api/quote` | — | 最新价、买一卖一及挂单量、价差、盘口累计买卖量与失衡度 |
| `GET /api/dom` | `levels=15` | 挂单簿逐档（买降序、卖升序） |
| `GET /api/candles` | `bars=300` | OHLC + volume + 每根 delta |
| `GET /api/cvd` | — | 每根 delta + 累计 delta（会话重置标记） |
| `GET /api/footprint` | `bar=-1&top=0&compact=1` | **单根逐价 footprint**：每档 bid/ask/vol/delta/笔数 + POC + VAH/VAL |
| `GET /api/footprint` | 不带 `bar` | 快照窗口内最近几根（一次拿多根） |
| `GET /api/profile` | `bars=200&percent=70&top=0` | **区间成交量分布**：POC / VAH / VAL / 逐价明细 |
| `GET /api/log` | `lines=80` | 桥内诊断日志（含 `SETTINGS` 审计行） |
| `GET /api/settings` | — | **列出全部设置项**：当前值 / 范围 / 是否可运行中改 / 说明 |
| `POST /api/settings` | `{set:{...}}` | **改设置**，立即生效（整批原子；口令不回显） |
| `POST /api/order` | `{direction,type,qty,price?,triggerPrice?,comment?,confirm}` | 下单（需双闸门） |
| `POST /api/order/cancel` | `{id,confirm}` | 撤单（需双闸门） |
| `POST /api/position/close` | `{volume?,confirm}` | 市价平仓（需双闸门） |

`bar` 用负数从当前往回数：`-1` = 正在形成的那根，`-2` = 上一根。

## MCP 工具

只读（11 个，始终注册）：

`atas_status` · `atas_quote` · `atas_dom` · `atas_footprint` · `atas_footprint_recent` ·
`atas_profile` · `atas_cvd` · `atas_candles` · `atas_log` ·
**`atas_get_settings`** · **`atas_set_settings`**（后两个 = 0.2.0 新增的设置读写）

常驻成本（口径：`name + description + inputSchema` 的 JSON 字符数，脚本可复算）：
**11 工具 = 2934 字符 ≈ 734 tokens**；0.1.0 的 9 工具 = 2185 字符，即新增两个设置工具
**+749 字符 ≈ +187 tokens**（对比 TradingView MCP 的 84 工具 / 7766 tokens）。

下单（3 个，**默认不注册**）：只有设置了环境变量 `ATAS_TRADE_TOKEN` 才会出现在工具列表里；
即使出现，ATAS 策略侧还要同时打开「允许下单」并填写同一口令，否则一律拒绝。

### 设置类工具怎么用

```
atas_get_settings()                                   # 先拿 key 与范围
atas_set_settings({"snapshotFootprintBars": 8})       # 想多看几根的逐价数据
atas_set_settings({"profileLookbackBars": 1000, "profileValueAreaPercent": 80})
atas_set_settings({"snapshotFootprintBars": 2})       # 用完改回去
```

越界 / 未知 key / 类型错会返回 `ok:false` 并列出可用项，不会静默吞掉。

### MCP 客户端配置（stdio）

```json
{
  "mcpServers": {
    "atas": {
      "command": "<仓库路径>\\mcp\\.venv\\Scripts\\python.exe",
      "args": ["<仓库路径>\\mcp\\server.py"]
    }
  }
}
```

先跑一次 `mcp\setup-venv.ps1`（建独立 venv 装 mcp 2.x）。
**必须用 venv**：若全局环境里是 mcp 1.x，而 `mcp.server.mcpserver.MCPServer` 在 1.x 不存在，
直接用全局 Python 会 `ImportError`；装到 venv 也不会顶掉其它依赖 mcp 1.x 的工具。

## 数据口径（都来自 ATAS 官方 API，非猜测）

| 数据 | API | 口径说明 |
|---|---|---|
| 逐价买卖量 | `IndicatorCandle.GetPriceVolumeInfo(price, cache)` → `PriceVolumeInfo{Price,Bid,Ask,Between,Volume,Ticks}` | delta = `Ask - Bid` |
| 单根 POC | `IndicatorCandle.MaxVolumePriceInfo.Price` | 可能为 null（无成交量时） |
| 单根价值区 | `IndicatorCandle.ValueArea.ValueAreaHigh / ValueAreaLow` | ATAS 自己算好的 |
| 区间 POC/VAH/VAL | 本桥把逐价量跨 bar 合并后按标准算法算 | 从 POC 起，每次并入上下两侧量较大的一侧，直到累计 ≥ `percent%` |
| 每根 delta | `IndicatorCandle.Delta`（另有 `MinDelta`/`MaxDelta`） | |
| 累计 delta | `candle.Delta` 累加 + `IsNewSession(bar)` 归零 | 与官方 `CumulativeDelta` 指标同口径 |
| 委托簿 | `MarketDepthInfo.GetMarketDepthSnapshot()` → `MarketDataArg{Price,Volume,DataType}` | `DataType==Ask` 为卖盘 |
| 盘口累计量 | `MarketDepthInfo.CumulativeDomBids / CumulativeDomAsks` | |

## 已知限制（重要）

1. **读不到"别人画在图上的东西"**：某个指标自己画的线/标签/表格文字没有 API 可读
   （TradingView 那边有 `data_get_pine_lines/labels/tables`，ATAS 没有对应物）。
   本桥是**按同一份原始数据自己重算**，所以数值口径可能与你图上那个指标略有出入
   （成交量类型、聚合方式、是否含未完成 bar 等设置差异）。
2. **单图表、单品种**：策略挂在哪个图就只看得见那个品种那个周期；多品种要挂多个实例
   （每实例占一个端口）。
3. **无行情时按需查询会超时**：`OnCalculate` 不触发 → 排队的历史 bar 查询等不到执行，
   3–5 秒后返回明确错误。收盘后想查历史，用快照端点或不带 `bar` 的 footprint。
4. **快照是节流的**：quote/DOM 200ms、K线/CVD/footprint 1s、profile 2s 刷新一次。
   不是逐笔实时流；需要推送要自己接 SSE（本版未做）。
5. **没有逐笔 tape**：`GetTrades` 未接入（你这次没选）。
6. **持仓口径**：净持仓按本策略自身成交（`MyTrades`）计算，账户里手动下的单**不计入**。
7. **设置改动不落盘**：`POST /api/settings` 只改内存，ATAS 重启 / 重新加载策略后回到
   属性面板里保存的值。ATAS 没有公开 API 让策略触发工作区设置落盘，所以"持久化"这条
   做不到，别承诺。想永久生效请在属性面板改并保存。
8. **设置改动后 ATAS 属性面板不会自动刷新**：面板打开着的话显示的还是旧值，
   关掉重开才会看到新值。`GET /api/settings` 才是权威现值。

## 下单：双闸门 + 契约门槛

下单要同时满足四件事才可能成功：

1. 策略处于**运行状态**（`IsActivated == true`；这是只读状态位，不是可勾的开关，见上文）；
2. 打开了 **允许下单**（`allowTrading`）；
3. 填了 **下单确认口令**（`tradeToken` 非空）；
4. MCP 进程的环境变量 **ATAS_TRADE_TOKEN** 与口令一致（且下单工具会因此被注册）。

第 3、4 条缺任一，下单端点一律拒绝。

> 🔴 **0.2.0 变更（权限边界，务必知情）**：第 2、3 条现在是**运行中可写**的——
> 也就是说 **AI 可以通过 `atas_set_settings` 自行武装这两道闸门**（这是本人 2026-09-25
> 明确选择的方案 B，不是默认行为）。
>
> 剩下的唯一人类独占锁是**第 4 条**：`ATAS_TRADE_TOKEN` 是 MCP 进程**启动时**的环境变量，
> AI 改不了自己进程的环境变量，也删不掉自己已注册的工具。所以：
> **只要没在 MCP 配置里设 ATAS_TRADE_TOKEN，AI 即使把前两道闸门全打开，也依然下不出单**
> （下单工具压根不在它的工具列表里）。
>
> 反过来：**一旦你在 MCP 配置里设了 ATAS_TRADE_TOKEN，AI 就具备单方面下单能力**
> （它自己能补齐第 2、3 条）。设这个变量 = 把最后一道锁交出去，请当成"打开实盘开关"对待。
>
> 每次闸门变更都会写审计行到 `/api/log`，且口令值永不回显（只报 `isSet` + `length`）。

> 按交易契约纪律（`trading-contract` skill → 契约 §2.3）：任何**资金移动**前必须先写出
> 「赚谁的钱 / 何时失效」两行可触发句，写不出不下单。上述权限不豁免这条：
> 闸门可以由 AI 打开，但**具体下单决策仍要过两行字**——本桥不替你写这两行。

## 开发环境坑（已踩过，别再踩）

1. **`.ps1` 在 Windows PowerShell 5.1 下**：它读取**无 BOM 的 `.ps1`** 时按系统 ANSI
   （zh-CN = GBK）解码 → 中文注释变乱码 → 语法解析直接崩（报错位置还很误导）。
   **所有 `.ps1` 一律只用 ASCII**（本项目的 `build.ps1`、`setup-venv.ps1` 都是）。
2. **`.cs` 里的中文**：csc 在中文 Windows 上可能按 ANSI 读源码 → 已在 csproj 里设
   `<CodePage>65001</CodePage>` 强制 UTF-8。
3. `MarketDataType` 在 `ATAS.DataFeedsCore` 与 `ATAS.Indicators` 里同名，必须用
   `using MarketDataType = ATAS.Indicators.MarketDataType;` 消歧。
4. 编译用 PATH 里的 `dotnet`；若本机装在用户目录未进 PATH，`build.ps1` 会回退到
   `%USERPROFILE%\.dotnet\dotnet.exe`。
