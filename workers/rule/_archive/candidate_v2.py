"""Tideline Rule candidate_v2 — pre-committed before backtest.

This file is THE rule definition. Committed to git before running
phase3_state_machine.py so that any post-hoc tweaking is visible
in diffs. Do not modify after the backtest runs — create candidate_v3.

States: EASY | NORMAL | WATCH | STRESS
Horizon: daily evaluation, minimum 3-day dwell between transitions.
All velocity thresholds are rolling 5-year percentile (1260 trading days)
so there is no hardcoded point cutoff that can be curve-fit.

NFCI is released T+5 relative to its week-ending date. All rules that
reference NFCI use a 5-trading-day lagged version enforced by the backtest.

VIX3M-dependent rule (backwardation) only activates from 2007-09-18 onward
because ^VIX3M has no history before that date. Pre-2007, use proxy
= rolling 63-day mean of VIX.

Rules evaluated in priority order per day:
  1. Pure-vol escape (WATCH -> STRESS if VIX 1D > +15 OR VIX > 35)
  2. Primary transitions (see below)
  3. Recovery transitions
"""

from dataclasses import dataclass
from typing import Literal, Optional

State = Literal["EASY", "NORMAL", "WATCH", "STRESS"]


@dataclass(frozen=True)
class TransitionContext:
    """Values available at evaluation time t (enforced by backtest)."""
    # Levels (daily, most recent)
    hy_oas: float                        # BAA10Y level, %
    vix: float                           # VIXCLS level
    vix3m: Optional[float]               # Yahoo ^VIX3M, None pre-2007
    curve_3m10y: float                   # T10Y3M level, %
    nfci_lagged: float                   # NFCI with T+5 release lag enforced

    # Velocity (changes over lookback windows, backward-only)
    hy_oas_5d_change: float              # (hy_oas_t - hy_oas_t-5) in %
    vix_1d_change: float                 # (vix_t - vix_t-1)
    vix_3d_change: float                 # (vix_t - vix_t-3)
    vix_5d_change: float                 # (vix_t - vix_t-5)
    nfci_1w_change: float                # NFCI lagged, 5-day change

    # Rolling 5y (1260 trading days) percentiles, excluding today
    pct_hy_5d: dict                      # {50, 75, 85, 90, 25, 15, 10}
    pct_vix_3d: dict
    pct_vix_5d: dict
    pct_hy_level: dict
    pct_vix_level: dict


def _primary_transition(prior_state: State, ctx: TransitionContext) -> Optional[State]:
    """Return new state or None if no primary transition applies."""
    in_calm_base = ctx.vix < ctx.pct_vix_level.get(30, ctx.vix)

    # NORMAL -> WATCH
    if prior_state == "NORMAL":
        hy_triggered = ctx.hy_oas_5d_change > ctx.pct_hy_5d[85]
        vix_triggered = ctx.vix > 18 and ctx.vix_3d_change > ctx.pct_vix_3d[85]
        if hy_triggered or vix_triggered:
            return "WATCH"

    # EASY -> WATCH
    if prior_state == "EASY":
        level_triggered = ctx.hy_oas > ctx.pct_hy_level[75]
        shock_triggered = (
            ctx.vix_5d_change > ctx.pct_vix_5d[85] and in_calm_base
        )
        if level_triggered or shock_triggered:
            return "WATCH"

    # WATCH -> STRESS (primary: HY-confirmed)
    if prior_state == "WATCH":
        hy_severe = ctx.hy_oas_5d_change > ctx.pct_hy_5d[90]
        # Backwardation only when VIX3M is available
        backwardation = (ctx.vix3m is not None and ctx.vix / ctx.vix3m > 1.0)
        vol_confirm = ctx.vix > 25 or backwardation
        if hy_severe and vol_confirm:
            return "STRESS"

    # STRESS -> WATCH (recovery)
    if prior_state == "STRESS":
        hy_relief = ctx.hy_oas_5d_change < ctx.pct_hy_5d[15]
        vol_relief = ctx.vix < 25 and ctx.vix_3d_change < ctx.pct_vix_3d[15]
        if hy_relief and vol_relief:
            return "WATCH"

    # WATCH -> NORMAL (recovery)
    if prior_state == "WATCH":
        hy_calming = ctx.hy_oas_5d_change < ctx.pct_hy_5d[25]
        vol_calm = ctx.vix < 20
        nfci_loosening = ctx.nfci_1w_change < 0
        if hy_calming and vol_calm and nfci_loosening:
            return "NORMAL"

    # NORMAL -> EASY
    if prior_state == "NORMAL":
        tight_spread = ctx.hy_oas < ctx.pct_hy_level[25]
        low_vol = ctx.vix < 15
        healthy_curve = ctx.curve_3m10y > 0
        if tight_spread and low_vol and healthy_curve:
            return "EASY"

    return None


def _pure_vol_escape(prior_state: State, ctx: TransitionContext) -> Optional[State]:
    """WATCH -> STRESS on extreme single-day vol (Volmageddon fix).
    Fires regardless of credit confirmation."""
    if prior_state == "WATCH":
        if ctx.vix_1d_change > 15 or ctx.vix > 35:
            return "STRESS"
    # Also NORMAL -> STRESS skipping WATCH if truly extreme
    if prior_state == "NORMAL":
        if ctx.vix_1d_change > 20 or ctx.vix > 40:
            return "STRESS"
    return None


def evaluate_transition(prior_state: State, ctx: TransitionContext) -> tuple[State, Optional[str]]:
    """Return (new_state, trigger_description). new_state == prior_state if no transition."""
    # Priority 1: pure-vol escape
    vol_escape = _pure_vol_escape(prior_state, ctx)
    if vol_escape and vol_escape != prior_state:
        return vol_escape, f"pure_vol_escape_to_{vol_escape}"

    # Priority 2: primary transitions
    primary = _primary_transition(prior_state, ctx)
    if primary and primary != prior_state:
        return primary, f"primary_to_{primary}"

    return prior_state, None


# Pre-declared go criterion (committed before backtest runs)
GO_CRITERION = {
    "historical_events": {
        "volmageddon_2018_02_05": {"must_reach_by_day": 3, "state_floor": "WATCH"},
        "credit_drawdown_2018_12_17": {"must_reach_by_day": 5, "state_floor": "WATCH"},
        "covid_2020_03_09": {"must_reach_by_day": 5, "state_floor": "STRESS"},
        "tariff_shock_2025_04_02": {"must_reach_by_day": 5, "state_floor": "STRESS"},
    },
    "min_events_passing": 3,  # at least 3 of 4 must resolve within windows
    "volmageddon_special": "must reach WATCH within 3 trading days",
    "transition_frequency_range": [6, 30],  # transitions per year
    "stress_edge": "block-bootstrap CI on STRESS-state 20D SPY DOWN rate must exclude baseline",
}
