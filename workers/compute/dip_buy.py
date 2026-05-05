"""Simple dip-buy detector — fires only in clean uptrends.

Two outcomes: DIP_BUY (textbook setup) or QUIET (anything else, with a
one-line reason). The macro filter is the whole point — when the regime
is showing stress, this signal stays silent on principle, even if there's
a tempting pullback. We're explicitly NOT trying to catch falling knives.

Pre-conditions (all must hold):
  - Faber GREEN (SPY > 200DMA AND 50DMA > 200DMA) — broader trend bullish
  - Regime in {NORMAL, EASY}                       — macro not warning

Trigger:
  - SPY between PULLBACK_MIN% and PULLBACK_MAX% below its 20-day high

If the pullback is deeper than PULLBACK_MAX, we go quiet on purpose —
that's the "could be turning macro" zone where you don't want a naive
dip-buy call. The proper bottom signal would be a different rule entirely.

Backtest compares DIP_BUY days to a natural conditional baseline:
"calm uptrend, no dip" (Faber GREEN + NORMAL/EASY, pullback < threshold).
That's the right A/B — it asks "given we're already in a calm uptrend,
does adding the dip filter improve forward returns?" not the easier
"vs the all-time SPY-up bias."
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from rule.v1 import faber_signal


PULLBACK_MIN = 0.02   # at least 2% off 20d high to count as a dip
PULLBACK_MAX = 0.08   # more than 8% = could be macro turning, stay quiet
HIGH_LOOKBACK = 20    # 20 trading days


def _classify_row(row) -> tuple[str, str]:
    """Return (state, reason) for one day. row needs faber, state, pullback_20d_pct."""
    pb = row["pullback_20d_pct"]
    if row.get("faber") != "GREEN":
        return "QUIET", "broader trend not bullish — not a dip-buy environment"
    if row.get("state") not in ("NORMAL", "EASY"):
        return "QUIET", "macro showing stress — could be weakness, ignoring"
    if pb < PULLBACK_MIN * 100:
        return "QUIET", f"no meaningful pullback yet ({pb:.1f}% off 20d high)"
    if pb > PULLBACK_MAX * 100:
        return "QUIET", f"pullback too deep ({pb:.1f}%) — could be turning macro"
    return "DIP_BUY", f"calm uptrend, {pb:.1f}% pullback from 20d high"


def compute_dip_buy(df: pd.DataFrame) -> pd.DataFrame:
    """Annotate df with dip_buy_state, dip_buy_reason, pullback_20d_pct, faber.

    df must have: spy, state (regime), ma_50, ma_200.
    """
    out = df.copy()
    if "faber" not in out.columns:
        out["faber"] = [faber_signal(s, m50, m200) for s, m50, m200 in
                        zip(out["spy"], out["ma_50"], out["ma_200"])]

    # Prior 20-day high (excludes today so we don't compare to self).
    out["spy_20d_high"] = out["spy"].rolling(HIGH_LOOKBACK, min_periods=5).max().shift(1)
    out["pullback_20d_pct"] = (
        (out["spy_20d_high"] - out["spy"]) / out["spy_20d_high"] * 100
    ).fillna(0.0).round(2)

    states, reasons = [], []
    for _, row in out.iterrows():
        if pd.isna(row["spy_20d_high"]) or row["spy_20d_high"] <= 0:
            states.append("QUIET"); reasons.append("not enough panel history yet"); continue
        s, r = _classify_row(row)
        states.append(s); reasons.append(r)
    out["dip_buy_state"] = states
    out["dip_buy_reason"] = reasons
    return out


def _backtest(df: pd.DataFrame) -> dict:
    """Forward-return distribution: DIP_BUY days vs calm-uptrend-no-dip baseline."""
    for h in (5, 10, 20):
        df[f"fwd_{h}d"] = df["spy"].shift(-h) / df["spy"] - 1

    dip = df[df["dip_buy_state"] == "DIP_BUY"]
    baseline = df[
        (df["faber"] == "GREEN")
        & (df["state"].isin(["NORMAL", "EASY"]))
        & (df["pullback_20d_pct"] < PULLBACK_MIN * 100)
    ]

    horizons = {}
    for h in (5, 10, 20):
        dip_ret = dip[f"fwd_{h}d"].dropna()
        base_ret = baseline[f"fwd_{h}d"].dropna()
        if not len(dip_ret) or not len(base_ret):
            horizons[f"H{h}"] = {"insufficient_data": True}; continue
        dip_up = float((dip_ret > 0).mean())
        base_up = float((base_ret > 0).mean())
        horizons[f"H{h}"] = {
            "dip_buy_n": int(len(dip_ret)),
            "dip_buy_up_rate": round(dip_up, 3),
            "dip_buy_median_ret_pct": round(float(dip_ret.median() * 100), 2),
            "baseline_n": int(len(base_ret)),
            "baseline_up_rate": round(base_up, 3),
            "baseline_median_ret_pct": round(float(base_ret.median() * 100), 2),
            "edge_pp": round((dip_up - base_up) * 100, 1),
        }

    return {
        "panel_range": [str(df.index.min().date()), str(df.index.max().date())],
        "total_dip_days": int(len(dip)),
        "total_baseline_days": int(len(baseline)),
        "first_fire": str(dip.index[0].date()) if len(dip) else None,
        "last_fire":  str(dip.index[-1].date()) if len(dip) else None,
        "horizons": horizons,
    }


def build_payload(df: pd.DataFrame) -> dict:
    """Today's state + backtest stats. Called from regime.run()."""
    annotated = compute_dip_buy(df)
    today = annotated.iloc[-1]

    # Days since the last DIP_BUY fire (None if today is a dip).
    fired_mask = annotated["dip_buy_state"] == "DIP_BUY"
    days_since = None
    if today["dip_buy_state"] != "DIP_BUY":
        prior = fired_mask.iloc[:-1]
        if prior.any():
            last_idx = prior[prior].index[-1]
            days_since = int((today.name - last_idx).days)

    return {
        "live": {
            "state": today["dip_buy_state"],
            "reason": today["dip_buy_reason"],
            "pullback_20d_pct": float(today["pullback_20d_pct"]),
            "spy_close": round(float(today["spy"]), 2),
            "spy_20d_high": round(float(today["spy_20d_high"]), 2) if pd.notna(today["spy_20d_high"]) else None,
            "faber": today.get("faber"),
            "regime": today.get("state"),
            "days_since_last_dip_buy": days_since,
        },
        "rule": {
            "pullback_min_pct": PULLBACK_MIN * 100,
            "pullback_max_pct": PULLBACK_MAX * 100,
            "high_lookback_days": HIGH_LOOKBACK,
            "pre_conditions": "Faber=GREEN AND regime in {NORMAL, EASY}",
            "summary": (
                "Fires only when broader trend is bullish AND macro is calm AND "
                f"SPY is {PULLBACK_MIN*100:.0f}-{PULLBACK_MAX*100:.0f}% below its "
                f"{HIGH_LOOKBACK}-day high. Stays quiet during macro warnings on purpose."
            ),
        },
        "backtest": _backtest(annotated),
    }
