#if MAC_INDICATOR
namespace ATAS.AiBridge;

using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using ATAS.DataFeedsCore;
using ATAS.DataFeedsCore.Statistics;
using ATAS.Indicators;

// Only called on the indicator calculation thread. No SDK objects leave this boundary.
internal static class NativeJournalDto
{
    internal const string SecurityId = "BTCUSDT@BinanceFutures";
    internal const int ScanLimit = 10000;
    private static string D(decimal value) => value.ToString(CultureInfo.InvariantCulture);
    private static object Time(DateTime value) => new {
        iso = value == default ? null : value.ToString("o", CultureInfo.InvariantCulture),
        kind = value.Kind.ToString()
    };

    internal static Dictionary<string, object> Build(IEnumerable<HistoryMyTrade> history,
        IEnumerable<MyTrade> fills, string account, DateTime from, DateTime to, int days, int limit)
    {
        var fillIndex = new Dictionary<string, MyTrade>(StringComparer.Ordinal);
        var scanned = 0;
        foreach (var fill in fills)
        {
            if (++scanned > ScanLimit) throw new InvalidOperationException("Native fill scan limit exceeded.");
            if (fill != null && fill.AccountID == account && fill.SecurityId == SecurityId && !string.IsNullOrEmpty(fill.Id))
            {
                // Ambiguous duplicate fill IDs cannot establish entry direction.
                if (fillIndex.ContainsKey(fill.Id)) fillIndex[fill.Id] = null;
                else fillIndex.Add(fill.Id, fill);
            }
        }
        var rows = new List<(DateTime Time, Dictionary<string, object> Row)>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        var excluded = 0; var duplicate = 0; var incomplete = 0; scanned = 0;
        foreach (var item in history)
        {
            if (++scanned > ScanLimit) throw new InvalidOperationException("Native history scan limit exceeded.");
            if (item == null) throw new InvalidOperationException("Malformed native history row.");
            if (item.AccountID != account || item.SecurityId != SecurityId) { excluded++; continue; }
            var nativeTime = item.CloseTime == default ? item.OpenTime : item.CloseTime;
            // Compare native wall-clock ticks, without converting unknown kinds to UTC.
            if (nativeTime == default || nativeTime.Ticks < from.Ticks || nativeTime.Ticks > to.Ticks) { excluded++; continue; }
            var entry = item.EnterTrade;
            if (entry == null && item.EnterTradeId != null) fillIndex.TryGetValue(item.EnterTradeId, out entry);
            string side = null;
            if (entry != null && entry.AccountID == account && entry.SecurityId == SecurityId &&
                entry.Id == item.EnterTradeId && entry.OrderDirection is OrderDirections.Buy or OrderDirections.Sell)
                side = entry.OrderDirection.ToString();
            var row = new Dictionary<string, object> {
                ["native_id"] = item.Id.ToString(CultureInfo.InvariantCulture),
                ["account_id"] = account, ["security_id"] = SecurityId,
                ["entry_ref"] = string.IsNullOrEmpty(item.EnterTradeId) ? null : item.EnterTradeId,
                ["exit_ref"] = string.IsNullOrEmpty(item.ExitTradeId) ? null : item.ExitTradeId,
                ["open_time"] = Time(item.OpenTime), ["close_time"] = Time(item.CloseTime),
                ["open_price"] = D(item.OpenPrice), ["close_price"] = D(item.ClosePrice),
                ["open_volume"] = D(item.OpenVolume), ["close_volume"] = D(item.CloseVolume),
                ["pnl"] = D(item.PnL), ["ticks_pnl"] = D(item.TicksPnL), ["price_pnl"] = D(item.PricePnL),
                ["commission"] = item.Commission.HasValue ? D(item.Commission.Value) : null,
                ["side"] = side, ["is_complete"] = item.IsComplete,
                ["missing_data_case"] = item.MissingDataCase.ToString()
            };
            if (!seen.Add(JsonSerializer.Serialize(row))) { duplicate++; continue; }
            if (!item.IsComplete) incomplete++;
            rows.Add((nativeTime, row));
        }
        var selected = rows.OrderByDescending(x => x.Time.Ticks).Take(limit).Select(x => x.Row).ToArray();
        return new Dictionary<string, object> {
            ["coverage"] = new { mode = "load_history", from = Time(from), to = Time(to), days, limit,
                returned = selected.Length, total_scope_rows = rows.Count, truncated = rows.Count > limit,
                incomplete_rows = incomplete, excluded_rows = excluded, duplicate_rows = duplicate },
            ["trades"] = selected,
            ["quality"] = new[] { "native_paired_rows_not_roundtrips", "native_pnl_fee_semantics_unverified",
                "native_wall_clock_range_timezone_not_inferred", "historical_delta_dom_risk_mae_mfe_missing" }
        };
    }
}

public partial class AiBridgeStrategy
{
    private readonly string _journalInstance = Guid.NewGuid().ToString("N");
    private string _journalScope = Guid.NewGuid().ToString("N");
    private string _journalAccount;
    private string _journalSecurity;
    private bool? _journalReal;
    private Portfolio _journalPortfolio;
    private ITradingStatisticsProvider _journalProvider;
    private int _journalBusy;
    private long _journalSelectionEpoch;
    private long _journalObservedEpoch;
    private ITradingManager _journalManager;

    // Selection notifications may arrive between calculation callbacks. They only invalidate
    // an epoch; all SDK state reads and subscription ownership remain on the host boundary.
    private void JournalPortfolioChanged(Portfolio _) => Interlocked.Increment(ref _journalSelectionEpoch);
    private void JournalSecurityChanged(Security _) => Interlocked.Increment(ref _journalSelectionEpoch);

    private void DetachJournalSelections()
    {
        var manager = _journalManager;
        _journalManager = null;
        if (manager != null) {
            manager.PortfolioSelected -= JournalPortfolioChanged;
            manager.PortfolioChanged -= JournalPortfolioChanged;
            manager.SecuritySelected -= JournalSecurityChanged;
        }
        Interlocked.Increment(ref _journalSelectionEpoch);
    }


    // Called for every current-bar callback before queued work, including changes back to a prior account.
    private void ObserveJournalScope()
    {
        var manager = TradingManager;
        if (!ReferenceEquals(manager, _journalManager)) {
            DetachJournalSelections();
            _journalManager = manager;
            if (manager != null) {
                manager.PortfolioSelected += JournalPortfolioChanged;
                manager.PortfolioChanged += JournalPortfolioChanged;
                manager.SecuritySelected += JournalSecurityChanged;
            }
        }
        var epoch = Interlocked.Read(ref _journalSelectionEpoch);
        var portfolio = Portfolio;
        var account = portfolio?.AccountID;
        var security = TradingManager?.Security?.SecurityId;
        var provider = TradingStatisticsProvider;
        if (epoch != _journalObservedEpoch || !ReferenceEquals(portfolio, _journalPortfolio) || account != _journalAccount ||
            security != _journalSecurity || portfolio?.IsRealAccount != _journalReal || !ReferenceEquals(provider, _journalProvider))
        {
            _journalObservedEpoch = epoch;
            _journalScope = Guid.NewGuid().ToString("N");
            _journalPortfolio = portfolio; _journalAccount = account;
            _journalSecurity = security; _journalProvider = provider; _journalReal = portfolio?.IsRealAccount;
        }
    }

    private string ReadNativeJournal(Dictionary<string, string> query)
    {
        if (query.Keys.Any(key => key != "days" && key != "limit")) return Error("Invalid journal parameters.");
        int Bounded(string key, int fallback, int maximum) {
            if (!query.TryGetValue(key, out var raw)) return fallback;
            if (raw.Length == 0 || raw.Any(c => c < '0' || c > '9') ||
                !int.TryParse(raw, out var result) || result < 1 || result > maximum)
                throw new InvalidOperationException("Invalid journal bounds.");
            return result;
        }
        int days, limit;
        try { days = Bounded("days", 7, 7); limit = Bounded("limit", 100, 100); }
        catch { return Error("Invalid journal bounds."); }
        if (_indicatorDisposed || !_hostRunning) return Error("Native journal origin is stopped.");
        if (Interlocked.CompareExchange(ref _journalBusy, 1, 0) != 0) return Error("Native journal read already in progress.");
        var reply = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously);
        var expired = 0;
        string Failure(string code, string message) => JsonSerializer.Serialize(new { ok = false, code, error = message }, JsonOpts);
        void Finish(string json) { reply.TrySetResult(json); Interlocked.Exchange(ref _journalBusy, 0); }
        void Enqueue(Func<string> work) => _actions.Enqueue(new CommandRequest { Work = work,
            Tcs = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously) });
        Enqueue(() => {
            try {
                if (Volatile.Read(ref expired) != 0 || _indicatorDisposed) { Finish(Error("Native journal request expired.")); return "{}"; }
                ObserveJournalScope();
                if (Portfolio == null || Portfolio.IsRealAccount || string.IsNullOrWhiteSpace(_journalAccount) ||
                    _journalSecurity != NativeJournalDto.SecurityId || _journalProvider == null)
                { Finish(Error("Select a simulation account and BTCUSDT@BinanceFutures on this indicator.")); return "{}"; }
                var scope = _journalScope; var account = _journalAccount; var provider = _journalProvider;
                var selectionEpoch = Interlocked.Read(ref _journalSelectionEpoch);
                var server = _server; var port = server?.Port ?? 0;
                bool OriginUnchanged()
                {
                    // Do not reattach selection subscriptions after disposal.
                    if (_indicatorDisposed) return false;
                    ObserveJournalScope();
                    return !_indicatorDisposed && Volatile.Read(ref expired) == 0 && _hostRunning &&
                        ReferenceEquals(server, _server) && scope == _journalScope &&
                        selectionEpoch == Interlocked.Read(ref _journalSelectionEpoch) &&
                        ReferenceEquals(provider, _journalProvider) && Portfolio?.IsRealAccount == false;
                }
                var to = DateTime.Now; var from = to.AddDays(-days);
                Task<ITradingStatistics> load;
                try { load = provider.LoadHistoryAsync(from, to, new[] { account }, new[] { NativeJournalDto.SecurityId })
                    ?? throw new InvalidOperationException("Native history returned no task."); }
                catch { Finish(Failure("native_history_start_failed", "Native history API unavailable; no other source was substituted.")); return "{}"; }
                _ = load.ContinueWith(task => {
                    // Observe faults even if disposal/timeout prevents final enumeration.
                    if (task.IsFaulted) { _ = task.Exception; Finish(Failure("native_history_load_failed", "Native history read failed.")); return; }
                    if (task.IsCanceled || _indicatorDisposed || Volatile.Read(ref expired) != 0)
                    { Finish(Error("Native history request expired or origin stopped.")); return; }
                    Enqueue(() => {
                        try {
                            if (!OriginUnchanged())
                            { Finish(Failure("native_origin_changed", "Native journal origin changed.")); return "{}"; }
                            var statistics = task.Result ?? throw new InvalidOperationException("Native history unavailable.");
                            var dto = NativeJournalDto.Build(statistics.HistoryMyTrades, statistics.MyTrades, account, from, to, days, limit);
                            dto["ok"] = true; dto["schema_version"] = 1; dto["source"] = "atas_native_journal";
                            dto["origin"] = new { instance_id = _journalInstance, scope_id = scope, port,
                                account_id = account, security_id = NativeJournalDto.SecurityId,
                                account_is_real = false, host_mode = "indicator" };
                            var json = JsonSerializer.Serialize(dto, JsonOpts);
                            // SDK enumeration and serialization can run selection/lifetime callbacks.
                            // Validate the original epoch and source after both, immediately before publication.
                            if (!OriginUnchanged())
                            { Finish(Failure("native_origin_changed", "Native journal origin changed.")); return "{}"; }
                            Finish(json);
                        } catch { Finish(Failure("native_history_snapshot_failed", "Native journal snapshot failed.")); }
                        return "{}";
                    });
                }, TaskScheduler.Default);
                return "{}";
            } catch { Finish(Error("Native journal could not start.")); return "{}"; }
        });
        if (!reply.Task.Wait(8000)) {
            Interlocked.Exchange(ref expired, 1);
            // A running LoadHistoryAsync has no cancellation API. Keep single-flight until it completes.
            // A queued, not-started request releases itself on the next host callback.
            return Error("Native journal timed out; no cached or alternate source was used.");
        }
        return reply.Task.Result;
    }
}
#endif
