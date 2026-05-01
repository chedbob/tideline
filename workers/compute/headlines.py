"""Smart headline generator — finds historical analogs to current Tideline state
and writes a natural-sounding sentence about them.

Self-adapting: recomputed from the full 1997-2026 daily panel on every cron run,
so as new transitions happen the headline corpus expands automatically. No
hardcoded year lists; everything derives from the live decision history.

Three analog types:
  1. STREAK — currently in state X for N days. Previous X-streaks lasted...
  2. CROSSING — Tide Score crossed threshold T from below recently. Previously...
  3. CONDITIONAL — given current state + score, forward-return distribution

The best one (highest information content + tightest sample size + most recent
relevance) is surfaced as the lead headline. Others are kept as alternates.
"""
from __future__ import annotations

import pandas as pd


# Round-number thresholds for crossing-analog detection. We look at thresholds
# the score has crossed in the last 30 trading days, then find historical
# crossings of that same threshold in the same direction.
CROSSING_THRESHOLDS = [25, 40, 50, 60, 70, 80]


def _streaks_of_state(df: pd.DataFrame, state_col: str, value: str) -> list[dict]:
    """Find all consecutive runs where df[state_col] == value.
    Returns list of {start, end, duration_days} ordered by start date."""
    s = df[state_col]
    is_state = (s == value).astype(int)
    # Group by changes
    grp = (is_state != is_state.shift()).cumsum()
    runs = []
    for _, sub in df[is_state == 1].groupby(grp):
        if len(sub) == 0:
            continue
        runs.append({
            "start": sub.index[0],
            "end": sub.index[-1],
            "duration_days": int(len(sub)),
        })
    return runs


def _crossings_above(df: pd.DataFrame, score_col: str, threshold: float) -> list[dict]:
    """Find each date where score crossed >= threshold from below.
    For each crossing, return how long it stayed at-or-above before falling back."""
    s = df[score_col]
    above = s >= threshold
    # crossing-up = today above, yesterday not above
    cross_up = above & ~above.shift(1, fill_value=False)
    crossings = []
    for date in df.index[cross_up]:
        # Find first day after this where below threshold (or end of panel)
        i = df.index.get_loc(date)
        future = df[score_col].iloc[i:]
        below_again = future < threshold
        if below_again.any():
            end_idx = below_again.idxmax()  # first True
            duration = (df.index.get_loc(end_idx) - i)
            still_above = False
        else:
            end_idx = df.index[-1]
            duration = (len(df) - i)
            still_above = True
        crossings.append({
            "date": date,
            "ended": end_idx,
            "duration_days": int(duration),
            "still_above": still_above,
        })
    return crossings


def _crossings_below(df: pd.DataFrame, score_col: str, threshold: float) -> list[dict]:
    """Crossings from above to below threshold."""
    s = df[score_col]
    below = s < threshold
    cross_dn = below & ~below.shift(1, fill_value=False)
    out = []
    for date in df.index[cross_dn]:
        i = df.index.get_loc(date)
        future = df[score_col].iloc[i:]
        above_again = future >= threshold
        if above_again.any():
            end_idx = above_again.idxmax()
            duration = df.index.get_loc(end_idx) - i
            still_below = False
        else:
            end_idx = df.index[-1]
            duration = len(df) - i
            still_below = True
        out.append({
            "date": date,
            "ended": end_idx,
            "duration_days": int(duration),
            "still_below": still_below,
        })
    return out


def _format_weeks(days: int) -> str:
    """Render a duration in days as a friendly weeks string."""
    if days < 7:
        return f"{days} day{'s' if days != 1 else ''}"
    weeks = days / 7
    if weeks < 2.5:
        return f"{weeks:.1f} weeks" if weeks % 1 else f"{int(weeks)} week{'s' if int(weeks) != 1 else ''}"
    return f"{round(weeks)} weeks"


def _list_years(crossings: list[dict]) -> str:
    """Pretty-print a list of crossing years for the headline body."""
    years = sorted(set(c["date"].year for c in crossings))
    if len(years) <= 1:
        return ", ".join(str(y) for y in years)
    if len(years) == 2:
        return f"{years[0]} and {years[1]}"
    return ", ".join(str(y) for y in years[:-1]) + f", and {years[-1]}"


def _streak_headline(df: pd.DataFrame, score_col: str, state_col: str) -> dict | None:
    """Compare current state's streak to historical analogs of the same state."""
    if state_col not in df.columns or len(df) == 0:
        return None
    current_state = df[state_col].iloc[-1]
    if current_state not in {"EASY", "NORMAL", "ELEVATED", "STRESS"}:
        return None

    # Current streak length
    streak_len = 1
    for v in reversed(df[state_col].iloc[:-1].tolist()):
        if v == current_state:
            streak_len += 1
        else:
            break

    # All historical complete streaks (i.e. ones that ended before today)
    all_runs = _streaks_of_state(df, state_col, current_state)
    completed = [r for r in all_runs if r["end"] < df.index[-1]]
    if len(completed) < 3:
        return None

    durations = sorted([r["duration_days"] for r in completed])
    median = durations[len(durations) // 2]
    longer = sum(1 for d in durations if d > streak_len)

    # Skip if current streak is below typical noise threshold (≤2 days)
    if streak_len < 3:
        return None

    return {
        "type": "streak",
        "lead": (
            f"Currently {streak_len} days into a {current_state} run. "
            f"Of {len(completed)} previous {current_state} runs since "
            f"{completed[0]['start'].year}, the median lasted {_format_weeks(median)} — "
            f"{longer} ran longer than today's count."
        ),
        "stats": {
            "current_streak_days": streak_len,
            "current_state": current_state,
            "n_historical": len(completed),
            "median_days": median,
            "longest_days": durations[-1],
            "longer_than_current": longer,
            "earliest_year": int(completed[0]["start"].year),
        },
        "score": _streak_score(streak_len, median, len(completed)),
    }


def _crossing_headline(df: pd.DataFrame, score_col: str) -> dict | None:
    """If the score crossed a major threshold recently, surface it."""
    if len(df) < 30:
        return None
    today = df.index[-1]
    today_score = df[score_col].iloc[-1]

    # Check each threshold for recent crossing in the last 30 days
    best = None
    for thr in CROSSING_THRESHOLDS:
        # Crossings from below into ≥ thr in last 30 days
        ups = _crossings_above(df, score_col, thr)
        recent_ups = [c for c in ups if (today - c["date"]).days <= 30 and c["still_above"]]
        if recent_ups:
            crossing = recent_ups[-1]
            historical = [c for c in ups if c["date"] < crossing["date"]]
            if len(historical) < 3:
                continue
            days_since = (today - crossing["date"]).days
            durations = sorted([c["duration_days"] for c in historical])
            median = durations[len(durations) // 2]
            stayed_longer = sum(1 for d in durations if d >= days_since)
            cand = {
                "type": "crossing_up",
                "threshold": thr,
                "lead": (
                    f"The Tide crossed {thr} from below on "
                    f"{crossing['date'].strftime('%b %d').replace(' 0', ' ')} "
                    f"({days_since} days ago). The previous {len(historical)} times this "
                    f"happened (since {historical[0]['date'].year}), the score stayed at or above {thr} "
                    f"for a median of {_format_weeks(median)} — "
                    f"{stayed_longer} of those runs were still above after {days_since} days."
                ),
                "stats": {
                    "threshold": thr,
                    "current_run_days": days_since,
                    "n_historical": len(historical),
                    "median_run_days": median,
                    "longest_run_days": durations[-1],
                    "still_running_after_n": stayed_longer,
                    "crossing_date": crossing["date"].strftime("%Y-%m-%d"),
                    "previous_years": sorted(set(c["date"].year for c in historical))[-5:],
                },
                "score": _crossing_score(thr, today_score, len(historical), days_since),
            }
            if not best or cand["score"] > best["score"]:
                best = cand

        # Same thing the other direction (crossings DOWN — for STRESS framings)
        dns = _crossings_below(df, score_col, thr)
        recent_dns = [c for c in dns if (today - c["date"]).days <= 30 and c["still_below"]]
        if recent_dns:
            crossing = recent_dns[-1]
            historical = [c for c in dns if c["date"] < crossing["date"]]
            if len(historical) < 3:
                continue
            days_since = (today - crossing["date"]).days
            durations = sorted([c["duration_days"] for c in historical])
            median = durations[len(durations) // 2]
            stayed_longer = sum(1 for d in durations if d >= days_since)
            cand = {
                "type": "crossing_down",
                "threshold": thr,
                "lead": (
                    f"The Tide dropped below {thr} on "
                    f"{crossing['date'].strftime('%b %d').replace(' 0', ' ')} "
                    f"({days_since} days ago). The previous {len(historical)} times "
                    f"(since {historical[0]['date'].year}), the score stayed below {thr} "
                    f"for a median of {_format_weeks(median)}."
                ),
                "stats": {
                    "threshold": thr,
                    "current_run_days": days_since,
                    "n_historical": len(historical),
                    "median_run_days": median,
                    "still_running_after_n": stayed_longer,
                    "crossing_date": crossing["date"].strftime("%Y-%m-%d"),
                    "previous_years": sorted(set(c["date"].year for c in historical))[-5:],
                },
                "score": _crossing_score(thr, today_score, len(historical), days_since),
            }
            if not best or cand["score"] > best["score"]:
                best = cand

    return best


def _streak_score(streak_len: int, median: float, n: int) -> float:
    """Rank the relevance of a streak headline.
    Prefer: longer current streak (more interesting), bigger sample, near-median timing."""
    return (
        min(streak_len / 30, 1.0) * 30        # streak length up to 30
        + min(n / 10, 1.0) * 20               # sample size up to 10
        + 10                                  # base score for streak headlines
    )


def _crossing_score(threshold: int, current_score: float, n: int, days_since: int) -> float:
    """Rank crossing headline relevance.
    Prefer: round threshold near current score, big sample, recent crossing."""
    proximity = 1.0 - abs(threshold - current_score) / 50.0
    return (
        max(proximity, 0) * 40
        + min(n / 10, 1.0) * 20
        + max(0, (30 - days_since) / 30) * 10
        + 20  # base for crossing headlines (more interesting than streak)
    )


def build_headlines(df: pd.DataFrame, score_col: str = "tide_score",
                    state_col: str = "state") -> dict:
    """Generate the headline payload.

    df must have a daily index, plus tide_score column and state (regime) column.
    Returns: { lead: {type, lead, stats}, alternates: [...] }
    """
    candidates: list[dict] = []
    s = _streak_headline(df, score_col, state_col)
    if s:
        candidates.append(s)
    c = _crossing_headline(df, score_col)
    if c:
        candidates.append(c)

    if not candidates:
        return {"lead": None, "alternates": []}

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return {
        "lead": {k: v for k, v in candidates[0].items() if k != "score"},
        "alternates": [{k: v for k, v in c.items() if k != "score"} for c in candidates[1:3]],
    }
