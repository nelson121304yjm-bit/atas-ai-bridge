namespace ATAS.AiBridge;

using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using ATAS.DataFeedsCore;
using ATAS.Indicators;
using ATAS.Strategies.Chart;

// MarketDataType 在 ATAS.DataFeedsCore 与 ATAS.Indicators 里同名，这里显式取后者
// （委托簿快照 MarketDataArg.DataType 用的是 ATAS.Indicators.MarketDataType）。
using MarketDataType = ATAS.Indicators.MarketDataType;

/// <summary>
/// ATAS AI Bridge —— 把图表上的订单流数据暴露给 AI 助手（MCP）。
///
/// 设计要点：
///  1) 只读数据来自 ATAS 官方 API，全部在 ATAS 线程（OnCalculate）内取：
///     - 逐价买卖量：candle.GetPriceVolumeInfo(price, cache) -> PriceVolumeInfo{Price,Bid,Ask,Volume,Between,Ticks}
///     - 每根 POC：candle.MaxVolumePriceInfo?.Price
///     - 价值区：candle.ValueArea?.ValueAreaHigh / ValueAreaLow
///     - 每根 delta：candle.Delta（官方 CumulativeDelta 就是 candle.Delta 累加 + IsNewSession 重置）
///     - 委托簿：MarketDepthInfo.GetMarketDepthSnapshot() -> MarketDataArg{Price,Volume,DataType}
///  2) 读端点走内存快照（不排队、不受 tick 影响）；只有指定历史 bar 的按需查询和
///     下单类请求才排队到 ATAS 线程执行，并带超时（无行情时 OnCalculate 不触发）。
///  3) 下单默认关闭：AllowTrading=false 且 TradeToken 为空时，下单端点一律拒绝。
///  4) HTTP 用 TcpListener 自实现，免管理员/免 urlacl，只绑 127.0.0.1。
/// </summary>
#if MAC_INDICATOR
[System.ComponentModel.DisplayName("AI Bridge — read only")]
public partial class AiBridgeStrategy : Indicator
#else
public partial class AiBridgeStrategy : ChartStrategy
#endif
{
    private const string BridgeId = "atas-ai-bridge";
#if MAC_INDICATOR
    private const string BridgeVersion = "0.3.0-macos.2";
#else
    private const string BridgeVersion = "0.2.0";
#endif
    private const int FirstPort = 8790;
    private const int LastPort = 8810;

    #region 设置（ATAS 策略属性面板 + /api/settings 运行时可写）

    // 线程模型：这些设置项都是普通 CLR 属性（显式字段 + volatile），不碰任何 ATAS
    // 内部集合，所以 HTTP 线程可以直接读写：
    //   * int / bool / string 引用的赋值与读取本身是原子的，读不到"撕裂值"；
    //   * volatile 保证图表线程（OnCalculate）能立刻看到 HTTP 线程写入的新值。
    // 为什么不排队到 ATAS 线程（RunOnAtasThread）：无行情时 OnCalculate 不触发，
    // 排队会让"收盘后调设置"直接超时——而收盘后恰恰是最想调设置的时候。
    //
    // 持久化限制：运行中修改只在内存生效。ATAS 没有公开 API 让策略触发工作区设置
    // 落盘，所以 ATAS 重启 / 重新加载策略后会回到属性面板里保存的值。

    private volatile bool _bridgeEnabled = true;

    [Display(Name = "启用 AI 桥", GroupName = "AI Bridge", Order = 10)]
    public bool BridgeEnabled { get => _bridgeEnabled; set => _bridgeEnabled = value; }

    private volatile int _snapshotBars = 300;

    [Range(50, 5000)]
    [Display(Name = "快照 K 线根数", GroupName = "AI Bridge", Order = 20)]
    public int SnapshotBars { get => _snapshotBars; set => _snapshotBars = value; }

    private volatile int _snapshotFootprintBars = 2;

    [Range(0, 20)]
    [Display(Name = "快照 footprint 根数", GroupName = "AI Bridge", Order = 30)]
    public int SnapshotFootprintBars { get => _snapshotFootprintBars; set => _snapshotFootprintBars = value; }

    private volatile int _profileLookbackBars = 200;

    [Range(20, 5000)]
    [Display(Name = "区间 profile 回看根数", GroupName = "AI Bridge", Order = 40)]
    public int ProfileLookbackBars { get => _profileLookbackBars; set => _profileLookbackBars = value; }

    private volatile int _profileValueAreaPercent = 70;

    [Range(1, 100)]
    [Display(Name = "profile 价值区百分比", GroupName = "AI Bridge", Order = 50)]
    public int ProfileValueAreaPercent { get => _profileValueAreaPercent; set => _profileValueAreaPercent = value; }

    private volatile int _defaultDomLevels = 15;

    [Range(1, 200)]
    [Display(Name = "默认 DOM 档数", GroupName = "AI Bridge", Order = 60)]
    public int DefaultDomLevels { get => _defaultDomLevels; set => _defaultDomLevels = value; }

#if MAC_INDICATOR
    [System.ComponentModel.Browsable(false)]
    public bool AllowTrading => false;
    [System.ComponentModel.Browsable(false)]
    public string TradeToken => "";
#else
    private volatile bool _allowTrading = false;

    [Display(Name = "允许下单（默认关闭）", GroupName = "AI Bridge 风控", Order = 10)]
    public bool AllowTrading { get => _allowTrading; set => _allowTrading = value; }

    private volatile string _tradeToken = "";

    [Display(Name = "下单确认口令（留空=禁止下单）", GroupName = "AI Bridge 风控", Order = 20)]
    public string TradeToken { get => _tradeToken; set => _tradeToken = value ?? ""; }
#endif

    #endregion

    #region 状态

    private sealed class CommandRequest
    {
        public Func<string> Work;
        public TaskCompletionSource<string> Tcs;
    }

    private readonly ConcurrentQueue<CommandRequest> _actions = new ConcurrentQueue<CommandRequest>();
    private readonly PriceVolumeInfo _pvCache = new PriceVolumeInfo();
    private readonly List<string> _log = new List<string>();
    private readonly object _logLock = new object();

    private BridgeServer _server;

    private volatile string _statusJson = "{}";
    private volatile string _quoteJson = "{}";
    private volatile string _domJson = "{}";
    private volatile string _candlesJson = "[]";
    private volatile string _cvdJson = "[]";
    private volatile string _footprintJson = "{}";
    private volatile string _profileJson = "{}";
    private volatile string _positionJson = "{}";
    private volatile string _ordersJson = "[]";

    // 快照"是用哪组参数构建的"：缓存命中必须比对这些值，而不是比对"当前设置值"。
    // 否则改完设置（或改了 ATAS 属性面板）后，第一次请求会命中旧快照、返回与参数
    // 不符的数据——这是 0.2.0 加设置写入后必须堵掉的坑。
    private volatile int _domJsonLevels = -1;
    private volatile int _candlesJsonBars = -1;
    private volatile int _profileJsonBars = -1;
    private volatile int _profileJsonPercent = -1;

    /// <summary>设置被改动后置位：让下一根 tick 立刻重建全部快照，而不是各等各的节流窗口。</summary>
    private volatile bool _refreshAllRequested;

    private DateTime _tStatus = DateTime.MinValue;
    private DateTime _tMarket = DateTime.MinValue;
    private DateTime _tSeries = DateTime.MinValue;
    private DateTime _tProfile = DateTime.MinValue;

    private static readonly JsonSerializerOptions JsonOpts = new JsonSerializerOptions { WriteIndented = false };

    #endregion

    #region 生命周期（ATAS 线程）

#if !MAC_INDICATOR
    protected override void OnStarted()
    {
        Log("OnStarted, activated=" + IsActivated + ", bridge=" + BridgeEnabled);

        if (!BridgeEnabled)
            return;

        try
        {
            _server = new BridgeServer(HandleRequest, Log);
            if (_server.Start(FirstPort, LastPort, out var error))
            {
                WritePortFile(_server.Port);
                Log("listening on 127.0.0.1:" + _server.Port);
            }
            else
            {
                Log("bind failed: " + error);
            }
        }
        catch (Exception ex)
        {
            Log("start error: " + ex.Message);
        }
    }

    protected override void OnStopped()
    {
        Log("OnStopped");
        try { _server?.Stop(); } catch { }
        _server = null;
    }
#endif

    protected override void OnCalculate(int bar, decimal value)
    {
        try
        {
#if MAC_INDICATOR
            if (!_hostRunning || bar != CurrentBar - 1)
                return;
#endif
            DrainActions();
            RefreshSnapshots(bar);
#if MAC_INDICATOR
            Interlocked.Exchange(ref _lastSnapshotTicks, DateTime.UtcNow.Ticks);
#endif
        }
        catch (Exception ex)
        {
            Log("OnCalculate error: " + ex.Message);
        }
    }

#if !MAC_INDICATOR
    protected override void OnOrderChanged(Order order)
    {
        try
        {
            if (order == null)
                return;
            Log("order changed: " + order.Id + " " + order.Direction + " " + order.Type + " qty=" + order.QuantityToFill);
            RefreshAccount();
        }
        catch (Exception ex)
        {
            Log("OnOrderChanged error: " + ex.Message);
        }
    }
#endif

    #endregion

    #region 快照刷新（节流，避免每 tick 都做重活）

    private void RefreshSnapshots(int bar)
    {
        var now = DateTime.UtcNow;
        var force = _refreshAllRequested;
        if (force)
            _refreshAllRequested = false;

        if (force || (now - _tStatus).TotalMilliseconds >= 250)
        {
            _tStatus = now;
            _statusJson = BuildStatus();
            _positionJson = BuildPosition();
        }

        if (force || (now - _tMarket).TotalMilliseconds >= 200)
        {
            _tMarket = now;
            var domLevels = ClampDomLevels(DefaultDomLevels);
            _quoteJson = BuildQuote();
            _domJson = BuildDom(domLevels);
            _domJsonLevels = domLevels;
            _ordersJson = BuildOrders();
        }

        if (force || (now - _tSeries).TotalMilliseconds >= 1000)
        {
            _tSeries = now;
            var seriesBars = Math.Min(SnapshotBars, Math.Max(1, bar + 1));
            _candlesJson = BuildCandles(seriesBars);
            _candlesJsonBars = seriesBars;
            _cvdJson = BuildCvd(seriesBars);
            _footprintJson = BuildFootprintWindow();
        }

        if (force || (now - _tProfile).TotalSeconds >= 2)
        {
            _tProfile = now;
            var profileBars = Math.Min(ProfileLookbackBars, Math.Max(1, bar + 1));
            var profilePercent = ClampProfilePercent(ProfileValueAreaPercent);
            _profileJson = BuildProfile(profileBars, profilePercent, 0);
            _profileJsonBars = profileBars;
            _profileJsonPercent = profilePercent;
        }
    }

    private void DrainActions()
    {
        while (_actions.TryDequeue(out var request))
        {
            try
            {
                request.Tcs.TrySetResult(request.Work());
            }
            catch (Exception ex)
            {
                request.Tcs.TrySetResult(Error("ATAS 线程执行失败: " + ex.Message));
            }
        }
    }

    /// <summary>把工作排到 ATAS 线程执行并等待结果（带超时，避免无行情时挂死 HTTP 线程）。</summary>
    private string RunOnAtasThread(Func<string> work, int timeoutMs)
    {
        var request = new CommandRequest
        {
            Work = work,
            Tcs = new TaskCompletionSource<string>(TaskCreationOptions.RunContinuationsAsynchronously)
        };

        _actions.Enqueue(request);

        if (!request.Tcs.Task.Wait(timeoutMs))
            return Error("ATAS 线程在 " + timeoutMs + "ms 内没有 tick（无行情时 OnCalculate 不触发）。请稍后重试，或使用快照端点 /api/footprint（不带 bar 参数）。");

        return request.Tcs.Task.Result;
    }

    #endregion

    #region 数据构造（ATAS 线程）

    private string BuildStatus()
    {
        try
        {
            return JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["ok"] = true,
                ["bridge"] = BridgeId,
                ["version"] = BridgeVersion,
                ["port"] = _server?.Port ?? 0,
                ["instrument"] = InstrumentInfo?.Instrument,
                ["tickSize"] = InstrumentInfo?.TickSize ?? 0m,
                ["currentBar"] = CurrentBar,
                ["portfolio"] = Portfolio?.AccountID,
                ["isActivated"] = IsActivated,
                ["allowTrading"] = AllowTrading,
                ["tradingArmed"] = IsTradingArmed(),
                ["snapshotBars"] = SnapshotBars,
                ["snapshotFootprintBars"] = SnapshotFootprintBars,
                ["profileLookbackBars"] = ProfileLookbackBars,
                ["profileValueAreaPercent"] = ProfileValueAreaPercent,
                ["defaultDomLevels"] = DefaultDomLevels,
                ["tradeTokenSet"] = !string.IsNullOrWhiteSpace(TradeToken),
#if MAC_INDICATOR
                ["hostMode"] = "indicator",
                ["readOnly"] = true,
                ["securityId"] = TradingManager?.Security?.SecurityId,
                ["accountIsReal"] = Portfolio?.IsRealAccount,
                ["accountConnectionState"] = Portfolio?.ConnectionState.ToString(),
#endif
                ["serverTime"] = DateTime.UtcNow.ToString("o", CultureInfo.InvariantCulture)
            }, JsonOpts);
        }
        catch (Exception ex)
        {
            return Error("status: " + ex.Message);
        }
    }

    private bool IsTradingArmed()
    {
        return AllowTrading && !string.IsNullOrWhiteSpace(TradeToken) && IsActivated;
    }

    private string BuildQuote()
    {
        try
        {
            var depths = MarketDepthInfo?.GetMarketDepthSnapshot();
            decimal? bestBid = null, bestAsk = null, bidVol = 0m, askVol = 0m;
            decimal cumBids = 0m, cumAsks = 0m;

            if (depths != null)
            {
                foreach (var level in depths)
                {
                    if (level.DataType == MarketDataType.Ask)
                    {
                        if (bestAsk == null || level.Price < bestAsk)
                        {
                            bestAsk = level.Price;
                            askVol = level.Volume;
                        }
                    }
                    else
                    {
                        if (bestBid == null || level.Price > bestBid)
                        {
                            bestBid = level.Price;
                            bidVol = level.Volume;
                        }
                    }
                }
            }

            try
            {
                cumBids = MarketDepthInfo?.CumulativeDomBids ?? 0m;
                cumAsks = MarketDepthInfo?.CumulativeDomAsks ?? 0m;
            }
            catch { }

            decimal last = 0m;
            try { last = GetCandle(CurrentBar - 1)?.Close ?? 0m; } catch { }

            return JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["ok"] = true,
                ["last"] = last,
                ["bestBid"] = bestBid,
                ["bidSize"] = bidVol,
                ["bestAsk"] = bestAsk,
                ["askSize"] = askVol,
                ["spread"] = bestBid != null && bestAsk != null ? bestAsk - bestBid : null,
                ["cumDomBids"] = cumBids,
                ["cumDomAsks"] = cumAsks,
                ["domImbalance"] = cumBids + cumAsks != 0 ? (cumBids - cumAsks) / (cumBids + cumAsks) : 0m,
                ["tickSize"] = InstrumentInfo?.TickSize ?? 0m
            }, JsonOpts);
        }
        catch (Exception ex)
        {
            return Error("quote: " + ex.Message);
        }
    }

    private string BuildDom(int levels)
    {
        try
        {
            var depths = MarketDepthInfo?.GetMarketDepthSnapshot();
            var bids = new List<Dictionary<string, object>>();
            var asks = new List<Dictionary<string, object>>();

            if (depths != null)
            {
                foreach (var level in depths)
                {
                    var item = new Dictionary<string, object>
                    {
                        ["price"] = level.Price,
                        ["volume"] = level.Volume
                    };
                    if (level.DataType == MarketDataType.Ask)
                        asks.Add(item);
                    else
                        bids.Add(item);
                }
            }

            bids = bids.OrderByDescending(x => (decimal)x["price"]).Take(levels).ToList();
            asks = asks.OrderBy(x => (decimal)x["price"]).Take(levels).ToList();

            return JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["ok"] = true,
                ["bids"] = bids,
                ["asks"] = asks,
                ["cumDomBids"] = MarketDepthInfo?.CumulativeDomBids ?? 0m,
                ["cumDomAsks"] = MarketDepthInfo?.CumulativeDomAsks ?? 0m
            }, JsonOpts);
        }
        catch (Exception ex)
        {
            return Error("dom: " + ex.Message);
        }
    }

    private string BuildCandles(int bars)
    {
        var list = new List<Dictionary<string, object>>();
        var from = Math.Max(0, CurrentBar - bars);

        for (var i = from; i < CurrentBar; i++)
        {
            try
            {
                var candle = GetCandle(i);
                if (candle == null)
                    continue;
                list.Add(new Dictionary<string, object>
                {
                    ["i"] = i,
                    ["t"] = candle.Time.ToString("o", CultureInfo.InvariantCulture),
                    ["o"] = candle.Open,
                    ["h"] = candle.High,
                    ["l"] = candle.Low,
                    ["c"] = candle.Close,
                    ["v"] = candle.Volume,
                    ["delta"] = candle.Delta
                });
            }
            catch { }
        }

        return JsonSerializer.Serialize(list, JsonOpts);
    }

    /// <summary>累计 delta：与官方 CumulativeDelta 同口径（candle.Delta 累加，IsNewSession 重置）。</summary>
    private string BuildCvd(int bars)
    {
        var list = new List<Dictionary<string, object>>();
        var from = Math.Max(0, CurrentBar - bars);
        var cum = 0m;

        for (var i = from; i < CurrentBar; i++)
        {
            try
            {
                var candle = GetCandle(i);
                if (candle == null)
                    continue;

                if (IsNewSession(i))
                    cum = 0m;

                cum += candle.Delta;

                list.Add(new Dictionary<string, object>
                {
                    ["i"] = i,
                    ["t"] = candle.Time.ToString("o", CultureInfo.InvariantCulture),
                    ["delta"] = candle.Delta,
                    ["cvd"] = cum,
                    ["sessionStart"] = IsNewSession(i)
                });
            }
            catch { }
        }

        return JsonSerializer.Serialize(list, JsonOpts);
    }

    private string BuildFootprintWindow()
    {
        var result = new Dictionary<string, object>();
        var bars = Math.Max(0, SnapshotFootprintBars);
        var from = Math.Max(0, CurrentBar - 1 - bars + 1);

        for (var i = from; i < CurrentBar; i++)
        {
            var json = BuildFootprintAt(i, 0, true);
            result[i.ToString(CultureInfo.InvariantCulture)] = json;
        }

        return JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["ok"] = true,
            ["currentBar"] = CurrentBar,
            ["bars"] = result
        }, JsonOpts);
    }

    /// <summary>单根 K 的逐价买卖量（footprint）。top>0 时只返回成交量最大的前 top 档。</summary>
    private string BuildFootprintAt(int bar, int top, bool compact)
    {
        var candle = GetCandle(bar);
        if (candle == null)
            return Error("bar " + bar + " 无数据");

        var tick = InstrumentInfo?.TickSize ?? 0m;
        if (tick <= 0m)
            return Error("tick size 不可用");

        var levels = new List<Dictionary<string, object>>();
        var totalVolume = 0m;
        var totalDelta = 0m;
        var maxLevels = 20000;

        for (var price = candle.Low; price <= candle.High && levels.Count < maxLevels; price += tick)
        {
            PriceVolumeInfo info;
            try { info = candle.GetPriceVolumeInfo(price, _pvCache); }
            catch { continue; }

            if (info == null)
                continue;

            var delta = info.Ask - info.Bid;
            totalVolume += info.Volume;
            totalDelta += delta;

            if (compact && info.Volume == 0m && delta == 0m)
                continue;

            levels.Add(new Dictionary<string, object>
            {
                ["p"] = price,
                ["bid"] = info.Bid,
                ["ask"] = info.Ask,
                ["v"] = info.Volume,
                ["d"] = delta,
                ["n"] = info.Ticks
            });
        }

        var returned = levels;
        var truncated = false;
        if (top > 0 && levels.Count > top)
        {
            returned = levels.OrderByDescending(x => (decimal)x["v"]).Take(top).ToList();
            truncated = true;
        }

        decimal poc = 0m;
        try { poc = candle.MaxVolumePriceInfo?.Price ?? 0m; } catch { }

        decimal vah = 0m, val = 0m;
        try
        {
            var va = candle.ValueArea;
            if (va != null)
            {
                vah = va.ValueAreaHigh;
                val = va.ValueAreaLow;
            }
        }
        catch { }

        return JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["ok"] = true,
            ["bar"] = bar,
            ["time"] = candle.Time.ToString("o", CultureInfo.InvariantCulture),
            ["o"] = candle.Open,
            ["h"] = candle.High,
            ["l"] = candle.Low,
            ["c"] = candle.Close,
            ["volume"] = candle.Volume,
            ["delta"] = candle.Delta,
            ["minDelta"] = candle.MinDelta,
            ["maxDelta"] = candle.MaxDelta,
            ["poc"] = poc,
            ["vah"] = vah,
            ["val"] = val,
            ["levelCount"] = levels.Count,
            ["totalVolume"] = totalVolume,
            ["totalDelta"] = totalDelta,
            ["truncated"] = truncated,
            ["levels"] = returned
        }, JsonOpts);
    }

    /// <summary>区间成交量分布：POC + 价值区（VAH/VAL，按 valueAreaPercent 从 POC 向两侧扩展）。</summary>
    private string BuildProfile(int bars, int percent, int top)
    {
        var from = Math.Max(0, CurrentBar - bars);
        var tick = InstrumentInfo?.TickSize ?? 0m;
        if (tick <= 0m)
            return Error("tick size 不可用");

        var volumeAtPrice = new Dictionary<decimal, decimal>();
        var bidAtPrice = new Dictionary<decimal, decimal>();
        var askAtPrice = new Dictionary<decimal, decimal>();
        var total = 0m;
        var usedBars = 0;

        for (var i = from; i < CurrentBar; i++)
        {
            IndicatorCandle candle;
            try { candle = GetCandle(i); } catch { continue; }
            if (candle == null)
                continue;

            usedBars++;
            for (var price = candle.Low; price <= candle.High; price += tick)
            {
                PriceVolumeInfo info;
                try { info = candle.GetPriceVolumeInfo(price, _pvCache); }
                catch { continue; }
                if (info == null)
                    continue;

                if (volumeAtPrice.TryGetValue(price, out var v))
                {
                    volumeAtPrice[price] = v + info.Volume;
                    bidAtPrice[price] += info.Bid;
                    askAtPrice[price] += info.Ask;
                }
                else
                {
                    volumeAtPrice[price] = info.Volume;
                    bidAtPrice[price] = info.Bid;
                    askAtPrice[price] = info.Ask;
                }

                total += info.Volume;
            }
        }

        if (volumeAtPrice.Count == 0)
            return Error("区间内没有成交量数据");

        // 注意：sorted 按价格降序，所以 index 越小 = 价格越高。
        var sorted = volumeAtPrice.OrderByDescending(x => x.Key).ToList();
        var pocPrice = sorted.OrderByDescending(x => x.Value).First().Key;

        // 价值区：从 POC 起，每次并入"上方/下方成交量较大的一侧"，直到累计 >= percent%
        var target = total * Math.Max(1, Math.Min(100, percent)) / 100m;
        var pocIndex = sorted.FindIndex(x => x.Key == pocPrice);
        var highIndex = pocIndex;   // 向上扩展 -> index 减小（价格更高）
        var lowIndex = pocIndex;    // 向下扩展 -> index 增大（价格更低）
        var cumulative = sorted[pocIndex].Value;

        while (cumulative < target && (highIndex > 0 || lowIndex < sorted.Count - 1))
        {
            var upperVol = highIndex > 0 ? sorted[highIndex - 1].Value : -1m;
            var lowerVol = lowIndex < sorted.Count - 1 ? sorted[lowIndex + 1].Value : -1m;

            if (upperVol < 0m && lowerVol < 0m)
                break;

            if (upperVol >= lowerVol)
            {
                highIndex--;
                cumulative += sorted[highIndex].Value;
            }
            else
            {
                lowIndex++;
                cumulative += sorted[lowIndex].Value;
            }
        }

        var levels = sorted.Select(x => new Dictionary<string, object>
        {
            ["p"] = x.Key,
            ["v"] = x.Value,
            ["bid"] = bidAtPrice.TryGetValue(x.Key, out var b) ? b : 0m,
            ["ask"] = askAtPrice.TryGetValue(x.Key, out var a) ? a : 0m
        }).ToList();

        var returned = levels;
        var truncated = false;
        if (top > 0 && levels.Count > top)
        {
            returned = levels.OrderByDescending(x => (decimal)x["v"]).Take(top).ToList();
            truncated = true;
        }

        return JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["ok"] = true,
            ["fromBar"] = from,
            ["toBar"] = CurrentBar - 1,
            ["usedBars"] = usedBars,
            ["valueAreaPercent"] = percent,
            ["poc"] = pocPrice,
            ["vah"] = sorted[highIndex].Key,
            ["val"] = sorted[lowIndex].Key,
            ["totalVolume"] = total,
            ["valueAreaVolume"] = cumulative,
            ["levelCount"] = levels.Count,
            ["truncated"] = truncated,
            ["levels"] = returned
        }, JsonOpts);
    }

#if MAC_INDICATOR
    private string BuildPosition() => Error("Account positions are not provided by this read-only indicator.");
    private string BuildOrders() => Error("Account orders are not provided by this read-only indicator.");
#else
    private string BuildPosition()
    {
        try
        {
            var net = 0m;
            if (MyTrades != null)
            {
                foreach (var trade in MyTrades)
                    net += trade.OrderDirection == OrderDirections.Buy ? trade.Volume : -trade.Volume;
            }

            return JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["ok"] = true,
                ["netVolume"] = Math.Abs(net),
                ["direction"] = net > 0m ? "Buy" : net < 0m ? "Sell" : "Flat",
                ["portfolio"] = Portfolio?.AccountID,
                ["note"] = "净持仓按本策略自身成交（MyTrades）计算，账户里手动下的单不计入"
            }, JsonOpts);
        }
        catch (Exception ex)
        {
            return Error("position: " + ex.Message);
        }
    }

    private string BuildOrders()
    {
        try
        {
            var list = new List<Dictionary<string, object>>();
            if (Orders != null)
            {
                foreach (var order in Orders)
                {
                    list.Add(new Dictionary<string, object>
                    {
                        ["id"] = order.Id,
                        ["direction"] = order.Direction.ToString(),
                        ["type"] = order.Type.ToString(),
                        ["price"] = order.Price,
                        ["triggerPrice"] = order.TriggerPrice,
                        ["quantity"] = order.QuantityToFill,
                        ["comment"] = order.Comment
                    });
                }
            }
            return JsonSerializer.Serialize(list, JsonOpts);
        }
        catch (Exception ex)
        {
            return Error("orders: " + ex.Message);
        }
    }

    private void RefreshAccount()
    {
        _positionJson = BuildPosition();
        _ordersJson = BuildOrders();
    }
#endif

    #endregion

    #region 设置读写（HTTP 线程直接调用；见「设置」区的线程模型说明）

    private sealed class SettingDescriptor
    {
        public string Key;
        public string Name;
        public string Group;
        public string Type;          // int | bool | string
        public int Min;
        public int Max;
        public string Description;
        public bool Sensitive = false; // 值不回显（口令类）
        public bool RuntimeEditable; // 运行中改是否有效
        public Func<object> Read;
        public Action<object> Write;
    }

    /// <summary>
    /// 设置表 = 唯一权威：GET/POST /api/settings 的列举、校验、写入都读它。
    /// 新增设置项只改这里一处。每次请求重建（10 来个对象，代价可忽略），
    /// 这样不必在字段初始化器里引用 this。
    /// </summary>
    private List<SettingDescriptor> BuildSettingsTable()
    {
        return new List<SettingDescriptor>
        {
            new SettingDescriptor
            {
                Key = "bridgeEnabled", Name = "启用 AI 桥", Group = "AI Bridge", Type = "bool",
                RuntimeEditable = false,
                Description = "只在策略启动时读取。运行中改它不会起停监听端口，请在 ATAS 属性面板操作。",
                Read = () => BridgeEnabled,
                Write = value => BridgeEnabled = (bool)value
            },
            new SettingDescriptor
            {
                Key = "snapshotBars", Name = "快照 K 线根数", Group = "AI Bridge", Type = "int",
                Min = 50, Max = 5000, RuntimeEditable = true,
                Description = "/api/candles 与 /api/cvd 的快照长度，也是它们不传 bars 时的默认值。",
                Read = () => SnapshotBars,
                Write = value => SnapshotBars = (int)value
            },
            new SettingDescriptor
            {
                Key = "snapshotFootprintBars", Name = "快照 footprint 根数", Group = "AI Bridge", Type = "int",
                Min = 0, Max = 20, RuntimeEditable = true,
                Description = "/api/footprint 不带 bar 参数时，快照里保留最近几根的逐价数据。",
                Read = () => SnapshotFootprintBars,
                Write = value => SnapshotFootprintBars = (int)value
            },
            new SettingDescriptor
            {
                Key = "profileLookbackBars", Name = "区间 profile 回看根数", Group = "AI Bridge", Type = "int",
                Min = 20, Max = 5000, RuntimeEditable = true,
                Description = "/api/profile 不传 bars 时的默认回看根数。",
                Read = () => ProfileLookbackBars,
                Write = value => ProfileLookbackBars = (int)value
            },
            new SettingDescriptor
            {
                Key = "profileValueAreaPercent", Name = "profile 价值区百分比", Group = "AI Bridge", Type = "int",
                Min = 1, Max = 100, RuntimeEditable = true,
                Description = "/api/profile 不传 percent 时的默认价值区百分比（70 = 标准价值区）。",
                Read = () => ProfileValueAreaPercent,
                Write = value => ProfileValueAreaPercent = (int)value
            },
            new SettingDescriptor
            {
                Key = "defaultDomLevels", Name = "默认 DOM 档数", Group = "AI Bridge", Type = "int",
                Min = 1, Max = 200, RuntimeEditable = true,
                Description = "/api/dom 不传 levels 时的默认档数，也是快照里 DOM 的档数。",
                Read = () => DefaultDomLevels,
                Write = value => DefaultDomLevels = (int)value
            },
#if !MAC_INDICATOR
            new SettingDescriptor
            {
                Key = "allowTrading", Name = "允许下单", Group = "AI Bridge 风控", Type = "bool",
                RuntimeEditable = true,
                Description = "第一道下单闸门。打开后还需：策略已激活 + 口令非空 + MCP 进程的 "
                            + "ATAS_TRADE_TOKEN 与口令一致（且工具因此被注册），才可能下单。",
                Read = () => AllowTrading,
                Write = value => AllowTrading = (bool)value
            },
            new SettingDescriptor
            {
                Key = "tradeToken", Name = "下单确认口令", Group = "AI Bridge 风控", Type = "string",
                Sensitive = true, RuntimeEditable = true,
                Description = "第二道下单闸门：留空 = 永久禁止下单。值不回显，只报是否已设置与长度。",
                Read = () => TradeToken,
                Write = value => TradeToken = (string)value
            }
#endif
        };
    }

    private List<Dictionary<string, object>> BuildSettingsJson()
    {
        var items = new List<Dictionary<string, object>>();

        foreach (var setting in BuildSettingsTable())
        {
            var item = new Dictionary<string, object>
            {
                ["key"] = setting.Key,
                ["name"] = setting.Name,
                ["group"] = setting.Group,
                ["type"] = setting.Type,
#if MAC_INDICATOR
                ["runtimeEditable"] = false,
#else
                ["runtimeEditable"] = setting.RuntimeEditable,
#endif
                ["description"] = setting.Description
            };

            if (setting.Type == "int")
            {
                item["value"] = setting.Read();
                item["min"] = setting.Min;
                item["max"] = setting.Max;
            }
            else if (setting.Sensitive)
            {
                // 口令永不回显：只给"是否已设置 + 长度"，避免把口令写进 AI 的上下文/日志。
                var raw = setting.Read() as string ?? "";
                item["value"] = null;
                item["isSet"] = raw.Length > 0;
                item["length"] = raw.Length;
            }
            else
            {
                item["value"] = setting.Read();
            }

            items.Add(item);
        }

        return items;
    }

    private string GetSettings()
    {
        try
        {
            return JsonSerializer.Serialize(new Dictionary<string, object>
            {
                ["ok"] = true,
                ["bridge"] = BridgeId,
                ["version"] = BridgeVersion,
                ["isActivated"] = IsActivated,
                ["tradingArmed"] = IsTradingArmed(),
                ["settings"] = BuildSettingsJson(),
#if MAC_INDICATOR
                ["readOnly"] = true,
                ["note"] = "只读指标：只能在 ATAS 指标设置中配置参数，HTTP 不接受修改。"
#else
                ["note"] = "运行中修改只在内存生效：ATAS 重启 / 重新加载策略后会回到属性面板里保存的值。"
                           + "口令类设置（tradeToken）不回显原值。"
#endif
            }, JsonOpts);
        }
        catch (Exception ex)
        {
            return Error("settings: " + ex.Message);
        }
    }

    private string UpdateSettings(string body)
    {
        if (string.IsNullOrWhiteSpace(body))
            return Error("需要 JSON body，例如 {\"set\":{\"snapshotBars\":500}}");

        try
        {
            using var doc = JsonDocument.Parse(body);
            return ApplySettings(doc.RootElement);
        }
        catch (JsonException ex)
        {
            return Error("JSON 解析失败: " + ex.Message);
        }
        catch (Exception ex)
        {
            return Error("settings: " + ex.Message);
        }
    }

    private string ApplySettings(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object)
            return Error("body 必须是 JSON 对象");

        if (!root.TryGetProperty("set", out var set) || set.ValueKind != JsonValueKind.Object)
            return Error("缺少 \"set\" 对象，例如 {\"set\":{\"snapshotBars\":500}}");

        var byKey = new Dictionary<string, SettingDescriptor>(StringComparer.OrdinalIgnoreCase);
        foreach (var setting in BuildSettingsTable())
            byKey[setting.Key] = setting;

        // 先全量校验、再统一写入（原子语义）：任一项不合法就整批拒绝，
        // 避免出现"改了一半"的中间状态让调用方以为成功了。
        var plan = new List<KeyValuePair<SettingDescriptor, object>>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var prop in set.EnumerateObject())
        {
            if (!byKey.TryGetValue(prop.Name, out var setting))
                return Error("未知设置项: " + prop.Name + "。可用项: " + string.Join(", ", byKey.Keys));

            if (!seen.Add(setting.Key))
                return Error("设置项重复: " + setting.Key);

            if (!setting.RuntimeEditable)
                return Error("设置项 " + setting.Key + " 不支持运行中修改：" + setting.Description);

            switch (setting.Type)
            {
                case "bool":
                    if (prop.Value.ValueKind != JsonValueKind.True && prop.Value.ValueKind != JsonValueKind.False)
                        return Error(setting.Key + " 需要布尔值 true / false");
                    plan.Add(new KeyValuePair<SettingDescriptor, object>(setting, prop.Value.GetBoolean()));
                    break;

                case "int":
                    if (prop.Value.ValueKind != JsonValueKind.Number || !prop.Value.TryGetInt32(out var intValue))
                        return Error(setting.Key + " 需要整数");
                    if (intValue < setting.Min || intValue > setting.Max)
                        return Error(setting.Key + " 超出范围 " + setting.Min + ".." + setting.Max + "（收到 " + intValue + "）");
                    plan.Add(new KeyValuePair<SettingDescriptor, object>(setting, intValue));
                    break;

                case "string":
                    if (prop.Value.ValueKind != JsonValueKind.String)
                        return Error(setting.Key + " 需要字符串（清空传 \"\"）");
                    var textValue = prop.Value.GetString() ?? "";
                    if (textValue.Length > 256)
                        return Error(setting.Key + " 长度上限 256（收到 " + textValue.Length + "）");
                    plan.Add(new KeyValuePair<SettingDescriptor, object>(setting, textValue));
                    break;

                default:
                    return Error("设置项 " + setting.Key + " 类型未知: " + setting.Type);
            }
        }

        if (plan.Count == 0)
            return Error("\"set\" 是空的，没有要改的设置项");

        var applied = new Dictionary<string, object>();
        var touchedTradingGate = false;

        foreach (var entry in plan)
        {
            var setting = entry.Key;
            var before = setting.Read();
            setting.Write(entry.Value);

            var from = setting.Sensitive ? MaskSecret(before) : before;
            var to = setting.Sensitive ? MaskSecret(entry.Value) : entry.Value;

            applied[setting.Key] = new Dictionary<string, object> { ["from"] = from, ["to"] = to };

            if (setting.Key == "allowTrading" || setting.Key == "tradeToken")
                touchedTradingGate = true;

            // 审计：每次设置变更都留一行（口令只记长度，不记明文）。
            Log("SETTINGS " + setting.Key + " " + DescribeValue(from) + " -> " + DescribeValue(to));
        }

        // 让下一根 tick 立刻按新参数重建快照，而不是各等各的节流窗口。
        _refreshAllRequested = true;

        var warnings = new List<string>
        {
            "运行中修改只在内存生效：ATAS 重启 / 重新加载策略后会回到属性面板里保存的值。"
        };

        if (touchedTradingGate)
        {
            warnings.Add("你刚改了下单闸门。当前 tradingArmed=" + IsTradingArmed()
                         + "（还需 IsActivated + 口令非空 + MCP 进程环境变量 ATAS_TRADE_TOKEN 与口令一致，"
                         + "下单工具才会被注册）。");
        }

        return JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["ok"] = true,
            ["applied"] = applied,
            ["isActivated"] = IsActivated,
            ["tradingArmed"] = IsTradingArmed(),
            ["settings"] = BuildSettingsJson(),
            ["warnings"] = warnings
        }, JsonOpts);
    }

    /// <summary>口令类只报"是否已设置 + 长度"，永不回显明文。</summary>
    private static string MaskSecret(object value)
    {
        var text = value as string ?? "";
        return text.Length == 0 ? "(空)" : "(已设置, " + text.Length + " 字符)";
    }

    private static string DescribeValue(object value)
    {
        if (value == null)
            return "null";
        if (value is bool flag)
            return flag ? "true" : "false";
        return Convert.ToString(value, CultureInfo.InvariantCulture) ?? "";
    }

    #endregion

    #region HTTP 路由

    private string HandleRequest(string method, string path, Dictionary<string, string> query, string body)
    {
#if MAC_INDICATOR
        if (method != "GET")
            return Error("This indicator is read-only; only GET requests are supported.");
        if (path == "/api/position" || path == "/api/orders" ||
            path == "/api/order" || path.StartsWith("/api/order/", StringComparison.Ordinal) ||
            path.StartsWith("/api/position/", StringComparison.Ordinal))
            return Error("Trading and account endpoints are unavailable in the read-only indicator.");
        if (path is "/api/quote" or "/api/dom" or "/api/cvd" or "/api/candles" or "/api/footprint" or "/api/profile")
            if (!_hostRunning || Interlocked.Read(ref _lastSnapshotTicks) == 0)
                return Error("Waiting for chart data; inspect /api/status for freshness.");
#endif
        switch (path)
        {
            case "/api/health":
                return JsonSerializer.Serialize(new Dictionary<string, object>
                {
                    ["ok"] = true,
                    ["bridge"] = BridgeId,
                    ["version"] = BridgeVersion,
#if MAC_INDICATOR
                    ["hostMode"] = "indicator",
                    ["readOnly"] = true,
#endif
                    ["port"] = _server?.Port ?? 0,
                    ["isActivated"] = IsActivated
                }, JsonOpts);

#if MAC_INDICATOR
            case "/api/status": return WithHostFreshness(_statusJson);
#else
            case "/api/status": return _statusJson;
#endif
            case "/api/quote": return _quoteJson;
            case "/api/position": return _positionJson;
            case "/api/orders": return _ordersJson;
            case "/api/cvd": return _cvdJson;
            case "/api/log": return BuildLog(QueryInt(query, "lines", 80));

            case "/api/settings":
                if (method == "POST")
                    return UpdateSettings(body);
                return GetSettings();

            case "/api/dom":
            {
                var levels = Math.Max(1, Math.Min(200, QueryInt(query, "levels", ClampDomLevels(DefaultDomLevels))));
                if (levels == _domJsonLevels)
                    return _domJson;
                return RunOnAtasThread(() => BuildDom(levels), 3000);
            }

            case "/api/candles":
            {
                var bars = QueryInt(query, "bars", SnapshotBars);
                bars = Math.Max(1, Math.Min(5000, bars));
                if (bars == _candlesJsonBars)
                    return _candlesJson;
                return RunOnAtasThread(() => BuildCandles(bars), 3000);
            }

            case "/api/footprint":
            {
                var hasBar = query.ContainsKey("bar");
                var top = Math.Max(0, QueryInt(query, "top", 0));
                var compact = QueryBool(query, "compact", true);

                if (!hasBar)
                    return _footprintJson;

                var bar = QueryInt(query, "bar", -1);
                return RunOnAtasThread(() =>
                {
                    var resolved = bar < 0 ? CurrentBar + bar : bar;
                    if (resolved < 0 || resolved > CurrentBar - 1)
                        return Error("bar 越界：解析为 " + resolved + "，有效范围 0.." + (CurrentBar - 1));
                    return BuildFootprintAt(resolved, top, compact);
                }, 3000);
            }

            case "/api/profile":
            {
                var bars = Math.Max(1, Math.Min(5000, QueryInt(query, "bars", ProfileLookbackBars)));
                var percent = Math.Max(1, Math.Min(100, QueryInt(query, "percent", ClampProfilePercent(ProfileValueAreaPercent))));
                var top = Math.Max(0, QueryInt(query, "top", 0));

                if (top == 0 && bars == _profileJsonBars && percent == _profileJsonPercent)
                    return _profileJson;

                return RunOnAtasThread(() => BuildProfile(bars, percent, top), 5000);
            }

#if !MAC_INDICATOR
            case "/api/order":
                if (method != "POST")
                    return Error("POST required");
                return RunOnAtasThread(() => PlaceOrder(body), 4000);

            case "/api/order/cancel":
                if (method != "POST")
                    return Error("POST required");
                return RunOnAtasThread(() => CancelOrderById(body), 4000);

            case "/api/position/close":
                if (method != "POST")
                    return Error("POST required");
                return RunOnAtasThread(() => FlattenPosition(body), 4000);
#endif

            default:
                return Error("unknown endpoint: " + path);
        }
    }

    private string BuildLog(int lines)
    {
        string[] snapshot;
        lock (_logLock)
        {
            snapshot = _log.Skip(Math.Max(0, _log.Count - lines)).ToArray();
        }
        return JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["ok"] = true,
            ["lines"] = snapshot
        }, JsonOpts);
    }

    #endregion

    #region 下单（默认关闭，双重闸门）

#if !MAC_INDICATOR
    private string PlaceOrder(string body)
    {
        if (!IsTradingArmed())
            return Error("下单已禁用。需要在 ATAS 策略设置里同时：勾选 IsActivated、打开「允许下单」、并填写「下单确认口令」。");

        using var doc = JsonDocument.Parse(body);
        var root = doc.RootElement;

        if (GetStr(root, "confirm") != TradeToken)
            return Error("确认口令不正确");

        var directionStr = GetStr(root, "direction");
        var typeStr = GetStr(root, "type");
        var qty = GetDec(root, "qty");
        var price = GetDec(root, "price");
        var trigger = GetDec(root, "triggerPrice");

        if (!Enum.TryParse<OrderDirections>(directionStr, true, out var direction))
            return Error("direction 必须是 buy 或 sell");
        if (!Enum.TryParse<OrderTypes>(typeStr, true, out var type))
            return Error("type 必须是 limit/stop/market/stoplimit");
        if (qty <= 0m)
            return Error("qty 必须 > 0");
        if (type == OrderTypes.Limit && price <= 0m)
            return Error("limit 单必须给 price");
        if (type == OrderTypes.Stop && trigger <= 0m)
            return Error("stop 单必须给 triggerPrice");
        if (type == OrderTypes.StopLimit && (price <= 0m || trigger <= 0m))
            return Error("stoplimit 单必须给 price 和 triggerPrice");

        if (Portfolio == null)
            return Error("策略里没有选择账户（Portfolio）");

        var order = new Order
        {
            Portfolio = Portfolio,
            Security = Security,
            Direction = direction,
            Type = type,
            QuantityToFill = qty,
            Comment = GetStr(root, "comment")
        };

        if (type == OrderTypes.Limit)
            order.Price = ShrinkPrice(price);
        else if (type == OrderTypes.Stop)
            order.TriggerPrice = ShrinkPrice(trigger);
        else if (type == OrderTypes.StopLimit)
        {
            order.TriggerPrice = ShrinkPrice(trigger);
            order.Price = ShrinkPrice(price);
        }

        OpenOrder(order);
        Log("ORDER SENT " + direction + " " + type + " qty=" + qty + " price=" + order.Price + " trigger=" + order.TriggerPrice);

        return JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["ok"] = true,
            ["message"] = "order sent",
            ["id"] = order.Id,
            ["direction"] = direction.ToString(),
            ["type"] = type.ToString(),
            ["qty"] = qty,
            ["price"] = order.Price,
            ["triggerPrice"] = order.TriggerPrice
        }, JsonOpts);
    }

    private string CancelOrderById(string body)
    {
        if (!IsTradingArmed())
            return Error("下单已禁用。");

        using var doc = JsonDocument.Parse(body);
        var root = doc.RootElement;

        if (GetStr(root, "confirm") != TradeToken)
            return Error("确认口令不正确");

        var id = GetStr(root, "id");
        if (string.IsNullOrWhiteSpace(id))
            return Error("需要 id");
        if (Orders == null)
            return Error("没有可撤的挂单");

        var order = Orders.FirstOrDefault(o => string.Equals(o.Id, id, StringComparison.OrdinalIgnoreCase));
        if (order == null)
            return Error("找不到挂单: " + id);

        CancelOrder(order);
        Log("ORDER CANCEL " + id);

        return JsonSerializer.Serialize(new Dictionary<string, object> { ["ok"] = true, ["message"] = "cancel sent", ["id"] = id }, JsonOpts);
    }

    private string FlattenPosition(string body)
    {
        if (!IsTradingArmed())
            return Error("下单已禁用。");

        using var doc = JsonDocument.Parse(string.IsNullOrWhiteSpace(body) ? "{}" : body);
        var root = doc.RootElement;

        if (GetStr(root, "confirm") != TradeToken)
            return Error("确认口令不正确");

        var net = 0m;
        if (MyTrades != null)
        {
            foreach (var trade in MyTrades)
                net += trade.OrderDirection == OrderDirections.Buy ? trade.Volume : -trade.Volume;
        }

        if (net == 0m)
            return Error("当前没有持仓可平");

        var volume = GetDec(root, "volume");
        if (volume <= 0m)
            volume = Math.Abs(net);

        var closeDirection = net > 0m ? OrderDirections.Sell : OrderDirections.Buy;

        var order = new Order
        {
            Portfolio = Portfolio,
            Security = Security,
            Direction = closeDirection,
            Type = OrderTypes.Market,
            QuantityToFill = volume,
            Comment = "ai-bridge-flatten"
        };

        OpenOrder(order);
        Log("FLATTEN SENT " + closeDirection + " qty=" + volume);

        return JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["ok"] = true,
            ["message"] = "flatten sent",
            ["volume"] = volume,
            ["direction"] = closeDirection.ToString()
        }, JsonOpts);
    }
#endif

    #endregion

    #region 工具

    private static string Error(string message)
    {
        return JsonSerializer.Serialize(new Dictionary<string, object> { ["ok"] = false, ["error"] = message }, JsonOpts);
    }

    private static int ClampDomLevels(int levels)
    {
        return Math.Max(1, Math.Min(200, levels));
    }

    private static int ClampProfilePercent(int percent)
    {
        return Math.Max(1, Math.Min(100, percent));
    }

    private static int QueryInt(Dictionary<string, string> query, string key, int fallback)
    {
        if (query != null && query.TryGetValue(key, out var raw) && int.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out var value))
            return value;
        return fallback;
    }

    private static bool QueryBool(Dictionary<string, string> query, string key, bool fallback)
    {
        if (query != null && query.TryGetValue(key, out var raw))
        {
            if (raw == "1" || string.Equals(raw, "true", StringComparison.OrdinalIgnoreCase))
                return true;
            if (raw == "0" || string.Equals(raw, "false", StringComparison.OrdinalIgnoreCase))
                return false;
        }
        return fallback;
    }

    private static string GetStr(JsonElement root, string name)
    {
        if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty(name, out var el) && el.ValueKind == JsonValueKind.String)
            return el.GetString();
        return null;
    }

    private static decimal GetDec(JsonElement root, string name)
    {
        if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty(name, out var el))
        {
            if (el.ValueKind == JsonValueKind.Number && el.TryGetDecimal(out var value))
                return value;
            if (el.ValueKind == JsonValueKind.String && decimal.TryParse(el.GetString(), NumberStyles.Any, CultureInfo.InvariantCulture, out var parsed))
                return parsed;
        }
        return 0m;
    }

    private void Log(string message)
    {
        var line = DateTime.Now.ToString("HH:mm:ss.fff", CultureInfo.InvariantCulture) + " " + message;
        lock (_logLock)
        {
            _log.Add(line);
            if (_log.Count > 500)
                _log.RemoveRange(0, _log.Count - 500);
        }
    }

    private void WritePortFile(int port)
    {
        try
        {
            var text = port.ToString(CultureInfo.InvariantCulture);
            var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            foreach (var dir in new[] { "ATAS", "ATAS X" })
            {
                try
                {
                    var target = Path.Combine(appData, dir);
                    if (Directory.Exists(target))
                        File.WriteAllText(Path.Combine(target, "AiBridge.port"), text);
                }
                catch { }
            }
        }
        catch { }
    }

    #endregion
}
