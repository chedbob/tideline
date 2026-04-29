"""Phase 3b — Rerun state machine with candidate_v3 rules.

v3 fixes 3 bugs from v2 (see rule/candidate_v3.py and research_log.md Entry 9):
  1. EASY -> WATCH shock rule now uses vix_5d_ago instead of today's VIX
  2. Pure-vol escape added from EASY state
  3. STRESS -> WATCH recovery relaxed (OR on relief signals) + 60d auto-downgrade failsafe
  4. WATCH -> NORMAL no longer gates on NFCI
  5. NORMAL -> EASY dwell extended to 20 days

Same data + same feature computation as phase3_state_machine.py — only the rule changes.
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

from rule._archive.candidate_v3 import TransitionContext, evaluate_transition, GO_CRITERION
from backtest.phase3_state_machine import (
    build_panel, add_features, compute_rolling_percentiles,
    evaluate_events, evaluate_stress_edge, summarize_transitions,
)

DWELL_DAYS_DEFAULT = 3
DWELL_DAYS_NORMAL_TO_EASY = 20


def run_state_machine_v3(df, pct):
    states = []
    triggers = []
    current_state = "NORMAL"
    days_in_state = 0

    for i, (date, row) in enumerate(df.iterrows()):
        def p(pct_key, pct_level):
            val = pct[pct_key][pct_level].iloc[i] if i < len(pct[pct_key][pct_level]) else None
            return float(val) if val is not None and not pd.isna(val) else 0

        pct_hy_5d = {x: p("hy_5d", x) for x in pct["hy_5d"]}
        pct_vix_3d = {x: p("vix_3d", x) for x in pct["vix_3d"]}
        pct_vix_5d = {x: p("vix_5d", x) for x in pct["vix_5d"]}
        pct_hy_level = {x: p("hy_level", x) for x in pct["hy_level"]}
        pct_vix_level = {x: p("vix_level", x) for x in pct["vix_level"]}

        if pct_hy_5d.get(85, 0) == 0 and i < 252:
            states.append(current_state)
            triggers.append(None)
            days_in_state += 1
            continue

        if pd.isna(row["nfci_lagged"]) or pd.isna(row["nfci_1w_change"]):
            states.append(current_state)
            triggers.append(None)
            days_in_state += 1
            continue

        vix_5d_change = row["vix_5d_change"] if not pd.isna(row["vix_5d_change"]) else 0
        vix_5d_ago = row["vix"] - vix_5d_change

        ctx = TransitionContext(
            hy_oas=row["hy_oas"],
            vix=row["vix"],
            vix3m=row["vix3m"] if not pd.isna(row["vix3m"]) else None,
            curve_3m10y=row["curve_3m10y"],
            nfci_lagged=row["nfci_lagged"],
            hy_oas_5d_change=row["hy_oas_5d_change"] if not pd.isna(row["hy_oas_5d_change"]) else 0,
            vix_1d_change=row["vix_1d_change"] if not pd.isna(row["vix_1d_change"]) else 0,
            vix_3d_change=row["vix_3d_change"] if not pd.isna(row["vix_3d_change"]) else 0,
            vix_5d_change=vix_5d_change,
            nfci_1w_change=row["nfci_1w_change"] if not pd.isna(row["nfci_1w_change"]) else 0,
            vix_5d_ago=vix_5d_ago,
            days_in_state=days_in_state,
            pct_hy_5d=pct_hy_5d,
            pct_vix_3d=pct_vix_3d,
            pct_vix_5d=pct_vix_5d,
            pct_hy_level=pct_hy_level,
            pct_vix_level=pct_vix_level,
        )

        # Dwell: NORMAL -> EASY takes 20 days dwell, all others 3 days
        required_dwell = DWELL_DAYS_NORMAL_TO_EASY if current_state == "NORMAL" else DWELL_DAYS_DEFAULT
        if days_in_state < required_dwell:
            new_state, trigger = current_state, None
        else:
            new_state, trigger = evaluate_transition(current_state, ctx)

        if new_state != current_state:
            days_in_state = 1
            current_state = new_state
        else:
            days_in_state += 1

        states.append(current_state)
        triggers.append(trigger)

    df = df.copy()
    df["state"] = states
    df["trigger"] = triggers
    return df


def main():
    api_key = os.environ["FRED_API_KEY"]
    df = build_panel(api_key)
    df = add_features(df)
    pct = compute_rolling_percentiles(df)

    print("[phase3b] running state machine v3...")
    df_with_states = run_state_machine_v3(df, pct)

    event_dates = {
        "volmageddon_2018_02_05":       {"must_reach_by_day": 3, "state_floor": "WATCH"},
        "credit_drawdown_2018_12_17":   {"must_reach_by_day": 5, "state_floor": "WATCH"},
        "covid_2020_03_09":             {"must_reach_by_day": 5, "state_floor": "STRESS"},
        "tariff_shock_2025_04_02":      {"must_reach_by_day": 5, "state_floor": "STRESS"},
    }
    events = evaluate_events(df_with_states, event_dates)
    transitions = summarize_transitions(df_with_states)
    edge = evaluate_stress_edge(df_with_states)

    events_passing = sum(1 for e in events.values() if e.get("passed"))
    volmageddon_ok = events.get("volmageddon_2018_02_05", {}).get("reached_day") is not None and \
                     events.get("volmageddon_2018_02_05", {}).get("reached_day") <= 3
    tpy = transitions["transitions_per_year"]
    tpy_ok = 6 <= tpy <= 40

    print("\n=== Phase 3b State Machine — candidate_v3 ===\n")
    print(f"Panel: {df.index.min().date()} to {df.index.max().date()}, n={len(df):,}\n")
    print(f"--- Transitions ---")
    print(f"  Total: {transitions['total_transitions']}")
    print(f"  Per year: {tpy:.1f}")
    print(f"  State occupation: {transitions['state_occupation_pct']}")
    print(f"  By trigger type: {transitions['transitions_by_type']}\n")

    print(f"--- Historical events ---")
    for name, e in events.items():
        s = "PASS" if e.get("passed") else "FAIL"
        reached = e.get("reached", "never")
        day = e.get("reached_day", "n/a")
        print(f"  {name:35}  event_date={e.get('event_date','?')}  reached={reached} by day {day}  [{s}]")
    print()

    print(f"--- STRESS-state 20D edge (descriptive only, not a product claim in v3) ---")
    if "error" in edge:
        print(f"  {edge['error']}")
    else:
        print(f"  n={edge['n_stress_days']:,}  stress_avg_20d={edge['stress_avg_20d_return_pct']:+.2f}%  stress_down={edge['stress_down_rate']:.3f}  baseline_down={edge['baseline_down_rate']:.3f}  edge={edge['edge_pp']:+.1f}pp")
        print(f"  Block bootstrap CI: [{edge['block_bootstrap_ci_95'][0]:.3f}, {edge['block_bootstrap_ci_95'][1]:.3f}]  excludes_baseline={edge['bootstrap_excludes_baseline']}")
    print()

    print(f"--- Go decision (v3 criterion: events + transition rate; NO stress-edge required) ---")
    go_v3 = {
        "events_passing": events_passing,
        "volmageddon_special_met": volmageddon_ok,
        "transitions_per_year": tpy,
        "tpy_in_range": tpy_ok,
        "OVERALL_PASS": events_passing >= GO_CRITERION["min_events_passing"] and volmageddon_ok and tpy_ok,
    }
    for k, v in go_v3.items():
        print(f"  {k}: {v}")

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    payload = {
        "rule_version": "candidate_v3",
        "panel": {"start": str(df.index.min().date()), "end": str(df.index.max().date()), "n": len(df)},
        "transitions": transitions,
        "historical_events": events,
        "stress_state_descriptive": edge,
        "go_decision_v3": go_v3,
    }
    (out_dir / "phase3b_v3_result.json").write_text(json.dumps(payload, indent=2, default=str))
    df_with_states[["state", "trigger", "spy", "hy_oas", "vix", "vix3m"]].to_csv(out_dir / "phase3b_v3_history.csv")
    print(f"\nArtifacts: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
