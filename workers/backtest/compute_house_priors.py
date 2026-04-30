"""Compute weekly Mon-open-to-Fri-close house priors conditional on Faber state.

Output: workers/backtest/out/house_priors.json — three probability vectors
[P(UP), P(FLAT), P(DOWN)] keyed by Faber state at Monday open. Used by
voting-worker as the rules-based prior on the weekly poll target.

Bucketing:
  UP    if Friday/Monday return >  +0.25%
  DOWN  if Friday/Monday return <  -0.25%
  FLAT  otherwise

Faber state at Monday open is computed from SPY close on the prior Friday:
  GREEN   : SPY > 200DMA AND 50DMA > 200DMA
  CAUTION : SPY < 200DMA AND 50DMA < 200DMA
  NEUTRAL : indicators disagree

Shrinkage: any conditional with n < 200 is shrunk toward the unconditional
base rate via a Beta-binomial-style soft-mix at weight = n/(n+200).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

START = "1997-01-01"


def fetch_spy(client):
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = client.get("https://query1.finance.yahoo.com/v8/finance/chart/SPY",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    s = pd.Series(data["indicators"]["quote"][0]["close"], index=ts).dropna()
    return s[~s.index.duplicated(keep="last")]


def main():
    print("[priors] fetching SPY...")
    with httpx.Client() as c:
        spy = fetch_spy(c)
    df = pd.DataFrame({"spy": spy})
    df["ma_50"] = df["spy"].rolling(50).mean()
    df["ma_200"] = df["spy"].rolling(200).mean()
    df["dow"] = df.index.dayofweek   # 0 = Mon ... 4 = Fri
    df = df.dropna(subset=["spy", "ma_50", "ma_200"])

    # Faber state per day (point-in-time)
    above_200 = df["spy"] > df["ma_200"]
    fifty_above = df["ma_50"] > df["ma_200"]
    df["faber"] = "NEUTRAL"
    df.loc[above_200 & fifty_above, "faber"] = "GREEN"
    df.loc[(~above_200) & (~fifty_above), "faber"] = "CAUTION"

    print(f"[priors] panel: {df.shape}, {df.index.min().date()} to {df.index.max().date()}")

    # Walk through each ISO calendar week. Monday = first trading day on/after Mon.
    df["iso_year"] = df.index.isocalendar().year
    df["iso_week"] = df.index.isocalendar().week

    weeks = []
    for (y, w), grp in df.groupby(["iso_year", "iso_week"]):
        if len(grp) < 2:
            continue
        # Monday's open ≈ first trading day of the week's open. We use prior
        # session close as the proxy for "Monday open" because Yahoo daily
        # bars give close, not open. Equivalent for our purposes.
        first = grp.iloc[0]
        last = grp.iloc[-1]
        ret = (last["spy"] - first["spy"]) / first["spy"]
        if ret > 0.0025:
            outcome = "UP"
        elif ret < -0.0025:
            outcome = "DOWN"
        else:
            outcome = "FLAT"
        # Faber state to condition on = state at the START of the week
        faber_at_start = first["faber"]
        weeks.append({
            "iso_year": y, "iso_week": w,
            "start_date": str(grp.index[0].date()),
            "end_date": str(grp.index[-1].date()),
            "start_spy": float(first["spy"]),
            "end_spy": float(last["spy"]),
            "return": float(ret),
            "outcome": outcome,
            "faber_at_start": faber_at_start,
        })

    weeks_df = pd.DataFrame(weeks)
    print(f"[priors] {len(weeks_df)} weeks, range {weeks_df['start_date'].min()} to {weeks_df['end_date'].max()}")

    # Unconditional base rate
    unc = weeks_df["outcome"].value_counts(normalize=True).reindex(["UP", "FLAT", "DOWN"], fill_value=0).to_dict()
    n_total = len(weeks_df)

    # Conditional on Faber state
    SHRINK_PRIOR_N = 200  # weight at which shrinkage is 50/50
    out = {
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "panel_range": [weeks_df["start_date"].min(), weeks_df["end_date"].max()],
        "n_weeks_total": n_total,
        "bucket_thresholds_pct": {"up": 0.25, "down": -0.25},
        "shrinkage_prior_n": SHRINK_PRIOR_N,
        "unconditional": {k: round(v, 4) for k, v in unc.items()},
        "by_faber_state": {},
    }
    for state in ["GREEN", "NEUTRAL", "CAUTION"]:
        sub = weeks_df[weeks_df["faber_at_start"] == state]
        n = len(sub)
        raw = sub["outcome"].value_counts(normalize=True).reindex(["UP", "FLAT", "DOWN"], fill_value=0).to_dict()
        # Shrinkage toward unconditional
        w = n / (n + SHRINK_PRIOR_N)
        shrunk = {k: round(w * raw[k] + (1 - w) * unc[k], 4) for k in ["UP", "FLAT", "DOWN"]}
        out["by_faber_state"][state] = {
            "n": n,
            "raw": {k: round(v, 4) for k, v in raw.items()},
            "shrinkage_weight": round(w, 4),
            "prior": shrunk,
        }

    out_path = Path(__file__).parent / "out" / "house_priors.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n[priors] wrote {out_path}")

    # Pretty print
    print("\n=== House Priors (Mon-open to Fri-close, 3-way) ===\n")
    print(f"Unconditional ({n_total} weeks):")
    print(f"  UP   {unc['UP']*100:5.1f}%   FLAT {unc['FLAT']*100:5.1f}%   DOWN {unc['DOWN']*100:5.1f}%")
    print()
    for state in ["GREEN", "NEUTRAL", "CAUTION"]:
        s = out["by_faber_state"][state]
        print(f"{state} (n={s['n']}, shrinkage_weight={s['shrinkage_weight']}):")
        print(f"  raw      UP {s['raw']['UP']*100:5.1f}%   FLAT {s['raw']['FLAT']*100:5.1f}%   DOWN {s['raw']['DOWN']*100:5.1f}%")
        print(f"  shrunk   UP {s['prior']['UP']*100:5.1f}%   FLAT {s['prior']['FLAT']*100:5.1f}%   DOWN {s['prior']['DOWN']*100:5.1f}%")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
