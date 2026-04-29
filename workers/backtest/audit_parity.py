"""Audit #1: Parity check.

Run both candidate_v4 and rule/v1 state machines on the same panel.
Expected: IDENTICAL transitions except state name WATCH <-> ELEVATED.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import httpx
import pandas as pd

from rule._archive import candidate_v4
from rule import v1 as rule_v1
from backtest.phase3_state_machine import build_panel as build_panel_v4, add_features, compute_rolling_percentiles
from backtest.phase3c_v4 import run_v4 as run_candidate_v4
from compute.regime import run_state_machine as run_v1_machine


def main():
    api_key = os.environ["FRED_API_KEY"]
    df = build_panel_v4(api_key)
    df = add_features(df)
    pct = compute_rolling_percentiles(df)

    # Need ma_50 and ma_200 for v1 pipeline (it's in compute/regime.py build_panel,
    # but phase3 doesn't compute them — not needed for state machine)
    # Actually rule/v1.py state machine doesn't use ma columns, so phase3's df is fine.

    print("[audit] running candidate_v4...")
    states_v4 = run_candidate_v4(df, pct)["state"]

    print("[audit] running v1...")
    # Need to add the ma columns that regime.py expects; actually regime.py's
    # run_state_machine only looks at the raw features, not ma_50/ma_200.
    # But add to make sure column references work.
    if "ma_50" not in df.columns:
        df["ma_50"] = df["spy"].rolling(50).mean()
    if "ma_200" not in df.columns:
        df["ma_200"] = df["spy"].rolling(200).mean()
    states_v1 = run_v1_machine(df, pct)["state"]

    # Rename WATCH -> ELEVATED in v4 for comparison (candidate_v4 still uses WATCH)
    states_v4_renamed = states_v4.replace({"WATCH": "ELEVATED"})

    # Compare
    matches = (states_v4_renamed == states_v1).sum()
    total = len(states_v1)
    diffs_idx = states_v4_renamed[states_v4_renamed != states_v1].index

    print(f"\n=== PARITY RESULT ===")
    print(f"Panel size: {total:,}")
    print(f"Identical state: {matches:,}  ({matches/total*100:.2f}%)")
    print(f"Divergent:       {len(diffs_idx):,}")

    if len(diffs_idx) > 0:
        print(f"\nFirst 10 divergences:")
        for d in diffs_idx[:10]:
            print(f"  {d.date()}: v4={states_v4_renamed.loc[d]:10} v1={states_v1.loc[d]}")

    if matches == total:
        print("\nVERDICT: PASS — v1 is bit-identical to candidate_v4 (after WATCH->ELEVATED rename)")
    else:
        print(f"\nVERDICT: FAIL — {len(diffs_idx)} divergences. Regression introduced during port.")
    return 0 if matches == total else 1


if __name__ == "__main__":
    sys.exit(main())
