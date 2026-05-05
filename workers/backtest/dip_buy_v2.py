"""DIP_BUY v2 backtest — pre-registered spec from research_log.md Entry 12.

Spec (DO NOT MODIFY — frozen by git commit before running):
  Pre-conditions:
    - Faber == GREEN              (SPY > 200DMA AND 50DMA > 200DMA)
    - regime ∈ {NORMAL, EASY}

  Trigger (all 3):
    - SPY ≤ SPY_20d_high * 0.95   (>=5% off 20-day high)
    - BAA10Y 20-day change ≤ 0    (credit flat or tightening)
    - VIX / VIX3M ≤ 1.0           (vol curve in contango)

  Baseline: pre-conditions hold AND DIP_BUY did NOT fire.

Tests run (all pre-committed):
  1. Block bootstrap (Kunsch 1989, 10k iters, L=60) on H10 edge
  2. Train/test split: 1997-2014 vs 2015-2026 OOS retention
  3. Fire frequency
  4. BH-FDR @ q=0.10 across H5/H10/H20

Go criterion: H10 bootstrap lower-bound CI > calm-uptrend baseline + 3pp,
OOS H10 edge >= 50% of training edge same sign, >=10 fires OOS, BH-FDR pass.
"""
from __future__ import annotations

import os
import sys
import json
import numpy as np
import pandas as pd

# Ensure workers/ is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compute import regime as regime_compute
from rule.v1 import faber_signal


def build_dip_buy_v2_panel(api_key: str) -> pd.DataFrame:
    """Build the full panel with everything we need."""
    df = regime_compute.build_panel(api_key)
    df = regime_compute._add_features(df)
    pct = regime_compute._rolling_percentiles(df)
    df_states = regime_compute.run_state_machine(df, pct)

    # Faber state
    df_states["faber"] = [faber_signal(s, m50, m200) for s, m50, m200 in
                          zip(df_states["spy"], df_states["ma_50"], df_states["ma_200"])]

    # Pre-conditions
    df_states["pre_ok"] = (df_states["faber"] == "GREEN") & df_states["state"].isin(["NORMAL", "EASY"])

    # Trigger conditions (all 3 round-number, no tuning)
    df_states["spy_20d_high"] = df_states["spy"].rolling(20, min_periods=20).max().shift(1)
    cond_pullback = df_states["spy"] <= df_states["spy_20d_high"] * 0.95
    cond_credit_calm = df_states["hy_oas"].diff(20) <= 0  # 20-day BAA10Y change ≤ 0
    cond_contango = (df_states["vix"] / df_states["vix3m"]) <= 1.0
    df_states["dip_buy_v2"] = (
        df_states["pre_ok"]
        & cond_pullback
        & cond_credit_calm
        & cond_contango
    ).fillna(False)

    # Forward returns
    for h in (5, 10, 20):
        df_states[f"fwd_{h}d"] = df_states["spy"].shift(-h) / df_states["spy"] - 1
        df_states[f"fwd_{h}_up"] = (df_states[f"fwd_{h}d"] > 0).astype(float)

    return df_states


def block_bootstrap_edge(df: pd.DataFrame, fire_col: str, base_col: str,
                         outcome_col: str, n_iter: int = 10000, block_len: int = 60,
                         seed: int = 42) -> tuple[float, float, float]:
    """Block bootstrap on (fire_up_rate − base_up_rate) edge.

    Method: resample blocks from the original time series, recompute fire/base
    populations and edge per resample. Returns (median_edge, lo_95, hi_95).

    df rows must have fire_col (bool), base_col (bool), outcome_col (0/1).
    """
    sub = df.dropna(subset=[outcome_col]).reset_index(drop=True)
    n = len(sub)
    if n < block_len * 2:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n_blocks = int(np.ceil(n / block_len))
    edges = np.empty(n_iter)

    fire_arr = sub[fire_col].values
    base_arr = sub[base_col].values
    out_arr = sub[outcome_col].values

    for i in range(n_iter):
        starts = rng.integers(0, n - block_len + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]
        f = fire_arr[idx]
        b = base_arr[idx]
        o = out_arr[idx]
        f_n, b_n = f.sum(), b.sum()
        if f_n < 5 or b_n < 30:
            edges[i] = np.nan
            continue
        edges[i] = (o[f].mean() if f_n else 0) - (o[b].mean() if b_n else 0)

    edges = edges[~np.isnan(edges)]
    return (float(np.median(edges)), float(np.quantile(edges, 0.025)), float(np.quantile(edges, 0.975)))


def bootstrap_p_value(edges_distribution: np.ndarray) -> float:
    """One-sided p-value: fraction of bootstrap edges ≤ 0."""
    return float((edges_distribution <= 0).mean())


def run() -> dict:
    """Run the full pre-registered backtest. Return verdict dict."""
    api_key = os.environ.get("FRED_API_KEY")
    assert api_key, "FRED_API_KEY required"
    df = build_dip_buy_v2_panel(api_key)

    # Define regions
    train = df[(df.index >= "1997-01-01") & (df.index <= "2014-12-31")].copy()
    oos = df[(df.index >= "2015-01-01")].copy()

    print("=" * 72)
    print("DIP_BUY v2 BACKTEST — pre-registered spec")
    print("=" * 72)
    print(f"Train window: 1997-01-01 to 2014-12-31 ({len(train)} days)")
    print(f"OOS   window: 2015-01-01 to {df.index.max().date()} ({len(oos)} days)")
    print()

    # Frequency
    train_fires = int(train["dip_buy_v2"].sum())
    oos_fires = int(oos["dip_buy_v2"].sum())
    train_baseline_n = int((train["pre_ok"] & ~train["dip_buy_v2"]).sum())
    oos_baseline_n = int((oos["pre_ok"] & ~oos["dip_buy_v2"]).sum())
    print(f"FIRES — train: {train_fires} (committed ~45)")
    print(f"FIRES — OOS:   {oos_fires} (committed ~30, min 10)")
    print(f"BASELINE — train: {train_baseline_n} days, OOS: {oos_baseline_n} days")
    print()

    # Edges per horizon (raw)
    def horizon_edges(sub: pd.DataFrame) -> dict:
        sub_clean = sub.dropna(subset=["fwd_5_up", "fwd_10_up", "fwd_20_up"]).copy()
        sub_clean["base"] = sub_clean["pre_ok"] & ~sub_clean["dip_buy_v2"]
        out = {}
        for h in (5, 10, 20):
            col = f"fwd_{h}_up"
            fire_up = sub_clean.loc[sub_clean["dip_buy_v2"], col]
            base_up = sub_clean.loc[sub_clean["base"], col]
            if len(fire_up) == 0 or len(base_up) == 0:
                out[f"H{h}"] = None
                continue
            edge = (fire_up.mean() - base_up.mean()) * 100
            out[f"H{h}"] = {
                "fire_n": int(len(fire_up)),
                "fire_up_rate": float(fire_up.mean()),
                "base_n": int(len(base_up)),
                "base_up_rate": float(base_up.mean()),
                "edge_pp": float(edge),
            }
        return out

    train_edges = horizon_edges(train)
    oos_edges = horizon_edges(oos)

    print("RAW EDGE — train (1997-2014):")
    for h, e in train_edges.items():
        if e is None:
            print(f"  {h}: insufficient data")
            continue
        print(f"  {h}: fire {e['fire_up_rate']*100:.1f}% (n={e['fire_n']}) | "
              f"base {e['base_up_rate']*100:.1f}% (n={e['base_n']}) | edge {e['edge_pp']:+.1f}pp")
    print()
    print("RAW EDGE — OOS (2015-2026):")
    for h, e in oos_edges.items():
        if e is None:
            print(f"  {h}: insufficient data")
            continue
        print(f"  {h}: fire {e['fire_up_rate']*100:.1f}% (n={e['fire_n']}) | "
              f"base {e['base_up_rate']*100:.1f}% (n={e['base_n']}) | edge {e['edge_pp']:+.1f}pp")
    print()

    # Block bootstrap on FULL panel for primary go criterion (H10)
    print("BLOCK BOOTSTRAP — full panel (1997-2026), 10k iters, L=60")
    full = df.copy()
    full["base"] = full["pre_ok"] & ~full["dip_buy_v2"]
    boot_results = {}
    rng = np.random.default_rng(42)
    full_clean = full.dropna(subset=["fwd_10_up"]).reset_index(drop=True)
    n_obs = len(full_clean)
    block_len = 60
    n_iter = 10000
    n_blocks = int(np.ceil(n_obs / block_len))
    for h in (5, 10, 20):
        col = f"fwd_{h}_up"
        clean = full.dropna(subset=[col]).reset_index(drop=True)
        n = len(clean)
        if n < block_len * 2:
            boot_results[f"H{h}"] = None
            continue
        edges = np.empty(n_iter)
        fire_arr = clean["dip_buy_v2"].values
        base_arr = clean["base"].values
        out_arr = clean[col].values
        for i in range(n_iter):
            starts = rng.integers(0, n - block_len + 1, size=n_blocks)
            idx = np.concatenate([np.arange(s, s + block_len) for s in starts])[:n]
            f = fire_arr[idx]; b = base_arr[idx]; o = out_arr[idx]
            f_n, b_n = f.sum(), b.sum()
            if f_n < 5 or b_n < 30:
                edges[i] = np.nan; continue
            edges[i] = o[f].mean() - o[b].mean()
        valid = edges[~np.isnan(edges)]
        med = float(np.median(valid))
        lo = float(np.quantile(valid, 0.025))
        hi = float(np.quantile(valid, 0.975))
        # one-sided p-value: fraction of resamples with edge ≤ 0
        p_one = float((valid <= 0).mean())
        boot_results[f"H{h}"] = {"median_edge_pp": med * 100, "lo_95_pp": lo * 100,
                                  "hi_95_pp": hi * 100, "p_one_sided": p_one,
                                  "n_valid_iters": int(len(valid))}
        print(f"  {h}: median {med*100:+.1f}pp | 95% CI [{lo*100:+.1f}, {hi*100:+.1f}] | "
              f"p={p_one:.4f}")

    # Benjamini-Hochberg q=0.10 across H5/H10/H20 p-values
    print()
    print("BENJAMINI-HOCHBERG (q=0.10) across H5/H10/H20:")
    p_values = [(k, boot_results[k]["p_one_sided"]) for k in ("H5", "H10", "H20")
                if boot_results.get(k) is not None]
    p_values_sorted = sorted(p_values, key=lambda x: x[1])
    bh_pass = {}
    m = len(p_values_sorted)
    for i, (k, p) in enumerate(p_values_sorted, start=1):
        threshold = (i / m) * 0.10
        passes = p <= threshold
        bh_pass[k] = passes
        print(f"  rank {i}: {k} p={p:.4f} | BH threshold {threshold:.4f} | {'PASS' if passes else 'fail'}")

    # Verdicts against pre-committed criteria
    print()
    print("=" * 72)
    print("VERDICT vs PRE-COMMITTED GO CRITERIA")
    print("=" * 72)

    h10_train_edge = train_edges.get("H10", {}).get("edge_pp") if train_edges.get("H10") else None
    h10_oos_edge = oos_edges.get("H10", {}).get("edge_pp") if oos_edges.get("H10") else None
    h10_boot = boot_results.get("H10")

    crit_a = h10_boot is not None and h10_boot["lo_95_pp"] > 3.0
    crit_b1 = oos_fires >= 10
    crit_b2 = train_fires >= 30
    if h10_train_edge is not None and h10_oos_edge is not None and h10_train_edge != 0:
        retention = (h10_oos_edge / h10_train_edge) if h10_train_edge > 0 else 0
        crit_b3 = (h10_oos_edge > 0 and retention >= 0.50)
    else:
        retention = None; crit_b3 = False
    crit_d = bh_pass.get("H10", False)

    def status(b): return "PASS" if b else "FAIL"

    print(f"A. H10 bootstrap LB > baseline+3pp: {status(crit_a)}  (LB={h10_boot['lo_95_pp']:.1f}pp, threshold +3.0)")
    print(f"B1. >=10 fires OOS:                  {status(crit_b1)}  ({oos_fires} OOS fires)")
    print(f"B2. >=30 fires training:             {status(crit_b2)}  ({train_fires} training fires)")
    print(f"B3. OOS H10 edge >= 50% of training: {status(crit_b3)}  (train {h10_train_edge:+.1f}pp → OOS {h10_oos_edge:+.1f}pp, retention {retention:.0%} if positive)" if retention is not None else f"B3. OOS retention check: insufficient data")
    print(f"D. BH-FDR q=0.10 H10 passes:        {status(crit_d)}")

    overall = crit_a and crit_b1 and crit_b2 and crit_b3 and crit_d
    print()
    print(f"OVERALL: {'PASS — SHIP' if overall else 'FAIL — DO NOT SHIP'}")

    return {
        "overall_pass": overall,
        "fires": {"train": train_fires, "oos": oos_fires},
        "edges": {"train": train_edges, "oos": oos_edges},
        "bootstrap": boot_results,
        "bh": bh_pass,
        "criteria": {
            "A_bootstrap_h10_lb": crit_a,
            "B1_oos_fires_10": crit_b1,
            "B2_train_fires_30": crit_b2,
            "B3_oos_retention_50": crit_b3,
            "D_bh_h10": crit_d,
        }
    }


if __name__ == "__main__":
    out = run()
    print()
    print("(verdict dict for log)")
    print(json.dumps({k: v for k, v in out.items() if k != "edges"}, indent=2, default=str))
