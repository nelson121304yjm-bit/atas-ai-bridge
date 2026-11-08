#if MAC_INDICATOR
namespace ATAS.AiBridge;

using System;
using System.ComponentModel;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text.Json.Nodes;
using System.Threading;
using ATAS.DataFeedsCore;
using ATAS.Indicators;
using OFT.Rendering.Context;
using OFT.Rendering.Tools;

public partial class AiBridgeStrategy
{
    private volatile bool _hostRunning;
    private volatile string _hostStatus = "Waiting for indicator initialization.";
    private long _lastSnapshotTicks;
    private long _lastMarketEventTicks;
    private readonly RenderFont _statusFont = new RenderFont("Arial", 12);
    private bool _renderErrorLogged;
    private Portfolio Portfolio => TradingManager?.Portfolio;
    private bool IsActivated => _hostRunning;

    [DisplayName("Bridge status (read only)")]
    public string BridgeStatus
    {
        get
        {
            if (!_hostRunning) return _hostStatus;
            var now = DateTime.UtcNow.Ticks;
            var age = AgeMilliseconds(Interlocked.Read(ref _lastMarketEventTicks), now);
            var state = age == null ? "waiting live tick" : age > 5000 ? "stale" : "receiving data";
            return "AI Bridge | READ ONLY | :" + (_server?.Port ?? 0) + " | " + state;
        }
    }

    [DisplayName("Show bridge status on chart")]
    public bool ShowBridgeStatus { get; set; } = true;

    public AiBridgeStrategy() : base(true)
    {
        DenyToChangePanel = true;
        DataSeries[0].IsHidden = true;
        ((ValueDataSeries)DataSeries[0]).VisualType = VisualMode.Hide;
        EnableCustomDrawing = true;
        DrawAbovePrice = true;
        SubscribeToDrawingEvents(DrawingLayouts.Final);
    }

    protected override void OnInitialize()
    {
        if (_server != null) return;
        if (!BridgeEnabled) { _hostStatus = "Disabled in indicator settings."; return; }
        try
        {
            _statusJson = Error("Waiting for chart data.");
            _server = new BridgeServer(HandleRequest, Log);
            if (!_server.Start(FirstPort, LastPort, out var error))
            {
                _server = null;
                _hostStatus = "Listener failed: " + error;
                return;
            }
            _hostRunning = true;
            SubscribeToTimer(TimeSpan.FromSeconds(1), RedrawStatus);
            WritePortFile(_server.Port);
            _hostStatus = "READ ONLY | 127.0.0.1:" + _server.Port;
            Log("Indicator listening on 127.0.0.1:" + _server.Port + " (read-only)");
        }
        catch (Exception ex)
        {
            _hostRunning = false;
            _server?.Stop();
            _server = null;
            _hostStatus = "Listener failed: " + ex.Message;
        }
    }

    protected override void OnDispose()
    {
        _hostRunning = false;
        UnsubscribeFromTimer(TimeSpan.FromSeconds(1), RedrawStatus);
        var port = _server?.Port ?? 0;
        _server?.Stop();
        _server = null;
        _hostStatus = "Stopped.";
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        foreach (var name in new[] { "ATAS", "ATAS X" })
            RemovePortFileIfOwned(Path.Combine(appData, name, "AiBridge.port"), port);
        base.OnDispose();
    }

    private void RedrawStatus()
    {
        if (_hostRunning && ShowBridgeStatus)
            RedrawChart(new RedrawArg(ChartArea));
    }

    protected override void OnRender(RenderContext context, DrawingLayouts layout)
    {
        if (!ShowBridgeStatus || ChartInfo == null) return;
        try
        {
            var area = ChartArea;
            if (area.Width < 100 || area.Height < 40) return;
            var text = BridgeStatus;
            var measured = context.MeasureString(text, _statusFont);
            var width = Math.Min(area.Width - 16, (int)Math.Ceiling((double)measured.Width) + 20);
            var box = new Rectangle(area.X + 8, area.Y + 8, width, 28);
            context.FillRectangle(Color.FromArgb(230, 20, 28, 40), box);
            context.DrawString(text, _statusFont, Color.White,
                new Rectangle(box.X + 10, box.Y + 5, box.Width - 20, 22));
        }
        catch (Exception ex)
        {
            if (!_renderErrorLogged) Log("Status label: " + ex.Message);
            _renderErrorLogged = true;
        }
    }

    protected override void OnNewTrade(MarketDataArg trade)
    {
        if (_hostRunning && trade != null)
            Interlocked.Exchange(ref _lastMarketEventTicks, DateTime.UtcNow.Ticks);
    }

    private string WithHostFreshness(string snapshot)
    {
        var result = JsonNode.Parse(snapshot)!.AsObject();
        var now = DateTime.UtcNow.Ticks;
        var captured = Interlocked.Read(ref _lastSnapshotTicks);
        var received = Interlocked.Read(ref _lastMarketEventTicks);
        var snapshotAge = AgeMilliseconds(captured, now);
        var eventAge = AgeMilliseconds(received, now);
        result["freshness"] = new JsonObject
        {
            ["snapshotCapturedAt"] = UtcTimestamp(captured),
            ["snapshotAgeMs"] = snapshotAge,
            ["lastMarketEventReceivedAt"] = UtcTimestamp(received),
            ["marketEventAgeMs"] = eventAge,
            ["snapshotStale"] = snapshotAge == null || snapshotAge > 5000,
            ["marketEventStale"] = eventAge == null || eventAge > 5000,
            ["note"] = "Local capture/reception times; not exchange timestamps or a guarantee of feed completeness."
        };
        return result.ToJsonString(JsonOpts);
    }

    private static long? AgeMilliseconds(long ticks, long now) =>
        ticks == 0 ? null : Math.Max(0, (now - ticks) / TimeSpan.TicksPerMillisecond);

    private static string UtcTimestamp(long ticks) => ticks == 0 ? null :
        new DateTime(ticks, DateTimeKind.Utc).ToString("o", CultureInfo.InvariantCulture);

    private static void RemovePortFileIfOwned(string path, int port)
    {
        if (port == 0) return;
        try
        {
            if (File.Exists(path) && File.ReadAllText(path).Trim() == port.ToString(CultureInfo.InvariantCulture))
                File.Delete(path);
        }
        catch { /* Discovery files are best effort; never stop ATAS disposal. */ }
    }
}
#endif
