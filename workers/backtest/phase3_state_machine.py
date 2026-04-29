"""Phase 3 — Full state-machine backtest for candidate_v2 rules.

Pre-committed rules in workers/rule/candidate_v2.py.
Pre-committed go criterion in the same file.
Committed to git BEFORE this runs so any tweak is visible in diffs.

Handles:
  - NFCI T+5 release lag (backtest uses only info available at timestamp)
  - VIX3M only post-2007 (pre-2007 uses 63-day SMA of VIX as proxy; flagged in log)
  - Rolling 5-year (1260 trading day) percentile thresholds, excluding today
  - 3-day minimum dwell between transitions
  - Block bootstrap on STRESS-state 20D forward SPY DOWN rate

Usage:
  cd workers
  set -a; source .env; set +a
  python -m backtest.phase3_state_machine
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from rule._archive.candidate_v2 import TransitionContext, evaluate_transition, GO_CRITERION

START = "1997-01-01"
PERCENTILE_WINDOW_DAYS = 1260  # 5 years trading days
DWELL_DAYS = 3
NFCI_LAG_DAYS = 5  # NFCI for week ending Friday releases following Wednesday
HORIZON = 20       # SPY 20D forward for STRESS-state edge check
N_BOOT = 10_000
BLOCK_LEN = 60
RNG_SEED = 42


def fetch_fred(client, sid, key, retries=3):
    for attempt in range(retries):
        try:
            r = client.get("https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": sid, "api_key": key, "file_type": "json",
                        "observation_start": START, "sort_order": "asc"}, timeout=30)
            r.raise_for_status()
            recs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
            if not recs:
                return pd.Series(dtype=float, name=sid)
            d, v = zip(*recs)
            return pd.Series(v, index=pd.to_datetime(d), name=sid)
        except Exception as e:
            if attempt == retries - 1:
                raise
            print(f"[phase3]   retry {attempt+1} for {sid}: {e}")
            time.sleep(2)


def fetch_yahoo(client, ticker, start=START):
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    s = pd.Series(data["indicators"]["quote"][0]["close"], index=ts, name=ticker).dropna()
    return s[~s.index.duplicated(keep="last")]


def build_panel(api_key):
    print("[phase3] fetching data...")
    with httpx.Client() as fc, httpx.Client() as yc:
        hy_oas = fetch_fred(fc, "BAA10Y", api_key)
        vix = fetch_fred(fc, "VIXCLS", api_key)
        curve = fetch_fred(fc, "T10Y3M", api_key)
        nfci = fetch_fred(fc, "NFCI", api_key)
        spy = fetch_yahoo(yc, "SPY")
        vix3m_yahoo = fetch_yahoo(yc, "^VIX3M")

    df = pd.DataFrame({"spy": spy})
    df["hy_oas"] = hy_oas.reindex(df.index, method="ffill")
    df["vix"] = vix.reindex(df.index, method="ffill")
    df["curve_3m10y"] = curve.reindex(df.index, method="ffill")

    # NFCI is weekly, released T+5. Shift by 5 trading days to enforce release lag.
    nfci_daily = nfci.reindex(df.index, method="ffill")
    df["nfci_lagged"] = nfci_daily.shift(NFCI_LAG_DAYS)

    # VIX3M: use Yahoo where available, fallback to 63-day SMA of VIX before 2007-09-18
    vix3m_daily = vix3m_yahoo.reindex(df.index)
    vix3m_proxy = df["vix"].rolling(63, min_periods=20).mean()
    df["vix3m"] = vix3m_daily.combine_first(vix3m_proxy)
    df["vix3m_source"] = "yahoo"
    mask_proxy = vix3m_daily.isna() & vix3m_proxy.notna()
    df.loc[mask_proxy, "vix3m_source"] = "sma63_proxy"

    df = df.dropna(subset=["spy", "hy_oas", "vix", "curve_3m10y"])
    print(f"[phase3]   panel: {df.shape}, {df.index.min().date()} to {df.index.max().date()}")
    return df


def add_features(df):
    df = df.copy()
    df["hy_oas_5d_change"] = df["hy_oas"].diff(5)
    df["vix_1d_change"] = df["vix"].diff(1)
    df["vix_3d_change"] = df["vix"].diff(3)
    df["vix_5d_change"] = df["vix"].diff(5)
    df["nfci_1w_change"] = df["nfci_lagged"].diff(5)
    return df


def compute_rolling_percentiles(df):
    """Rolling 5y percentiles, computed from PRIOR data only (shift by 1)."""
    perc_levels = [10, 15, 25, 30, 50, 70, 75, 85, 90]

    def make_percentiles(series, name):
        out = {}
        shifted = series.shift(1)  # exclude today
        for p in perc_levels:
            out[p] = shifted.rolling(PERCENTILE_WINDOW_DAYS, min_periods=252).quantile(p/100)
        return {p: out[p].rename(f"{name}_p{p}") for p in perc_levels}

    pct = {
        "hy_5d": make_percentiles(df["hy_oas_5d_change"], "hy_5d"),
        "vix_3d": make_percentiles(df["vix_3d_change"], "vix_3d"),
        "vix_5d": make_percentiles(df["vix_5d_change"], "vix_5d"),
        "hy_level": make_percentiles(df["hy_oas"], "hy_level"),
        "vix_level": make_percentiles(df["vix"], "vix_level"),
    }
    return pct


def run_state_machine(df, pct):
    """Run the state machine day by day. Returns DataFrame with state + transition log."""
    states = []
    triggers = []
    current_state = "NORMAL"
    days_in_state = 0
    last_transition_date = None

    for i, (date, row) in enumerate(df.iterrows()):
        # Build TransitionContext
        def safe_pct(p_dict, p, i):
            return p_dict[p].iloc[i] if i < len(p_dict[p]) else None

        pct_hy_5d = {p: float(pct["hy_5d"][p].iloc[i]) if not pd.isna(pct["hy_5d"][p].iloc[i]) else 0 for p in pct["hy_5d"]}
        pct_vix_3d = {p: float(pct["vix_3d"][p].iloc[i]) if not pd.isna(pct["vix_3d"][p].iloc[i]) else 0 for p in pct["vix_3d"]}
        pct_vix_5d = {p: float(pct["vix_5d"][p].iloc[i]) if not pd.isna(pct["vix_5d"][p].iloc[i]) else 0 for p in pct["vix_5d"]}
        pct_hy_level = {p: float(pct["hy_level"][p].iloc[i]) if not pd.isna(pct["hy_level"][p].iloc[i]) else 0 for p in pct["hy_level"]}
        pct_vix_level = {p: float(pct["vix_level"][p].iloc[i]) if not pd.isna(pct["vix_level"][p].iloc[i]) else 0 for p in pct["vix_level"]}

        # Not enough history yet for percentiles
        if pct_hy_5d[85] == 0 and i < 252:
            states.append(current_state)
            triggers.append(None)
            days_in_state += 1
            continue

        # Skip if nfci_lagged is NaN (early in panel)
        if pd.isna(row["nfci_lagged"]) or pd.isna(row["nfci_1w_change"]):
            states.append(current_state)
            triggers.append(None)
            days_in_state += 1
            continue

        ctx = TransitionContext(
            hy_oas=row["hy_oas"],
            vix=row["vix"],
            vix3m=row["vix3m"] if not pd.isna(row["vix3m"]) else None,
            curve_3m10y=row["curve_3m10y"],
            nfci_lagged=row["nfci_lagged"],
            hy_oas_5d_change=row["hy_oas_5d_change"] if not pd.isna(row["hy_oas_5d_change"]) else 0,
            vix_1d_change=row["vix_1d_change"] if not pd.isna(row["vix_1d_change"]) else 0,
            vix_3d_change=row["vix_3d_change"] if not pd.isna(row["vix_3d_change"]) else 0,
            vix_5d_change=row["vix_5d_change"] if not pd.isna(row["vix_5d_change"]) else 0,
            nfci_1w_change=row["nfci_1w_change"] if not pd.isna(row["nfci_1w_change"]) else 0,
            pct_hy_5d=pct_hy_5d,
            pct_vix_3d=pct_vix_3d,
            pct_vix_5d=pct_vix_5d,
            pct_hy_level=pct_hy_level,
            pct_vix_level=pct_vix_level,
        )

        # Enforce dwell
        if days_in_state < DWELL_DAYS:
            new_state, trigger = current_state, None
        else:
            new_state, trigger = evaluate_transition(current_state, ctx)

        if new_state != current_state:
            days_in_state = 1
            last_transition_date = date
            current_state = new_state
        else:
            days_in_state += 1

        states.append(current_state)
        triggers.append(trigger)

    df = df.copy()
    df["state"] = states
    df["trigger"] = triggers
    return df


def evaluate_events(df, events):
    results = {}
    for event_name, event_spec in events.items():
        event_date_str = event_name.rsplit("_", 3)[-3:]
        event_date = pd.Timestamp("-".join(event_date_str))
        if event_date not in df.index:
            # Find nearest
            nearest = df.index[df.index >= event_date]
            if len(nearest) == 0:
                results[event_name] = {"error": "date not in panel"}
                continue
            event_date = nearest[0]

        window = df.loc[event_date:].head(event_spec["must_reach_by_day"] + 1)
        reached = None
        reached_day = None
        state_order = {"EASY": 0, "NORMAL": 1, "WATCH": 2, "STRESS": 3}
        floor = state_order[event_spec["state_floor"]]
        for day_offset, (d, row) in enumerate(window.iterrows()):
            if state_order[row["state"]] >= floor:
                reached = row["state"]
                reached_day = day_offset
                break

        results[event_name] = {
            "event_date": str(event_date.date()),
            "must_reach_by_day": event_spec["must_reach_by_day"],
            "state_floor": event_spec["state_floor"],
            "reached": reached,
            "reached_day": reached_day,
            "passed": reached is not None,
            "states_in_window": [{"day": i, "date": str(d.date()), "state": row["state"]}
                                   for i, (d, row) in enumerate(window.iterrows())],
        }
    return results


def wilson(h, n, z=1.96):
    if n == 0:
        return (0, 0)
    p = h/n
    d = 1 + z*z/n
    c = (p + z*z/(2*n))/d
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return (c-half, c+half)


def block_bootstrap_down_rate(outcomes, block_len, n_iter, rng):
    n = len(outcomes)
    n_blocks = int(np.ceil(n / block_len))
    results = np.empty(n_iter)
    for i in range(n_iter):
        starts = rng.integers(0, n - block_len + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]
        results[i] = (outcomes[idx] == 0).mean()  # DOWN rate (outcome=0 = DOWN)
    return results


def evaluate_stress_edge(df):
    fwd = df["spy"].shift(-HORIZON) / df["spy"] - 1
    actual_up = (fwd > 0).astype(int)

    paired = pd.concat({"state": df["state"], "up": actual_up, "fwd": fwd}, axis=1).dropna()
    baseline_up = paired["up"].mean()
    baseline_down = 1 - baseline_up

    stress_days = paired[paired["state"] == "STRESS"]
    if len(stress_days) < 30:
        return {"error": f"too few STRESS days ({len(stress_days)})"}

    stress_down_rate = 1 - stress_days["up"].mean()
    stress_avg_fwd = stress_days["fwd"].mean() * 100

    hits_down = (stress_days["up"] == 0).sum()
    n = len(stress_days)
    w_lo, w_hi = wilson(hits_down, n)

    rng = np.random.default_rng(RNG_SEED)
    boot = block_bootstrap_down_rate(stress_days["up"].to_numpy(), BLOCK_LEN, N_BOOT, rng)

    return {
        "n_stress_days": n,
        "stress_avg_20d_return_pct": float(stress_avg_fwd),
        "baseline_down_rate": float(baseline_down),
        "stress_down_rate": float(stress_down_rate),
        "edge_pp": float((stress_down_rate - baseline_down) * 100),
        "wilson_ci_95": [float(w_lo), float(w_hi)],
        "block_bootstrap_ci_95": [float(np.quantile(boot, 0.025)), float(np.quantile(boot, 0.975))],
        "block_bootstrap_mean": float(boot.mean()),
        "bootstrap_excludes_baseline": bool(np.quantile(boot, 0.025) > baseline_down),
    }


def summarize_transitions(df):
    diffs = df["state"].ne(df["state"].shift())
    transitions = df[diffs]
    n_transitions = len(transitions) - 1  # first is the starting state
    years = (df.index.max() - df.index.min()).days / 365.25
    return {
        "total_transitions": n_transitions,
        "transitions_per_year": n_transitions / years if years else 0,
        "state_occupation_pct": df["state"].value_counts(normalize=True).round(3).to_dict(),
        "transitions_by_type": df["trigger"].value_counts().to_dict(),
    }


def main():
    api_key = os.environ["FRED_API_KEY"]
    df = build_panel(api_key)
    df = add_features(df)
    pct = compute_rolling_percentiles(df)

    print("[phase3] running state machine...")
    df_with_states = run_state_machine(df, pct)

    print("[phase3] evaluating historical events...")
    # Note: pre-committed event dates
    event_dates = {
        "volmageddon_2018_02_05":       {"must_reach_by_day": 3, "state_floor": "WATCH"},
        "credit_drawdown_2018_12_17":   {"must_reach_by_day": 5, "state_floor": "WATCH"},
        "covid_2020_03_09":             {"must_reach_by_day": 5, "state_floor": "STRESS"},
        "tariff_shock_2025_04_02":      {"must_reach_by_day": 5, "state_floor": "STRESS"},
    }
    events = evaluate_events(df_with_states, event_dates)

    print("[phase3] summarizing transitions...")
    transitions = summarize_transitions(df_with_states)

    print("[phase3] evaluating STRESS-state edge (block bootstrap)...")
    edge = evaluate_stress_edge(df_with_states)

    # Pre-committed go criterion
    events_passing = sum(1 for e in events.values() if e.get("passed"))
    volmageddon_ok = events.get("volmageddon_2018_02_05", {}).get("reached_day") is not None and \
                     events.get("volmageddon_2018_02_05", {}).get("reached_day") <= 3
    tpy = transitions["transitions_per_year"]
    tpy_ok = 6 <= tpy <= 30
    edge_ok = edge.get("bootstrap_excludes_baseline", False) if isinstance(edge, dict) else False

    go_decision = {
        "events_passing": events_passing,
        "events_required": GO_CRITERION["min_events_passing"],
        "volmageddon_special_met": volmageddon_ok,
        "transitions_per_year": tpy,
        "tpy_in_range": tpy_ok,
        "stress_edge_bootstrap_excludes_baseline": edge_ok,
        "OVERALL_PASS": events_passing >= GO_CRITERION["min_events_passing"] and volmageddon_ok and tpy_ok and edge_ok,
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule_version": "candidate_v2",
        "panel": {"start": str(df.index.min().date()), "end": str(df.index.max().date()), "n": len(df)},
        "transitions": transitions,
        "historical_events": events,
        "stress_state_edge": edge,
        "go_decision": go_decision,
    }

    # Report
    print("\n\n=== Phase 3 State Machine Backtest ===\n")
    print(f"Rule version: candidate_v2 (pre-committed)")
    print(f"Panel: {df.index.min().date()} to {df.index.max().date()}, n={len(df):,}\n")

    print("--- Transitions ---")
    print(f"  Total: {transitions['total_transitions']}")
    print(f"  Per year: {transitions['transitions_per_year']:.1f}  (target range 6-30)")
    print(f"  State occupation: {transitions['state_occupation_pct']}")
    print(f"  By trigger type: {transitions['transitions_by_type']}\n")

    print("--- Historical events ---")
    for name, e in events.items():
        status = "PASS" if e.get("passed") else "FAIL"
        reached = e.get("reached", "never")
        day = e.get("reached_day", "n/a")
        print(f"  {name:35}  event_date={e.get('event_date','?')}  reached={reached} by day {day}  [{status}]")
    print()

    print("--- STRESS-state 20D edge ---")
    if "error" in edge:
        print(f"  {edge['error']}")
    else:
        print(f"  n={edge['n_stress_days']:,}")
        print(f"  avg 20D SPY return in STRESS: {edge['stress_avg_20d_return_pct']:+.2f}%")
        print(f"  stress DOWN rate: {edge['stress_down_rate']:.3f}  baseline DOWN: {edge['baseline_down_rate']:.3f}")
        print(f"  edge: {edge['edge_pp']:+.1f} pp")
        print(f"  Wilson 95% CI: [{edge['wilson_ci_95'][0]:.3f}, {edge['wilson_ci_95'][1]:.3f}]")
        print(f"  Block bootstrap 95% CI: [{edge['block_bootstrap_ci_95'][0]:.3f}, {edge['block_bootstrap_ci_95'][1]:.3f}]")
        print(f"  bootstrap excludes baseline? {edge['bootstrap_excludes_baseline']}")
    print()

    print("--- Go Decision ---")
    for k, v in go_decision.items():
        print(f"  {k}: {v}")

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase3_state_machine.json").write_text(json.dumps(payload, indent=2, default=str))

    # Also save the full state history for inspection
    df_with_states[["state", "trigger", "spy", "hy_oas", "vix", "vix3m", "nfci_lagged"]].to_csv(out_dir / "phase3_state_history.csv")
    print(f"\nArtifacts: {out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
