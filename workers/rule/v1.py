"""Tideline rule v1 — FROZEN PRODUCTION RULE.

This file is the live rule. It is immutable after launch. Changes require a
new version (v2) that resets the accuracy counter per CLAUDE.md integrity
rules. Retired rules stay in this repository, never deleted.

Version lineage (see workers/backtest/research_log.md):
  candidate_v2 — FAILED (3 bugs: in_calm_base, missing vol escape, stuck recovery)
  candidate_v3 — PARTIAL (2/4 events, dwell blocked pure-vol escape)
  candidate_v4 — PASSED (4/4 events, 5/5 OOS near-miss, phase 4 validated)
  v1           — candidate_v4 with WATCH renamed ELEVATED, frozen for production

Two independent signals live in this file:

  Zone 0 — FABER TREND SIGNAL (binary, predictive, bootstrap-robust)
    GREEN    : SPY > 200DMA AND 50DMA > 200DMA
    CAUTION  : SPY < 200DMA AND 50DMA < 200DMA
    NEUTRAL  : indicators disagree (rare, <4% of days)
    Claim: when CAUTION fires, SPY historical 20D DOWN rate = 46.0% vs
           36.9% baseline. Block-bootstrap L=60 95% CI [37.7%, 55.8%].
           Sample 1997-2026, n=1541.

  Zone 1 — REGIME STATE (4-state descriptor, NOT predictive)
    EASY / NORMAL / ELEVATED / STRESS
    Reacts to stress events within 0-3 days (validated on 9 historical
    stress episodes including 5 not used to design the rule).
    NOT a directional forecast. Descriptive only.

Zone 2 (component panel) is raw data, not a rule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

RULE_VERSION = "v1"
RULE_LINEAGE = "candidate_v4 (rename WATCH->ELEVATED)"

# ---- Zone 0: Faber Trend Signal ----

FaberState = Literal["GREEN", "CAUTION", "NEUTRAL"]


def faber_signal(spy: float, ma_50: float, ma_200: float) -> FaberState:
    """Binary trend signal. Bootstrap-robust +9.1pp DOWN edge on CAUTION.

    Requires both indicators bearish/bullish to commit. Mixed = NEUTRAL.
    """
    above_200 = spy > ma_200
    fifty_above_200 = ma_50 > ma_200
    if above_200 and fifty_above_200:
        return "GREEN"
    if (not above_200) and (not fifty_above_200):
        return "CAUTION"
    return "NEUTRAL"


# Public evidence for Zone 0 historical claim (from Phase 0 research).
FABER_CLAIM = {
    "window": "1997-01-02 to 2026-04-22",
    "n_caution_days": 1541,
    "caution_down_rate_20d": 0.460,
    "baseline_down_rate_20d": 0.369,
    "edge_pp": 9.1,
    "wilson_ci_95": [43.5, 48.5],
    "block_bootstrap_ci_95": [37.7, 55.8],
    "framing": "Risk-management signal, not return forecast",
    "literature": "Faber (2007) — A Quantitative Approach to Tactical Asset Allocation",
}


# ---- Zone 1: 4-State Regime (descriptive) ----

RegimeState = Literal["EASY", "NORMAL", "ELEVATED", "STRESS"]


@dataclass(frozen=True)
class RegimeContext:
    """Values available at evaluation time t (enforced by compute layer)."""
    hy_oas: float                # BAA10Y level, %
    vix: float                   # VIXCLS level
    vix3m: Optional[float]       # ^VIX3M, None pre-2007 (uses SMA63 proxy)
    curve_3m10y: float           # T10Y3M level, %
    nfci_lagged: float           # NFCI with T+5 release lag enforced

    hy_oas_5d_change: float
    vix_1d_change: float
    vix_3d_change: float
    vix_5d_change: float
    nfci_1w_change: float

    vix_5d_ago: float            # pre-shock VIX, for shock-from-calm rule
    days_in_state: int           # dwell counter

    # Rolling 5y (1260 trading day) percentiles, excluding today
    pct_hy_5d: dict
    pct_vix_3d: dict
    pct_vix_5d: dict
    pct_hy_level: dict
    pct_vix_level: dict


def pure_vol_escape(prior: RegimeState, ctx: RegimeContext) -> Optional[RegimeState]:
    """Always evaluated first, ALWAYS bypasses dwell.
    Extreme single-day vol shocks must override normal flow."""
    if prior != "STRESS":
        if ctx.vix_1d_change > 15 or ctx.vix > 35:
            return "STRESS"
    return None


def primary_transition(prior: RegimeState, ctx: RegimeContext) -> Optional[RegimeState]:
    """Primary state transition. Subject to dwell rules in the runner."""

    if prior == "NORMAL":
        hy_trig = ctx.hy_oas_5d_change > ctx.pct_hy_5d[85]
        vix_trig = ctx.vix > 18 and ctx.vix_3d_change > ctx.pct_vix_3d[85]
        if hy_trig or vix_trig:
            return "ELEVATED"

    if prior == "EASY":
        level_trig = ctx.hy_oas > ctx.pct_hy_level[75]
        pre_calm = ctx.vix_5d_ago < ctx.pct_vix_level[30]
        shock_trig = ctx.vix_5d_change > ctx.pct_vix_5d[85] and pre_calm
        if level_trig or shock_trig:
            return "ELEVATED"

    if prior == "ELEVATED":
        hy_severe = ctx.hy_oas_5d_change > ctx.pct_hy_5d[90]
        backwardation = (ctx.vix3m is not None and ctx.vix / ctx.vix3m > 1.0)
        if hy_severe and (ctx.vix > 25 or backwardation):
            return "STRESS"

    if prior == "STRESS":
        level_ok = ctx.vix < 25
        hy_relief = ctx.hy_oas_5d_change < ctx.pct_hy_5d[15]
        vol_relief = ctx.vix_3d_change < ctx.pct_vix_3d[15]
        if level_ok and (hy_relief or vol_relief):
            return "ELEVATED"
        # Failsafe: auto step-down after 60 days if no new deterioration
        if ctx.days_in_state >= 60 and ctx.vix < 30 and ctx.hy_oas_5d_change < 0:
            return "ELEVATED"

    if prior == "ELEVATED":
        if ctx.hy_oas_5d_change < ctx.pct_hy_5d[25] and ctx.vix < 20:
            return "NORMAL"

    if prior == "NORMAL":
        if (ctx.hy_oas < ctx.pct_hy_level[25] and ctx.vix < 15 and ctx.curve_3m10y > 0):
            return "EASY"

    return None


def evaluate(prior: RegimeState, ctx: RegimeContext, dwell_required: int) -> tuple[RegimeState, Optional[str]]:
    """Full evaluation: escape first, then primary with dwell check."""
    escape = pure_vol_escape(prior, ctx)
    if escape and escape != prior:
        return escape, f"escape_to_{escape}"

    candidate = primary_transition(prior, ctx)
    if candidate and candidate != prior and ctx.days_in_state >= dwell_required:
        return candidate, f"primary_to_{candidate}"

    return prior, None


# Dwell rules: asymmetric — relaxing to EASY takes 20 days to avoid oscillation
DWELL_DEFAULT = 3
DWELL_NORMAL_TO_EASY = 20


def dwell_for(current: RegimeState, candidate: Optional[RegimeState]) -> int:
    if current == "NORMAL" and candidate == "EASY":
        return DWELL_NORMAL_TO_EASY
    return DWELL_DEFAULT


# Percentile window for regime thresholds
PERCENTILE_WINDOW_DAYS = 1260   # 5 trading years
NFCI_RELEASE_LAG_DAYS = 5       # NFCI week ends Friday, releases following Wednesday


# Component classification for Zone 2 display
def classify_hy(hy_oas: float, pct_5y: float) -> str:
    if pct_5y < 0.25: return "tight"
    if pct_5y < 0.50: return "moderate"
    if pct_5y < 0.75: return "elevated"
    return "stressed"


def classify_vix(vix: float) -> str:
    if vix < 15: return "calm"
    if vix < 20: return "normal"
    if vix < 25: return "elevated"
    if vix < 35: return "stressed"
    return "panic"


def classify_curve(curve_3m10y: float) -> str:
    if curve_3m10y > 1.0: return "steep"
    if curve_3m10y > 0: return "normal"
    if curve_3m10y > -1.0: return "flat_inverted"
    return "deeply_inverted"


def classify_nfci(nfci: float) -> str:
    # Chicago Fed NFCI: negative = looser than average, positive = tighter
    if nfci < -0.5: return "very_loose"
    if nfci < 0: return "loose"
    if nfci < 0.5: return "tight"
    return "very_tight"
