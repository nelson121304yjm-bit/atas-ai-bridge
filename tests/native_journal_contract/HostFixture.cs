namespace ATAS.AiBridge;
using System.Collections.Concurrent;
using System.Text.Json;
using ATAS.DataFeedsCore;
using ATAS.DataFeedsCore.Statistics;
using ATAS.Indicators;

// Inert host boundary; all journal logic is linked from production source.
public partial class AiBridgeStrategy
{
    private bool _hostRunning = true;
    private bool _indicatorDisposed;
    private FixtureServer _server = new();
    private static readonly JsonSerializerOptions JsonOpts = new();
    private readonly ConcurrentQueue<CommandRequest> _actions = new();
    private class CommandRequest { public Func<string> Work; public TaskCompletionSource<string> Tcs; }
    private class FixtureServer { public int Port => 34000; }
    private readonly Security _security = new() { SecurityId = NativeJournalDto.SecurityId };
    private readonly List<Action<Portfolio>> _selected = new();
    private readonly List<Action<Portfolio>> _changed = new();
    private readonly List<Action<Security>> _securityChanged = new();
    private ITradingManager _fixtureManager;
    private ITradingManager TradingManager => _fixtureManager ??= FixtureProxy.Create<ITradingManager>((m, a) => {
        if (m.Name == "get_Security") return _security;
        var portfolios = m.Name.Contains("PortfolioSelected") ? _selected : _changed;
        if (m.Name.StartsWith("add_Portfolio")) { portfolios.Add((Action<Portfolio>)a[0]); return null; }
        if (m.Name.StartsWith("remove_Portfolio")) { portfolios.Remove((Action<Portfolio>)a[0]); return null; }
        if (m.Name == "add_SecuritySelected") { _securityChanged.Add((Action<Security>)a[0]); return null; }
        if (m.Name == "remove_SecuritySelected") { _securityChanged.Remove((Action<Security>)a[0]); return null; }
        throw new InvalidOperationException("Unexpected manager call " + m.Name);
    });
    private Portfolio Portfolio => TestPortfolio;
    private ITradingStatisticsProvider TradingStatisticsProvider => TestProvider;
    private Portfolio _fixturePortfolio;
    public Portfolio TestPortfolio { get => _fixturePortfolio; set { _fixturePortfolio = value; foreach(var callback in _selected.ToArray()) callback(value); } }
    public ITradingStatisticsProvider TestProvider { get; set; }
    public string TestSecurity { set { _security.SecurityId = value; foreach(var callback in _securityChanged.ToArray()) callback(_security); } }
    public int TestSubscriptionCount => _selected.Count + _changed.Count + _securityChanged.Count;
    public bool TestHasWork => !_actions.IsEmpty;
    public bool TestBusy => _journalBusy != 0;
    public string TestRead(Dictionary<string,string> query) => ReadNativeJournal(query);
    public void TestReplaceServer() => _server = new();
    public void TestDispose() { _indicatorDisposed = true; _hostRunning = false; DetachJournalSelections(); }
    public void TestPump() { ObserveJournalScope(); while (_actions.TryDequeue(out var item)) item.Tcs.TrySetResult(item.Work()); }
    private static string Error(string message) => JsonSerializer.Serialize(new { ok = false, error = message });
}
