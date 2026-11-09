# ATAS 原生停靠复盘面板设计

日期：2026-11-09。用户已明确选择：**必须是可停靠侧栏或标签页**。

## 交付目标

在 ATAS X 工作区创建 `AI 复盘` 标签页，沿用宿主原生停靠能力，显示现有中文本机工作台。
重复打开激活同一插件所属面板；关闭后从指标设置重新打开。用户可用 ATAS 的布局操作移到图表旁。
独立浮动窗口、外部浏览器、画在图表内的仿面板不算满足要求。
图上大按钮和状态标签默认隐藏，保留显式设置可显示。主要入口为指标设置中的打开面板操作。
不抢占系统或交易快捷键。浏览器启动器保留为手动备用，不自动代替停靠面板。

## 宿主依据

本机 ATAS X 8.0.15.663 只读程序集元数据确认公共接口：
- `PanelDockFactory.Instance`、`WorkspaceManager`、`CanOpenWidget`、`CreateWidget`、`CreateLayout`。
- `MainViewModel.AddTab(IProportionalDock, string, bool)` 与 `PanelViewModel.Activate()`。
- `WorkspaceLayoutType.WebPages`、`WebPagesSettings.CurrentUrl`、`WebPagesViewModel.Navigator`。
- `WebViewControl.Navigate`、`PostWebMessageAsJson`、`IsWebMessagingSupported`。

采用宿主 WebPages widget 和原生 dock。不得修改宿主程序集、反射私有字段、跳过许可/回放/品牌限制，
或把用户已有的任意 WebPages 页面当成插件面板。API 有版本耦合，不描述为官方稳定扩展协议。
只引用本机程序集，不分发 vendor DLL。

## 登录及保存

Python 后台继续负责证据、凭据和分析。ATAS 只启动/复用后台并显示页面。
启动器通过重定向 stdout 管道提供规范的 `http://127.0.0.1:8770..8780/` 地址和短时一次性 token。
不得把 token、Key、cookie 放进命令参数、日志、布局、标题或持久化 URL。
首选 `PostWebMessageAsJson` 向已加载的精确本机 origin 传递 token。
网页用原有 `/api/bootstrap` 换取 HttpOnly 会话，保持同源、CSRF、Key 目的地绑定、显式发送检查。
原生消息不提供执行命令、下单、任意网址、文件访问或通用 RPC。处理页面就绪竞态，避免 hash-only 重开。
若宿主不支持可靠消息传递，只能用经独立审查、证明不会保存或记录凭证的等效办法，不能削弱认证。

持久化面板 URL 只包含无凭证本机根地址。后台重启导致过期时，从指标设置重新打开获得新会话。
只在明确打开时启动后台；不自动调用模型、不自动分析、不交易。

## 所属关系和生命周期

- 新面板用稳定专属 ID，并核对 widget 类型/地址；只激活匹配面板。
- GUI 操作在宿主 GUI 线程执行；进程等待/网络在后台线程执行。
- 复用刷新认证，不重复创建后台或注册网页事件。
- 关闭释放该面板事件/WebView 引用；指标卸载不关闭其他图表、记录器、后台或用户面板。
- 恢复布局只恢复无凭证 URL；重新打开可认领精确标记面板。
- 缺失接口、许可拒绝、WebView 不支持、后台失败显示具体状态，保持行情采集。

## 验收与范围

编译/自动化后，在现有 Crypto Sim / BTCUSDT 验证真正的 ATAS 标签页。
验证四个模块、激活/关闭/重开、后台复用、过期会话恢复、默认不遮挡图表、布局不含 token。
只使用合成模型端点/凭据；无真实 provider 请求、交易或 Binance 自动连接变更。
保留现有图表、账户、指标数量、采集设置。不激活试用、不升级、不绕过平台限制。
Windows 条件构建保持通过，不等于 Windows UI 实机验收。

## 取舍

首版提供新原生标签页，再由用户按宿主规则停靠；不自动重排交易图表。
网页基线 `a3bda9e` 已推送用户 fork。最终文档区分编译、自动化、原生 UI 的验证范围。
