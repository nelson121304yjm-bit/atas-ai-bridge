# ATAS 原生交易日志验证记录

本次只实现原生日志数据路径；不安装、不启动／停止 ATAS、不调用真实模型、不交易。
原生 dock 工作暂停，浏览器启动器是本次验收入口。实机对账由独立控制器补记，未完成前不声明通过。

## 公开 API 与数据

当前 SDK 的 `ITradingStatisticsProvider.LoadHistoryAsync(from,to,accounts,securities)` 返回独立查询统计，
只读取 `HistoryMyTrades` 与用于明确开仓方向的 `MyTrades`，不修改全局过滤器，不读取 Replay。
SDK 将该方法标记为 `[Obsolete]`，属性 Message 为空、IsError=false；公开接口未提供另一条同范围加载方法。
本次保留这一个明确的 CS0612 警告，没有新增全局抑制，也不换成未限定范围的集合。运行时支持仍待实机确认。

`GET /api/journal`：默认7天、100行，参数 days=1..7、limit=1..100，拒绝账户/品种或其他参数。
HTTP等待8秒；伴随端的该固定路径等待10秒。无tick/加载失败/切换来源/卸载均关闭失败，
未取消的 SDK 任务保持单飞直到完成，不用旧缓存或替代账户。

所有价格／数量／原生盈亏／手续费采用十进制字符串，缺手续费保持 null；时间保留 ISO 与 Kind，
Unspecified 不增加 Z。记录按原生配对行返回，部分平仓不拼接成整次交易。
证据采用明确字段白名单，移除账户／行／成交私有引用，不混入当前行情或推断净收益。

## 实施记录

- Python 全回归：`rtk proxy mcp/.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -q`，148 通过（临时回环服务需沙箱外测试权限）。后续覆盖范围字段白名单检查：12 项原生日志测试通过。
- UI：`rtk proxy /Users/nelson/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node --test tests/workbench_ui.test.mjs`，14 通过。新增本地证据零模型配置、十进制显示、来源改变后丢弃迟到结果检查。
- SDK DTO／生命周期：`rtk proxy env DOTNET_CLI_HOME=/private/tmp/atas-dotnet-home DOTNET_CLI_TELEMETRY_OPTOUT=1 /private/tmp/atas-dotnet/dotnet run --project tests/native_journal_contract -- /Applications/'ATAS X.app'/Contents/MonoBundle`，123 检查通过。合成 HistoryMyTrade／MyTrade、两账户、部分平仓、重复／截断、无手续费、未知方向／时区、宿主线程、故障／超时、选择切走再切回、卸载、通知解绑均覆盖。
- Mac 0.5.0-macos.1：构建至 `/private/tmp/atas-native-journal-build/mac/`，0 错误、1 个上述 CS0612；旧策略另建至 `/private/tmp/atas-native-journal-build/legacy/`，0 错误／0 警告。未把二进制写入 Git。
- 真实编译 DLL 的已有 host_contract：Mac 167 检查通过（含重复参数／journal只读检查），旧策略2检查通过。
- `rtk proxy git diff --check` 通过。全程未触碰 ATAS 进程／UI／安装目录／交易账户，未调用真实模型。

实机加载与独立原生记录对账待控制器执行。当前 `LoadHistoryAsync` 返回集合在宿主回调线程转为 DTO；HTTP 线程不枚举 SDK 集合。
没有当前已加载 Realtime 回退，因为没有实机证据证明需要或能安全获得相同查询范围；失败会明确报错。
无行情回调时请求最多等待8秒。SDK不提供取消，超时加载直到完成仍保持单飞；永不返回旧账户缓存。

