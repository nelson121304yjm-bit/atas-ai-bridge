#if MAC_INDICATOR
namespace ATAS.AiBridge;

using System;
using System.ComponentModel;
using ATAS.DataFeedsCore;
using ATAS.Indicators;

public partial class AiBridgeStrategy
{
    private volatile bool _hostRunning;
    private volatile string _hostStatus = "Waiting for indicator initialization.";
    private Portfolio Portfolio => TradingManager?.Portfolio;
    private bool IsActivated => _hostRunning;

    [DisplayName("Bridge status (read only)")]
    public string BridgeStatus => _hostStatus;

    public AiBridgeStrategy() : base(true)
    {
        DenyToChangePanel = true;
        DataSeries[0].IsHidden = true;
        ((ValueDataSeries)DataSeries[0]).VisualType = VisualMode.Hide;
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
        _server?.Stop();
        _server = null;
        _hostStatus = "Stopped.";
        base.OnDispose();
    }
}
#endif
