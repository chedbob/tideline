"""Phase 3c — candidate_v4: pure-vol escape bypasses dwell."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from rule._archive.candidate_v4 import TransitionContext, pure_vol_escape, primary_transition, GO_CRITERION
from backtest.phase3_state_machine import (
    build_panel, add_features, compute_rolling_percentiles,
    evaluate_events, evaluate_stress_edge, summarize_transitions,
)

DWELL_DEFAULT = 3
DWELL_NORMAL_TO_EASY = 20


def run_v4(df, pct):
    states, triggers = [], []
    current, dwell = "NORMAL", 0

    for i, (date, row) in enumerate(df.iterrows()):
        def p(key, lvl):
            v = pct[key][lvl].iloc[i] if i < len(pct[key][lvl]) else None
            return float(v) if v is not None and not pd.isna(v) else 0

        pct_hy5 = {x: p("hy_5d", x) for x in pct["hy_5d"]}
        pct_v3 = {x: p("vix_3d", x) for x in pct["vix_3d"]}
        pct_v5 = {x: p("vix_5d", x) for x in pct["vix_5d"]}
        pct_hyl = {x: p("hy_level", x) for x in pct["hy_level"]}
        pct_vxl = {x: p("vix_level", x) for x in pct["vix_level"]}

        if pct_hy5.get(85, 0) == 0 and i < 252:
            states.append(current); triggers.append(None); dwell += 1; continue
        if pd.isna(row["nfci_lagged"]) or pd.isna(row["nfci_1w_change"]):
            states.append(current); triggers.append(None); dwell += 1; continue

        v5chg = row["vix_5d_change"] if not pd.isna(row["vix_5d_change"]) else 0
        v5ago = row["vix"] - v5chg

        ctx = TransitionContext(
            hy_oas=row["hy_oas"], vix=row["vix"],
            vix3m=row["vix3m"] if not pd.isna(row["vix3m"]) else None,
            curve_3m10y=row["curve_3m10y"], nfci_lagged=row["nfci_lagged"],
            hy_oas_5d_change=row["hy_oas_5d_change"] if not pd.isna(row["hy_oas_5d_change"]) else 0,
            vix_1d_change=row["vix_1d_change"] if not pd.isna(row["vix_1d_change"]) else 0,
            vix_3d_change=row["vix_3d_change"] if not pd.isna(row["vix_3d_change"]) else 0,
            vix_5d_change=v5chg, nfci_1w_change=row["nfci_1w_change"] if not pd.isna(row["nfci_1w_change"]) else 0,
            vix_5d_ago=v5ago, days_in_state=dwell,
            pct_hy_5d=pct_hy5, pct_vix_3d=pct_v3, pct_vix_5d=pct_v5,
            pct_hy_level=pct_hyl, pct_vix_level=pct_vxl,
        )

        # Pure-vol escape always checked first (bypasses dwell)
        new_state = pure_vol_escape(current, ctx)
        trigger = None
        if new_state and new_state != current:
            trigger = f"pure_vol_escape_to_{new_state}"
        else:
            # Primary transition subject to dwell
            # Asymmetric dwell: NORMAL->EASY gets 20 days, everything else 3
            required_dwell = DWELL_DEFAULT
            primary_candidate = primary_transition(current, ctx)
            if current == "NORMAL" and primary_candidate == "EASY":
                required_dwell = DWELL_NORMAL_TO_EASY

            if dwell >= required_dwell and primary_candidate and primary_candidate != current:
                new_state = primary_candidate
                trigger = f"primary_to_{new_state}"
            else:
                new_state = current

        if new_state != current:
            dwell = 1
            current = new_state
        else:
            dwell += 1
        states.append(current)
        triggers.append(trigger)

    df = df.copy(); df["state"] = states; df["trigger"] = triggers
    return df


def main():
    api_key = os.environ["FRED_API_KEY"]
    df = build_panel(api_key)
    df = add_features(df)
    pct = compute_rolling_percentiles(df)
    print("[phase3c] running v4...")
    out = run_v4(df, pct)

    events = evaluate_events(out, {
        "volmageddon_2018_02_05":     {"must_reach_by_day": 3, "state_floor": "WATCH"},
        "credit_drawdown_2018_12_17": {"must_reach_by_day": 5, "state_floor": "WATCH"},
        "covid_2020_03_09":           {"must_reach_by_day": 5, "state_floor": "STRESS"},
        "tariff_shock_2025_04_02":    {"must_reach_by_day": 5, "state_floor": "STRESS"},
    })
    trans = summarize_transitions(out)
    edge = evaluate_stress_edge(out)

    print("\n=== Phase 3c — candidate_v4 ===\n")
    print(f"Panel: {out.index.min().date()} to {out.index.max().date()}, n={len(out):,}\n")
    print(f"--- Transitions ---")
    print(f"  Total: {trans['total_transitions']}  per_year: {trans['transitions_per_year']:.1f}")
    print(f"  occupation: {trans['state_occupation_pct']}")
    print(f"  by trigger: {trans['transitions_by_type']}\n")
    print(f"--- Events ---")
    for n, e in events.items():
        s = "PASS" if e.get("passed") else "FAIL"
        print(f"  {n:35}  reached={e.get('reached','never')} by day {e.get('reached_day','n/a')}  [{s}]")
    passing = sum(1 for e in events.values() if e.get("passed"))
    vol_ok = events.get("volmageddon_2018_02_05", {}).get("reached_day") is not None and \
             events.get("volmageddon_2018_02_05", {}).get("reached_day") <= 3
    tpy = trans["transitions_per_year"]
    tpy_ok = 6 <= tpy <= 40

    print(f"\n--- STRESS descriptive (not a product claim) ---")
    if "error" not in edge:
        print(f"  n={edge['n_stress_days']}  stress_down={edge['stress_down_rate']:.3f}  base={edge['baseline_down_rate']:.3f}  edge={edge['edge_pp']:+.1f}pp")
        print(f"  block bootstrap CI [{edge['block_bootstrap_ci_95'][0]:.3f}, {edge['block_bootstrap_ci_95'][1]:.3f}]")

    print(f"\n--- Go decision ---")
    pass_overall = passing >= 3 and vol_ok and tpy_ok
    print(f"  events_passing={passing}  volmageddon_ok={vol_ok}  tpy={tpy:.1f}  tpy_ok={tpy_ok}")
    print(f"  OVERALL_PASS: {pass_overall}")

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase3c_v4_result.json").write_text(json.dumps({
        "rule": "candidate_v4", "transitions": trans, "events": events,
        "stress_state_descriptive": edge,
        "go_overall_pass": pass_overall,
    }, indent=2, default=str))
    out[["state","trigger","spy","vix","hy_oas"]].to_csv(out_dir / "phase3c_v4_history.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
