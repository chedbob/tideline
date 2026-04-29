"""Block bootstrap CI on Faber rule + golden cross.

GPT-5 Thinking recommendation: Wilson CI assumes IID samples but our
overlapping 20D forward returns are strongly autocorrelated. Block
bootstrap preserves the serial correlation structure and gives honest
intervals.

Method: moving block bootstrap (Kunsch 1989), block length = 60 (3x
horizon) and 20 (1x horizon) for comparison. 10,000 iterations.

We bootstrap full (signal, outcome) tuples in blocks to preserve the
joint time-series structure.

Reports for Faber, Golden Cross, and Combined rules:
  - UP call:   accuracy + 95% bootstrap CI + 95% Wilson CI for comparison
  - DOWN call: same
  - Whether bootstrap CI lower bound still exceeds baseline

Decision rule: if block bootstrap CI at L=60 remains comfortably above
baseline on at least one rule/side, pivoted product is defensible.
If CIs crater into / below baseline, demote headline claim further.

Usage: set -a; source .env; set +a; python -m backtest.block_bootstrap
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

START = "1997-01-01"
N_BOOT = 10_000
BLOCK_LENGTHS = [20, 60]
HORIZON_DAYS = 20
RNG_SEED = 42


def fetch_yahoo(client, ticker):
    p1 = int(datetime.fromisoformat(START).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    r = client.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
        params={"period1": p1, "period2": p2, "interval": "1d"},
        headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
    s = pd.Series(data["indicators"]["quote"][0]["close"], index=ts).dropna()
    return s[~s.index.duplicated(keep="last")]


def wilson(h, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = h / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    half = z * math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return (c - half, c + half)


def moving_block_bootstrap_accuracy(
    outcomes: np.ndarray,
    predictions: np.ndarray,
    block_length: int,
    n_iter: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Return array of bootstrapped accuracies."""
    n = len(outcomes)
    n_blocks = int(np.ceil(n / block_length))
    results = np.empty(n_iter)
    for i in range(n_iter):
        starts = rng.integers(0, n - block_length + 1, size=n_blocks)
        idx = np.concatenate([np.arange(s, s + block_length) for s in starts])[:n]
        hits = (predictions[idx] == outcomes[idx]).sum()
        results[i] = hits / n
    return results


def evaluate_rule(
    name: str,
    signal: pd.Series,     # -1 / 0 / +1 per day
    actual_up: pd.Series,  # 0 / 1 per day
    baseline_up: float,
) -> dict:
    """Evaluate UP-call and DOWN-call buckets separately with Wilson and block bootstrap."""
    rng = np.random.default_rng(RNG_SEED)
    result = {"rule": name, "up_call": {}, "dn_call": {}}

    # Align, drop NaNs
    paired = pd.concat({"signal": signal, "up": actual_up}, axis=1).dropna()

    for bucket_name, bucket_signal_val, predicts_up in [
        ("up_call", 1, True),
        ("dn_call", -1, False),
    ]:
        sub = paired[paired["signal"] == bucket_signal_val]
        n = len(sub)
        if n == 0:
            result[bucket_name] = {"n": 0}
            continue

        outcomes = sub["up"].to_numpy().astype(int)
        predictions = np.full(n, 1 if predicts_up else 0, dtype=int)
        hits = int((predictions == outcomes).sum())
        acc = hits / n

        # Wilson CI
        w_lo, w_hi = wilson(hits, n)

        # Block bootstrap CIs
        boot_intervals = {}
        for L in BLOCK_LENGTHS:
            if L >= n:
                boot_intervals[f"L{L}"] = {"error": "block length >= sample size"}
                continue
            boot = moving_block_bootstrap_accuracy(outcomes, predictions, L, N_BOOT, rng)
            boot_intervals[f"L{L}"] = {
                "mean": float(boot.mean()),
                "p2_5":  float(np.quantile(boot, 0.025)),
                "p97_5": float(np.quantile(boot, 0.975)),
                "std": float(boot.std()),
            }

        # What baseline to compare to
        if predicts_up:
            target_baseline = baseline_up
            baseline_label = f"unconditional UP rate = {baseline_up:.3f}"
        else:
            target_baseline = 1 - baseline_up
            baseline_label = f"unconditional DOWN rate = {1 - baseline_up:.3f}"

        # Excludes baseline?
        wilson_excludes = w_lo > target_baseline
        block_excludes = {L: boot_intervals[f"L{L}"]["p2_5"] > target_baseline
                          for L in BLOCK_LENGTHS if "error" not in boot_intervals[f"L{L}"]}

        result[bucket_name] = {
            "n": n,
            "hits": hits,
            "accuracy": acc,
            "edge_pp_vs_baseline": (acc - target_baseline) * 100,
            "baseline_label": baseline_label,
            "target_baseline": target_baseline,
            "wilson_ci_95": [w_lo, w_hi],
            "wilson_excludes_baseline": wilson_excludes,
            "block_bootstrap_ci_95": boot_intervals,
            "block_excludes_baseline": block_excludes,
        }
    return result


def main() -> int:
    print("[bootstrap] fetching SPY (1997-present)...")
    with httpx.Client() as c:
        spy = fetch_yahoo(c, "SPY")
    df = pd.DataFrame({"spy": spy})
    df = df.dropna()
    print(f"[bootstrap]   panel: {df.shape}, {df.index.min().date()} to {df.index.max().date()}")

    # Forward SPY 20D return
    fwd = df["spy"].shift(-HORIZON_DAYS) / df["spy"] - 1
    actual_up = (fwd > 0).astype(int)
    baseline_up = actual_up.mean()
    print(f"[bootstrap]   baseline UP rate: {baseline_up:.3%}")

    # Rules
    ma200 = df["spy"].rolling(200).mean()
    ma50 = df["spy"].rolling(50).mean()

    faber = pd.Series(0.0, index=df.index)
    faber[df["spy"] > ma200] = 1
    faber[df["spy"] < ma200] = -1

    golden = pd.Series(0.0, index=df.index)
    golden[ma50 > ma200] = 1
    golden[ma50 < ma200] = -1

    combined = pd.Series(0.0, index=df.index)
    combined[(df["spy"] > ma200) & (ma50 > ma200)] = 1
    combined[(df["spy"] < ma200) & (ma50 < ma200)] = -1

    rules = [
        ("Faber (SPY > 200DMA)", faber),
        ("Golden Cross (50 > 200 MA)", golden),
        ("Combined (SPY > 200 AND 50 > 200)", combined),
    ]

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "panel": {"start": str(df.index.min().date()), "end": str(df.index.max().date()), "n": len(df)},
        "horizon_days": HORIZON_DAYS,
        "baseline_up_rate": baseline_up,
        "n_bootstrap_iter": N_BOOT,
        "block_lengths": BLOCK_LENGTHS,
        "results": [],
    }

    print(f"\n[bootstrap] running {N_BOOT:,} iterations per bucket x 2 block lengths x 3 rules = {N_BOOT * 6:,} total...")

    for name, sig in rules:
        print(f"\n[bootstrap] evaluating {name}...")
        res = evaluate_rule(name, sig, actual_up, baseline_up)
        out["results"].append(res)

    # Report
    print("\n\n=== Block Bootstrap Results ===")
    print(f"Panel 1997-2026, n={len(df):,}, horizon={HORIZON_DAYS}D, {N_BOOT:,} iters, 95% CIs\n")
    for res in out["results"]:
        print(f"--- {res['rule']} ---")
        for bucket in ["up_call", "dn_call"]:
            b = res[bucket]
            if b.get("n", 0) == 0:
                continue
            label = "UP call" if bucket == "up_call" else "DOWN call"
            print(f"  {label}  n={b['n']:5,}  acc={b['accuracy']:.3f}  edge_vs_base={b['edge_pp_vs_baseline']:+.1f}pp")
            print(f"    Wilson 95% CI: [{b['wilson_ci_95'][0]:.3f}, {b['wilson_ci_95'][1]:.3f}]  excludes {b['target_baseline']:.3f}? {b['wilson_excludes_baseline']}")
            for L, bs in b["block_bootstrap_ci_95"].items():
                if "error" in bs:
                    continue
                excl = bs["p2_5"] > b["target_baseline"]
                print(f"    Block {L}  95% CI: [{bs['p2_5']:.3f}, {bs['p97_5']:.3f}]  mean={bs['mean']:.3f}  excludes baseline? {excl}")
        print()

    # Verdict
    print("=== Verdict ===")
    any_survive = False
    for res in out["results"]:
        name = res["rule"]
        for bucket in ["up_call", "dn_call"]:
            b = res[bucket]
            if b.get("n", 0) == 0:
                continue
            if b.get("block_excludes_baseline", {}).get(60, False):
                label = "UP" if bucket == "up_call" else "DOWN"
                print(f"  SURVIVES block-bootstrap L=60: {name} [{label} call] — edge {b['edge_pp_vs_baseline']:+.1f}pp, CI [{b['block_bootstrap_ci_95']['L60']['p2_5']:.3f}, {b['block_bootstrap_ci_95']['L60']['p97_5']:.3f}]")
                any_survive = True
    if not any_survive:
        print("  NO rule/bucket survives L=60 block bootstrap — headline claim must be demoted further.")

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "block_bootstrap_result.json").write_text(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
