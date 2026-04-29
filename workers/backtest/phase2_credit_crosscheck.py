"""Sanity check: does swapping BAA10Y for the real HY OAS (BAMLH0A0HYM2)
give materially different regime classifications?

Both series are available 2023-04-24 onwards (when ICE truncated HY OAS
on FRED to 3 years). We compare:

  1. Direct correlation between BAA10Y and HY OAS (level + 20D change).
  2. Composite-level correlation with each series plugged in.
  3. Tercile agreement rate — does the regime classification match?
  4. Crisis-moment divergence — do they disagree at stress events?

Caveat: 3 years of QE-era data. Conclusions about crisis-period divergence
are limited — can only assess if the swap is sensible under normal conditions
plus mini-stress events (SVB 2023, Apr 2025 tariff vol, etc.).

Note on z-scoring: we use an in-window fixed z-score (not expanding) because
there's no long burn-in available on HY OAS. This is acceptable for a DATA
QUALITY comparison but would be look-ahead biased for a predictive claim.

Usage:  set -a; source .env; set +a; python -m backtest.phase2_credit_crosscheck
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Tideline-CrossCheck/0.1)"}

START = "2023-04-24"  # earliest HY OAS available on FRED


def fetch_fred(client, sid, api_key):
    r = client.get(FRED_BASE, params={
        "series_id": sid, "api_key": api_key, "file_type": "json",
        "observation_start": START, "sort_order": "asc"
    }, timeout=30.0)
    r.raise_for_status()
    recs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
    dates, vals = zip(*recs)
    return pd.Series(vals, index=pd.to_datetime(dates), name=sid)


def fetch_yahoo(client, ticker):
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = client.get(YAHOO.format(ticker=ticker),
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers=HEADERS, timeout=30.0)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    closes = data["indicators"]["quote"][0]["close"]
    s = pd.Series(closes, index=ts, name=ticker).dropna()
    return s[~s.index.duplicated(keep="last")]


def in_window_zscore(s):
    return (s - s.mean()) / s.std()


def assign_tercile(composite):
    q33, q67 = composite.quantile([1/3, 2/3])
    out = pd.Series(0, index=composite.index, dtype=float)
    out[composite >= q67] = 1
    out[composite <= q33] = -1
    return out


def main():
    api_key = os.environ["FRED_API_KEY"]
    print(f"[crosscheck] window starts {START}")

    with httpx.Client() as fc, httpx.Client(headers=HEADERS) as yc:
        hy_oas = fetch_fred(fc, "BAMLH0A0HYM2", api_key)
        baa10y = fetch_fred(fc, "BAA10Y", api_key)
        vix = fetch_fred(fc, "VIXCLS", api_key)
        curve = fetch_fred(fc, "T10Y3M", api_key)
        nfci = fetch_fred(fc, "NFCI", api_key)
        spy = fetch_yahoo(yc, "SPY")

    # Align to SPY trading days
    df = pd.DataFrame({"spy": spy})
    for name, s in [("hy_oas", hy_oas), ("baa10y", baa10y), ("vix", vix), ("curve", curve), ("nfci", nfci)]:
        s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
        df[name] = s.reindex(df.index, method="ffill")
    df = df.dropna()
    print(f"[crosscheck] panel: {df.shape}, range {df.index.min().date()} to {df.index.max().date()}")

    # --- 1. Direct correlation between the two credit series ---
    corr_level = df["hy_oas"].corr(df["baa10y"])
    corr_chg_20d = df["hy_oas"].diff(20).corr(df["baa10y"].diff(20))
    corr_chg_5d = df["hy_oas"].diff(5).corr(df["baa10y"].diff(5))

    # --- 2. Composite correlation (in-window z-score, not expanding) ---
    z_vix_inv = in_window_zscore(-df["vix"])
    z_curve = in_window_zscore(df["curve"])
    z_nfci_inv = in_window_zscore(-df["nfci"])

    composite_with_hy = in_window_zscore(-df["hy_oas"]) + z_vix_inv + z_curve + z_nfci_inv
    composite_with_baa = in_window_zscore(-df["baa10y"]) + z_vix_inv + z_curve + z_nfci_inv
    comp_corr = composite_with_hy.corr(composite_with_baa)

    # --- 3. Tercile agreement ---
    t_hy = assign_tercile(composite_with_hy)
    t_baa = assign_tercile(composite_with_baa)
    agreement = (t_hy == t_baa).mean()
    # Exact confusion
    confusion = pd.crosstab(t_baa.rename("baa"), t_hy.rename("hy"), normalize=False)

    # --- 4. Crisis-moment divergence ---
    # Take days where |hy_oas 20D change| is in top 5% (max stress) or bottom 5% (max easing)
    hy_chg_20d = df["hy_oas"].diff(20)
    stress_days = hy_chg_20d[hy_chg_20d > hy_chg_20d.quantile(0.95)]
    easing_days = hy_chg_20d[hy_chg_20d < hy_chg_20d.quantile(0.05)]

    stress_agreement = (t_hy.loc[stress_days.index] == t_baa.loc[stress_days.index]).mean()
    easing_agreement = (t_hy.loc[easing_days.index] == t_baa.loc[easing_days.index]).mean()

    # Spot-check specific dates
    notable_dates = [
        ("2023-03-10", "SVB collapse"),
        ("2023-03-13", "SVB aftermath"),
        ("2024-08-05", "Yen carry unwind"),
        ("2025-04-07", "Tariff vol (approx)"),
    ]
    spot_checks = []
    for d, desc in notable_dates:
        if d in df.index.strftime("%Y-%m-%d"):
            idx = pd.Timestamp(d)
            if idx in df.index:
                spot_checks.append({
                    "date": d,
                    "event": desc,
                    "hy_oas": round(df.loc[idx, "hy_oas"], 3),
                    "baa10y": round(df.loc[idx, "baa10y"], 3),
                    "composite_hy": round(composite_with_hy.loc[idx], 3),
                    "composite_baa": round(composite_with_baa.loc[idx], 3),
                    "tercile_hy": int(t_hy.loc[idx]),
                    "tercile_baa": int(t_baa.loc[idx]),
                    "agree": bool(t_hy.loc[idx] == t_baa.loc[idx]),
                })

    payload = {
        "window": {"start": str(df.index.min().date()), "end": str(df.index.max().date()), "n": len(df)},
        "direct_correlation": {
            "level": round(corr_level, 4),
            "change_5d": round(corr_chg_5d, 4),
            "change_20d": round(corr_chg_20d, 4),
        },
        "composite_correlation": round(comp_corr, 4),
        "tercile_agreement_overall": round(agreement, 4),
        "tercile_agreement_stress_days_top5pct": round(stress_agreement, 4) if len(stress_days) else None,
        "tercile_agreement_easing_days_bot5pct": round(easing_agreement, 4) if len(easing_days) else None,
        "confusion_matrix_baa_vs_hy": confusion.to_dict(),
        "notable_dates": spot_checks,
    }

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase2_credit_crosscheck.json").write_text(json.dumps(payload, indent=2, default=str))

    print("\n=== Credit Series Cross-Check ===\n")
    print(f"Window: {df.index.min().date()} to {df.index.max().date()}, n={len(df)} days\n")
    print("Direct correlation BAA10Y vs HY OAS:")
    print(f"  Level:         {corr_level:.3f}")
    print(f"  5D change:     {corr_chg_5d:.3f}")
    print(f"  20D change:    {corr_chg_20d:.3f}")
    print(f"\nComposite correlation (HY vs BAA version): {comp_corr:.3f}")
    print(f"\nTercile agreement:")
    print(f"  Overall:           {agreement:.1%} of days classify to same tercile")
    print(f"  Stress (top 5%):   {stress_agreement:.1%}" if len(stress_days) else "  Stress: n/a")
    print(f"  Easing (bot 5%):   {easing_agreement:.1%}" if len(easing_days) else "  Easing: n/a")
    print(f"\nConfusion matrix (rows=BAA tercile, cols=HY tercile):")
    print(confusion.to_string())

    if spot_checks:
        print("\nNotable dates:")
        for s in spot_checks:
            mark = "AGREE" if s["agree"] else "DISAGREE"
            print(f"  {s['date']} [{s['event']}]: HY_OAS={s['hy_oas']}, BAA10Y={s['baa10y']}, terciles: HY={s['tercile_hy']:+d} / BAA={s['tercile_baa']:+d}  {mark}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
