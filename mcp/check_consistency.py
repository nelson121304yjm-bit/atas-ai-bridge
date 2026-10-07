"""ATAS AI Bridge 口径一致性自检。

不只验证"能返回数据"，而是验证数据自身与 ATAS 口径是否自洽：
  - 逐价档数 == (high-low)/tickSize + 1
  - 逐价量之和 == 整根 volume
  - 逐价 delta 之和 == 整根 delta（delta 定义 = ask - bid）
  - POC == 成交量最大档的价位
  - VAL <= POC <= VAH
  - 区间 profile 的 val <= poc <= vah，且价值区成交量 >= percent% 目标
  - CVD 末值 == 返回区间内各根 delta 之和（会话重置点后重新累加）
  - 委托簿买盘降序、卖盘升序、买一 < 卖一
  - quote 的价差 == tickSize

用法：
    .\\.venv\\Scripts\\python.exe check_consistency.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from atas_client import AtasBridgeClient, BridgeError  # noqa: E402

TOL = 1e-6
passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    mark = "PASS" if ok else "FAIL"
    if ok:
        passed += 1
    else:
        failed += 1
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail else ""))


def close(a: float, b: float, tol: float = TOL) -> bool:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= tol * scale


def main() -> int:
    client = AtasBridgeClient()
    print(f"目标: {client.base}\n")

    try:
        status = client.status()
        quote = client.quote()
        footprint = client.footprint(bar=-1, top=0)
        profile = client.profile()
        cvd = client.cvd()
        dom = client.dom(10)
    except BridgeError as exc:
        print(f"桥不可达: {exc}")
        return 2

    tick = float(status.get("tickSize") or 0)

    print("== 基础 ==")
    check("tickSize > 0", tick > 0, f"tick={tick}")
    check("instrument 非空", bool(status.get("instrument")), str(status.get("instrument")))
    check("isActivated", bool(status.get("isActivated")))

    print("\n== quote ==")
    bid, ask = quote.get("bestBid"), quote.get("bestAsk")
    if bid is not None and ask is not None:
        check("买一 < 卖一", float(bid) < float(ask), f"{bid} < {ask}")
        check("价差 == tickSize", close(float(ask) - float(bid), tick),
              f"spread={float(ask) - float(bid)} tick={tick}")
    else:
        check("quote 含买一卖一", False, f"bid={bid} ask={ask}")

    print("\n== footprint（单根逐价） ==")
    if not isinstance(footprint, dict) or footprint.get("ok") is False:
        check("footprint 返回成功", False, str(footprint)[:200])
    else:
        levels = footprint.get("levels") or []
        high, low = float(footprint["h"]), float(footprint["l"])
        expected_levels = int(round((high - low) / tick)) + 1
        check("档数 == (high-low)/tick + 1",
              len(levels) == expected_levels and footprint.get("levelCount") == expected_levels,
              f"返回={len(levels)} levelCount={footprint.get('levelCount')} 期望={expected_levels}")

        sum_vol = sum(float(x["v"]) for x in levels)
        sum_delta = sum(float(x["d"]) for x in levels)
        sum_bid = sum(float(x["bid"]) for x in levels)
        sum_ask = sum(float(x["ask"]) for x in levels)
        sum_ticks = sum(int(x["n"]) for x in levels)

        check("Σ逐价量 == 整根 volume", close(sum_vol, float(footprint["volume"])),
              f"Σ={sum_vol:.6f} volume={footprint['volume']}")
        check("Σ逐价 delta == 整根 delta", close(sum_delta, float(footprint["delta"])),
              f"Σ={sum_delta:.6f} delta={footprint['delta']}")
        check("Σbid + Σask == Σvolume", close(sum_bid + sum_ask, sum_vol),
              f"bid={sum_bid:.6f} ask={sum_ask:.6f} vol={sum_vol:.6f}")
        check("delta 定义 == ask - bid", close(sum_delta, sum_ask - sum_bid),
              f"Σd={sum_delta:.6f} Σask-Σbid={sum_ask - sum_bid:.6f}")

        if levels:
            argmax = max(levels, key=lambda x: float(x["v"]))["p"]
            check("POC == 最大量档位", close(float(argmax), float(footprint["poc"])),
                  f"argmax={argmax} poc={footprint['poc']}")

        vah, val, poc = (float(footprint["vah"]), float(footprint["val"]), float(footprint["poc"]))
        check("VAL <= POC <= VAH", val <= poc <= vah, f"VAL={val} POC={poc} VAH={vah}")

        in_range = all(low <= float(x["p"]) <= high for x in levels)
        check("所有价位落在 [low, high]", in_range)
        check("成交笔数 n > 0", sum_ticks > 0, f"Σn={sum_ticks}")

    print("\n== profile（区间分布） ==")
    if not isinstance(profile, dict) or profile.get("ok") is False:
        check("profile 返回成功", False, str(profile)[:200])
    else:
        p_poc, p_vah, p_val = float(profile["poc"]), float(profile["vah"]), float(profile["val"])
        check("VAL <= POC <= VAH", p_val <= p_poc <= p_vah,
              f"VAL={p_val} POC={p_poc} VAH={p_vah}")
        check("usedBars > 0", int(profile.get("usedBars", 0)) > 0, f"usedBars={profile.get('usedBars')}")
        total = float(profile["totalVolume"])
        va_vol = float(profile["valueAreaVolume"])
        pct = float(profile["valueAreaPercent"])
        check("价值区成交量 >= percent% 总成交量", va_vol + TOL >= total * pct / 100.0,
              f"VA量={va_vol:.2f} 目标={total * pct / 100.0:.2f} 总量={total:.2f}")
        p_levels = profile.get("levels") or []
        sum_p = sum(float(x["v"]) for x in p_levels)
        check("Σ逐价量 == totalVolume", close(sum_p, total), f"Σ={sum_p:.6f} total={total}")

    print("\n== CVD（累计 delta） ==")
    if not isinstance(cvd, list) or not cvd:
        check("cvd 返回非空序列", False, str(cvd)[:200])
    else:
        running = 0.0
        expected_last = None
        for row in cvd:
            if row.get("sessionStart"):
                running = 0.0
            running += float(row["delta"])
            expected_last = running
        check("末根 cvd == 会话内 delta 累加", close(float(cvd[-1]["cvd"]), expected_last),
              f"末值={cvd[-1]['cvd']} 复算={expected_last:.6f}")
        check("序列长度 > 0", len(cvd) > 0, f"{len(cvd)} 根")
        check("含 sessionStart 标记", any(r.get("sessionStart") for r in cvd),
              "本会话内未出现重置点（正常，跨会话才出现）"
              if not any(r.get("sessionStart") for r in cvd) else "")

    print("\n== DOM（委托簿） ==")
    bids, asks = dom.get("bids") or [], dom.get("asks") or []
    if bids and asks:
        bp = [float(x["price"]) for x in bids]
        ap = [float(x["price"]) for x in asks]
        check("买盘降序", bp == sorted(bp, reverse=True), f"{bp[:3]}...")
        check("卖盘升序", ap == sorted(ap), f"{ap[:3]}...")
        check("买一 < 卖一（无交叉）", max(bp) < min(ap), f"max bid={max(bp)} min ask={min(ap)}")
    else:
        check("DOM 含买卖两档", False, f"bids={len(bids)} asks={len(asks)}")

    print(f"\n结果: {passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
