"""Phase 2 redundancy test — does the composite add value over single features?

The 4-feature composite shows 79% 20D state persistence (BAA10Y variant) /
80% (real HY OAS variant). Base rate ~48%, so nominal edge +31pp. But if any
single feature matches that persistence alone, the "composite" is redundant
aggregation and Tideline should simplify.

Test procedure:
  1. For each feature individually (credit, VIX, curve, NFCI): expanding
     z-score, expanding tercile, measure 20D same-state persistence.
  2. Compare to the full 4-feature composite.
  3. Also test pairwise feature combinations to see if 2 features capture
     most of the composite signal.
  4. Compute tercile-correlation: how much does each single feature's
     tercile assignment agree with the composite's?

Decision:
  - If any single feature's persistence >= composite - 3pp, composite is
    redundant and we ship the single feature.
  - If a 2-feature combination matches, ship the 2-feature composite.
  - Otherwise 4-feature composite earns its keep.

Uses the production credit series (BAA10Y). HY OAS archive is private research
only (see data_archive/README.md).

Usage:  set -a; source .env; set +a; python -m backtest.phase2_redundancy
"""
from __future__ import annotations

import itertools
import json
import os
import sys
from pathlib import Path

import httpx
import pandas as pd

# Reuse phase2's data plumbing
from backtest.phase2 import (
    build_panel,
    expanding_zscore,
    expanding_tercile,
)


# Define how each raw feature maps to a "calm = high" z-score
FEATURE_SIGNS = {
    "credit_spread": -1,   # higher spread = stress
    "vix":           -1,
    "curve_3m10y":   +1,   # positive curve = healthy
    "nfci":          -1,
}


def make_feature_z(df: pd.DataFrame, feature: str) -> pd.Series:
    sign = FEATURE_SIGNS[feature]
    return expanding_zscore(sign * df[feature])


def state_persistence(tercile: pd.Series, horizon: int = 20) -> dict:
    fwd = tercile.shift(-horizon)
    paired = pd.concat({"today": tercile, "future": fwd}, axis=1).dropna()
    paired = paired[paired["today"].isin([-1, 0, 1])]
    n = len(paired)
    same_rate = (paired["today"] == paired["future"]).mean()
    base = paired["future"].value_counts(normalize=True).max()

    # Per-tercile persistence (top, middle, bottom individually)
    per_tercile = {}
    for state in [-1, 0, 1]:
        sub = paired[paired["today"] == state]
        if len(sub) > 0:
            per_tercile[state] = {
                "n": len(sub),
                "stay_rate": float((sub["future"] == state).mean()),
            }
    return {
        "n": n,
        "same_state_rate": float(same_rate),
        "base_rate_most_common": float(base),
        "edge_pp": (float(same_rate) - float(base)) * 100,
        "per_tercile_stay": per_tercile,
    }


def composite_from_features(df: pd.DataFrame, features: list[str]) -> pd.Series:
    zs = [make_feature_z(df, f) for f in features]
    return sum(zs)


def main():
    api_key = os.environ["FRED_API_KEY"]
    df = build_panel(api_key)
    all_features = list(FEATURE_SIGNS.keys())

    results = {}

    # --- Single-feature persistence ---
    print("\n=== Single-feature 20D persistence ===")
    single_results = {}
    for f in all_features:
        z = make_feature_z(df, f)
        t = expanding_tercile(z)
        p = state_persistence(t)
        single_results[f] = p
        print(f"  {f:20} n={p['n']:5,}  same={p['same_state_rate']:.1%}  base={p['base_rate_most_common']:.1%}  edge={p['edge_pp']:+.1f}pp")
    results["single_feature"] = single_results

    # --- Pairwise combinations ---
    print("\n=== 2-feature composite persistence ===")
    pair_results = {}
    for pair in itertools.combinations(all_features, 2):
        comp = composite_from_features(df, list(pair))
        t = expanding_tercile(comp)
        p = state_persistence(t)
        key = "+".join(pair)
        pair_results[key] = p
        print(f"  {key:40} n={p['n']:5,}  same={p['same_state_rate']:.1%}  edge={p['edge_pp']:+.1f}pp")
    results["pairs"] = pair_results

    # --- 3-feature combinations ---
    print("\n=== 3-feature composite persistence ===")
    triple_results = {}
    for triple in itertools.combinations(all_features, 3):
        comp = composite_from_features(df, list(triple))
        t = expanding_tercile(comp)
        p = state_persistence(t)
        key = "+".join(triple)
        triple_results[key] = p
        print(f"  {key:55} n={p['n']:5,}  same={p['same_state_rate']:.1%}  edge={p['edge_pp']:+.1f}pp")
    results["triples"] = triple_results

    # --- Full 4-feature composite (reference) ---
    comp_full = composite_from_features(df, all_features)
    t_full = expanding_tercile(comp_full)
    full_persist = state_persistence(t_full)
    results["full_4_feature"] = full_persist
    print(f"\n=== 4-feature composite (reference) ===")
    print(f"  all 4: n={full_persist['n']:,}  same={full_persist['same_state_rate']:.1%}  edge={full_persist['edge_pp']:+.1f}pp")

    # --- Correlation: composite tercile vs each single-feature tercile ---
    print(f"\n=== Tercile-assignment correlation with 4-feature composite ===")
    corrs = {}
    for f in all_features:
        z = make_feature_z(df, f)
        t_single = expanding_tercile(z)
        joined = pd.concat({"composite": t_full, "single": t_single}, axis=1).dropna()
        agree_rate = (joined["composite"] == joined["single"]).mean()
        corrs[f] = {
            "pearson_corr": float(joined["composite"].corr(joined["single"])),
            "exact_agreement_rate": float(agree_rate),
        }
        print(f"  {f:20} corr={corrs[f]['pearson_corr']:+.3f}  exact_agreement={corrs[f]['exact_agreement_rate']:.1%}")
    results["tercile_correlations"] = corrs

    # --- Verdict ---
    best_single = max(single_results.values(), key=lambda x: x["same_state_rate"])
    best_single_name = max(single_results.keys(), key=lambda k: single_results[k]["same_state_rate"])
    best_pair = max(pair_results.values(), key=lambda x: x["same_state_rate"])
    best_pair_name = max(pair_results.keys(), key=lambda k: pair_results[k]["same_state_rate"])
    full_rate = full_persist["same_state_rate"]

    print(f"\n=== Verdict ===")
    print(f"  Best single feature: {best_single_name} at {best_single['same_state_rate']:.1%}")
    print(f"  Best pair:           {best_pair_name} at {best_pair['same_state_rate']:.1%}")
    print(f"  Full 4-feature:      {full_rate:.1%}")
    print(f"  Single vs full:      {(full_rate - best_single['same_state_rate']) * 100:+.1f}pp")
    print(f"  Pair vs full:        {(full_rate - best_pair['same_state_rate']) * 100:+.1f}pp")

    gain_over_single = (full_rate - best_single["same_state_rate"]) * 100
    if gain_over_single < 3.0:
        print(f"\n  VERDICT: Composite is REDUNDANT. Ship '{best_single_name}' alone — gains only {gain_over_single:+.1f}pp over best single feature.")
    elif (full_rate - best_pair["same_state_rate"]) * 100 < 3.0:
        print(f"\n  VERDICT: Ship 2-feature composite '{best_pair_name}' — 4-feature adds < 3pp.")
    else:
        print(f"\n  VERDICT: 4-feature composite earns its keep (+{gain_over_single:.1f}pp over best single).")

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "phase2_redundancy.json").write_text(json.dumps(results, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
