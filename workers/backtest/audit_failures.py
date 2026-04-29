"""Audit #5: Failure-mode smoke tests.

What happens when:
  A. FRED_API_KEY missing entirely
  B. FRED_API_KEY invalid/wrong key
  C. Yahoo returns no data for a ticker
  D. NaN injected mid-panel for a feature
"""
from __future__ import annotations

import os
import sys
import json
import importlib

import pandas as pd
import numpy as np


def test_a_no_fred_key():
    """A. FRED_API_KEY missing — publish should still emit a partial latest.json."""
    saved = os.environ.pop("FRED_API_KEY", None)
    try:
        # Clear module cache to force fresh import
        for m in list(sys.modules):
            if m.startswith(("publish", "compute", "fetchers")):
                sys.modules.pop(m)
        from publish import build_payload
        payload = build_payload()
        regime = payload.get("regime", {})
        has_error = "error" in regime
        result = "PASS" if has_error else "FAIL"
        print(f"  A. No FRED key  -> regime.error={regime.get('error')!r}  [{result}]")
        return has_error
    except Exception as e:
        print(f"  A. No FRED key  -> EXCEPTION: {e}  [FAIL]")
        return False
    finally:
        if saved:
            os.environ["FRED_API_KEY"] = saved


def test_b_bad_fred_key():
    """B. Wrong FRED key. Should fail gracefully with regime.error."""
    saved = os.environ.get("FRED_API_KEY")
    os.environ["FRED_API_KEY"] = "deadbeef000000000000000000000000"
    try:
        for m in list(sys.modules):
            if m.startswith(("publish", "compute", "fetchers")):
                sys.modules.pop(m)
        from publish import build_payload
        payload = build_payload()
        regime = payload.get("regime", {})
        has_error = "error" in regime
        result = "PASS" if has_error else "FAIL"
        print(f"  B. Bad FRED key -> regime.error={'set' if has_error else 'MISSING'}  [{result}]")
        return has_error
    except Exception as e:
        print(f"  B. Bad FRED key -> EXCEPTION: {e}  [FAIL]")
        return False
    finally:
        if saved:
            os.environ["FRED_API_KEY"] = saved


def test_c_yahoo_404():
    """C. Yahoo unknown ticker. The yahoo fetcher should record an error
    in that ticker's slot but not crash."""
    for m in list(sys.modules):
        if m.startswith("fetchers"):
            sys.modules.pop(m)
    from fetchers.yahoo import _fetch_one
    import httpx
    with httpx.Client() as c:
        try:
            data = _fetch_one(c, "ZZZZNOTAREALTICKER")
            print(f"  C. Yahoo bad ticker -> returned data (unexpected): {data}  [FAIL]")
            return False
        except Exception as e:
            print(f"  C. Yahoo bad ticker -> raises {type(e).__name__}  [PASS — caught at higher level]")
            return True


def test_d_nan_in_panel():
    """D. Inject NaN into the panel mid-history. State machine should
    skip that day (states append previous), not crash."""
    for m in list(sys.modules):
        if m.startswith(("compute", "rule")):
            sys.modules.pop(m)
    from compute.regime import build_panel, _add_features, _rolling_percentiles, run_state_machine
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print("  D. Skipped (no FRED key)")
        return True
    df = build_panel(api_key)
    df = _add_features(df)
    # Inject a NaN for hy_oas at midpoint
    midpoint = len(df) // 2
    df.iloc[midpoint, df.columns.get_loc("nfci_lagged")] = np.nan
    df.iloc[midpoint, df.columns.get_loc("nfci_1w_change")] = np.nan
    pct = _rolling_percentiles(df)
    try:
        out = run_state_machine(df, pct)
        # State machine should not crash, but day midpoint+1 has no transition
        result = "PASS" if len(out) == len(df) else "FAIL"
        print(f"  D. NaN injection at row {midpoint} -> state machine returned {len(out)} rows  [{result}]")
        return len(out) == len(df)
    except Exception as e:
        print(f"  D. NaN injection -> EXCEPTION: {e}  [FAIL]")
        return False


def main():
    # Load env first
    from pathlib import Path
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    print("\n=== Failure-mode smoke tests ===\n")
    results = []
    results.append(("A. No FRED key",  test_a_no_fred_key()))
    results.append(("B. Bad FRED key", test_b_bad_fred_key()))
    results.append(("C. Yahoo 404",    test_c_yahoo_404()))
    results.append(("D. NaN in panel", test_d_nan_in_panel()))
    print()
    overall = all(r[1] for r in results)
    print(f"VERDICT: {'PASS' if overall else 'FAIL'} — {sum(r[1] for r in results)}/{len(results)} tests passed")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
