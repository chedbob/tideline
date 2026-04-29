"""Tideline Rule candidate_v4.

Changes from v3 (documented in research_log.md):
  - FIX: pure-vol escape ALWAYS fires before dwell is checked (never blocked by dwell)
  - FIX: extended dwell (20d) applies ONLY to NORMAL -> EASY transition, not all NORMAL-originated ones
  - Rules v3 bug: dwell blocked pure-vol escape, so Apr 2025 tariff (VIX 22->45) stayed NORMAL
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
    vix_5d_ago: float
    days_in_state: int
    pct_hy_5d: dict
    pct_vix_3d: dict
    pct_vix_5d: dict
    pct_hy_level: dict
    pct_vix_level: dict


def pure_vol_escape(prior_state: State, ctx: TransitionContext) -> Optional[State]:
    """Always evaluated first, ALWAYS bypasses dwell."""
    if prior_state != "STRESS":
        if ctx.vix_1d_change > 15 or ctx.vix > 35:
            return "STRESS"
    return None


def _primary_transition(prior_state: State, ctx: TransitionContext) -> Optional[State]:
    if prior_state == "NORMAL":
        hy_trig = ctx.hy_oas_5d_change > ctx.pct_hy_5d[85]
        vix_trig = ctx.vix > 18 and ctx.vix_3d_change > ctx.pct_vix_3d[85]
        if hy_trig or vix_trig:
            return "WATCH"
    if prior_state == "EASY":
        level_trig = ctx.hy_oas > ctx.pct_hy_level[75]
        pre_calm = ctx.vix_5d_ago < ctx.pct_vix_level[30]
        shock_trig = ctx.vix_5d_change > ctx.pct_vix_5d[85] and pre_calm
        if level_trig or shock_trig:
            return "WATCH"
    if prior_state == "WATCH":
        hy_severe = ctx.hy_oas_5d_change > ctx.pct_hy_5d[90]
        bw = (ctx.vix3m is not None and ctx.vix / ctx.vix3m > 1.0)
        if hy_severe and (ctx.vix > 25 or bw):
            return "STRESS"
    if prior_state == "STRESS":
        level_ok = ctx.vix < 25
        hy_relief = ctx.hy_oas_5d_change < ctx.pct_hy_5d[15]
        vol_relief = ctx.vix_3d_change < ctx.pct_vix_3d[15]
        if level_ok and (hy_relief or vol_relief):
            return "WATCH"
        if ctx.days_in_state >= 60 and ctx.vix < 30 and ctx.hy_oas_5d_change < 0:
            return "WATCH"
    if prior_state == "WATCH":
        if ctx.hy_oas_5d_change < ctx.pct_hy_5d[25] and ctx.vix < 20:
            return "NORMAL"
    if prior_state == "NORMAL":
        # NOTE: NORMAL -> EASY handled in state-machine runner with extended dwell
        if (ctx.hy_oas < ctx.pct_hy_level[25] and ctx.vix < 15 and ctx.curve_3m10y > 0):
            return "EASY"
    return None


def primary_transition(prior_state: State, ctx: TransitionContext) -> Optional[State]:
    """Primary transition (subject to dwell in runner)."""
    return _primary_transition(prior_state, ctx)


GO_CRITERION = {
    "historical_events": {
        "volmageddon_2018_02_05": {"must_reach_by_day": 3, "state_floor": "WATCH"},
        "credit_drawdown_2018_12_17": {"must_reach_by_day": 5, "state_floor": "WATCH"},
        "covid_2020_03_09": {"must_reach_by_day": 5, "state_floor": "STRESS"},
        "tariff_shock_2025_04_02": {"must_reach_by_day": 5, "state_floor": "STRESS"},
    },
    "min_events_passing": 3,
    "transition_frequency_range": [6, 40],
}
