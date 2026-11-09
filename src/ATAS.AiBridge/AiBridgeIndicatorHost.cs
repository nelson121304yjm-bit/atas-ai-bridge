#if MAC_INDICATOR
namespace ATAS.AiBridge;

using System;
using System.ComponentModel;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text.Json.Nodes;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Controls;
using Avalonia.Threading;
using ATAS.DataFeedsCore;
using ATAS.Indicators;
using OFT.Rendering.Context;
using OFT.Rendering.Control;
using OFT.Rendering.Tools;
using OFT.Rendering;

public partial class AiBridgeStrategy
{
    private volatile bool _hostRunning;
    private volatile string _hostStatus = "Waiting for indicator initialization.";
    private long _lastSnapshotTicks;
    private long _lastMarketEventTicks;
    private readonly RenderFont _statusFont = new RenderFont("Arial", 12);
    private readonly RenderFont _workbenchFont = new RenderFont("Arial", 12);
    private const string WorkbenchButtonLabel = "AI Review";
    private bool _renderErrorLogged;
    private bool _workbenchPressed;
    private int _workbenchLaunching;
    private volatile string _workbenchMessage;
    private readonly CancellationTokenSource _reviewCancellation = new CancellationTokenSource();
    private ReviewOpenAction _reviewOpenAction;
    private volatile bool _indicatorDisposed;
    private bool _reviewRegistered;
    private Portfolio Portfolio => TradingManager?.Portfolio;
    private bool IsActivated => _hostRunning;

    [DisplayName("Bridge status (read only)")]
    public string BridgeStatus
    {
        get
        {
            if (!_hostRunning)
                return _workbenchMessage == null ? _hostStatus :
                    (_hostStatus ?? "AI Bridge") + " | " + _workbenchMessage;
            var now = DateTime.UtcNow.Ticks;
            var age = AgeMilliseconds(Interlocked.Read(ref _lastMarketEventTicks), now);
            var state = age == null ? "waiting live tick" : age > 5000 ? "stale" : "receiving data";
            var status = "AI Bridge | READ ONLY | :" + (_server?.Port ?? 0) + " | " + state;
            return _workbenchMessage == null ? status : status + " | " + _workbenchMessage;
        }
    }

    [DisplayName("Show bridge status on chart")]
    [DefaultValue(false)]
    public bool ShowBridgeStatus { get; set; } = false;

    [DisplayName("Show AI review button on chart")]
    [DefaultValue(false)]
    public bool ShowReviewButton { get; set; } = false;

    [DisplayName("Open AI review tab")]
    [Editor(typeof(ReviewOpenEditor), typeof(Control))]
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    [System.Xml.Serialization.XmlIgnore]
    [System.Text.Json.Serialization.JsonIgnore]
    [Newtonsoft.Json.JsonIgnore]
    [OFT.Attributes.IgnoreClone]
    public ReviewOpenAction OpenReviewTab
    {
        get => _reviewOpenAction ??= new ReviewOpenAction(RequestOpenReview);
        // A writable value keeps the host editor enabled. Applying, restoring or
        // deserializing settings cannot launch anything: only the editor Click can.
        set { }
    }

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
        if (!_reviewRegistered) { ReviewDockHost.Retain(); _reviewRegistered = true; }
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
        _indicatorDisposed = true;
        _reviewCancellation.Cancel();
        if (_reviewRegistered) { _reviewRegistered = false; ReviewDockHost.Release(); }
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
        if (ChartInfo == null) return;
        try
        {
            var area = WorkbenchArea;
            if (area.Width < 100 || area.Height < 40) return;
            var button = ShowReviewButton ? WorkbenchButton(area) : Rectangle.Empty;
            if (!button.IsEmpty)
            {
                context.FillRectangle(Color.FromArgb(235, 25, 90, 102), button);
                context.DrawString(WorkbenchButtonLabel, _workbenchFont, Color.White,
                                   button.X + 10, button.Y + 6);
            }
            if (ShowBridgeStatus)
            {
                var text = BridgeStatus;
                var measured = context.MeasureString(text, _statusFont);
                var width = Math.Min(area.Width - 16, (int)Math.Ceiling((double)measured.Width) + 20);
                var height = Math.Max(28, (int)Math.Ceiling((double)measured.Height) + 12);
                var y = button.IsEmpty ? area.Y + 8 : button.Bottom + 8;
                if (y + height > area.Bottom - 8) return;
                var box = new Rectangle(area.X + 8, y, width, height);
                context.FillRectangle(Color.FromArgb(230, 20, 28, 40), box);
                // The rectangle overload clips the text on the tested Retina host.
                context.DrawString(text, _statusFont, Color.White, box.X + 10, box.Y + 6);
            }
        }
        catch (Exception ex)
        {
            if (!_renderErrorLogged) Log("Status label: " + ex.Message);
            _renderErrorLogged = true;
        }
    }

    // The price plot can be smaller than ChartArea (axes, panels and side tools).
    // Use the SDK's plot coordinates for both drawing and mouse input; do not infer DPI.
    private Rectangle WorkbenchArea => ChartInfo?.PriceChartContainer?.Region ?? Rectangle.Empty;

    private static Rectangle WorkbenchButton(Rectangle area) =>
        area.Width >= 236 && area.Height >= 56
            ? new Rectangle(area.X + 8, area.Y + 8, 220, 40)
            : Rectangle.Empty;

    private static bool IsWorkbenchButtonHit(Rectangle area, int x, int y) =>
        !WorkbenchButton(area).IsEmpty && WorkbenchButton(area).Contains(x, y);

    private bool ShouldConsumeWorkbenchMouseUp(bool insideButton)
    {
        var consume = _workbenchPressed && insideButton;
        _workbenchPressed = false;
        return consume;
    }

    public override bool ProcessMouseDown(RenderControlMouseEventArgs e)
    {
        _workbenchPressed = false;
        try
        {
            if (e != null && e.ChangedButton == RenderControlMouseButtons.Left &&
                ShowReviewButton && ChartInfo != null && IsWorkbenchButtonHit(WorkbenchArea, e.X, e.Y))
            {
                _workbenchPressed = true;
                return true;
            }
        }
        catch (Exception) { _workbenchMessage = "Workbench button unavailable"; }
        return false;
    }

    public override bool ProcessMouseUp(RenderControlMouseEventArgs e)
    {
        try
        {
            var insideButton = e != null && e.ChangedButton == RenderControlMouseButtons.Left &&
                ShowReviewButton && ChartInfo != null && IsWorkbenchButtonHit(WorkbenchArea, e.X, e.Y);
            if (ShouldConsumeWorkbenchMouseUp(insideButton))
            {
                RequestOpenReview();
                return true;
            }
        }
        catch (Exception)
        {
            _workbenchPressed = false;
            _workbenchMessage = "Workbench button unavailable";
        }
        return false;
    }

    public override StdCursor GetCursor(RenderControlMouseEventArgs e)
    {
        try
        {
            if (ShowReviewButton && e != null && ChartInfo != null && IsWorkbenchButtonHit(WorkbenchArea, e.X, e.Y))
                return StdCursor.Hand;
        }
        catch { /* A chart callback must not propagate an optional UI error. */ }
        return base.GetCursor(e);
    }

    private void RequestOpenReview()
    {
        if (_indicatorDisposed || Interlocked.CompareExchange(ref _workbenchLaunching, 1, 0) != 0) return;
        SetWorkbenchMessage("Opening native AI review tab");
        _ = Task.Run(LaunchWorkbench);
    }

    private void SetWorkbenchMessage(string message)
    {
        _workbenchMessage = message;
        Dispatcher.UIThread.Post(() =>
        {
            if (!_indicatorDisposed) RaisePropertyChanged(nameof(BridgeStatus));
        });
    }

    private async Task LaunchWorkbench()
    {
        try
        {
            SetWorkbenchMessage(await ReviewDockHost.OpenInstalledAsync(_server?.Port ?? 0, _reviewCancellation.Token));
        }
        catch (OperationCanceledException) { SetWorkbenchMessage("AI review opening canceled."); }
        catch (InvalidOperationException ex)
        {
            SetWorkbenchMessage(ex.Message);
        }
        catch (Exception) { SetWorkbenchMessage("Native review unavailable; check ATAS/installation compatibility. Capture remains active."); }
        finally
        {
            Interlocked.Exchange(ref _workbenchLaunching, 0);
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
