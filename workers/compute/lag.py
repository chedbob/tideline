"""Lag analysis — measure how late Tideline reacts to actual SPY moves.

For every historical SPY drawdown >= 3%, find:
  - When the drawdown peaked (before the fall)
  - When Tideline's state shifted into ELEVATED or STRESS
  - days_to_warn = transition_date - peak_date  (positive = lag, negative = early warning)
  - drawdown_pct already done before warn

For every recovery from STRESS:
  - When the trough hit (lowest SPY)
  - When Tideline's state cleared back to NORMAL/EASY
  - days_to_clear = clear_date - trough_date

Results expose where the system lags so we can decide what to tighten.
"""
from __future__ import annotations

import pandas as pd
import numpy as np


DRAWDOWN_THRESHOLD = 0.03   # 3% SPY drop minimum
WARN_STATES = {"ELEVATED", "STRESS"}   # states that count as "warning issued"
RECOVERY_STATES = {"NORMAL", "EASY"}   # states that count as "all-clear"


def find_drawdown_episodes(spy: pd.Series, threshold: float = DRAWDOWN_THRESHOLD) -> list[dict]:
    """Identify discrete drawdown episodes >= threshold.

    Each episode: peak -> trough -> recovery (back to peak).
    Returns list of {peak_date, peak_price, trough_date, trough_price, recovery_date, drawdown_pct}.
    """
    episodes = []
    running_peak_idx = 0
    running_peak_val = spy.iloc[0]
    in_drawdown = False
    trough_idx = None
    trough_val = None
    peak_for_episode = None

    for i in range(1, len(spy)):
        v = spy.iloc[i]

        if not in_drawdown:
            if v >= running_peak_val:
                # New peak
                running_peak_val = v
                running_peak_idx = i
            else:
                # Started falling — possible drawdown
                if (running_peak_val - v) / running_peak_val >= threshold * 0.5:
                    # 1.5% threshold to start tracking, then need full 3% before recording
                    in_drawdown = True
                    trough_idx = i
                    trough_val = v
                    peak_for_episode = (running_peak_idx, running_peak_val)
        else:
            if v < trough_val:
                trough_idx = i
                trough_val = v
            elif v >= peak_for_episode[1]:
                # Recovered to original peak — episode closes
                drawdown_pct = (peak_for_episode[1] - trough_val) / peak_for_episode[1]
                if drawdown_pct >= threshold:
                    episodes.append({
                        "peak_date":     spy.index[peak_for_episode[0]],
                        "peak_price":    float(peak_for_episode[1]),
                        "trough_date":   spy.index[trough_idx],
                        "trough_price":  float(trough_val),
                        "recovery_date": spy.index[i],
                        "drawdown_pct":  float(drawdown_pct),
                        "duration_days": int(i - peak_for_episode[0]),
                    })
                # Reset
                in_drawdown = False
                running_peak_val = v
                running_peak_idx = i

    # Capture an open drawdown at end
    if in_drawdown and trough_val is not None:
        drawdown_pct = (peak_for_episode[1] - trough_val) / peak_for_episode[1]
        if drawdown_pct >= threshold:
            episodes.append({
                "peak_date":     spy.index[peak_for_episode[0]],
                "peak_price":    float(peak_for_episode[1]),
                "trough_date":   spy.index[trough_idx],
                "trough_price":  float(trough_val),
                "recovery_date": None,
                "drawdown_pct":  float(drawdown_pct),
                "duration_days": int(len(spy) - 1 - peak_for_episode[0]),
                "still_open": True,
            })

    return episodes


def measure_warn_lag(df: pd.DataFrame, episodes: list[dict]) -> list[dict]:
    """For each drawdown, find first day Tideline shifted into ELEVATED/STRESS
    after the peak, and measure lag in trading days."""
    state = df["state"]
    out = []
    for ep in episodes:
        peak_idx = df.index.get_loc(ep["peak_date"])
        # Look from peak forward through the recovery (or end of panel)
        end_idx = df.index.get_loc(ep["recovery_date"]) if ep.get("recovery_date") else len(df) - 1
        window = state.iloc[peak_idx:end_idx + 1]
        warn_mask = window.isin(WARN_STATES)
        if warn_mask.any():
            first_warn_local_idx = int(warn_mask.values.argmax())  # first True
            warn_idx = peak_idx + first_warn_local_idx
            warn_date = df.index[warn_idx]
            lag = first_warn_local_idx
            warn_state = window.iloc[first_warn_local_idx]
            # How much of the drawdown was already done at warn time?
            warn_price = df["spy"].iloc[warn_idx]
            drawdown_at_warn = (ep["peak_price"] - warn_price) / ep["peak_price"]
            captured = ep["drawdown_pct"] - drawdown_at_warn
        else:
            warn_date = None
            lag = None
            warn_state = None
            drawdown_at_warn = None
            captured = None

        out.append({
            "peak_date":          str(ep["peak_date"].date()),
            "trough_date":        str(ep["trough_date"].date()),
            "drawdown_pct":       round(ep["drawdown_pct"] * 100, 2),
            "warn_date":          str(warn_date.date()) if warn_date else None,
            "warn_state":         warn_state,
            "warn_lag_days":      lag,
            "drawdown_done_at_warn_pct": round(drawdown_at_warn * 100, 2) if drawdown_at_warn is not None else None,
            "drawdown_remaining_at_warn_pct": round(captured * 100, 2) if captured is not None else None,
        })
    return out


def measure_clear_lag(df: pd.DataFrame, episodes: list[dict]) -> list[dict]:
    """For each drawdown, find first day Tideline cleared back to NORMAL/EASY
    after the trough, and measure lag in trading days."""
    state = df["state"]
    out = []
    for ep in episodes:
        if not ep.get("recovery_date"):
            continue
        trough_idx = df.index.get_loc(ep["trough_date"])
        recovery_idx = df.index.get_loc(ep["recovery_date"])
        window = state.iloc[trough_idx:recovery_idx + 1]
        clear_mask = window.isin(RECOVERY_STATES)
        if clear_mask.any():
            first_clear_local_idx = int(clear_mask.values.argmax())
            clear_idx = trough_idx + first_clear_local_idx
            clear_date = df.index[clear_idx]
            lag = first_clear_local_idx
            # How much of the recovery had happened by clear time?
            clear_price = df["spy"].iloc[clear_idx]
            recovery_pct = (clear_price - ep["trough_price"]) / (ep["peak_price"] - ep["trough_price"])
        else:
            clear_date = None
            lag = None
            recovery_pct = None

        out.append({
            "trough_date":          str(ep["trough_date"].date()),
            "recovery_date":        str(ep["recovery_date"].date()),
            "clear_date":           str(clear_date.date()) if clear_date else None,
            "clear_lag_days":       lag,
            "recovery_done_at_clear_pct": round(recovery_pct * 100, 2) if recovery_pct is not None else None,
        })
    return out


def aggregate(stats: list[dict], key: str) -> dict | None:
    vals = [float(s[key]) for s in stats if s.get(key) is not None]
    if not vals:
        return None
    return {
        "n":      int(len(vals)),
        "median": float(np.median(vals)),
        "mean":   round(float(np.mean(vals)), 1),
        "p25":    float(np.percentile(vals, 25)),
        "p75":    float(np.percentile(vals, 75)),
        "min":    float(min(vals)),
        "max":    float(max(vals)),
    }


def build_lag_report(df: pd.DataFrame) -> dict:
    """Run lag analysis on the full panel."""
    episodes = find_drawdown_episodes(df["spy"])
    warn_stats = measure_warn_lag(df, episodes)
    clear_stats = measure_clear_lag(df, episodes)

    # Last-12-months subset for "recent" view
    cutoff = df.index[-1] - pd.DateOffset(months=12)
    recent_warn = [w for w in warn_stats if pd.Timestamp(w["peak_date"]) >= cutoff]
    recent_clear = [c for c in clear_stats if pd.Timestamp(c["trough_date"]) >= cutoff]

    return {
        "panel_range":     [str(df.index.min().date()), str(df.index.max().date())],
        "n_drawdowns":     len(episodes),
        "drawdown_threshold_pct": DRAWDOWN_THRESHOLD * 100,
        "warn_lag_all":    aggregate(warn_stats, "warn_lag_days"),
        "warn_lag_12mo":   aggregate(recent_warn, "warn_lag_days"),
        "clear_lag_all":   aggregate(clear_stats, "clear_lag_days"),
        "clear_lag_12mo":  aggregate(recent_clear, "clear_lag_days"),
        "drawdown_done_at_warn_all": aggregate(warn_stats, "drawdown_done_at_warn_pct"),
        "warn_episodes":   warn_stats[-20:],     # last 20 for the dashboard
        "clear_episodes":  clear_stats[-20:],
    }
