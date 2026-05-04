"""Dip-buy signal computation — identify potential bottoms in real time.

Five candidate signals, each pre-declared with round-number thresholds:

  1. TERM_REVERSAL    : VIX backwardation (VIX > VIX3M for 3+ days), then ratio
                        falling for 2+ consecutive days  (post-2007 only)
  2. CAPIT_FOLLOWTHRU : SPY -2%+ single-day with VIX +5pts, then SPY +2%+ within 5 days
  3. CREDIT_PEAK      : BAA10Y at 5y >75th pct, then declining 5+ days
  4. VIX_PANIC_RELEASE: VIX rose >50% in 5d from calm base (pre-shock <16),
                        now closed lower 2 consecutive days
  5. DRAWDOWN_VOL_EASE: SPY ≤ -5% from 60d high AND VIX dropped 3+pts in 3 days

Each signal returns a binary {fired_today: bool, days_since_fire: int|null}
plus the historical distribution of forward returns when it fired.
"""
from __future__ import annotations

import pandas as pd
import numpy as np


def _spy_drawdown(spy: pd.Series, lookback: int = 60) -> pd.Series:
    """SPY drawdown from rolling lookback-day high (negative number)."""
    rolling_max = spy.rolling(lookback, min_periods=10).max()
    return spy / rolling_max - 1.0


def _vix3m_safe(df: pd.DataFrame) -> pd.Series:
    """VIX3M from data, fallback to VIX 63d MA when missing (pre-2007)."""
    if "vix3m" in df.columns:
        return df["vix3m"]
    return df["vix"].rolling(63, min_periods=20).mean()


def _signal_term_reversal(df: pd.DataFrame) -> pd.Series:
    """VIX > VIX3M for 3+ days, then ratio falling 2 consecutive days."""
    vix3m = _vix3m_safe(df)
    ratio = df["vix"] / vix3m
    backwardation = ratio > 1.0
    backwardation_3d = backwardation.rolling(3).sum() == 3
    ratio_falling_2d = (ratio.diff() < 0) & (ratio.diff().shift(1) < 0)
    return (backwardation_3d.shift(2) & ratio_falling_2d).fillna(False)


def _signal_capit_followthru(df: pd.DataFrame) -> pd.Series:
    """SPY -2%+ day with VIX +5pts, then SPY +2%+ within 5 days."""
    spy_1d = df["spy"].pct_change()
    vix_1d = df["vix"].diff()
    capit_day = (spy_1d <= -0.02) & (vix_1d >= 5)
    fire = pd.Series(False, index=df.index)
    capit_idx = df.index[capit_day]
    for d in capit_idx:
        i = df.index.get_loc(d)
        for j in range(1, min(6, len(df) - i)):
            if spy_1d.iloc[i + j] >= 0.02:
                fire.iloc[i + j] = True
                break
    return fire


def _signal_credit_peak(df: pd.DataFrame) -> pd.Series:
    """BAA10Y in top quartile of 5y rolling, then declining 5 consecutive days."""
    hy = df["hy_oas"]
    p75 = hy.shift(1).rolling(1260, min_periods=252).quantile(0.75)
    elevated = hy > p75
    falling_5d = (
        (hy.diff() < 0) &
        (hy.diff().shift(1) < 0) &
        (hy.diff().shift(2) < 0) &
        (hy.diff().shift(3) < 0) &
        (hy.diff().shift(4) < 0)
    )
    return (elevated.shift(5) & falling_5d).fillna(False)


def _signal_vix_panic_release(df: pd.DataFrame) -> pd.Series:
    """VIX rose >50% in 5d from <16 base, then closed lower 2 consecutive days."""
    vix = df["vix"]
    vix_5d_ago = vix.shift(5)
    spike = (vix / vix_5d_ago > 1.5) & (vix_5d_ago < 16)
    falling_2d = (vix.diff() < 0) & (vix.diff().shift(1) < 0)
    return (spike.rolling(7).max().shift(0).astype(bool) & falling_2d).fillna(False)


def _signal_drawdown_vol_ease(df: pd.DataFrame) -> pd.Series:
    """SPY <=-5% from 60d high AND VIX dropped 3+pts in 3 days."""
    dd = _spy_drawdown(df["spy"], 60)
    deep_dd = dd <= -0.05
    vix_3d_drop = df["vix"].diff(3) <= -3
    return (deep_dd & vix_3d_drop).fillna(False)


SIGNALS = {
    "TERM_REVERSAL":     _signal_term_reversal,
    "CAPIT_FOLLOWTHRU":  _signal_capit_followthru,
    "CREDIT_PEAK":       _signal_credit_peak,
    "VIX_PANIC_RELEASE": _signal_vix_panic_release,
    "DRAWDOWN_VOL_EASE": _signal_drawdown_vol_ease,
}


def _block_bootstrap_acc(outcomes: np.ndarray, n_iter: int = 5000, block_len: int = 20,
                          seed: int = 42) -> tuple[float, float]:
    """Block bootstrap CI on hit rate (binary outcomes 0/1)."""
    if len(outcomes) < block_len:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n = len(outcomes)
    n_blocks = int(np.ceil(n / block_len))
    accs = np.empty(n_iter)
    for i in range(n_iter):
        starts = rng.integers(0, n - block_len + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]
        accs[i] = outcomes[idx].mean()
    return (float(np.quantile(accs, 0.025)), float(np.quantile(accs, 0.975)))


def backtest_signal(df: pd.DataFrame, signal_name: str, horizons: list[int] = [5, 10, 20]) -> dict:
    """For one signal: backtest with both UNCONDITIONAL baseline AND
    CONDITIONAL-on-drawdown baseline (>=3% from 60d high).

    The conditional baseline is the honest one for a dip-buy signal: it asks
    "given we're already in stress, does the signal add edge over just being
    in stress?" — not the irrelevant "vs all-time SPY-up bias"."""
    fire = SIGNALS[signal_name](df)
    fire_dates = df.index[fire]
    spy = df["spy"]
    in_drawdown = _spy_drawdown(spy, 60) <= -0.03

    horizon_results = {}
    for H in horizons:
        fwd = spy.shift(-H) / spy - 1

        # Unconditional baseline
        unc_baseline = float((fwd > 0).dropna().mean())

        # Conditional-on-drawdown baseline (days where we're in dd, signal didn't fire)
        cond_pop = fwd[in_drawdown & ~fire].dropna()
        cond_baseline = float((cond_pop > 0).mean()) if len(cond_pop) >= 30 else None

        # Fire-day outcomes
        outcomes_returns = fwd.loc[fire_dates].dropna()
        n = len(outcomes_returns)
        if n < 5:
            horizon_results[f"H{H}"] = {"n": int(n), "fire_dates_too_few": True}
            continue
        outcomes_up = (outcomes_returns > 0).astype(int).values
        hit_rate = float(outcomes_up.mean())
        median_ret = float(np.median(outcomes_returns) * 100)
        boot_lo, boot_hi = _block_bootstrap_acc(outcomes_up, n_iter=5000, block_len=10)

        unc_edge = (hit_rate - unc_baseline) * 100
        unc_survives = boot_lo > unc_baseline
        cond_edge = (hit_rate - cond_baseline) * 100 if cond_baseline is not None else None
        cond_survives = (boot_lo > cond_baseline) if cond_baseline is not None else None

        horizon_results[f"H{H}"] = {
            "n": int(n),
            "hit_rate": round(hit_rate, 3),
            "median_fwd_return_pct": round(median_ret, 2),
            "bootstrap_ci_95": [round(boot_lo, 3), round(boot_hi, 3)],
            "unconditional": {
                "baseline": round(unc_baseline, 3),
                "edge_pp": round(unc_edge, 1),
                "survives_bootstrap": bool(unc_survives),
            },
            "conditional_drawdown": {
                "baseline": round(cond_baseline, 3) if cond_baseline is not None else None,
                "edge_pp": round(cond_edge, 1) if cond_edge is not None else None,
                "survives_bootstrap": bool(cond_survives) if cond_survives is not None else None,
                "baseline_n": int(len(cond_pop)),
            },
        }

    return {
        "signal": signal_name,
        "total_fires": int(len(fire_dates)),
        "first_fire": str(fire_dates[0].date()) if len(fire_dates) else None,
        "last_fire":  str(fire_dates[-1].date()) if len(fire_dates) else None,
        "horizons": horizon_results,
    }


def current_state(df: pd.DataFrame) -> dict:
    """For each signal, report whether it fired today and days since last fire."""
    today = df.index[-1]
    out = {}
    for name in SIGNALS:
        fire = SIGNALS[name](df)
        fired_today = bool(fire.iloc[-1])
        prior = fire.iloc[:-1]
        if prior.any():
            last_fire_idx = prior.where(prior).last_valid_index()
            days_since = int((today - last_fire_idx).days) if last_fire_idx else None
        else:
            days_since = None
        out[name] = {
            "fired_today": fired_today,
            "days_since_last_fire": days_since,
        }
    return out


def build_dip_signals_payload(df: pd.DataFrame) -> dict:
    """Full payload: backtest stats + current live status."""
    backtest = {name: backtest_signal(df, name) for name in SIGNALS}
    live = current_state(df)
    return {
        "panel_range": [str(df.index.min().date()), str(df.index.max().date())],
        "backtest": backtest,
        "live": live,
    }
