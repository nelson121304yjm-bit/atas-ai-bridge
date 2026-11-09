// Tests the actual compiled DLL without opening ATAS or invoking its trading API.
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Text.Json;

if (args.Length != 3)
    throw new ArgumentException("Usage: host_contract <plugin.dll> <SDK directory> indicator|strategy");
var sdk = Path.GetFullPath(args[1]);
AssemblyLoadContext.Default.Resolving += (_, name) =>
{
    var path = Path.Combine(sdk, name.Name + ".dll");
    return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
};
var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(Path.GetFullPath(args[0]));
var type = assembly.GetType("ATAS.AiBridge.AiBridgeStrategy", throwOnError: true)!;
var flags = BindingFlags.Instance | BindingFlags.NonPublic;
var checks = 0;
void Check(bool condition, string message)
{
    if (!condition) throw new InvalidOperationException("FAIL: " + message);
    checks++;
}

if (args[2] == "strategy")
{
    Check(type.BaseType?.FullName == "ATAS.Strategies.Chart.ChartStrategy", "legacy strategy base type");
    Check(type.GetMethod("PlaceOrder", flags) != null, "legacy trading implementation retained");
}
else
{
    Check(type.BaseType?.FullName == "ATAS.Indicators.Indicator", "macOS host must load as an Indicator");
    Check(type.GetMethod("PlaceOrder", flags) == null, "indicator must not contain order submission code");
    Check(type.GetMethod("CancelOrderById", flags) == null, "indicator must not contain cancellation code");
    Check(type.GetMethod("FlattenPosition", flags) == null, "indicator must not contain position-close code");
    Check(type.GetProperty("AllowTrading")?.SetMethod == null, "trading cannot be enabled on an indicator");
    Check(type.GetProperty("TradeToken")?.SetMethod == null, "indicator cannot accept a trade token");
    Check(type.GetProperty("BridgeStatus")?.PropertyType == typeof(string), "visible status property exists");
    var mouseFlags = flags | BindingFlags.Public;
    foreach (var name in new[] { "ProcessMouseDown", "ProcessMouseUp", "GetCursor" })
        Check(type.GetMethod(name, mouseFlags)?.DeclaringType == type,
              name + " must be handled by the indicator chart button");
    var hit = type.GetMethod("IsWorkbenchButtonHit", BindingFlags.Static | BindingFlags.NonPublic);
    Check(hit != null, "chart button hit test exists");
    var chart = new System.Drawing.Rectangle(10, 20, 500, 300);
    Check((bool)hit!.Invoke(null, new object[] { chart, 390, 30 })!, "button accepts inside click");
    Check(!(bool)hit.Invoke(null, new object[] { chart, 100, 30 })!, "button ignores chart clicks");
    Check(!(bool)hit.Invoke(null, new object[] { chart, 390, 100 })!, "button ignores clicks below it");

    // Avoid constructing ATAS UI/services. These rejection paths must fail closed
    // before they access any SDK state or mutable instance collections.
    var host = RuntimeHelpers.GetUninitializedObject(type);
    var cursorMethod = type.GetMethod("GetCursor", mouseFlags)!;
    var defaultCursor = cursorMethod.Invoke(host, new object?[] { null });
    Check(defaultCursor != null, "cursor callback handles absent mouse data");
    type.GetField("_workbenchMessage", flags)!.SetValue(host, "Workbench setup required");
    Check((((string?)type.GetProperty("BridgeStatus")!.GetValue(host)) ?? "").Contains("Workbench setup required"),
          "workbench installation status remains visible while bridge is unavailable");
    var handler = type.GetMethod("HandleRequest", flags)!;
    var waitingJson = (string)handler.Invoke(host, new object[] {
        "GET", "/api/quote", new Dictionary<string, string>(), "" })!;
    using (var waiting = JsonDocument.Parse(waitingJson ?? "{}"))
        Check(waiting.RootElement.TryGetProperty("ok", out var waitingOk) && !waitingOk.GetBoolean(),
              "market queries before initialization must report unavailable data");
    foreach (var (method, path, body) in new[] {
        ("POST", "/api/settings", "{\"set\":{\"allowTrading\":true}}"),
        ("POST", "/api/order", "{}"), ("GET", "/api/order", ""),
        ("POST", "/api/order/cancel", "{}"), ("POST", "/api/position/close", "{}"),
        ("GET", "/api/position", ""), ("GET", "/api/orders", ""),
        ("PUT", "/api/health", ""), ("DELETE", "/api/settings", "") })
    {
        var json = (string)handler.Invoke(host, new object[] {
            method, path, new Dictionary<string, string>(), body })!;
        using var result = JsonDocument.Parse(json);
        Check(result.RootElement.GetProperty("ok").GetBoolean() == false,
              method + " " + path + " must be rejected");
        Check(!result.RootElement.TryGetProperty("netVolume", out _), "do not fabricate account flatness");
    }

    type.GetField("_statusJson", flags)!.SetValue(host, "{\"ok\":true}");
    JsonDocument Status() => JsonDocument.Parse((string)handler.Invoke(host,
        new object[] { "GET", "/api/status", new Dictionary<string, string>(), "" })!);
    using (var status = Status())
    {
        Check(status.RootElement.TryGetProperty("freshness", out var freshness),
              "status must expose collection freshness before any live data");
        Check(freshness.GetProperty("snapshotStale").GetBoolean(), "unobserved data must be stale");
        Check(freshness.GetProperty("marketEventStale").GetBoolean(), "unobserved market events must be stale");
    }
    foreach (var field in new[] { "_lastSnapshotTicks", "_lastMarketEventTicks" })
        type.GetField(field, flags)!.SetValue(host, DateTime.UtcNow.AddSeconds(-10).Ticks);
    using (var status = Status())
        Check(status.RootElement.GetProperty("freshness").GetProperty("snapshotStale").GetBoolean(),
              "cached data becomes stale even without another chart callback");
    foreach (var field in new[] { "_lastSnapshotTicks", "_lastMarketEventTicks" })
        type.GetField(field, flags)!.SetValue(host, DateTime.UtcNow.Ticks);
    using (var status = Status())
        Check(!status.RootElement.GetProperty("freshness").GetProperty("marketEventStale").GetBoolean(),
              "a recent event must be reported as recent");

    var removePort = type.GetMethod("RemovePortFileIfOwned", BindingFlags.Static | BindingFlags.NonPublic);
    Check(removePort != null, "indicator must clean up only its own port discovery file");
    var temp = Path.GetTempFileName();
    try
    {
        File.WriteAllText(temp, "8791");
        removePort!.Invoke(null, new object[] { temp, 8790 });
        Check(File.ReadAllText(temp) == "8791", "disposing an old instance must preserve a newer port");
        removePort.Invoke(null, new object[] { temp, 8791 });
        Check(!File.Exists(temp), "disposing the owner removes its stale discovery file");
    }
    finally { File.Delete(temp); }
}
Console.WriteLine($"PASS: {args[2]} host contract ({checks} checks)");
