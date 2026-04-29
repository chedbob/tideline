"""Phase 1 tournament — features frozen, test 5 targets.

Pre-declared (before any run) direction mapping:
  Target        | Top tercile -> | Bottom tercile -> | Horizon
  -----------------------------------------------------------
  SPY           | UP             | DOWN              | 5D
  SPY/TLT ratio | UP             | DOWN              | 5D
  HYG           | UP             | DOWN              | 5D
  EEM           | UP             | DOWN              | 5D
  TLT           | DOWN           | UP                | 5D     (safe haven — flipped)
  SPY @ H=20    | UP             | DOWN              | 20D    (horizon extension)

Go rule: directional edge > +3 pp over target's unconditional up-rate, n>300,
Wilson CI does not cross baseline.

Decision policy if multiple pass: pick the most mechanistically-aligned
(HYG or SPY/TLT), NOT the highest-accuracy one (avoid p-hacking by picking).

Usage:  python -m backtest.phase1_tournament
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Tideline-Backtest/0.1)"}
RANGE = "10y"

INPUT_TICKERS = {
    "vix": "^VIX",
    "vix3m": "^VIX3M",
    "move": "^MOVE",
    "hyg": "HYG",
    "lqd": "LQD",
    "usdjpy": "USDJPY=X",
    # targets:
    "spy": "SPY",
    "tlt": "TLT",
    "eem": "EEM",
}


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
    s = s[~s.index.duplicated(keep="last")]
    return s


def build_panel() -> pd.DataFrame:
    with httpx.Client(headers=HEADERS) as client:
        series = {label: fetch_series(client, label, tk) for label, tk in INPUT_TICKERS.items()}
    df = pd.concat(series, axis=1)
    df = df.reindex(series["spy"].index)
    df = df.ffill(limit=2).dropna()
    return df


def expanding_zscore(s: pd.Series, min_periods: int = 252) -> pd.Series:
    mean = s.shift(1).expanding(min_periods=min_periods).mean()
    std = s.shift(1).expanding(min_periods=min_periods).std()
    return (s - mean) / std


def expanding_tercile(composite: pd.Series, min_periods: int = 252) -> pd.Series:
    out = pd.Series(index=composite.index, dtype="float64")
    prior = composite.shift(1)
    for i, (date, val) in enumerate(composite.items()):
        hist = prior.iloc[:i+1].dropna()
        if len(hist) < min_periods or pd.isna(val):
            out.loc[date] = np.nan
            continue
        q33, q67 = hist.quantile([1/3, 2/3])
        if val >= q67:
            out.loc[date] = 1
        elif val <= q33:
            out.loc[date] = -1
        else:
            out.loc[date] = 0
    return out


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2*n)) / denom
    half = z * math.sqrt(p * (1-p) / n + z**2 / (4 * n**2)) / denom
    return (center - half, center + half)


def build_composite(df: pd.DataFrame) -> pd.Series:
    vix_slope = (df["vix3m"] - df["vix"]) / df["vix"]
    move_inv = -df["move"]
    hyg_lqd = df["hyg"] / df["lqd"]
    usdjpy_mom = df["usdjpy"].pct_change(20)
    composite = (
        expanding_zscore(vix_slope)
        + expanding_zscore(move_inv)
        + expanding_zscore(hyg_lqd)
        + expanding_zscore(usdjpy_mom)
    )
    return composite


def evaluate_target(
    tercile: pd.Series,
    target_series: pd.Series,
    horizon: int,
    top_predicts_up: bool,
) -> dict:
    """Compute tercile-vs-baseline edge for a given target + direction mapping."""
    fwd_return = target_series.shift(-horizon) / target_series - 1
    up = (fwd_return > 0).astype(int)

    combined = pd.concat({
        "tercile": tercile,
        "fwd_return": fwd_return,
        "up": up,
    }, axis=1).dropna(subset=["tercile", "fwd_return"])

    baseline_up = combined["up"].mean()

    top = combined[combined["tercile"] == 1]
    bot = combined[combined["tercile"] == -1]
    mid = combined[combined["tercile"] == 0]

    def tercile_stats(sub, predicts_up):
        n = len(sub)
        if n == 0:
            return {"n": 0}
        up_rate = sub["up"].mean()
        hits = int((sub["up"] == (1 if predicts_up else 0)).sum())
        acc = hits / n
        lo, hi = wilson_ci(hits, n)
        return {
            "n": n,
            "up_rate": up_rate,
            "hits_predicting_" + ("up" if predicts_up else "down"): hits,
            "accuracy": acc,
            "wilson_ci_95": [lo, hi],
            "avg_fwd_return_pct": sub["fwd_return"].mean() * 100,
        }

    top_stats = tercile_stats(top, top_predicts_up)
    bot_stats = tercile_stats(bot, not top_predicts_up)
    mid_stats = {
        "n": len(mid),
        "up_rate": mid["up"].mean() if len(mid) else None,
        "avg_fwd_return_pct": (mid["fwd_return"].mean() * 100) if len(mid) else None,
    }

    # Directional combined: top + bottom terciles
    directional = pd.concat([top, bot])
    n_dir = len(directional)
    if n_dir:
        pred_up = directional["tercile"].map({1: int(top_predicts_up), -1: int(not top_predicts_up)})
        hits_dir = int((pred_up == directional["up"]).sum())
        acc_dir = hits_dir / n_dir
        lo_d, hi_d = wilson_ci(hits_dir, n_dir)
    else:
        acc_dir = float("nan")
        hits_dir = 0
        lo_d = hi_d = 0

    edge_pp = (acc_dir - baseline_up) * 100

    # Go test
    passes = (
        n_dir > 300
        and edge_pp > 3.0
        and lo_d > baseline_up
    )

    return {
        "baseline_up_rate": baseline_up,
        "top_tercile": top_stats,
        "bottom_tercile": bot_stats,
        "middle_tercile": mid_stats,
        "directional": {
            "n": n_dir,
            "hits": hits_dir,
            "accuracy": acc_dir,
            "wilson_ci_95": [lo_d, hi_d],
            "edge_pp_vs_baseline": edge_pp,
            "passes_go_rule": bool(passes),
        },
    }


TOURNAMENT = [
    # (label, target_col_or_callable, horizon, top_predicts_up, mechanism_note)
    ("SPY_5D", lambda df: df["spy"], 5, True, "risk-on asset"),
    ("SPY_TLT_ratio_5D", lambda df: df["spy"] / df["tlt"], 5, True, "classic risk-on/off rotation"),
    ("HYG_5D", lambda df: df["hyg"], 5, True, "credit stress asset (Galvão-Owyang primary channel)"),
    ("EEM_5D", lambda df: df["eem"], 5, True, "risk-on EM equity, independent of features"),
    ("TLT_5D", lambda df: df["tlt"], 5, False, "safe haven — direction flipped"),
    ("SPY_20D", lambda df: df["spy"], 20, True, "horizon extension — macro features on monthly window"),
]


def run_tournament() -> dict:
    print("[tournament] fetching panel...")
    df = build_panel()
    print(f"[tournament]   shape: {df.shape}, range {df.index.min().date()} to {df.index.max().date()}")

    composite = build_composite(df)
    tercile = expanding_tercile(composite)

    results = {}
    for label, target_fn, horizon, top_up, mech in TOURNAMENT:
        target_series = target_fn(df)
        print(f"[tournament] evaluating {label} (horizon={horizon}, top_predicts_up={top_up})")
        results[label] = {
            "horizon_days": horizon,
            "top_predicts_up": top_up,
            "mechanism_note": mech,
            **evaluate_target(tercile, target_series, horizon, top_up),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule_version": "v1_tournament",
        "features_frozen": ["vix_term_slope", "move_inv", "hyg_lqd", "usdjpy_mom20"],
        "direction_mapping_predeclared": True,
        "panel_range": {
            "start": str(df.index.min().date()),
            "end": str(df.index.max().date()),
            "trading_days": len(df),
        },
        "go_rule": "edge > +3pp AND n > 300 AND Wilson CI lower bound > baseline",
        "results": results,
    }


def format_markdown(r: dict) -> str:
    lines = [
        "# Tideline Phase 1 Tournament — Rule v1",
        "",
        f"**Features (frozen):** {', '.join(r['features_frozen'])}",
        f"**Panel:** {r['panel_range']['start']} to {r['panel_range']['end']} ({r['panel_range']['trading_days']} days)",
        f"**Go rule:** {r['go_rule']}",
        "",
        "## Results",
        "",
        "| Target | Horizon | Baseline UP | Directional Acc | n | Edge vs Baseline | Wilson CI | Passes? |",
        "|--------|---------|-------------|-----------------|---|------------------|-----------|---------|",
    ]
    for label, res in r["results"].items():
        d = res["directional"]
        lines.append(
            f"| {label} | {res['horizon_days']}D | {res['baseline_up_rate']:.1%} "
            f"| {d['accuracy']:.1%} | {d['n']:,} "
            f"| **{d['edge_pp_vs_baseline']:+.1f} pp** "
            f"| [{d['wilson_ci_95'][0]:.1%}, {d['wilson_ci_95'][1]:.1%}] "
            f"| {'YES' if d['passes_go_rule'] else 'no'} |"
        )
    lines.append("")
    lines.append("## Per-target detail")
    for label, res in r["results"].items():
        lines += [
            "",
            f"### {label}  ({res['mechanism_note']})",
            f"- Direction mapping: top tercile -> {'UP' if res['top_predicts_up'] else 'DOWN'}, bottom -> {'DOWN' if res['top_predicts_up'] else 'UP'}",
            f"- Top tercile: n={res['top_tercile']['n']:,}, accuracy={res['top_tercile'].get('accuracy', 0):.1%}, avg fwd ret={res['top_tercile'].get('avg_fwd_return_pct', 0):+.2f}%",
            f"- Bottom tercile: n={res['bottom_tercile']['n']:,}, accuracy={res['bottom_tercile'].get('accuracy', 0):.1%}, avg fwd ret={res['bottom_tercile'].get('avg_fwd_return_pct', 0):+.2f}%",
            f"- Middle tercile: n={res['middle_tercile']['n']:,}, up rate={res['middle_tercile']['up_rate']:.1%}, avg fwd ret={res['middle_tercile']['avg_fwd_return_pct']:+.2f}%",
        ]
    passed = [k for k, v in r["results"].items() if v["directional"]["passes_go_rule"]]
    lines += ["", "## Verdict"]
    if passed:
        lines.append(f"- **PASSES:** {', '.join(passed)}")
        lines.append("- Decision policy: pick the most mechanistically-aligned (HYG or SPY/TLT), not highest-accuracy.")
    else:
        lines.append("- **No target passes the go-rule.** Honest conclusion: these 4 features do not have tracker-grade directional edge on any tested target at these horizons.")
        lines.append("- Options: (i) get FRED key, test HY OAS change directly; (ii) kill project; (iii) rethink features entirely.")
    return "\n".join(lines)


def main() -> int:
    result = run_tournament()
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase1_tournament.json").write_text(json.dumps(result, indent=2, default=str))
    md = format_markdown(result)
    (out_dir / "phase1_tournament.md").write_text(md, encoding="utf-8")
    print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
