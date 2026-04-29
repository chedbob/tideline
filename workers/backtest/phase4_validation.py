"""Phase 4 — Honest validation of candidate_v4.

After iterating v2→v3→v4 on the same data, we need:
  1. Split-sample robustness: does v4 behave consistently on 1997-2015 vs 2016-2026?
     If metrics are stable across splits, rule structure generalizes.
     If they diverge, we over-fit to one period.
  2. False-positive sweep: confusion matrix of v4 STRESS vs ground-truth stress.
     Ground truth = days where ANY of:
       - SPY 10-day drawdown > 5%
       - VIX > 30 for 3+ consecutive days
       - BAA10Y > its rolling 5y p90 (true credit stress episode)
  3. Near-miss events we did NOT preregister (honest OOS):
       - Aug 5-9 2011 (S&P US downgrade)
       - Aug 24 2015 (China devaluation)
       - Apr 22 2022 (yen intervention rumors)
       - Oct 2022 (UK gilt crisis)
       - Mar 10-13 2023 (SVB banking panic)
     These were NOT in our iteration loop. Did v4 respond appropriately?

Reuses phase3c_v4 state machine.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from rule._archive.candidate_v4 import TransitionContext, pure_vol_escape, primary_transition
from backtest.phase3_state_machine import build_panel, add_features, compute_rolling_percentiles
from backtest.phase3c_v4 import run_v4


SPLIT_DATE = "2015-12-31"


def split_sample_analysis(df_states):
    train = df_states[df_states.index <= SPLIT_DATE]
    test = df_states[df_states.index > SPLIT_DATE]

    def summarize(sub, name):
        n = len(sub)
        if n == 0:
            return None
        transitions = (sub["state"] != sub["state"].shift()).sum() - 1
        years = (sub.index.max() - sub.index.min()).days / 365.25
        occ = sub["state"].value_counts(normalize=True).round(3).to_dict()
        return {
            "name": name,
            "n_days": n,
            "years": round(years, 2),
            "transitions": int(transitions),
            "per_year": round(transitions / years, 2) if years else None,
            "occupation": occ,
        }

    return {
        "train_1997_2015": summarize(train, "train"),
        "test_2016_2026":  summarize(test, "test"),
    }


def ground_truth_stress(df):
    """Binary 'is there real stress today?' label using three objective criteria.
    No future info: each criterion uses only data up to T."""
    dd_10d = df["spy"] / df["spy"].rolling(10, min_periods=1).max() - 1

    # SPY drawdown > 5% anchored backward
    spy_drawdown_stress = dd_10d < -0.05

    # VIX > 30 for 3+ consecutive days (rolling sum of "vix>30" indicator)
    vix_hot = (df["vix"] > 30).astype(int)
    vix_stress = vix_hot.rolling(3).sum() >= 3

    # Credit stress: BAA10Y > rolling 5y p90 (expanded; shifted to avoid look-ahead)
    hy_p90 = df["hy_oas"].shift(1).rolling(1260, min_periods=252).quantile(0.90)
    credit_stress = df["hy_oas"] > hy_p90

    gt = spy_drawdown_stress | vix_stress | credit_stress
    return gt.astype(int), {
        "spy_drawdown_days": int(spy_drawdown_stress.sum()),
        "vix_stress_days": int(vix_stress.sum()),
        "credit_stress_days": int(credit_stress.sum()),
    }


def confusion_analysis(states, gt):
    """states in {EASY,NORMAL,WATCH,STRESS}. gt binary."""
    paired = pd.concat({"state": states, "gt": gt}, axis=1).dropna()
    is_stress = paired["state"] == "STRESS"
    is_elevated = paired["state"].isin(["WATCH", "STRESS"])

    tp_s = int((is_stress & (paired["gt"] == 1)).sum())
    fp_s = int((is_stress & (paired["gt"] == 0)).sum())
    fn_s = int((~is_stress & (paired["gt"] == 1)).sum())
    tn_s = int((~is_stress & (paired["gt"] == 0)).sum())

    tp_e = int((is_elevated & (paired["gt"] == 1)).sum())
    fp_e = int((is_elevated & (paired["gt"] == 0)).sum())
    fn_e = int((~is_elevated & (paired["gt"] == 1)).sum())
    tn_e = int((~is_elevated & (paired["gt"] == 0)).sum())

    def prec_rec(tp, fp, fn, tn):
        p = tp / (tp + fp) if (tp + fp) else 0
        r = tp / (tp + fn) if (tp + fn) else 0
        fpr = fp / (fp + tn) if (fp + tn) else 0
        return {"precision": round(p, 3), "recall": round(r, 3), "false_positive_rate": round(fpr, 3),
                "tp": tp, "fp": fp, "fn": fn, "tn": tn}

    return {
        "stress_vs_gt": prec_rec(tp_s, fp_s, fn_s, tn_s),
        "watch_or_stress_vs_gt": prec_rec(tp_e, fp_e, fn_e, tn_e),
    }


def near_miss_events(df_states):
    """Events NOT used to design the rule — pure OOS."""
    events = {
        "us_downgrade_2011_08_08":  "2011-08-08",
        "china_devaluation_2015_08_24": "2015-08-24",
        "yen_intervention_2022_04_22": "2022-04-22",
        "uk_gilt_crisis_2022_09_26": "2022-09-26",
        "svb_banking_2023_03_10": "2023-03-10",
    }
    out = {}
    for label, date_str in events.items():
        date = pd.Timestamp(date_str)
        nearest_idx = df_states.index[df_states.index >= date]
        if len(nearest_idx) == 0:
            out[label] = {"error": "outside panel"}
            continue
        window_start = nearest_idx[0]
        window = df_states.loc[window_start:].head(10)
        states_seen = window["state"].unique().tolist()
        first_elevated = None
        first_stress = None
        for i, (d, row) in enumerate(window.iterrows()):
            if first_elevated is None and row["state"] in ("WATCH", "STRESS"):
                first_elevated = i
            if first_stress is None and row["state"] == "STRESS":
                first_stress = i
        out[label] = {
            "event_date": str(window_start.date()),
            "states_in_10d_window": states_seen,
            "first_elevated_day": first_elevated,
            "first_stress_day": first_stress,
            "daily_states": [{"day": i, "date": str(d.date()), "state": r["state"], "vix": round(r["vix"], 2)}
                              for i, (d, r) in enumerate(window.iterrows())],
        }
    return out


def main():
    api_key = os.environ["FRED_API_KEY"]
    df = build_panel(api_key)
    df = add_features(df)
    pct = compute_rolling_percentiles(df)
    print("[phase4] running v4 for validation...")
    df_states = run_v4(df, pct)

    print("\n[phase4] SPLIT-SAMPLE analysis...")
    split = split_sample_analysis(df_states)
    for k, v in split.items():
        if v:
            print(f"  {v['name']:8}  {v['years']}y  n={v['n_days']:,}  trans={v['transitions']}  per_yr={v['per_year']}  occupation={v['occupation']}")

    print("\n[phase4] FALSE POSITIVE sweep...")
    gt, gt_stats = ground_truth_stress(df)
    print(f"  Ground-truth stress day counts: {gt_stats}")
    total_gt = int(gt.sum())
    print(f"  Total ground-truth stress days: {total_gt} ({total_gt/len(gt)*100:.1f}% of panel)")

    print("\n[phase4] Confusion — TRAIN (1997-2015)")
    train_states = df_states.loc[:SPLIT_DATE, "state"]
    train_gt = gt.loc[:SPLIT_DATE]
    conf_train = confusion_analysis(train_states, train_gt)
    for k, v in conf_train.items():
        print(f"  {k}: precision={v['precision']}, recall={v['recall']}, FPR={v['false_positive_rate']}  (TP={v['tp']}, FP={v['fp']}, FN={v['fn']}, TN={v['tn']})")

    print("\n[phase4] Confusion — TEST  (2016-2026)")
    test_states = df_states.loc[df_states.index > SPLIT_DATE, "state"]
    test_gt = gt.loc[gt.index > SPLIT_DATE]
    conf_test = confusion_analysis(test_states, test_gt)
    for k, v in conf_test.items():
        print(f"  {k}: precision={v['precision']}, recall={v['recall']}, FPR={v['false_positive_rate']}  (TP={v['tp']}, FP={v['fp']}, FN={v['fn']}, TN={v['tn']})")

    print("\n[phase4] NEAR-MISS EVENTS (NOT used to design rule)...")
    near_miss = near_miss_events(df_states)
    for label, r in near_miss.items():
        if "error" in r:
            print(f"  {label}: {r['error']}")
            continue
        fe = r["first_elevated_day"]
        fs = r["first_stress_day"]
        print(f"  {label:35}  first_WATCH+={fe}d  first_STRESS={fs}d  states={r['states_in_10d_window']}")

    payload = {
        "split_sample": split,
        "ground_truth": gt_stats,
        "confusion_train": conf_train,
        "confusion_test": conf_test,
        "near_miss_events": near_miss,
    }
    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase4_validation.json").write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nSaved: {out_dir / 'phase4_validation.json'}")

    # Honest verdict
    tpy_train = split["train_1997_2015"]["per_year"]
    tpy_test = split["test_2016_2026"]["per_year"]
    stress_occ_train = split["train_1997_2015"]["occupation"].get("STRESS", 0)
    stress_occ_test = split["test_2016_2026"]["occupation"].get("STRESS", 0)

    print(f"\n=== Honest verdict ===")
    print(f"Transition-rate drift: train={tpy_train}/yr  test={tpy_test}/yr  diff={abs(tpy_train-tpy_test):.1f}")
    print(f"STRESS occupation drift: train={stress_occ_train*100:.1f}%  test={stress_occ_test*100:.1f}%")
    prec_train = conf_train["stress_vs_gt"]["precision"]
    prec_test = conf_test["stress_vs_gt"]["precision"]
    rec_train = conf_train["stress_vs_gt"]["recall"]
    rec_test = conf_test["stress_vs_gt"]["recall"]
    print(f"STRESS precision: train={prec_train}  test={prec_test}")
    print(f"STRESS recall:    train={rec_train}  test={rec_test}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
