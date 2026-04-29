"""Phase 2 validation — same null test as phase2.py but using the REAL
HY OAS (spliced archive + live) instead of BAA10Y.

Purpose: prove the null-test verdict (composite adds zero incremental info
for SPY direction after controls) holds with the authentic credit series,
not just the BAA10Y substitute. This closes the loop on whether our public
product's BAA10Y swap is masking real signal.

SCOPE: PRIVATE RESEARCH ONLY. Do not re-publish the HY OAS history in
public Tideline displays — ICE licensing was restricted in April 2026.

Usage:  set -a; source .env; set +a; python -m backtest.phase2_hy_validation
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

START_DATE = "1997-01-01"
SPLICED_HY_CSV = Path(__file__).parent.parent / "data_archive" / "hy_oas_spliced.csv"

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES = {
    "curve_3m10y":  "T10Y3M",
    "nfci":         "NFCI",
    "vix":          "VIXCLS",
}

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Tideline-Backtest/0.2-hy)"}


def fetch_fred(client, sid, api_key):
    r = client.get(FRED_BASE, params={
        "series_id": sid, "api_key": api_key, "file_type": "json",
        "observation_start": START_DATE, "sort_order": "asc"
    }, timeout=30.0)
    r.raise_for_status()
    recs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
    dates, vals = zip(*recs)
    return pd.Series(vals, index=pd.to_datetime(dates), name=sid)


def fetch_yahoo_daily(client, ticker, start=START_DATE):
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
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


def load_spliced_hy_oas():
    df = pd.read_csv(SPLICED_HY_CSV, index_col=0, parse_dates=True)
    return df["hy_oas"]


def expanding_zscore(s, min_periods=252):
    mean = s.shift(1).expanding(min_periods=min_periods).mean()
    std = s.shift(1).expanding(min_periods=min_periods).std()
    return (s - mean) / std


def expanding_tercile(composite, min_periods=252):
    out = pd.Series(index=composite.index, dtype="float64")
    prior = composite.shift(1)
    q33 = prior.expanding(min_periods=min_periods).quantile(1/3)
    q67 = prior.expanding(min_periods=min_periods).quantile(2/3)
    mask = composite.notna() & q33.notna() & q67.notna()
    out[mask & (composite >= q67)] = 1.0
    out[mask & (composite <= q33)] = -1.0
    out[mask & (composite > q33) & (composite < q67)] = 0.0
    return out


def main():
    api_key = os.environ["FRED_API_KEY"]

    print(f"[phase2-hy] loading spliced HY OAS (archive+live)...")
    hy_oas = load_spliced_hy_oas()
    print(f"[phase2-hy]   hy_oas range: {hy_oas.index.min().date()} to {hy_oas.index.max().date()} (n={len(hy_oas)})")

    print(f"[phase2-hy] fetching other FRED + Yahoo data...")
    with httpx.Client() as fc, httpx.Client(headers=HEADERS) as yc:
        others = {k: fetch_fred(fc, sid, api_key) for k, sid in FRED_SERIES.items()}
        spy = fetch_yahoo_daily(yc, "SPY")

    df = pd.DataFrame({"spy": spy})
    df["hy_oas"] = hy_oas.reindex(df.index, method="ffill")
    for k, s in others.items():
        df[k] = s.reindex(df.index, method="ffill")
    df = df.dropna()
    print(f"[phase2-hy]   panel: {df.shape}, range {df.index.min().date()} to {df.index.max().date()}")

    # Composite using REAL HY OAS
    composite = (
        expanding_zscore(-df["hy_oas"])
        + expanding_zscore(-df["vix"])
        + expanding_zscore(df["curve_3m10y"])
        + expanding_zscore(-df["nfci"])
    )
    tercile = expanding_tercile(composite)

    # Controls (ChatGPT's incremental info null test)
    r_1d = df["spy"].pct_change()
    controls = pd.DataFrame({
        "lag_ret_5d": df["spy"].pct_change(5).shift(1),
        "realized_vol_20d": r_1d.rolling(20).std().shift(1),
        "drawdown_60d": (df["spy"] / df["spy"].rolling(60, min_periods=20).max() - 1).shift(1),
        "hy_oas_level": df["hy_oas"].shift(1),
        "hy_oas_chg_20d": df["hy_oas"].diff(20).shift(1),
        "trend_50d": (df["spy"] / df["spy"].shift(50) - 1).shift(1),
    })

    import statsmodels.api as sm
    out = {
        "data_source": "spliced HY OAS (archive + live)",
        "panel": {"start": str(df.index.min().date()), "end": str(df.index.max().date()), "n": len(df)},
        "incremental_info": {},
        "state_persistence_20d": {},
        "crisis_subset_edge": {},
    }

    print("\n=== Incremental information null test (HAC OLS) ===")
    for H in [5, 20, 60]:
        fwd = (df["spy"].shift(-H) / df["spy"] - 1) * 100
        panel = pd.concat({"fwd": fwd, "composite": composite}, axis=1).join(controls).dropna()
        X1 = sm.add_constant(panel[["composite"]])
        m1 = sm.OLS(panel["fwd"], X1).fit(cov_type="HAC", cov_kwds={"maxlags": H})
        X2 = sm.add_constant(panel[["composite"] + list(controls.columns)])
        m2 = sm.OLS(panel["fwd"], X2).fit(cov_type="HAC", cov_kwds={"maxlags": H})
        verdict = "PASS" if abs(float(m2.tvalues["composite"])) > 2 else "FAIL"
        out["incremental_info"][f"H={H}D"] = {
            "n": len(panel),
            "composite_only_tstat": float(m1.tvalues["composite"]),
            "composite_only_r2": float(m1.rsquared),
            "with_controls_tstat": float(m2.tvalues["composite"]),
            "with_controls_r2": float(m2.rsquared),
            "verdict": verdict,
        }
        print(f"  H={H}D  n={len(panel):,}  composite-only t={m1.tvalues['composite']:+.2f} R2={m1.rsquared:.3f}  |  with controls t={m2.tvalues['composite']:+.2f} R2={m2.rsquared:.3f}  -> {verdict}")

    # State persistence 20D
    fwd_terc = tercile.shift(-20)
    paired = pd.concat({"today": tercile, "future": fwd_terc}, axis=1).dropna()
    paired = paired[paired["today"].isin([-1, 0, 1])]
    same = (paired["today"] == paired["future"]).mean()
    base = paired["future"].value_counts(normalize=True).max()
    out["state_persistence_20d"] = {
        "n": len(paired),
        "same_state_rate": float(same),
        "base_rate_most_common": float(base),
        "edge_pp": (float(same) - float(base)) * 100,
    }
    print(f"\n=== State persistence 20D ===")
    print(f"  same-state rate: {same:.1%}  base rate: {base:.1%}  edge: {(same-base)*100:+.1f}pp  n={len(paired):,}")

    # Crisis subsets
    print(f"\n=== Crisis subset directional edge ===")
    subsets = {
        "dotcom_2000_2002": ("2000-01-01", "2002-12-31"),
        "gfc_2008_2009":    ("2008-01-01", "2009-12-31"),
        "euro_2011":        ("2011-07-01", "2011-12-31"),
        "covid_2020":       ("2020-01-01", "2020-12-31"),
        "rate_shock_2022":  ("2022-01-01", "2022-12-31"),
    }
    for H in [5, 20, 60]:
        fwd = df["spy"].shift(-H) / df["spy"] - 1
        for name, (s, e) in subsets.items():
            sub = pd.concat({"t": tercile, "f": fwd}, axis=1).loc[s:e].dropna()
            if len(sub) < 30:
                continue
            baseline = (sub["f"] > 0).mean()
            dirs = sub[sub["t"] != 0].copy()
            dirs["pred_up"] = (dirs["t"] == 1).astype(int)
            dirs["actual_up"] = (dirs["f"] > 0).astype(int)
            acc = (dirs["pred_up"] == dirs["actual_up"]).mean() if len(dirs) else float("nan")
            edge = (acc - baseline) * 100
            out["crisis_subset_edge"][f"H={H}D__{name}"] = {"baseline": float(baseline), "acc": float(acc), "edge_pp": float(edge), "n": len(dirs)}
            print(f"  H={H}D  {name:20} n={len(dirs):4}  baseline={baseline:.1%}  acc={acc:.1%}  edge={edge:+5.1f}pp")

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase2_hy_validation.json").write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
