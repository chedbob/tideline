"""Phase 0 backtest — go/no-go gate for Tideline.

Tests a candidate rule against ~10 years of daily data using only Yahoo
(no FRED key needed). If this fails, we iterate the rule before building
the live site.

Rule v0 (pre-declared, no curve-fitting):
  Features (z-scored on EXPANDING window — no look-ahead):
    f1 = VIX term slope (VIX3M - VIX) / VIX            → + = contango = calm
    f2 = MOVE index (inverted)                         → + = bond calm
    f3 = HYG / LQD ratio                               → + = HY outperforming = risk-on
    f4 = USDJPY 20D momentum                           → + = JPY weakening = carry-on

  Composite = sum of 4 z-scores.

  Classification (expanding-window terciles of composite):
    Top tercile    → predict SPY UP    over next 5 trading days
    Bottom tercile → predict SPY DOWN
    Middle tercile → abstain (no call)

Resolution:
  SPY close[t+5] > SPY close[t]  → UP
  otherwise                      → DOWN
  (no "neutral band" — clean directional test)

Baseline = unconditional SPY 5D UP rate over the same period.

If (top tercile UP rate - baseline) > ~3pp with N > 300, we have a plausible edge.
Otherwise the rule needs iteration or we kill the project.

Usage:
  cd workers
  python -m backtest.phase0

Outputs JSON + markdown summary to workers/backtest/out/.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd
import numpy as np

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Tideline-Backtest/0.1)"}

TICKERS = {
    "spy": "SPY",
    "vix": "^VIX",
    "vix3m": "^VIX3M",
    "move": "^MOVE",
    "hyg": "HYG",
    "lqd": "LQD",
    "usdjpy": "USDJPY=X",
}

RANGE = "10y"


def fetch_series(client: httpx.Client, label: str, ticker: str) -> pd.Series:
    r = client.get(
        YAHOO.format(ticker=ticker),
        params={"range": RANGE, "interval": "1d", "includePrePost": "false"},
        headers=HEADERS,
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize()
    closes = data["indicators"]["quote"][0]["close"]
    s = pd.Series(closes, index=ts, name=label).dropna()
    # Dedup any duplicate dates (can happen at DST transitions)
    s = s[~s.index.duplicated(keep="last")]
    return s


def build_panel() -> pd.DataFrame:
    with httpx.Client(headers=HEADERS) as client:
        series = {label: fetch_series(client, label, tk) for label, tk in TICKERS.items()}
    df = pd.concat(series, axis=1)
    # Align to SPY trading days (drop days SPY didn't trade)
    df = df.reindex(series["spy"].index)
    # Forward-fill FX + vol up to 2 days for weekend/holiday gaps, then drop any remaining NaNs
    df = df.ffill(limit=2).dropna()
    return df


def expanding_zscore(s: pd.Series, min_periods: int = 252) -> pd.Series:
    """Z-score using only history through t-1 (no look-ahead)."""
    mean = s.shift(1).expanding(min_periods=min_periods).mean()
    std = s.shift(1).expanding(min_periods=min_periods).std()
    return (s - mean) / std


def expanding_tercile(composite: pd.Series, min_periods: int = 252) -> pd.Series:
    """Assign each date to {-1, 0, 1} based on terciles of PRIOR composite history only."""
    out = pd.Series(index=composite.index, dtype="float64")
    prior = composite.shift(1)
    for i, (date, val) in enumerate(composite.items()):
        hist = prior.iloc[:i+1].dropna()
        if len(hist) < min_periods or pd.isna(val):
            out.loc[date] = np.nan
            continue
        q33, q67 = hist.quantile([1/3, 2/3])
        if val >= q67:
            out.loc[date] = 1      # top tercile → Risk-On → predict UP
        elif val <= q33:
            out.loc[date] = -1     # bottom tercile → Risk-Off → predict DOWN
        else:
            out.loc[date] = 0      # middle → abstain
    return out


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2*n)) / denom
    half = z * math.sqrt(p * (1-p) / n + z**2 / (4 * n**2)) / denom
    return (center - half, center + half)


def run_backtest() -> dict:
    print("[backtest] fetching 10y daily data from Yahoo...")
    df = build_panel()
    print(f"[backtest]   panel shape: {df.shape}, range: {df.index.min().date()} -> {df.index.max().date()}")

    # Features
    df["vix_term_slope"] = (df["vix3m"] - df["vix"]) / df["vix"]
    df["move_inv"] = -df["move"]
    df["hyg_lqd"] = df["hyg"] / df["lqd"]
    df["usdjpy_mom20"] = df["usdjpy"].pct_change(20)

    # Expanding-window z-scores (no look-ahead)
    MIN_BURN = 252
    z1 = expanding_zscore(df["vix_term_slope"], MIN_BURN)
    z2 = expanding_zscore(df["move_inv"], MIN_BURN)
    z3 = expanding_zscore(df["hyg_lqd"], MIN_BURN)
    z4 = expanding_zscore(df["usdjpy_mom20"], MIN_BURN)

    df["composite"] = z1 + z2 + z3 + z4
    df["tercile"] = expanding_tercile(df["composite"], MIN_BURN)

    # Forward 5-day return on SPY
    HORIZON = 5
    df["spy_fwd"] = df["spy"].shift(-HORIZON) / df["spy"] - 1
    df["spy_up"] = (df["spy_fwd"] > 0).astype(int)

    # Drop rows where we can't evaluate (insufficient burn-in OR no forward return yet)
    eval_df = df.dropna(subset=["tercile", "spy_fwd"]).copy()

    # Baseline: unconditional SPY 5D up rate over the evaluable window
    baseline_up_rate = eval_df["spy_up"].mean()

    # Per-tercile accuracy
    results = {}
    for tercile_value, label, predicted_up in [(1, "top_risk_on", True), (-1, "bottom_risk_off", False), (0, "middle_abstain", None)]:
        sub = eval_df[eval_df["tercile"] == tercile_value]
        n = len(sub)
        if predicted_up is None:
            up_rate = sub["spy_up"].mean() if n > 0 else float("nan")
            results[label] = {
                "n": n,
                "spy_up_rate": up_rate,
                "avg_5d_return": sub["spy_fwd"].mean() if n > 0 else None,
            }
        else:
            hits = int((sub["spy_up"] == (1 if predicted_up else 0)).sum())
            acc = hits / n if n > 0 else float("nan")
            lo, hi = wilson_ci(hits, n)
            results[label] = {
                "n": n,
                "hits": hits,
                "accuracy": acc,
                "wilson_ci_95": [lo, hi],
                "avg_5d_return": sub["spy_fwd"].mean() if n > 0 else None,
            }

    # Directional-only summary (top+bottom)
    directional = eval_df[eval_df["tercile"] != 0].copy()
    directional["pred_up"] = (directional["tercile"] == 1).astype(int)
    directional["correct"] = (directional["pred_up"] == directional["spy_up"]).astype(int)
    n_dir = len(directional)
    hits_dir = int(directional["correct"].sum())
    acc_dir = hits_dir / n_dir if n_dir else float("nan")
    lo, hi = wilson_ci(hits_dir, n_dir)

    # Year-by-year directional accuracy
    directional["year"] = directional.index.year
    by_year = directional.groupby("year").agg(
        n=("correct", "size"),
        hits=("correct", "sum"),
    )
    by_year["accuracy"] = by_year["hits"] / by_year["n"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_range": {
            "start": str(eval_df.index.min().date()),
            "end": str(eval_df.index.max().date()),
            "total_trading_days": len(eval_df),
        },
        "rule_version": "v0_yahoo_only",
        "features": ["vix_term_slope", "move_inv", "hyg_lqd_ratio", "usdjpy_mom20"],
        "horizon_trading_days": HORIZON,
        "baseline_spy_up_rate": baseline_up_rate,
        "per_tercile": results,
        "directional_summary": {
            "n_calls": n_dir,
            "hits": hits_dir,
            "accuracy": acc_dir,
            "wilson_ci_95": [lo, hi],
            "vs_baseline_pp": (acc_dir - baseline_up_rate) * 100,
        },
        "by_year": by_year.reset_index().to_dict(orient="records"),
    }


def format_markdown(r: dict) -> str:
    top = r["per_tercile"]["top_risk_on"]
    bot = r["per_tercile"]["bottom_risk_off"]
    mid = r["per_tercile"]["middle_abstain"]
    d = r["directional_summary"]
    baseline = r["baseline_spy_up_rate"]
    lines = [
        "# Tideline Phase 0 Backtest — Rule v0 (Yahoo-only)",
        "",
        f"**Window:** {r['data_range']['start']} to {r['data_range']['end']} ({r['data_range']['total_trading_days']} trading days)",
        f"**Horizon:** {r['horizon_trading_days']} trading days",
        f"**Features:** {', '.join(r['features'])}",
        "",
        "## Baseline",
        f"- Unconditional SPY {r['horizon_trading_days']}-day UP rate: **{baseline:.1%}**",
        "",
        "## Directional accuracy (top + bottom terciles only, middle = abstain)",
        f"- **{d['accuracy']:.1%}** on {d['n_calls']:,} calls ({d['hits']:,} hits)",
        f"- 95% Wilson CI: [{d['wilson_ci_95'][0]:.1%}, {d['wilson_ci_95'][1]:.1%}]",
        f"- **vs baseline: {d['vs_baseline_pp']:+.1f} pp**",
        "",
        "## Per tercile",
        f"- **Top tercile (predict UP):** {top['accuracy']:.1%} accuracy (n={top['n']:,}), avg 5D return = {top['avg_5d_return']*100:+.2f}%",
        f"- **Bottom tercile (predict DOWN):** {bot['accuracy']:.1%} accuracy (n={bot['n']:,}), avg 5D return = {bot['avg_5d_return']*100:+.2f}%",
        f"- **Middle tercile (abstain):** SPY UP rate here = {mid['spy_up_rate']:.1%} (n={mid['n']:,}), avg 5D return = {mid['avg_5d_return']*100:+.2f}%",
        "",
        "## By year (directional calls only)",
        "| Year | N | Accuracy |",
        "|------|---|----------|",
    ]
    for row in r["by_year"]:
        lines.append(f"| {row['year']} | {row['n']} | {row['accuracy']:.1%} |")
    lines += [
        "",
        "## Go / no-go",
        f"- Rule edge over baseline: **{d['vs_baseline_pp']:+.1f} pp**",
        f"- Top tercile avg 5D return vs middle: {(top['avg_5d_return'] - mid['avg_5d_return'])*100:+.2f} pp",
        f"- Bottom tercile avg 5D return vs middle: {(bot['avg_5d_return'] - mid['avg_5d_return'])*100:+.2f} pp",
        "",
        "> Target: directional edge > +3 pp over baseline with n > 300 for plausible signal.",
    ]
    return "\n".join(lines)


def main() -> int:
    try:
        result = run_backtest()
    except Exception as exc:
        print(f"[backtest] FAILED: {exc}", file=sys.stderr)
        raise

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase0_result.json").write_text(json.dumps(result, indent=2, default=str))
    md = format_markdown(result)
    (out_dir / "phase0_result.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
