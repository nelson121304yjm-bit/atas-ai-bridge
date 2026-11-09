# macOS / ATAS X 使用指南

本分支给现有 ATAS AI Bridge 增加 macOS 只读指标入口和本机 AI 复盘工作台；Windows 的
`build.ps1`、`mcp/setup-venv.ps1` 和交易开关行为保持原样。
MIT 许可证与上游署名保留。

## 1. 准备

- 已安装可正常使用的 ATAS X。编译使用本机安装目录的 SDK，不下载或分发 ATAS 厂商 DLL。
- .NET **10 SDK**（仅安装 runtime 不足以编译）。`dotnet --version` 应输出 10.x。
- Python **3.12+** 用于本机工作台；安装器在私有状态目录创建独立 `.venv`，只用标准库。
  MCP 客户端另用项目内的 `mcp/.venv`，不改全局包。
- 在项目根目录运行以下命令。脚本兼容 macOS 自带的 Bash 3.2，路径可含空格。

## 2. 构建和安装

```bash
bash build.sh
```

默认 SDK 目录：`/Applications/ATAS X.app/Contents/MonoBundle`。
构建产物：`out/ATAS.AiBridge.dll`。这一命令不会安装插件或重启平台。

退出 ATAS 后安装：

```bash
bash build.sh --install
```

默认安装位置：`~/Library/Application Support/ATAS/Indicators/ATAS.AiBridge.dll`。
脚本只复制本项目 DLL；已有同名 DLL 会在同目录保存为 `.bak.*`，再替换。
若之前安装过本分支的策略版，新指标复制成功后，`Strategies/ATAS.AiBridge.dll`
会被移到同目录的 `.bak.*` 备份，避免两个目录重复加载同名程序集。
检测到 `ATAS X` 或 `OFT.Platform` 进程时默认拒绝安装。
`--install --force` 可以显式覆盖此保护，但不代表运行中的平台已经重新加载插件；
须在 status 中核对实际加载版本；如果没有重新加载，再重启平台后验证。
本次测试中平台在安装后成功热加载，但不保证其他版本也支持。首次使用建议先退出 ATAS。

自定义路径和运行时：

```bash
bash build.sh --atas-base '/path/to/ATAS X.app/Contents/MonoBundle'
bash build.sh --install --atas-user-dir "$HOME/Library/Application Support/ATAS X"
DOTNET=/absolute/path/to/dotnet bash build.sh
```

也可通过 `ATAS_BASE`、`ATAS_USER_DIR` 设置默认值。`ATAS_USER_DIR` 应指向实际 ATAS 用户目录，
不是应用程序目录；它只影响安装位置。`bash build.sh --help` 可查看所有参数。

回退：退出 ATAS，将对应 `.bak.*` 文件复制回 `ATAS.AiBridge.dll`，再启动平台。
如果是首次安装且没有备份，可在退出 ATAS 后移除该 DLL。

## 3. 安装本机工作台

```bash
bash install-workbench.sh --install --python /absolute/path/to/python3.12
```

`--python` 可省略，此时使用 `python3`，但版本必须为 3.12 或更高。
安装器在 `~/Library/Application Support/ATAS AI Review` 建立专用 `.venv`、
只含本机配置的 `installation.json` 和可双击的 `Open ATAS AI Review.command`。
它不下载模型依赖、不安装 ATAS 指标，也不重启 ATAS。首次启动从图表的“打开 AI 工作台”
按钮或双击该 `.command` 文件；多次打开会复用现有本机后台。

可选接入已有**模拟交易日志**，四个参数必须同时给出，且指向现有文件：

```bash
bash install-workbench.sh --install \
  --journal-python /absolute/path/to/python3.12 \
  --journal-script /absolute/path/to/journal-server.py \
  --journal-db /absolute/path/to/existing-journal.sqlite \
  --journal-account YOUR_SIM_ACCOUNT_ID
```

这四项仅写入权限为 `0600` 的 `journal.json`，页面不能修改可执行程序或数据库路径。
脚本不会查找私有日志、自动创建数据库或读取模型密钥。再次安装且未给出日志参数时，
原 `journal.json` 保留；显式使用 `--disable-journal` 才移除这个适配器配置，
不会删除独立日志数据库。未配置或配置失效时“交易日志”显示未连接，行情分析仍可用。

“模型设置”填写完整 Base URL、模型名与用户自己的 Key。Key 默认只在本次后台会话中；
勾选后才存入 macOS Keychain。改变服务地址后必须重新输入该地址的 Key。
连接测试只发送固定合成文本；发送预览先显示目标、大小、完整证据和质量边界，
点击“发送至模型”后才发送行情或交易证据。报告文本不执行 HTML；演示报告始终标注
“本地演示”，不能追问或生成规则提案。接受提案会创建新版本，遇到版本冲突需手动合并。

回退／卸载工作台：先退出工作台后台和 ATAS，然后移走默认状态目录中的
`installation.json` 与 `Open ATAS AI Review.command`，图表入口将不再启动工作台。
需要完全卸载工作台环境时，可再移走该目录的 `.venv`。**保留** `workbench.sqlite3`、
`journal.json` 以及独立日志数据库和报告数据；若确需删除报告，请单独备份并明确操作。
`build.sh --install` 创建的指标可按上文 `.bak.*` 方法独立回退。
自定义 `--state-dir` 仅用于手动启动或隔离测试，图表入口仍查找默认目录。

## 4. 准备 MCP

```bash
bash mcp/setup-venv.sh
```

脚本创建 `mcp/.venv`、安装上游 `requirements.txt` 中的依赖，并检查
`mcp.server.mcpserver.MCPServer` 能导入。可用 `PYTHON=/absolute/path/to/python3`
选择 Python。脚本最终输出包含本机绝对路径的 MCP stdio 配置，复制到支持 MCP 的客户端。

例如，将以下路径替换为自己的实际项目目录：

```json
{
  "mcpServers": {
    "atas": {
      "command": "/absolute/path/atas-ai-bridge/mcp/.venv/bin/python",
      "args": ["/absolute/path/atas-ai-bridge/mcp/server.py"]
    }
  }
}
```

无需给这个数据桥填写模型 API Key；模型账户或 Key 由所选 AI 客户端管理。
MCP 数据桥与本机 AI 复盘工作台互相独立；MCP 客户端的 Key 不会被工作台读取。

## 5. 在模拟图表启动并检查

1. 打开 BTCUSDT（Binance Futures）图表，先选择 Crypto Sim 验证。
2. 打开图表的**指标**列表，切到 All 或 Custom，搜索 **AI Bridge — read only**。
3. 添加后点**应用**。此版本由指标生命周期启动，不需要策略启动按钮。
4. 图表上方会显示 `AI Bridge | READ ONLY | :端口 | 状态`；指标设置的
   `Bridge status (read only)` 也能读取诊断。`Show bridge status on chart` 控制状态条显示。
5. 先核对 health/status，再请求其他行情。`waiting live tick` 表示尚未收到新的公开成交；
   `stale` 表示超过 5 秒未收到公开成交，不应把旧缓存当成实时数据。

Mac 指标构建不包含交易实现，所有非 GET 请求都被拒绝，也不提供账户订单/持仓端点。
不设置 `ATAS_TRADE_TOKEN`；即便某客户端显示 `atas_set_settings`，Mac 指标也会拒绝写入。
Windows 策略版仍保留原来的设置写入和交易开关，不能把 Windows 服务也描述成完全只读。
`IsActivated` 始终是运行状态，不是勾选项。

这个指标与 **BTC Orderflow Lab — SIM recorder** 是两个组件。前者提供当前图表的查询，
后者负责手动模拟成交日志；添加旧记录器不会启动 AI Bridge，也不会出现 AI 聊天窗口。

启动后，端口会在 8790–8810 范围内选择。Mac 的发现顺序：

1. `ATAS_AI_BRIDGE_PORT` 的有效显式值。
2. `~/Library/Application Support/ATAS/AiBridge.port`。
3. `~/Library/Application Support/ATAS X/AiBridge.port`。
4. 默认端口 8790。

Windows 仍使用 `%APPDATA%` 下的对应文件。无效／不存在的端口文件会跳过。
C# 只在已经存在的 ATAS 用户目录内写端口文件。多图表／多实例可能覆盖同一个发现文件；
需要固定实例时可在客户端配置中设置 `ATAS_AI_BRIDGE_PORT`，或给 `server.py` 传 `--atas-port`。

先只读取状态（从项目根目录执行）：

```bash
mcp/.venv/bin/python - <<'PY'
import json
import sys
sys.path.insert(0, 'mcp')
from atas_client import AtasBridgeClient
client = AtasBridgeClient()
print(client.base)
print(json.dumps(client.health(), ensure_ascii=False, indent=2))
print(json.dumps(client.status(), ensure_ascii=False, indent=2))
PY
```

核对 `hostMode=indicator`、`readOnly=true`、`securityId`、账户、`isActivated` 和
`freshness`，再在 AI 客户端查询 footprint、CVD、DOM。`snapshotAgeMs` 是本地快照采集年龄，
`marketEventAgeMs` 是本地收到公开成交的年龄；两者都不是交易所原始时戳或完整性保证。
使用 Clash 等代理时，确保 `127.0.0.1`／`localhost` 走本机直连；不需要把桥端口开放到公网。

### 数据解释与查询限制

- 返回的是 ATAS 当前图表提供的数据。价格聚合会影响报价、DOM 和逐价数据；
  本次验收的图表返回 `tickSize=10`，不能把它当成交易所最小报价单位。
  `quote.last` 来自当前 K 线的 Close，不是独立保存的原始逐笔价格。
- CVD 从返回窗口起点累加，并在 `sessionStart=true` 时重置。窗口从会话中途开始时，
  第一次重置之前是局部累计值，不代表完整会话 CVD。
- 各端点有独立节流窗口，多次调用并非同一时刻的原子快照。
  status 的 freshness 反映整体采集／接收活动，不保证每个端点具有相同采集时间。
- 指定历史根数／bar 或其他非默认参数的查询可能排队等待 ATAS 的下一次计算回调。
  验收时一次 `candles(bars=5)` 等待 3000 ms 超时，稍后重试返回了 5 根有效数据。
  无新行情时应优先使用默认快照；不能把超时或空结果解释为零成交量。
- 单次 DOM 不能证明撤单、补单或吸收，返回档位也不保证覆盖交易所完整订单簿。
- 查询只提供当前与有限历史行情，不会自动创建用户手动成交日志，也不会持续保存全日 DOM。

### 在 AI 客户端开始使用

可以先使用下面这段提示词，再按自己的交易系统补充具体规则：

> 你是我的 BTC 模拟交易复盘助手。先调用 atas_status，核对标的、模拟账户、只读模式与数据年龄；
> 再按问题需要读取 candles、CVD、footprint、profile 和 DOM。请用中文分别给出：已观察到的事实、
> 可能的解释、反证或仍缺的数据、下一次模拟交易要验证的一项规则。
> 区分图表聚合数据与原始逐笔，标明 CVD 窗口和未闭合 K 线，不把挂单量或 Delta 当成多空持仓人数。
> 数据过期、覆盖不足或查询失败时明确说明，不编造缺失数据。
> 若没有独立成交日志，不推断我的入场、出场、盈亏或执行质量。只读分析，不执行交易。

例如：`检查当前 BTCUSDT 的数据质量，然后解释最近 5 根已闭合 M5 K 线的价格与 Delta 是否一致。`
复盘个人交易时还需要单独的交易日志 MCP；仅连接这个行情桥不能获得手动成交历史。

**不要把 `mcp/verify.py` 当成纯只读检查**：它会测试设置写入并尝试恢复。
本指南的初始检查只读取 health/status，不发送订单或修改设置。

## 验证范围

2026-11-08 在 Apple Silicon macOS、ATAS X **8.0.15.663**、.NET SDK **10.0.403**、
Python **3.13.2** 环境验证：

- 实际 SDK 的 Release 构建成功，0 警告、0 错误。
- 安装脚本使用临时 SDK／工具测试路径空格、显式安装、备份、运行中拒绝和构建失败不覆盖。
- 端口发现测试覆盖 Mac 路径、Windows 路径、优先级、无效文件和重新发现。
- MCP v2 虚拟环境可初始化 stdio 会话并列出工具；测试不调用行情、设置或交易工具。
- Mac 主机类型、只读请求边界、未就绪查询、数据年龄与端口文件归属由实际 DLL 的契约测试覆盖。
- `0.3.0-macos.2` 已在 BTCUSDT（Binance Futures）的 Crypto Sim 图表加载。
  图表完整显示 `AI Bridge | READ ONLY | :8790 | receiving data`。
- 实际 MCP status 返回 `hostMode=indicator`、`readOnly=true`、`accountIsReal=false`、
  `accountConnectionState=Connected`；`allowTrading`、`tradingArmed`、`tradeTokenSet` 均为 false。
  两次观测的采集时间持续前进，数据年龄均低于 1 秒。这不是交易所网络延迟测量。
- 实际 MCP 成功读取 quote、买卖各 15 档 DOM、300 根 candles、300 根 CVD、当前 bar footprint、
  200 根 profile（按量返回前 10 个价位），以及只读 settings。没有提交任何交易或设置写入请求。
- Python 回归测试 22 项通过；实际 Mac DLL 契约检查 34 项通过。
  以 `MacIndicatorHost=false` 编译的策略兼容分支也通过本机 SDK 编译及 2 项契约检查。

运行回归测试：

```bash
python3 -m unittest discover -s tests -v
# 含 MCP stdio 集成测试（需要已创建虚拟环境）：
mcp/.venv/bin/python -m unittest discover -s tests -v
# 用已构建 DLL 与本机 SDK 执行主机契约测试，不启动 ATAS、不发送订单：
dotnet run --project tests/host_contract -- out/ATAS.AiBridge.dll \
  '/Applications/ATAS X.app/Contents/MonoBundle' indicator
```

以上是指定版本与单一模拟图表的运行验证，不代表已完成所有 ATAS 版本或长时间稳定性测试。
Windows 行为有路径和条件编译回归测试，但没有在 Windows 主机上运行完整构建／平台测试。
Mac 指标不提供账户持仓；Windows 上游策略的
仓位仍只统计策略自身成交，不包括手动成交。
手动成交日志、完整交易复盘与用户交易系统／prompt 管理仍是另外的功能。
