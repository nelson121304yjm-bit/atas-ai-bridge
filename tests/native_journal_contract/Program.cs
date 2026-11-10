// Exercises production adapter source with public SDK DTOs and inert interface proxies.
// No ATAS application, UI, model or trading service is constructed.
using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using ATAS.AiBridge;
using ATAS.DataFeedsCore;
using ATAS.DataFeedsCore.Statistics;
using Utils.Common.Collections;

var sdk = args.Length > 0 ? args[0] : "/Applications/ATAS X.app/Contents/MonoBundle";
AssemblyLoadContext.Default.Resolving += (_, name) => {
    var path = Path.Combine(sdk, name.Name + ".dll");
    return File.Exists(path) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(path) : null;
};
Fixtures.Run();

public class FixtureProxy : DispatchProxy
{
    public Func<MethodInfo, object[], object> Handler;
    protected override object Invoke(MethodInfo method, object[] args) => Handler(method, args);
    public static T Create<T>(Func<MethodInfo, object[], object> handler) where T : class {
        var result = DispatchProxy.Create<T, FixtureProxy>();
        ((FixtureProxy)(object)result).Handler = handler;
        return result;
    }
}

static class Fixtures
{
    static int checks;
    static void Check(bool condition, string message) {
        if (!condition) throw new Exception("FAIL: " + message);
        checks++;
    }
    static readonly DateTime Now = new(2026, 11, 10, 12, 0, 0, DateTimeKind.Unspecified);
    static HistoryMyTrade Row(long id = 1, string account = "SYNTHETIC_SIM") => new() {
        Id = id, AccountID = account, SecurityId = NativeJournalDto.SecurityId,
        EnterTradeId = "entry", ExitTradeId = "exit-" + id,
        OpenTime = Now.AddHours(-1), CloseTime = Now,
        OpenPrice = 12345.123456789012345678m, ClosePrice = 12346.123456789012345678m,
        OpenVolume = 0.000000000000000123m, CloseVolume = 0.000000000000000100m,
        PnL = -0.00000000000000000123m, TicksPnL = 1, PricePnL = 1, Commission = null
    };
    static JsonElement Build(HistoryMyTrade[] rows, MyTrade[] fills = null, int limit = 100) =>
        JsonSerializer.SerializeToElement(NativeJournalDto.Build(rows, fills ?? Array.Empty<MyTrade>(),
            "SYNTHETIC_SIM", Now.AddDays(-7), Now.AddHours(1), 7, limit));
    static IEnumerable<T> Enumerate<T>(T[] rows, Action duringEnumeration)
    {
        foreach (var row in rows) {
            duringEnumeration?.Invoke();
            yield return row;
        }
    }
    static IMutableEnumerable<T> Items<T>(T[] rows, int owner, Action duringEnumeration = null) => FixtureProxy.Create<IMutableEnumerable<T>>((m, _) => {
        Check(Environment.CurrentManagedThreadId == owner, "SDK collection enumerated only by host pump");
        if (m.Name == "GetEnumerator") return Enumerate(rows, duringEnumeration).GetEnumerator();
        throw new Exception("Unexpected collection call " + m.Name);
    });
    static ITradingStatistics Statistics(HistoryMyTrade[] rows, int owner, Action duringEnumeration = null) => FixtureProxy.Create<ITradingStatistics>((m, _) =>
        m.Name == "get_HistoryMyTrades" ? Items(rows, owner, duringEnumeration) :
        m.Name == "get_MyTrades" ? Items(Array.Empty<MyTrade>(), owner) : throw new Exception("Unexpected statistics access"));

    [System.Runtime.CompilerServices.MethodImpl(System.Runtime.CompilerServices.MethodImplOptions.NoInlining)]
    public static void Run()
    {
        var obsolete = typeof(ITradingStatisticsProvider).GetMethod("LoadHistoryAsync").GetCustomAttribute<ObsoleteAttribute>();
        Console.WriteLine("LoadHistoryAsync ObsoleteAttribute: " + (obsolete == null ? "none" : (obsolete.Message ?? "<no message>") + "; IsError=" + obsolete.IsError));
        var entry = new MyTrade { Id = "entry", AccountID = "SYNTHETIC_SIM", SecurityId = NativeJournalDto.SecurityId,
            OrderDirection = OrderDirections.Buy };
        var one = Row(); var two = Row(2); two.Commission = 0.00000000000000000017m;
        var foreign = Row(3, "OTHER_SIM"); var wrong = Row(4); wrong.SecurityId = "ETHUSDT@BinanceFutures";
        var result = Build(new[] { one, two, one, foreign, wrong }, new[] { entry });
        var rows = result.GetProperty("trades"); var coverage = result.GetProperty("coverage");
        Check(rows.GetArrayLength() == 2, "partial exits remain two native paired rows");
        Check(rows[0].GetProperty("open_volume").GetString() == "0.000000000000000123", "decimal quantity precision");
        Check(rows[0].GetProperty("pnl").GetString() == "-0.00000000000000000123", "signed PnL precision");
        Check(rows[0].GetProperty("commission").ValueKind == JsonValueKind.Null, "null commission stays null");
        Check(rows[1].GetProperty("commission").GetString() == "0.00000000000000000017", "commission precision");
        Check(rows[0].GetProperty("side").GetString() == "Buy", "direction only from scoped entry fill");
        Check(rows[0].GetProperty("open_time").GetProperty("kind").GetString() == "Unspecified", "unknown time kind preserved");
        Check(!rows[0].GetProperty("open_time").GetProperty("iso").GetString().EndsWith("Z"), "unknown timezone never assigned Z");
        Check(coverage.GetProperty("duplicate_rows").GetInt32() == 1, "exact duplicate counted once");
        Check(coverage.GetProperty("excluded_rows").GetInt32() == 2, "two-account and security isolation");
        Check(Build(new[] { one }).GetProperty("trades")[0].GetProperty("side").ValueKind == JsonValueKind.Null,
            "missing entry direction never inferred from exit or price");
        Check(Build(new[] { one }, new[] { entry, entry }).GetProperty("trades")[0].GetProperty("side").ValueKind == JsonValueKind.Null,
            "ambiguous fill IDs do not establish direction");
        var capped = Build(new[] { one, two }, limit: 1).GetProperty("coverage");
        Check(capped.GetProperty("truncated").GetBoolean() && capped.GetProperty("total_scope_rows").GetInt32() == 2, "bounded result reports truncation");
        var incomplete = Row(5); incomplete.CloseTime = default; incomplete.ClosePrice = 0;
        var inc = Build(new[] { incomplete });
        Check(inc.GetProperty("coverage").GetProperty("incomplete_rows").GetInt32() == 1, "incomplete native row counted");
        Check(inc.GetProperty("trades")[0].GetProperty("close_time").GetProperty("iso").ValueKind == JsonValueKind.Null, "default time stays missing");
        try { Build(Enumerable.Repeat(one, NativeJournalDto.ScanLimit + 1).ToArray()); throw new Exception("scan accepted"); }
        catch (InvalidOperationException) { Check(true, "unbounded host collection rejected"); }

        // Lifecycle and asynchronous source races use the production ReadNativeJournal implementation.
        var owner = Environment.CurrentManagedThreadId;
        var loads = 0;
        TaskCompletionSource<ITradingStatistics> pending = null;
        var provider = FixtureProxy.Create<ITradingStatisticsProvider>((m, a) => {
            Check(Environment.CurrentManagedThreadId == owner, "history starts only on host pump");
            Check(m.Name == "LoadHistoryAsync", "global statistics filters are never accessed/mutated");
            Check(((ICollection<string>)a[2]).SequenceEqual(new[] { "SYNTHETIC_SIM" }), "exact account load filter");
            Check(((ICollection<string>)a[3]).SequenceEqual(new[] { NativeJournalDto.SecurityId }), "exact security load filter");
            Check((DateTime)a[1] - (DateTime)a[0] == TimeSpan.FromDays(7), "load bound exactly seven days");
            loads++; pending = new(TaskCreationOptions.RunContinuationsAsynchronously); return pending.Task;
        });
        AiBridgeStrategy Host() => new() { TestPortfolio = new Portfolio { AccountID = "SYNTHETIC_SIM", IsRealAccount = false },
            TestProvider = provider };
        Task<string> Begin(AiBridgeStrategy host) {
            var task = Task.Run(() => host.TestRead(new()));
            Check(SpinWait.SpinUntil(() => host.TestHasWork, 2000), "HTTP read queues host work");
            host.TestPump(); return task;
        }
        void AwaitFinal(AiBridgeStrategy host) {
            Check(SpinWait.SpinUntil(() => host.TestHasWork, 2000), "completion queues host snapshot"); host.TestPump();
        }
        var good = Host(); var goodRead = Begin(good);
        Check(!goodRead.IsCompleted, "provider load does not synchronously block host");
        Check(good.TestRead(new()).Contains("already in progress"), "single flight rejects concurrent read");
        pending.SetResult(Statistics(new[] { Row() }, owner)); AwaitFinal(good);
        Check(JsonDocument.Parse(goodRead.Result).RootElement.GetProperty("ok").GetBoolean(), "bounded scoped history returns success");

        foreach (var changed in new[] { "account", "security", "provider", "server", "real", "account-back", "account-between-callbacks", "real-back", "dispose" }) {
            var host = Host(); var read = Begin(host);
            if (changed == "account") host.TestPortfolio = new Portfolio { AccountID = "OTHER_SIM" };
            if (changed == "security") host.TestSecurity = "ETHUSDT@BinanceFutures";
            if (changed == "provider") host.TestProvider = FixtureProxy.Create<ITradingStatisticsProvider>((_, _) => null);
            if (changed == "server") host.TestReplaceServer();
            if (changed == "real") host.TestPortfolio.IsRealAccount = true;
            if (changed == "account-back") {
                var original = host.TestPortfolio;
                host.TestPortfolio = new Portfolio { AccountID = "OTHER_SIM" }; host.TestPump();
                host.TestPortfolio = original; host.TestPump();
            }
            if (changed == "account-between-callbacks") {
                var original = host.TestPortfolio;
                host.TestPortfolio = new Portfolio { AccountID = "OTHER_SIM" };
                host.TestPortfolio = original;
            }
            if (changed == "real-back") {
                host.TestPortfolio.IsRealAccount = true; host.TestPump();
                host.TestPortfolio.IsRealAccount = false; host.TestPump();
            }
            if (changed == "dispose") host.TestDispose();
            pending.SetResult(Statistics(new[] { Row() }, owner));
            if (changed != "dispose") AwaitFinal(host);
            else Check(host.TestSubscriptionCount == 0, "disposal detaches public selection notifications");
            Check(read.Wait(2000) && !JsonDocument.Parse(read.Result).RootElement.GetProperty("ok").GetBoolean(), changed + " rejects late result");
        }
        foreach (var changed in new[] { "account-away-back", "security-away-back", "dispose", "server", "provider" }) {
            var host = Host(); var read = Begin(host);
            var invoked = 0;
            pending.SetResult(Statistics(new[] { Row() }, owner, () => {
                invoked++;
                if (changed == "account-away-back") {
                    var original = host.TestPortfolio;
                    host.TestPortfolio = new Portfolio { AccountID = "OTHER_SIM" };
                    host.TestPortfolio = original;
                }
                if (changed == "security-away-back") {
                    host.TestSecurity = "ETHUSDT@BinanceFutures";
                    host.TestSecurity = NativeJournalDto.SecurityId;
                }
                if (changed == "dispose") host.TestDispose();
                if (changed == "server") host.TestReplaceServer();
                if (changed == "provider") host.TestProvider = FixtureProxy.Create<ITradingStatisticsProvider>((_, _) => null);
            }));
            AwaitFinal(host);
            Check(invoked == 1, changed + " occurs inside SDK enumeration");
            Check(read.Wait(2000), changed + " during enumeration completes caller");
            using var rejected = JsonDocument.Parse(read.Result);
            Check(!rejected.RootElement.GetProperty("ok").GetBoolean() &&
                rejected.RootElement.GetProperty("code").GetString() == "native_origin_changed",
                changed + " during enumeration cannot publish old DTO");
            Check(!rejected.RootElement.TryGetProperty("trades", out _), changed + " rejects data instead of publishing old rows");
            if (changed == "dispose") Check(host.TestSubscriptionCount == 0, "post-snapshot validation does not resubscribe disposed origin");
        }
        var failed = Host(); var failureRead = Begin(failed); pending.SetException(new InvalidOperationException("synthetic"));
        Check(failureRead.Wait(2000) && failureRead.Result.Contains("failed"), "provider failures fail closed");
        var before = loads;
        foreach (var query in new[] { new Dictionary<string,string>{{"days", "8"}}, new(){{"limit","101"}}, new(){{"limit","1.0"}}, new(){{"account","OTHER"}} })
            Check(Host().TestRead(query).Contains("Invalid"), "malformed bounds rejected before SDK access");
        Check(loads == before, "invalid reads do not reach provider");
        var queued = Host(); var timeout = Task.Run(() => queued.TestRead(new()));
        Check(timeout.Wait(10000) && timeout.Result.Contains("timed out"), "no host callback has bounded wait");
        queued.TestPump(); Check(loads == before, "expired queued read never starts SDK request");
        var slow = Host(); var slowRead = Begin(slow); var slowPending = pending;
        Check(slowRead.Wait(10000) && slowRead.Result.Contains("timed out"), "running load has bounded caller wait");
        Check(slow.TestRead(new()).Contains("already in progress"), "uncancelable timed-out load stays single flight");
        slowPending.SetResult(Statistics(new[] { Row() }, owner));
        Check(SpinWait.SpinUntil(() => !slow.TestBusy, 2000), "late completion releases gate without SDK enumeration");
        Console.WriteLine($"Native journal contracts passed: {checks}");
    }
}
