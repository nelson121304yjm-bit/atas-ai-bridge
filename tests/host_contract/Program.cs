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
    foreach (var name in new[] { "ShowBridgeStatus", "ShowReviewButton" })
    {
        var property = type.GetProperty(name)!;
        var attribute = property.GetCustomAttribute<System.ComponentModel.DefaultValueAttribute>();
        Check(attribute?.Value is false, name + " defaults to hidden");
    }
    var open = type.GetProperty("OpenReviewTab")!;
    Check(open.GetCustomAttribute<System.ComponentModel.EditorAttribute>()?.EditorTypeName.Contains("ReviewOpenEditor") == true,
          "settings action uses the host-supported Control custom editor");
    Check(open.GetCustomAttribute<System.Xml.Serialization.XmlIgnoreAttribute>() != null,
          "settings action is not serialized as XML");
    Check(open.GetCustomAttribute<System.Text.Json.Serialization.JsonIgnoreAttribute>() != null,
          "settings action is not serialized as JSON");
    Check(open.GetCustomAttributesData().Any(a => a.AttributeType.FullName == "Newtonsoft.Json.JsonIgnoreAttribute"),
          "settings action is ignored by the host Newtonsoft serializer");
    var mouseFlags = flags | BindingFlags.Public;
    foreach (var name in new[] { "ProcessMouseDown", "ProcessMouseUp", "GetCursor" })
        Check(type.GetMethod(name, mouseFlags)?.DeclaringType == type,
              name + " must be handled by the indicator chart button");
    var hit = type.GetMethod("IsWorkbenchButtonHit", BindingFlags.Static | BindingFlags.NonPublic);
    Check(hit != null, "chart button hit test exists");
    var chart = new System.Drawing.Rectangle(10, 20, 500, 300);
    Check((bool)hit!.Invoke(null, new object[] { chart, 30, 30 })!, "left button accepts inside click");
    Check(!(bool)hit.Invoke(null, new object[] { chart, 390, 30 })!, "button ignores chart clicks");
    Check(!(bool)hit.Invoke(null, new object[] { chart, 30, 100 })!, "button ignores clicks below it");
    var geometry = type.GetMethod("WorkbenchButton", BindingFlags.Static | BindingFlags.NonPublic);
    Check(geometry != null, "render and hit-test share bounded button geometry");
    var label = type.GetField("WorkbenchButtonLabel", BindingFlags.Static | BindingFlags.NonPublic);
    Check(label?.GetRawConstantValue() as string == "AI Review",
          "native entry uses readable ASCII with the installed Arial font");
    foreach (var plot in new[] { chart, new System.Drawing.Rectangle(80, 140, 2350, 1200),
                                new System.Drawing.Rectangle(20, 30, 236, 56) })
    {
        var button = (System.Drawing.Rectangle)geometry!.Invoke(null, new object[] { plot })!;
        Check(button == new System.Drawing.Rectangle(plot.X + 8, plot.Y + 8, 220, 40),
              "button anchors at safe plot left without DPI assumptions");
        Check(plot.Contains(button), "entire button remains inside price plot bounds");
        Check((bool)hit.Invoke(null, new object[] { plot, button.X + 1, button.Y + 1 })!,
              "rendered button and clickable region agree");
        Check(!(bool)hit.Invoke(null, new object[] { plot, button.Right, button.Bottom })!,
              "outside bottom-right edge is not clickable");
    }
    foreach (var plot in new[] { new System.Drawing.Rectangle(80, 140, 235, 100),
                                new System.Drawing.Rectangle(80, 140, 500, 55),
                                System.Drawing.Rectangle.Empty })
    {
        Check(((System.Drawing.Rectangle)geometry!.Invoke(null, new object[] { plot })!).IsEmpty,
              "insufficient plot area suppresses button instead of clipping its label");
        Check(!(bool)hit.Invoke(null, new object[] { plot, plot.X + 10, plot.Y + 10 })!,
              "hidden narrow-chart button cannot consume mouse input");
    }

    // Avoid constructing ATAS UI/services. These rejection paths must fail closed
    // before they access any SDK state or mutable instance collections.
    var host = RuntimeHelpers.GetUninitializedObject(type);
    Check(open.GetValue(host) != null, "reading settings exposes action without launching");
    open.SetValue(host, null);
    Check((int)type.GetField("_workbenchLaunching", flags)!.GetValue(host)! == 0,
          "settings get/set or restore cannot launch the review");

    var staticFlags = BindingFlags.Static | BindingFlags.NonPublic;
    // A settings action must reject an unbound origin before scheduling launch.
    // These are real indicator instances with inert SDK construction bypassed;
    // no subprocess, ATAS UI, provider or trade is involved in these paths.
    var capturePort = type.GetMethod("CaptureReviewBridgePort", flags);
    Check(capturePort != null, "native opening requires an originating live bridge port");
    var requestOpen = type.GetMethod("RequestOpenReview", flags)!;
    foreach (var state in new[] { "uninitialized", "disabled", "listener failed", "no listener" })
    {
        var origin = RuntimeHelpers.GetUninitializedObject(type);
        type.GetField("_bridgeEnabled", flags)!.SetValue(origin, state != "disabled");
        type.GetField("_reviewRegistered", flags)!.SetValue(origin, state != "uninitialized");
        type.GetField("_hostRunning", flags)!.SetValue(origin, state == "no listener");
        requestOpen.Invoke(origin, null);
        Check((int)type.GetField("_workbenchLaunching", flags)!.GetValue(origin)! == 0,
              state + " origin cannot schedule a launcher or dock mutation");
        var guidance = (string?)type.GetField("_workbenchMessage", flags)!.GetValue(origin);
        Check(guidance?.Contains("Apply", StringComparison.OrdinalIgnoreCase) == true &&
              guidance.Contains("Bridge status", StringComparison.OrdinalIgnoreCase),
              state + " origin explains apply/enable and bridge-status recovery");
    }
    var bridgeType = assembly.GetType("ATAS.AiBridge.BridgeServer", true)!;
    var requestHandlerType = bridgeType.GetNestedType("RequestHandler")!;
    var fixtureHandler = Delegate.CreateDelegate(requestHandlerType,
        typeof(ContractBridgeFixture).GetMethod(nameof(ContractBridgeFixture.Reply))!);
    object NewBridge() => Activator.CreateInstance(bridgeType, new object[] { fixtureHandler, (Action<string>)(_ => { }) })!;
    var sources = new[] { NewBridge(), NewBridge() };
    try
    {
        var ports = new List<int>();
        foreach (var source in sources)
        {
            var probe = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
            probe.Start();
            var port = ((System.Net.IPEndPoint)probe.LocalEndpoint).Port;
            probe.Stop();
            var startArgs = new object?[] { port, port, null };
            Check((bool)bridgeType.GetMethod("Start")!.Invoke(source, startArgs)!, "isolated originating bridge listens on its assigned port");
            ports.Add(port);
        }
        Check(ports[0] != ports[1], "origin fixtures have distinct live chart ports");
        using (var client = new System.Net.Sockets.TcpClient())
        {
            await client.ConnectAsync("127.0.0.1", ports[0]);
            var stream = client.GetStream();
            await stream.WriteAsync(System.Text.Encoding.ASCII.GetBytes("GET /api/journal?days=1&days=2 HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"));
            using var reader = new StreamReader(stream);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(3));
            var response = await reader.ReadToEndAsync(deadline.Token);
            Check(response.StartsWith("HTTP/1.1 400"), "duplicate journal bounds rejected before handler dispatch");
        }
        var origin = RuntimeHelpers.GetUninitializedObject(type);
        type.GetField("_bridgeEnabled", flags)!.SetValue(origin, true);
        type.GetField("_reviewRegistered", flags)!.SetValue(origin, true);
        type.GetField("_hostRunning", flags)!.SetValue(origin, true);
        type.GetField("_server", flags)!.SetValue(origin, sources[0]);
        var captured = (int)capturePort!.Invoke(origin, null)!;
        Check(captured == ports[0], "native request captures only its originating chart's positive port");
        var validateSource = type.GetMethod("ValidateReviewSource", flags)!;
        validateSource.Invoke(origin, new object[] { sources[0], captured });
        type.GetField("_server", flags)!.SetValue(origin, sources[1]);
        try { validateSource.Invoke(origin, new object[] { sources[0], captured }); Check(false, "changed source cannot silently retarget another chart"); }
        catch (TargetInvocationException error) { Check(error.InnerException is InvalidOperationException, "changed source is rejected before launch"); }
        type.GetField("_server", flags)!.SetValue(origin, sources[0]);
        bridgeType.GetMethod("Stop")!.Invoke(sources[0], null);
        try { capturePort.Invoke(origin, null); Check(false, "stopped listener cannot reuse its old positive port"); }
        catch (TargetInvocationException error) { Check(error.InnerException is InvalidOperationException, "stopped listener is rejected despite its retained port"); }
    }
    finally { foreach (var source in sources) bridgeType.GetMethod("Stop")!.Invoke(source, null); }

    // Execute the production GUI lifetime guard with a deterministic dispatcher.
    // The queue pauses after the worker check, before the GUI action executes.
    var lifetimeType = assembly.GetType("ATAS.AiBridge.ReviewDockLifetime", true)!;
    var retainOwner = lifetimeType.GetMethod("Retain", flags)!;
    var releaseOwner = lifetimeType.GetMethod("Release", flags)!;
    var checkOwner = lifetimeType.GetMethod("Check", flags)!;
    var invokeUi = lifetimeType.GetMethod("InvokeAsync", flags)!;
    async Task Canceled(Task task)
    {
        try { await task; Check(false, "stale queued GUI work must be canceled"); }
        catch (OperationCanceledException) { Check(true, "stale queued GUI work is canceled inside dispatch"); }
    }
    foreach (var stage in new[] { "create/activate", "bind", "begin" })
    {
        var queue = new ContractUiQueue();
        var subscriptions = 0;
        var mutations = 0;
        var detached = 0;
        var lifetime = Activator.CreateInstance(lifetimeType, flags, null,
            new object[] { (Func<Action, Task>)queue.Dispatch, (Action)(() => { subscriptions = 0; detached++; }) }, null)!;
        var owner = retainOwner.Invoke(lifetime, null)!;
        // "begin" starts with an already-published binding and four subscriptions.
        if (stage == "begin")
        {
            var bind = (Task)invokeUi.Invoke(lifetime, new object[] { owner, CancellationToken.None,
                (Action)(() => subscriptions = 4) })!;
            queue.Run(0); await bind;
        }
        checkOwner.Invoke(lifetime, new object[] { owner, CancellationToken.None });
        var pending = (Task)invokeUi.Invoke(lifetime, new object[] { owner, CancellationToken.None,
            (Action)(() => { mutations++; if (stage == "bind") subscriptions = 4; }) })!;
        var release = (Task)releaseOwner.Invoke(lifetime, new[] { owner })!;
        queue.Run(1); await release; // last-owner cleanup wins before queued GUI work
        queue.Run(0); await Canceled(pending);
        Check(mutations == 0, "disposed origin cannot run queued " + stage);
        Check(subscriptions == 0 && detached == 1,
              "last release leaves no binding or event subscriptions after " + stage);
    }
    {
        var queue = new ContractUiQueue();
        var detachCount = 0;
        var mutations = 0;
        var lifetime = Activator.CreateInstance(lifetimeType, flags, null,
            new object[] { (Func<Action, Task>)queue.Dispatch, (Action)(() => detachCount++) }, null)!;
        var oldOwner = retainOwner.Invoke(lifetime, null)!;
        var liveOwner = retainOwner.Invoke(lifetime, null)!;
        var stale = (Task)invokeUi.Invoke(lifetime, new object[] { oldOwner, CancellationToken.None,
            (Action)(() => mutations++) })!;
        await (Task)releaseOwner.Invoke(lifetime, new[] { oldOwner })!;
        queue.Run(0); await Canceled(stale);
        var live = (Task)invokeUi.Invoke(lifetime, new object[] { liveOwner, CancellationToken.None,
            (Action)(() => mutations++) })!;
        queue.Run(0); await live;
        Check(mutations == 1 && detachCount == 0, "disposing one indicator preserves another live owner's usable binding");
        using var cancellation = new CancellationTokenSource();
        checkOwner.Invoke(lifetime, new object[] { liveOwner, cancellation.Token });
        var canceled = (Task)invokeUi.Invoke(lifetime, new object[] { liveOwner, cancellation.Token,
            (Action)(() => mutations++) })!;
        cancellation.Cancel(); queue.Run(0); await Canceled(canceled);
        Check(mutations == 1, "request cancellation is rechecked inside queued GUI work");
        var release = (Task)releaseOwner.Invoke(lifetime, new[] { liveOwner })!;
        var newOwner = retainOwner.Invoke(lifetime, null)!;
        queue.Run(0); await release;
        var oldGeneration = (Task)invokeUi.Invoke(lifetime, new object[] { liveOwner, CancellationToken.None,
            (Action)(() => mutations++) })!;
        queue.Run(0); await Canceled(oldGeneration);
        Check(detachCount == 0 && mutations == 1, "newly retained owner cannot revive an earlier released request");
        var finalRelease = (Task)releaseOwner.Invoke(lifetime, new[] { newOwner })!;
        queue.Run(0); await finalRelease;
        Check(detachCount == 1, "final live owner release detaches exactly once");
    }

    var resultType = assembly.GetType("ATAS.AiBridge.ReviewLaunchResult", true)!;
    var parse = resultType.GetMethod("Parse", staticFlags)!;
    var rootUrl = resultType.GetMethod("IsRootUrl", staticFlags)!;
    var cleanUrl = "http://127.0.0.1:8770/";
    foreach (var url in new[] { cleanUrl, "http://127.0.0.1:8780/" })
        Check((bool)rootUrl.Invoke(null, new object[] { url })!, "canonical local root accepted");
    foreach (var url in new[] { "http://localhost:8770/", "http://user@127.0.0.1:8770/",
        "http://127.0.0.1:8769/", "http://127.0.0.1:8781/", "http://127.0.0.1:8770/path",
        "http://127.0.0.1:8770/?query=1", "http://127.0.0.1:8770/#token=bad", "https://127.0.0.1:8770/",
        "http://example.com:8770/", "http://127.0.0.1:8770/../", "http://127.0.0.1:08770/" })
        Check(!(bool)rootUrl.Invoke(null, new object[] { url })!, "noncanonical/local root rejected");
    var syntheticToken = new string('a', 32);
    object LaunchResult() => parse.Invoke(null, new object[] {
        JsonSerializer.Serialize(new { url = cleanUrl, token = syntheticToken }) })!;
    foreach (var output in new[] { "{}", "not json", "[]", "{\"url\":null,\"token\":1}",
        JsonSerializer.Serialize(new { url = cleanUrl + "#secret", token = syntheticToken }),
        JsonSerializer.Serialize(new { url = cleanUrl, token = "too-short" }),
        JsonSerializer.Serialize(new { url = cleanUrl, token = syntheticToken, extra = true }),
        new string('x', 2049) })
    {
        try { parse.Invoke(null, new object[] { output }); Check(false, "malformed native output rejected"); }
        catch (TargetInvocationException ex)
        { Check(ex.InnerException is InvalidOperationException, "malformed native output rejected without data exposure"); }
    }
    var dockType = assembly.GetType("ATAS.AiBridge.ReviewDockHost", true)!;
    var ownership = dockType.GetMethod("IsOwnedPanel", staticFlags)!;
    var panelId = (string)dockType.GetField("PanelId", staticFlags)!.GetRawConstantValue()!;
    Check((bool)ownership.Invoke(null, new object[] { panelId, true, cleanUrl })!, "matching own WebPages root accepted");
    Check(!(bool)ownership.Invoke(null, new object[] { "user-webpage", true, cleanUrl })!, "user panel never claimed by URL");
    Check(!(bool)ownership.Invoke(null, new object[] { panelId, false, cleanUrl })!, "wrong widget type never claimed");
    Check(!(bool)ownership.Invoke(null, new object[] { panelId, true, "http://example.com/" })!, "marked remote page never claimed");
    var deliveryType = assembly.GetType("ATAS.AiBridge.ReviewBootstrapDelivery", true)!;
    object Delivery() => Activator.CreateInstance(deliveryType, flags, null, new[] { LaunchResult() }, null)!;
    var takeMessage = deliveryType.GetMethod("TakeMessage", flags)!;
    var acknowledge = deliveryType.GetMethod("TryAcceptAcknowledgement", flags)!;
    foreach (var (url, success, supported) in new[] { (cleanUrl, false, true), (cleanUrl, true, false),
                                                    ("http://127.0.0.1:8771/", true, true) })
    {
        try { takeMessage.Invoke(Delivery(), new object[] { url, success, supported }); Check(false, "unsafe native delivery rejected"); }
        catch (TargetInvocationException ex) { Check(ex.InnerException is InvalidOperationException, "unsafe native delivery rejected"); }
    }
    var delivery = Delivery();
    var message = (string)takeMessage.Invoke(delivery, new object[] { cleanUrl, true, true })!;
    using (var payload = JsonDocument.Parse(message))
    {
        var value = payload.RootElement;
        Check(value.GetProperty("token").GetString() == syntheticToken, "native message contains one-use token only in memory");
        Check(value.GetProperty("type").GetString() == "atas-ai-review.bootstrap", "native channel is bootstrap-only");
        Check(takeMessage.Invoke(delivery, new object[] { cleanUrl, true, true }) == null, "duplicate native navigation never replays token");
        var requestId = value.GetProperty("requestId").GetString();
        var ack = JsonSerializer.Serialize(new { type = "atas-ai-review.session", version = 1, requestId, ok = true });
        foreach (var (content, source, current) in new[] { (ack, "http://example.com/", cleanUrl),
            (ack, cleanUrl, "http://example.com/"), ("{}", cleanUrl, cleanUrl),
            (ack.Replace(requestId!, new string('0', 32)), cleanUrl, cleanUrl),
            (ack.Replace("\"version\":1", "\"version\":\"bad\""), cleanUrl, cleanUrl) })
        {
            var parameters = new object[] { content, source, current, false };
            Check(!(bool)acknowledge.Invoke(delivery, parameters)!, "wrong source/current/request/schema acknowledgement rejected");
        }
        var accepted = new object[] { ack, cleanUrl, cleanUrl, false };
        Check((bool)acknowledge.Invoke(delivery, accepted)! && (bool)accepted[3], "exact native authenticated-session acknowledgement accepted");
        Check(!(bool)acknowledge.Invoke(delivery, accepted)!, "duplicate acknowledgement ignored");
    }

    // Exercise the actual redirected-pipe implementation with synthetic child
    // scripts, without opening ATAS, contacting a provider, or starting a server.
    var launchType = assembly.GetType("ATAS.AiBridge.ReviewLaunch", true)!;
    var runLauncher = launchType.GetMethod("RunAsync", staticFlags)!;
    var scratch = Path.Combine(Path.GetTempPath(), "atas-native-contract-" + Guid.NewGuid().ToString("N"));
    Directory.CreateDirectory(scratch);
    try
    {
        var script = Path.Combine(scratch, "launcher.py");
        var python = Environment.GetEnvironmentVariable("REVIEW_TEST_PYTHON") ?? "/usr/bin/python3";
        async Task Run(string source, TimeSpan timeout, bool succeeds)
        {
            File.WriteAllText(script, source);
            var task = (Task)runLauncher.Invoke(null, new object[] { python, script, 8790, CancellationToken.None, timeout })!;
            try { await task; Check(succeeds, "native launcher expected to reject synthetic child"); }
            catch (InvalidOperationException ex)
            {
                Check(!succeeds, "native launcher rejects malformed stdout, failure or timeout");
                Check(!ex.Message.Contains(syntheticToken), "launcher exception never echoes pipe credentials");
            }
        }
        var marker = Path.Combine(scratch, "started");
        foreach (var invalidPort in new[] { 0, -1, 65536 })
        {
            File.WriteAllText(script, "from pathlib import Path; Path(" + JsonSerializer.Serialize(marker) + ").touch()");
            var invalid = (Task)runLauncher.Invoke(null, new object[] { python, script, invalidPort, CancellationToken.None, TimeSpan.FromSeconds(3) })!;
            try { await invalid; Check(false, "native launcher requires a positive originating port"); }
            catch (InvalidOperationException) { Check(!File.Exists(marker), "invalid port is rejected before starting any subprocess"); }
        }
        await Run("import json,sys; assert sys.argv[1:] == ['--native-json','--bridge-port','8790']; print(json.dumps({'url':'http://127.0.0.1:8770/','token':'" + syntheticToken + "'}))", TimeSpan.FromSeconds(3), true);
        await Run("print('malformed synthetic output')", TimeSpan.FromSeconds(3), false);
        await Run("import sys; sys.exit(1)", TimeSpan.FromSeconds(3), false);
        await Run("print('x' * 2049)", TimeSpan.FromSeconds(3), false);
        await Run("import sys; print('x' * 2049, file=sys.stderr)", TimeSpan.FromSeconds(3), false);
        await Run("import time; time.sleep(5)", TimeSpan.FromMilliseconds(100), false);
    }
    finally { Directory.Delete(scratch, true); }
    var mouseSequence = type.GetMethod("ShouldConsumeWorkbenchMouseUp", flags);
    Check(mouseSequence != null, "mouse release sequence helper exists");
    var pressed = type.GetField("_workbenchPressed", flags)!;
    pressed.SetValue(host, false); // press began elsewhere
    Check(!(bool)mouseSequence!.Invoke(host, new object[] { true })!,
          "release over button after outside press must reach chart");
    pressed.SetValue(host, true); // press began inside
    Check((bool)mouseSequence.Invoke(host, new object[] { true })!,
          "press and release inside button is consumed");
    Check(!(bool)pressed.GetValue(host)!, "mouse release resets pressed state");
    pressed.SetValue(host, true);
    Check(!(bool)mouseSequence.Invoke(host, new object[] { false })!,
          "release outside button after inside press reaches chart");
    Check(!(bool)pressed.GetValue(host)!, "outside release resets pressed state");
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
        ("POST", "/api/journal", "{}"), ("GET", "/api/journal", ""),
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
    var builtStatus = (string)type.GetMethod("BuildStatus", flags)!.Invoke(host, null)!;
    using (var status = JsonDocument.Parse(builtStatus))
    {
        Check(status.RootElement.TryGetProperty("chartType", out _),
              "indicator status exposes actual chart type");
        Check(status.RootElement.TryGetProperty("timeFrame", out _),
              "indicator status exposes actual chart timeframe");
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

sealed class ContractUiQueue
{
    private readonly List<(Action Action, TaskCompletionSource<bool> Completion)> _pending = new();
    internal Task Dispatch(Action action)
    {
        var completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        _pending.Add((action, completion));
        return completion.Task;
    }
    internal void Run(int index)
    {
        var work = _pending[index];
        _pending.RemoveAt(index);
        try { work.Action(); work.Completion.SetResult(true); }
        catch (OperationCanceledException) { work.Completion.SetCanceled(); }
        catch (Exception error) { work.Completion.SetException(error); }
    }
}

static class ContractBridgeFixture
{
    public static string Reply(string method, string path, Dictionary<string, string> query, string body) => "{}";
}
