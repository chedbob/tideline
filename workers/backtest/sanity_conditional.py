"""Proper conditional test — when rule says UP, what % actually go UP?
When rule says DOWN, what % go DOWN? Edge = rule's calls vs uncond baseline.

Unlike the aggregated test, this measures whether the rule splits the world
into two buckets with MEANINGFULLY different forward-return distributions.
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

START = "1997-01-01"


def fetch_fred(c, sid, k):
    r = c.get("https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": sid, "api_key": k, "file_type": "json", "observation_start": START, "sort_order": "asc"}, timeout=30)
    r.raise_for_status()
    recs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
    d, v = zip(*recs)
    return pd.Series(v, index=pd.to_datetime(d))


def fetch_yahoo(c, t):
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    s = pd.Series(data["indicators"]["quote"][0]["close"], index=ts).dropna()
    return s[~s.index.duplicated(keep="last")]


def wilson(h, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = h/n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    h_ = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return (c-h_, c+h_)


def analyze(name, condition_up, condition_down, spy_fwd, baseline_up):
    up_mask = condition_up & spy_fwd.notna()
    dn_mask = condition_down & spy_fwd.notna()

    up_days = spy_fwd[up_mask]
    dn_days = spy_fwd[dn_mask]

    up_actual_up = (up_days > 0).sum()
    up_n = len(up_days)
    dn_actual_up = (dn_days > 0).sum()
    dn_n = len(dn_days)

    # When rule says UP: accuracy = actual UP rate. Edge = (actual UP rate) − (unconditional UP rate)
    up_acc = up_actual_up / up_n if up_n else float("nan")
    up_edge = (up_acc - baseline_up) * 100
    up_lo, up_hi = wilson(up_actual_up, up_n)

    # When rule says DOWN: accuracy = actual DOWN rate. Edge = (actual DOWN rate) − (unconditional DOWN rate)
    dn_acc = (dn_n - dn_actual_up) / dn_n if dn_n else float("nan")
    dn_edge = (dn_acc - (1 - baseline_up)) * 100
    dn_hits = dn_n - dn_actual_up
    dn_lo, dn_hi = wilson(dn_hits, dn_n)

    # Combined weighted accuracy across both calls
    total_hits = up_actual_up + (dn_n - dn_actual_up)
    total_n = up_n + dn_n
    combined_acc = total_hits / total_n if total_n else float("nan")
    # Weighted baseline for combined (what if we always predicted the majority class in each bucket at base rate?)
    weighted_baseline = (up_n * baseline_up + dn_n * (1 - baseline_up)) / total_n if total_n else float("nan")
    combined_edge = (combined_acc - weighted_baseline) * 100

    return {
        "name": name,
        "up_call": {"n": up_n, "accuracy": up_acc, "edge_pp": up_edge, "ci": [up_lo, up_hi]},
        "dn_call": {"n": dn_n, "accuracy": dn_acc, "edge_pp": dn_edge, "ci": [dn_lo, dn_hi]},
        "combined": {"n": total_n, "accuracy": combined_acc, "weighted_baseline": weighted_baseline, "edge_pp": combined_edge},
    }


def main():
    key = os.environ["FRED_API_KEY"]
    with httpx.Client() as fc, httpx.Client() as yc:
        spy = fetch_yahoo(yc, "SPY")
        vix = fetch_fred(fc, "VIXCLS", key)
        credit = fetch_fred(fc, "BAA10Y", key)

    df = pd.DataFrame({"spy": spy})
    df["vix"] = vix.reindex(df.index, method="ffill")
    df["credit"] = credit.reindex(df.index, method="ffill")
    df = df.dropna()

    H = 20
    fwd = df["spy"].shift(-H) / df["spy"] - 1
    baseline_up = (fwd > 0).mean()
    print(f"Panel: {df.index.min().date()} to {df.index.max().date()}, n={len(df):,}")
    print(f"Baseline SPY {H}D UP rate: {baseline_up:.1%}  (DOWN rate: {1-baseline_up:.1%})")
    print()

    ma200 = df["spy"].rolling(200).mean()
    ma50 = df["spy"].rolling(50).mean()

    rules = [
        ("Faber: SPY > 200DMA",
            df["spy"] > ma200,
            df["spy"] < ma200),
        ("Golden cross (50 > 200 MA)",
            ma50 > ma200,
            ma50 < ma200),
        ("Fast trend (SPY > 50DMA)",
            df["spy"] > ma50,
            df["spy"] < ma50),
        ("BAA10Y 20D change <-20bp / >+20bp",
            df["credit"].diff(20) < -0.2,
            df["credit"].diff(20) >  0.2),
        ("VIX 20D change < -2 / > +2",
            df["vix"].diff(20) < -2,
            df["vix"].diff(20) >  2),
        ("SPY > 200DMA AND Golden cross",
            (df["spy"] > ma200) & (ma50 > ma200),
            (df["spy"] < ma200) & (ma50 < ma200)),
    ]

    print(f"{'Rule':<40}  {'UP call':>38}  {'DOWN call':>38}")
    print(f"{'':<40}  {'n':>5}  {'Acc':>6}  {'Edge':>8}  {'CI':>14}  {'n':>5}  {'Acc':>6}  {'Edge':>8}  {'CI':>14}")
    print("-" * 150)

    for name, cond_up, cond_dn in rules:
        res = analyze(name, cond_up, cond_dn, fwd, baseline_up)
        up, dn = res["up_call"], res["dn_call"]
        print(f"  {name:<38}  {up['n']:>5,}  {up['accuracy']:>5.1%}  {up['edge_pp']:+7.1f}pp  [{up['ci'][0]:.2f},{up['ci'][1]:.2f}]  "
              f"{dn['n']:>5,}  {dn['accuracy']:>5.1%}  {dn['edge_pp']:+7.1f}pp  [{dn['ci'][0]:.2f},{dn['ci'][1]:.2f}]")

    print()
    print("Edge interpretation:")
    print(f"  UP call edge = (actual UP rate when rule says UP) - ({baseline_up:.1%} unconditional)")
    print(f"  DOWN call edge = (actual DOWN rate when rule says DOWN) - ({1-baseline_up:.1%} unconditional)")
    print("  Positive edge = rule distinguishes this regime from the average")
    return 0


if __name__ == "__main__":
    sys.exit(main())
