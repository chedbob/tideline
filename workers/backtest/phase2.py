"""Phase 2 — Long-history FRED composite + incremental information null test.

Per external review (GPT-5 Thinking + Gemini 2.5 Pro convergent recommendation):
  - Extend panel to 1997-2026 using FRED (captures dot-com, GFC, Euro crisis,
    COVID, 2022 inflation shock — not just QE-era bull market).
  - Swap to 100% FRED features (authoritative, consistent, long history).
  - Null test: after controlling for lagged return, realized vol, drawdown,
    HY OAS level, HY OAS change, and trend, does the composite add incremental
    information? If not, it's a dressed-up bounce flag.

Features v2 (all FRED, all expanding-window z-scored, no look-ahead):
  f1 = z(-BAA10Y credit spread)   — inverted: high spread = stress
  f2 = z(-VIX)                    — inverted: high VIX = stress
  f3 = z(3m10y curve)             — positive curve = healthy
  f4 = z(-NFCI)                   — inverted: high NFCI = tighter conditions

Composite = sum. Higher = calmer / risk-on conditions.

Targets:
  - SPY direction at 5D, 20D, 60D (long-history test of inversion pattern)
  - State persistence: does current tercile predict next-20D tercile?
    (ChatGPT's state-persistence metric, the actual regime-tracker claim)

Incremental information OLS (with Newey-West HAC SEs, lag=20):
  SPY_fwd_ret = a + b1*composite + b2*lag_ret + b3*realized_vol
              + b4*drawdown + b5*hy_oas + b6*hy_oas_chg20 + b7*trend_50d + err

Decision rule:
  - Inversion pattern must hold in 2000-2008 (non-QE regime) OR we reject
    the "stress → bounce" hypothesis as QE-era artifact.
  - Composite must have |t-stat| > 2 on at least one horizon AFTER controls,
    otherwise project is killed.

Usage:
  cd workers
  set -a; source .env; set +a    # loads FRED_API_KEY
  python -m backtest.phase2
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

START_DATE = "1997-01-01"

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
FRED_SERIES = {
    # NOTE: BAMLH0A0HYM2 (ICE BofA HY OAS) was restricted to 3yr history on
    # FRED in April 2026 by ICE's licensing change. Using BAA10Y instead —
    # Moody's Baa corporate bond yield minus 10Y Treasury, daily from 1986,
    # the academic-standard credit spread series (Gilchrist-Zakrajsek lineage).
    "credit_spread": "BAA10Y",  # daily from 1986
    "curve_3m10y":  "T10Y3M",   # daily from 1982
    "nfci":         "NFCI",     # weekly from 1971 (Friday release)
    "vix":          "VIXCLS",   # daily from 1990
}

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Tideline-Backtest/0.2)"}


# ----------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------

def fetch_fred(client: httpx.Client, series_id: str, api_key: str) -> pd.Series:
    r = client.get(
        FRED_BASE,
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": START_DATE,
            "sort_order": "asc",
        },
        timeout=30.0,
    )
    r.raise_for_status()
    obs = r.json()["observations"]
    records = [(o["date"], float(o["value"])) for o in obs if o["value"] not in (".", "")]
    if not records:
        raise ValueError(f"FRED returned no data for {series_id}")
    dates, values = zip(*records)
    idx = pd.to_datetime(dates)
    return pd.Series(values, index=idx, name=series_id)


def fetch_yahoo_daily(client: httpx.Client, ticker: str, start: str = START_DATE) -> pd.Series:
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = client.get(
        YAHOO.format(ticker=ticker),
        params={"period1": p1, "period2": p2, "interval": "1d", "includePrePost": "false"},
        headers=HEADERS,
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    closes = data["indicators"]["quote"][0]["close"]
    s = pd.Series(closes, index=ts, name=ticker).dropna()
    s = s[~s.index.duplicated(keep="last")]
    return s


def build_panel(api_key: str) -> pd.DataFrame:
    print(f"[phase2] fetching data from {START_DATE}...")
    with httpx.Client() as fc, httpx.Client(headers=HEADERS) as yc:
        fred = {k: fetch_fred(fc, sid, api_key) for k, sid in FRED_SERIES.items()}
        spy = fetch_yahoo_daily(yc, "SPY")

    # Build daily panel aligned to SPY trading days
    df = pd.DataFrame({"spy": spy})
    for k, s in fred.items():
        s.index = s.index.tz_localize(None) if s.index.tz is not None else s.index
        df[k] = s.reindex(df.index, method="ffill")  # FRED series ffill into SPY calendar (NFCI is weekly)
    # Drop initial rows where FRED series hadn't started yet
    df = df.dropna()
    print(f"[phase2]   panel: {df.shape}, range {df.index.min().date()} to {df.index.max().date()}")
    return df


# ----------------------------------------------------------------------
# Features
# ----------------------------------------------------------------------

def expanding_zscore(s: pd.Series, min_periods: int = 252) -> pd.Series:
    mean = s.shift(1).expanding(min_periods=min_periods).mean()
    std = s.shift(1).expanding(min_periods=min_periods).std()
    return (s - mean) / std


def expanding_tercile(composite: pd.Series, min_periods: int = 252) -> pd.Series:
    out = pd.Series(index=composite.index, dtype="float64")
    prior = composite.shift(1)
    # Faster than row-by-row via expanding quantiles
    q33 = prior.expanding(min_periods=min_periods).quantile(1/3)
    q67 = prior.expanding(min_periods=min_periods).quantile(2/3)
    mask = composite.notna() & q33.notna() & q67.notna()
    out[mask & (composite >= q67)] = 1.0
    out[mask & (composite <= q33)] = -1.0
    out[mask & (composite > q33) & (composite < q67)] = 0.0
    return out


def build_composite(df: pd.DataFrame) -> pd.Series:
    z_credit_inv = expanding_zscore(-df["credit_spread"])
    z_vix_inv = expanding_zscore(-df["vix"])
    z_curve = expanding_zscore(df["curve_3m10y"])
    z_nfci_inv = expanding_zscore(-df["nfci"])
    return z_credit_inv + z_vix_inv + z_curve + z_nfci_inv


def build_controls(df: pd.DataFrame) -> pd.DataFrame:
    r_1d = df["spy"].pct_change()
    out = pd.DataFrame({
        "lag_ret_5d": df["spy"].pct_change(5).shift(1),
        "realized_vol_20d": r_1d.rolling(20).std().shift(1),
        "drawdown_60d": (df["spy"] / df["spy"].rolling(60, min_periods=20).max() - 1).shift(1),
        "credit_spread_level": df["credit_spread"].shift(1),
        "credit_spread_chg_20d": df["credit_spread"].diff(20).shift(1),
        "trend_50d": (df["spy"] / df["spy"].shift(50) - 1).shift(1),
    })
    return out


# ----------------------------------------------------------------------
# Evaluation
# ----------------------------------------------------------------------

def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2*n)) / denom
    half = z * math.sqrt(p * (1-p) / n + z**2 / (4 * n**2)) / denom
    return (center - half, center + half)


def tournament(df: pd.DataFrame, tercile: pd.Series) -> dict:
    """Run SPY directional tests at 5D / 20D / 60D, full panel + crash subsets."""
    results = {}
    HORIZONS = [5, 20, 60]
    SUBSETS = {
        "full_1998_2026": (None, None),
        "dotcom_2000_2002": ("2000-01-01", "2002-12-31"),
        "gfc_2008_2009": ("2008-01-01", "2009-12-31"),
        "post_gfc_2010_2019": ("2010-01-01", "2019-12-31"),
        "covid_2020": ("2020-01-01", "2020-12-31"),
        "2022_inflation": ("2022-01-01", "2022-12-31"),
        "post_2023": ("2023-01-01", None),
    }
    for H in HORIZONS:
        fwd = df["spy"].shift(-H) / df["spy"] - 1
        for subset_name, (start, end) in SUBSETS.items():
            sub_tercile = tercile.copy()
            sub_fwd = fwd.copy()
            if start:
                sub_tercile = sub_tercile[sub_tercile.index >= start]
                sub_fwd = sub_fwd[sub_fwd.index >= start]
            if end:
                sub_tercile = sub_tercile[sub_tercile.index <= end]
                sub_fwd = sub_fwd[sub_fwd.index <= end]

            combined = pd.concat({"tercile": sub_tercile, "fwd": sub_fwd}, axis=1).dropna()
            if len(combined) < 30:
                continue
            combined["up"] = (combined["fwd"] > 0).astype(int)
            baseline = combined["up"].mean()

            # Directional: top predicts UP, bottom predicts DOWN
            dirs = combined[combined["tercile"] != 0].copy()
            dirs["pred_up"] = (dirs["tercile"] == 1).astype(int)
            dirs["hit"] = (dirs["pred_up"] == dirs["up"]).astype(int)
            n_dir = len(dirs)
            hits_dir = int(dirs["hit"].sum())
            acc = hits_dir / n_dir if n_dir else float("nan")
            lo, hi = wilson_ci(hits_dir, n_dir)

            top = combined[combined["tercile"] == 1]
            bot = combined[combined["tercile"] == -1]
            mid = combined[combined["tercile"] == 0]

            results[f"H={H}D__{subset_name}"] = {
                "horizon": H,
                "subset": subset_name,
                "n": len(combined),
                "baseline_up_rate": baseline,
                "directional": {
                    "n": n_dir,
                    "accuracy": acc,
                    "edge_pp": (acc - baseline) * 100,
                    "wilson_ci_95": [lo, hi],
                },
                "top_tercile": {"n": len(top), "up_rate": top["up"].mean() if len(top) else None, "avg_fwd": top["fwd"].mean() if len(top) else None},
                "bot_tercile": {"n": len(bot), "up_rate": bot["up"].mean() if len(bot) else None, "avg_fwd": bot["fwd"].mean() if len(bot) else None},
                "mid_tercile": {"n": len(mid), "up_rate": mid["up"].mean() if len(mid) else None, "avg_fwd": mid["fwd"].mean() if len(mid) else None},
            }
    return results


def incremental_info_test(df: pd.DataFrame, composite: pd.Series, controls: pd.DataFrame) -> dict:
    """OLS with Newey-West HAC SEs (lag = horizon) for each forward horizon.
    Tests whether composite coefficient has |t-stat| > 2 AFTER controls."""
    import statsmodels.api as sm

    results = {}
    for H in [5, 20, 60]:
        fwd = (df["spy"].shift(-H) / df["spy"] - 1) * 100  # % return for readable coefs
        panel = pd.concat({"fwd": fwd, "composite": composite}, axis=1).join(controls).dropna()

        # Model 1: composite alone (baseline)
        X1 = sm.add_constant(panel[["composite"]])
        m1 = sm.OLS(panel["fwd"], X1).fit(cov_type="HAC", cov_kwds={"maxlags": H})

        # Model 2: composite + controls (null test)
        X2 = sm.add_constant(panel[["composite"] + list(controls.columns)])
        m2 = sm.OLS(panel["fwd"], X2).fit(cov_type="HAC", cov_kwds={"maxlags": H})

        results[f"H={H}D"] = {
            "horizon": H,
            "n": len(panel),
            "model_composite_only": {
                "composite_coef": float(m1.params["composite"]),
                "composite_tstat": float(m1.tvalues["composite"]),
                "composite_pvalue": float(m1.pvalues["composite"]),
                "r_squared": float(m1.rsquared),
            },
            "model_with_controls": {
                "composite_coef": float(m2.params["composite"]),
                "composite_tstat": float(m2.tvalues["composite"]),
                "composite_pvalue": float(m2.pvalues["composite"]),
                "r_squared": float(m2.rsquared),
                "all_coefs": {k: float(v) for k, v in m2.params.items()},
                "all_tstats": {k: float(v) for k, v in m2.tvalues.items()},
            },
            "verdict_incremental_info": (abs(float(m2.tvalues["composite"])) > 2.0),
        }
    return results


def state_persistence(tercile: pd.Series, lookforward: int = 20) -> dict:
    """Does today's tercile predict the tercile 20 trading days from now?
    This is the actual regime-tracker claim (ChatGPT's recommended metric)."""
    fwd = tercile.shift(-lookforward)
    paired = pd.concat({"today": tercile, "future": fwd}, axis=1).dropna()
    paired = paired[paired["today"].isin([-1, 0, 1])]
    same = (paired["today"] == paired["future"]).mean()

    # Transition matrix
    trans = pd.crosstab(paired["today"], paired["future"], normalize="index")

    # Base rate = max marginal (if you always guess most-common state)
    marginals = paired["future"].value_counts(normalize=True)

    return {
        "horizon": lookforward,
        "n": len(paired),
        "same_state_rate": float(same),
        "transition_matrix": trans.to_dict(),
        "base_rate_most_common": float(marginals.max()),
        "edge_vs_base_rate_pp": (float(same) - float(marginals.max())) * 100,
    }


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> int:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print("ERROR: FRED_API_KEY not set. Run: set -a; source .env; set +a", file=sys.stderr)
        return 1

    df = build_panel(api_key)
    composite = build_composite(df)
    tercile = expanding_tercile(composite)
    controls = build_controls(df)

    print("[phase2] running tournament across horizons and subsets...")
    tour = tournament(df, tercile)

    print("[phase2] running state persistence test (20D ahead tercile)...")
    persist = state_persistence(tercile, lookforward=20)

    print("[phase2] running incremental information null test (HAC OLS)...")
    incr = incremental_info_test(df, composite, controls)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule_version": "v2_fred_composite",
        "features_frozen": ["z(-hy_oas)", "z(-vix)", "z(curve_3m10y)", "z(-nfci)"],
        "panel_range": {"start": str(df.index.min().date()), "end": str(df.index.max().date()), "days": len(df)},
        "tournament": tour,
        "state_persistence_20d": persist,
        "incremental_information_null_test": incr,
    }

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase2_result.json").write_text(json.dumps(payload, indent=2, default=str))

    # Print summary
    print("\n=== Phase 2 Summary ===\n")
    print(f"Panel: {df.index.min().date()} to {df.index.max().date()} ({len(df)} days)")
    print(f"\nFeatures: {payload['features_frozen']}")

    print("\n--- Tournament (directional SPY, tercile-conditional) ---")
    for key, r in tour.items():
        d = r["directional"]
        print(f"  {key:<40} n={r['n']:>4}  baseline={r['baseline_up_rate']:.1%}  dir_acc={d['accuracy']:.1%}  edge={d['edge_pp']:+.1f}pp  CI=[{d['wilson_ci_95'][0]:.1%},{d['wilson_ci_95'][1]:.1%}]")

    print("\n--- State persistence (today's tercile == tercile in 20D?) ---")
    print(f"  same-state rate: {persist['same_state_rate']:.1%}  (base rate: {persist['base_rate_most_common']:.1%}, edge: {persist['edge_vs_base_rate_pp']:+.1f}pp)")
    print(f"  transitions:")
    for today_state, row in persist["transition_matrix"].items():
        print(f"    from {today_state:+.0f}: {row}")

    print("\n--- Incremental information null test (HAC OLS) ---")
    for key, r in incr.items():
        m1 = r["model_composite_only"]
        m2 = r["model_with_controls"]
        verdict = "PASS (adds info)" if r["verdict_incremental_info"] else "FAIL (subsumed by controls)"
        print(f"  {key}: n={r['n']}")
        print(f"    composite-only:   coef={m1['composite_coef']:+.3f}  t={m1['composite_tstat']:+.2f}  R2={m1['r_squared']:.3f}")
        print(f"    with controls:    coef={m2['composite_coef']:+.3f}  t={m2['composite_tstat']:+.2f}  R2={m2['r_squared']:.3f}   -> {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
