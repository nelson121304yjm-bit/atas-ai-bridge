# 原生 AI 复盘标签页验证记录

日期：2026-11-09。目标宿主：本机 ATAS X 8.0.15.663 / .NET 10 / macOS。

## 实现边界

- 使用公共 `PanelDockFactory.Instance`、`CanOpenWidget(WebPages)`、`CreateWidget`、
  `CreateLayout`、`MainViewModel.AddTab`；不创建浮动窗口、不自动打开浏览器。
- 面板 ID 固定为 `atas-ai-bridge.review.v1`，认领须同时匹配 ID、WebPages 类型和规范本机根 URL。
  不按标题或 URL 认领用户页面；有重复标记、外部地址或类型不匹配时停止并报告。
- 只从所属 `WebPagesViewModel.Navigator -> WebPagesView.Content` 获取公共 `WebViewControl`。
  API 有宿主版本耦合，不宣称官方稳定插件协议；只引用本机 DLL，全部 `Private=false`。
- 指标设置的 `Open AI review tab` 使用宿主支持的 `EditorAttribute -> Avalonia Control`。
  只有按钮 Click 执行动作；属性读取、写入、克隆或反序列化不启动后台。
  只有已初始化、已启用且有实际监听器的当前指标可打开；请求捕获该实例的正数桥端口，
  启动及各次 UI 操作前重新核对同一实例/端口，不回退到后台上次选择的图表。
  不满足条件时先启用 **启用 AI 桥**、Apply 当前图表的指标设置，并检查 **Bridge status**。
- 后台进程等待及管道读取在后台线程，dock/WebView 操作在 Avalonia UI 线程。
  打开请求全局串行，同一面板只有一组事件；超时/取消清理待发凭证，关闭或最后一个指标卸载时清理引用/事件。
  每次排队的创建/激活、绑定和导航在 UI 执行时重新核对请求取消和唯一活跃所有者；
  最后一个所有者卸载后，旧请求不能重新建立绑定，新指标也不能恢复旧请求。
  不关闭宿主面板、其他图表、采集器或已启动的 Python 服务。

## 原生认证协议

`companion.launch --native-json` 的重定向 stdout 只有一个 JSON 对象：`url` 为
`http://127.0.0.1:8770..8780/`，`token` 为短时一次性值。`--open` / `--url-file`
保持原用途，三种模式互斥。原生管道限长 2048 字符、默认超时 10 秒，出错不打印 stdout/stderr。
命令参数只有启动器路径、模式和必需的当前指标桥端口；无效端口在启动子进程前拒绝。

精确根 URL 的成功 `NavigationCompleted` 后，宿主经 `PostWebMessageAsJson` 发送：

```json
{"type":"atas-ai-review.bootstrap","version":1,"token":"<one-use, memory only>","requestId":"<32 lowercase hex>"}
```

网页 `<head>` 中的监听器使用已验证的 `window.chrome.webview.addEventListener("message", ...)`。
只接受顶层本机根页面、固定协议类型/版本/格式，排队处理启动前后的消息并忽略重复 token。
网页沿用 `/api/bootstrap`，成功取得 CSRF 后经同一公共原生通道发送：

```json
{"type":"atas-ai-review.session","version":1,"requestId":"<same request>","ok":true}
```

失败为 `ok:false`。宿主核对消息 `Source` 与 WebView `CurrentUrl` 均精确等于预期根地址，
核对请求 ID 及完整 schema，且只接受一次。指标状态只有在成功确认后才显示
`native session authenticated`。不提供任意脚本、文件访问、下单、地址导航或通用 RPC。

## 已执行与待执行

自动化结果（`rtk` 包装实际执行；完整命令和失败后纠正记录见本次实现报告）：

| 检查 | 结果 |
|---|---|
| Python 全量回归，`-W error::ResourceWarning` | 136 通过 |
| 浏览器/原生消息 UI 回归 | 13 通过 |
| 本机 installed SDK Mac indicator 构建 | 0 编译器警告、0 错误；存在已记录的环境 CSSM 诊断 |
| 实际 Mac DLL host contract（含首轮审查修复） | 162 检查通过 |
| 同 SDK legacy 条件构建 | 0 编译器警告、0 错误 |
| 实际 legacy DLL host contract | 2 检查通过 |

发布说明不得把编译或合成测试当作 ATAS dock 实机通过。

控制器独立原生烟雾测试（公共宿主 WebView、合成页面）已确认：

- `IsSupported=True`、`IsWebMessagingSupported=True`；导航完成与规范 `CurrentUrl` 可用。
- 原生 JSON 在 `window.chrome.webview` 的 `message` 事件中为对象；普通 window message 不接收。
- 网页 `chrome.webview.postMessage` 产生公共 `WebMessageReceived`，带精确根 `Source` 和 JSON。
- 独立实际工作台、隔离状态、不可达桥端口、空模型/日志设置下，完成真实一次性会话交换并收到
  `ok:true`、请求匹配的原生确认。未请求真实模型或交易接口。

以下 ATAS 工作区验收仍由控制器/用户在审查后执行，本次实现未安装或操作 ATAS UI：

1. 记录现有 Crypto Sim / BTCUSDT 图表、指标数量、账户、采集器、Binance 自动连接和服务设置；
   用备份安装产物并按宿主要求重启/加载，保持这些设置。
2. 从 BTCUSDT 图表的 **Indicators → AI Bridge — read only → 指标设置 → Open AI review tab** 点击按钮，
   确认产生真正的 **AI 复盘** 标签页，可由宿主停靠到图表旁；
   核对四个中文模块可用、实际使用该图表的桥端口及 `Bridge status (read only)` 的所属面板、无凭证 URL、认证成功状态。
   未初始化/禁用/监听失败的设置入口必须给出启用、Apply 与 Bridge status 指引，不得启动或改选后台源；
   宿主若传入编辑副本，应先 Apply 并从实际运行指标入口验证，不得猜选其他图表。
3. 重复打开只激活同一面板；关闭重开、后台复用、会话过期后显式重开恢复均可用。
   确认图上按钮/状态新实例默认关闭，没有默认快捷键。旧工作区已保存的
   **Show bridge status on chart=true** 继续生效；需要时由用户显式关闭，不强制覆盖原设置。
4. 在许可/回放/品牌拒绝、后台失败、端口冲突或 WebView 异常时，确认具体状态可见且行情采集继续；
   不激活试用、升级订阅或绕过限制。
5. 保存布局，私下检查工作区文件及日志不含真实 bootstrap token、Key 或 cookie；只包含规范根 URL。
   关闭/恢复布局后显式打开仍只认领精确标记面板，不修改用户 WebPages。
6. 如需测试模型，只用合成服务及凭据、明确点击。不得进行真实 provider 调用、交易或连接配置变更。

Windows 条件构建和 legacy host contract 只证明原策略编译/合约仍成立，不代表 Windows UI 实机通过。
