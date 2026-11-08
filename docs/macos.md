# macOS / ATAS X 使用指南

本分支给现有 ATAS AI Bridge 增加 macOS 构建、安装与端口发现；Windows 的
`build.ps1`、`mcp/setup-venv.ps1` 和交易开关行为保持原样。
MIT 许可证与上游署名保留。

## 1. 准备

- 已安装可正常使用的 ATAS X。编译使用本机安装目录的 SDK，不下载或分发 ATAS 厂商 DLL。
- .NET **10 SDK**（仅安装 runtime 不足以编译）。`dotnet --version` 应输出 10.x。
- Python **3.10+**。Python 依赖安装到项目内的 `mcp/.venv`，不改全局包。
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

默认安装位置：`~/Library/Application Support/ATAS/Strategies/ATAS.AiBridge.dll`。
脚本只复制本项目 DLL；已有同名 DLL 会在同目录保存为 `.bak.*`，再替换。
检测到 `ATAS X` 或 `OFT.Platform` 进程时默认拒绝安装。
`--install --force` 可以显式覆盖此保护，但不代表运行中的平台已经重新加载插件；
仍须重启平台后再验证。首次使用建议先退出 ATAS。

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

## 3. 准备 MCP

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
这是数据桥的 macOS 兼容补丁，不是内置模型聊天／自动交易复盘产品。

## 4. 在模拟图表启动并检查

按照主 README 的策略加载步骤，添加 **AI Bridge 图表策略**并通过策略控制按钮启动。
`IsActivated` 是回读状态，不是设置开关。先在模拟账户验证；保留“允许下单”关闭，
不设置 `ATAS_TRADE_TOKEN`。默认 MCP 工具仍含 `atas_set_settings`，不能把它描述成完全只读服务。

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

核对实际图表、账户、标的和 `isActivated`，再在 AI 客户端查询 footprint、CVD、DOM。
使用 Clash 等代理时，确保 `127.0.0.1`／`localhost` 走本机直连；不需要把桥端口开放到公网。

**不要把 `mcp/verify.py` 当成纯只读检查**：它会测试设置写入并尝试恢复。
本指南的初始检查只读取 health/status，不发送订单或修改设置。

## 验证范围

2026-11-08 在 Apple Silicon macOS、ATAS X **8.0.15.663**、.NET SDK **10.0.403**、
Python **3.13.2** 环境验证：

- 实际 SDK 的 Release 构建成功，0 警告、0 错误。
- 安装脚本使用临时 SDK／工具测试路径空格、显式安装、备份、运行中拒绝和构建失败不覆盖。
- 端口发现测试覆盖 Mac 路径、Windows 路径、优先级、无效文件和重新发现。
- MCP v2 虚拟环境可初始化 stdio 会话并列出工具；测试不调用行情、设置或交易工具。
- 当前分支的 20 项自动化测试全部通过；MCP 测试使用 `mcp 2.3.0`。

运行回归测试：

```bash
python3 -m unittest discover -s tests -v
# 含 MCP stdio 集成测试（需要已创建虚拟环境）：
mcp/.venv/bin/python -m unittest discover -s tests -v
```

没有在本次开发中加载插件到运行中的 ATAS，因此**平台内加载、策略启动和实时行情端点尚未验收**。
Windows 行为有路径回归测试，但没有在 Windows 主机上运行完整构建／平台测试。
编译及 MCP 协议测试不等于实际行情有效，也不改变上游只统计策略自身成交的仓位口径；
手动成交日志、完整交易复盘与用户交易系统／prompt 管理仍是另外的功能。
