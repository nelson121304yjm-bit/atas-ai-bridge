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

    // Avoid constructing ATAS UI/services. These rejection paths must fail closed
    // before they access any SDK state or mutable instance collections.
    var host = RuntimeHelpers.GetUninitializedObject(type);
    var handler = type.GetMethod("HandleRequest", flags)!;
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
}
Console.WriteLine($"PASS: {args[2]} host contract ({checks} checks)");
