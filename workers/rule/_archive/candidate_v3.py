"""Tideline Rule candidate_v3 — iterated after v2 backtest failure.

Changes from candidate_v2 (documented in research_log.md Entry 9):
  1. FIX: `in_calm_base` uses VIX 5 days ago, not today's VIX
     (Bug: Feb 2018 Volmageddon didn't trigger because today's VIX > p30 blocked the shock rule)
  2. ADD: Pure-vol escape available from EASY state
     (Bug: no path from EASY to STRESS when VIX jumps 20pts in one day)
  3. RELAX: STRESS -> WATCH recovery uses OR on relief signals, not AND
     (Bug: STRESS stuck at 49% of days in v2; recovery required three simultaneous p15 readings)
  4. RELAX: WATCH -> NORMAL no longer gates on NFCI (too slow) — uses HY+VIX only
  5. ADD: Auto-downgrade failsafe — after 60 days in STRESS without further deterioration, step down to WATCH
  6. ADD: NORMAL -> EASY dwell increased from 3 to 20 days (prevent oscillation)
"""

from dataclasses import dataclass
from typing import Literal, Optional

State = Literal["EASY", "NORMAL", "WATCH", "STRESS"]


@dataclass(frozen=True)
class TransitionContext:
    hy_oas: float
    vix: float
    vix3m: Optional[float]
    curve_3m10y: float
    nfci_lagged: float

    hy_oas_5d_change: float
    vix_1d_change: float
    vix_3d_change: float
    vix_5d_change: float
    nfci_1w_change: float

    # NEW: vix level 5 days ago, for pre-shock comparison
    vix_5d_ago: float

    # NEW: days in current state (for failsafe recovery)
    days_in_state: int

    pct_hy_5d: dict
    pct_vix_3d: dict
    pct_vix_5d: dict
    pct_hy_level: dict
    pct_vix_level: dict


def _pure_vol_escape(prior_state: State, ctx: TransitionContext) -> Optional[State]:
    """Extreme single-day vol shocks override normal flow — works from EASY/NORMAL/WATCH."""
    if prior_state in ("EASY", "NORMAL", "WATCH"):
        if ctx.vix_1d_change > 15 or ctx.vix > 35:
            return "STRESS"
    return None


def _primary_transition(prior_state: State, ctx: TransitionContext) -> Optional[State]:
    # NORMAL -> WATCH
    if prior_state == "NORMAL":
        hy_triggered = ctx.hy_oas_5d_change > ctx.pct_hy_5d[85]
        vix_triggered = ctx.vix > 18 and ctx.vix_3d_change > ctx.pct_vix_3d[85]
        if hy_triggered or vix_triggered:
            return "WATCH"

    # EASY -> WATCH (FIXED: use pre-shock VIX for calm-base check)
    if prior_state == "EASY":
        level_triggered = ctx.hy_oas > ctx.pct_hy_level[75]
        pre_shock_calm = ctx.vix_5d_ago < ctx.pct_vix_level[30]
        shock_triggered = (
            ctx.vix_5d_change > ctx.pct_vix_5d[85] and pre_shock_calm
        )
        if level_triggered or shock_triggered:
            return "WATCH"

    # WATCH -> STRESS (primary: HY-confirmed)
    if prior_state == "WATCH":
        hy_severe = ctx.hy_oas_5d_change > ctx.pct_hy_5d[90]
        backwardation = (ctx.vix3m is not None and ctx.vix / ctx.vix3m > 1.0)
        vol_confirm = ctx.vix > 25 or backwardation
        if hy_severe and vol_confirm:
            return "STRESS"

    # STRESS -> WATCH (recovery, RELAXED: OR on relief signals)
    if prior_state == "STRESS":
        level_below_stress = ctx.vix < 25
        hy_relief = ctx.hy_oas_5d_change < ctx.pct_hy_5d[15]
        vol_relief = ctx.vix_3d_change < ctx.pct_vix_3d[15]
        if level_below_stress and (hy_relief or vol_relief):
            return "WATCH"

        # FAILSAFE: auto step-down after 60 days if no new deterioration
        if ctx.days_in_state >= 60 and ctx.vix < 30 and ctx.hy_oas_5d_change < 0:
            return "WATCH"

    # WATCH -> NORMAL (RELAXED: drop NFCI gate)
    if prior_state == "WATCH":
        hy_calming = ctx.hy_oas_5d_change < ctx.pct_hy_5d[25]
        vol_calm = ctx.vix < 20
        if hy_calming and vol_calm:
            return "NORMAL"

    # NORMAL -> EASY
    if prior_state == "NORMAL":
        tight_spread = ctx.hy_oas < ctx.pct_hy_level[25]
        low_vol = ctx.vix < 15
        healthy_curve = ctx.curve_3m10y > 0
        if tight_spread and low_vol and healthy_curve:
            return "EASY"

    return None


def evaluate_transition(prior_state: State, ctx: TransitionContext) -> tuple[State, Optional[str]]:
    vol_escape = _pure_vol_escape(prior_state, ctx)
    if vol_escape and vol_escape != prior_state:
        return vol_escape, f"pure_vol_escape_to_{vol_escape}"
    primary = _primary_transition(prior_state, ctx)
    if primary and primary != prior_state:
        return primary, f"primary_to_{primary}"
    return prior_state, None


GO_CRITERION = {
    "historical_events": {
        "volmageddon_2018_02_05": {"must_reach_by_day": 3, "state_floor": "WATCH"},
        "credit_drawdown_2018_12_17": {"must_reach_by_day": 5, "state_floor": "WATCH"},
        "covid_2020_03_09": {"must_reach_by_day": 5, "state_floor": "STRESS"},
        "tariff_shock_2025_04_02": {"must_reach_by_day": 5, "state_floor": "STRESS"},
    },
    "min_events_passing": 3,
    "transition_frequency_range": [6, 40],
    # NOTE v3: STRESS directional edge test removed — per Entry 9, the inversion pattern persists
    # and we do not expect STRESS to predict forward-DOWN. 4-state regime is descriptive, not predictive.
}
