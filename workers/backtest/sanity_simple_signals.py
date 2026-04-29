"""Sanity check — do simple, literature-documented signals have edge?

We've proven our 4-factor composite has no edge. Before concluding 'nothing
works', test the simplest published rules:

  1. SPY > 200DMA  ->  predict UP (Faber 2007 tactical asset allocation)
  2. SPY > 50DMA   ->  predict UP (faster trend)
  3. Credit spread 20D CHANGE (not level): widening = risk-off
  4. VIX term in backwardation: short-term stress signal
  5. Golden cross (50 > 200 MA): classic momentum

Target: SPY 20-day directional accuracy vs baseline.
Window: 1997-2026 panel (BAA10Y-based). No parameter tuning.
"""
from __future__ import annotations

import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

START = "1997-01-01"


def fetch_fred(client, sid, key):
    r = client.get("https://api.stlouisfed.org/fred/series/observations",
        params={"series_id": sid, "api_key": key, "file_type": "json",
                "observation_start": START, "sort_order": "asc"}, timeout=30)
    r.raise_for_status()
    recs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
    d, v = zip(*recs)
    return pd.Series(v, index=pd.to_datetime(d))


def fetch_yahoo(client, ticker):
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    closes = data["indicators"]["quote"][0]["close"]
    s = pd.Series(closes, index=ts).dropna()
    return s[~s.index.duplicated(keep="last")]


def wilson(h, n):
    if n == 0:
        return (0, 0)
    p = h/n; z = 1.96
    denom = 1 + z*z/n
    c = (p + z*z/(2*n))/denom
    half = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))/denom
    return (c-half, c+half)


def eval_rule(pred: pd.Series, actual_up: pd.Series, label: str, baseline: float):
    """pred in {+1, -1, 0}. Only count +1/-1 as calls."""
    pairs = pd.concat({"p": pred, "u": actual_up}, axis=1).dropna()
    calls = pairs[pairs["p"] != 0]
    n = len(calls)
    if n == 0:
        return None
    hits = int(((calls["p"] == 1) & (calls["u"] == 1) | (calls["p"] == -1) & (calls["u"] == 0)).sum())
    acc = hits / n
    lo, hi = wilson(hits, n)
    return {
        "label": label, "n": n, "accuracy": acc,
        "edge_pp": (acc - baseline) * 100,
        "ci": [lo, hi],
        "passes_50pct": acc > 0.5,
        "ci_excludes_baseline": lo > baseline,
    }


def main():
    key = os.environ["FRED_API_KEY"]
    print("[sanity] fetching SPY + BAA10Y + VIX...")
    with httpx.Client() as fc, httpx.Client() as yc:
        spy = fetch_yahoo(yc, "SPY")
        vix = fetch_fred(fc, "VIXCLS", key)
        credit = fetch_fred(fc, "BAA10Y", key)

    df = pd.DataFrame({"spy": spy})
    df["vix"] = vix.reindex(df.index, method="ffill")
    df["credit"] = credit.reindex(df.index, method="ffill")
    df = df.dropna()
    print(f"[sanity] panel: {df.shape}, {df.index.min().date()} to {df.index.max().date()}")

    H = 20
    fwd = df["spy"].shift(-H) / df["spy"] - 1
    actual_up = (fwd > 0).astype(int)
    baseline = actual_up.mean()
    print(f"[sanity] baseline UP rate at H={H}D: {baseline:.1%}\n")

    # Rule 1: SPY > 200DMA -> UP, else DOWN
    ma200 = df["spy"].rolling(200).mean()
    rule1 = pd.Series(0, index=df.index, dtype=float)
    rule1[df["spy"] > ma200] = 1
    rule1[df["spy"] < ma200] = -1

    # Rule 2: SPY > 50DMA -> UP
    ma50 = df["spy"].rolling(50).mean()
    rule2 = pd.Series(0, index=df.index, dtype=float)
    rule2[df["spy"] > ma50] = 1
    rule2[df["spy"] < ma50] = -1

    # Rule 3: 20D credit spread change. Widening by > 20bps -> DOWN, narrowing by > 20bps -> UP.
    #  Pre-declared threshold = 0.2% (20 bps), round-number, not tuned.
    credit_chg = df["credit"].diff(20)
    rule3 = pd.Series(0, index=df.index, dtype=float)
    rule3[credit_chg < -0.2] = 1     # narrowing -> risk-on
    rule3[credit_chg > 0.2]  = -1    # widening -> risk-off

    # Rule 4: VIX 20D change. Falling > 2pts -> UP, rising > 2pts -> DOWN.
    vix_chg = df["vix"].diff(20)
    rule4 = pd.Series(0, index=df.index, dtype=float)
    rule4[vix_chg < -2] = 1
    rule4[vix_chg > 2] = -1

    # Rule 5: Golden cross (50DMA > 200DMA) -> UP; death cross -> DOWN.
    rule5 = pd.Series(0, index=df.index, dtype=float)
    rule5[ma50 > ma200] = 1
    rule5[ma50 < ma200] = -1

    rules = [
        ("Faber: SPY > 200DMA", rule1),
        ("SPY > 50DMA (fast trend)", rule2),
        ("BAA10Y 20D change > +/-20bp", rule3),
        ("VIX 20D change > +/-2pts", rule4),
        ("Golden/death cross (50>200 MA)", rule5),
    ]

    print(f"{'Rule':<40}  {'n':>6}  {'Acc':>6}  {'Edge':>8}  {'Wilson CI':>18}  {'>50%':>6}  {'>baseline':>11}")
    print("-" * 105)
    results = []
    for name, r in rules:
        res = eval_rule(r, actual_up, name, baseline)
        if res:
            results.append(res)
            ci_str = f"[{res['ci'][0]:.1%},{res['ci'][1]:.1%}]"
            flag50 = "YES" if res["passes_50pct"] else "no"
            flagbase = "YES" if res["ci_excludes_baseline"] else "no"
            print(f"  {name:<38}  {res['n']:>6,}  {res['accuracy']:>5.1%}  {res['edge_pp']:+7.1f}pp  {ci_str:>18}  {flag50:>6}  {flagbase:>11}")

    print(f"\nBaseline (unconditional SPY {H}D UP rate): {baseline:.1%}")
    passing = [r for r in results if r["ci_excludes_baseline"]]
    print(f"\nRules whose Wilson CI excludes baseline: {len(passing)}/{len(results)}")
    if passing:
        print("  Passing rules:")
        for r in passing:
            print(f"    - {r['label']}  (acc={r['accuracy']:.1%}, edge={r['edge_pp']:+.1f}pp, n={r['n']:,})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
