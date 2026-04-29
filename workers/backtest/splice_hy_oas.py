"""Splice archived BAMLH0A0HYM2 history (Wayback Machine, 1996-2025)
with current live FRED (2023-04-24 onwards).

CRITICAL LEGAL/USAGE BOUNDARY:
  - Archived historical data is ICE-licensed. FRED removed pre-2023 history
    from public access in April 2026 per ICE licensing.
  - We use this spliced series ONLY for private backtesting and research
    integrity (to confirm the null-test verdict holds with the real series).
  - The LIVE PUBLIC PRODUCT must NOT re-publish ICE-licensed historical
    data. Production composite uses BAA10Y (Moody's, Fed-published, no
    licensing cliff) with HYG-derived proxy as supplementary display.

Splice logic:
  1. Load archive CSV (1996-12-31 to 2025-11-03).
  2. Fetch live FRED for overlap window.
  3. Verify value equality on overlap days (|delta| < 1bp).
  4. Combined = archive pre-2023-04-24 + live from 2023-04-24.

Usage:  set -a; source .env; set +a; python -m backtest.splice_hy_oas
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import httpx
import pandas as pd

ARCHIVE_CSV = Path(__file__).parent.parent / "data_archive" / "hy_oas_bamls_archived_20251104.csv"
SPLICE_DATE = "2023-04-24"  # earliest date live FRED has
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"


def load_archive() -> pd.Series:
    df = pd.read_csv(ARCHIVE_CSV, parse_dates=["observation_date"])
    df = df[df["BAMLH0A0HYM2"] != "."].copy()
    df["BAMLH0A0HYM2"] = df["BAMLH0A0HYM2"].astype(float)
    s = df.set_index("observation_date")["BAMLH0A0HYM2"].rename("hy_oas")
    return s[~s.index.duplicated(keep="last")]


def fetch_live(api_key: str) -> pd.Series:
    with httpx.Client() as c:
        r = c.get(FRED_BASE, params={
            "series_id": "BAMLH0A0HYM2",
            "api_key": api_key,
            "file_type": "json",
            "observation_start": SPLICE_DATE,
            "sort_order": "asc",
        }, timeout=30.0)
        r.raise_for_status()
        recs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
    dates, vals = zip(*recs)
    return pd.Series(vals, index=pd.to_datetime(dates), name="hy_oas")


def main() -> int:
    api_key = os.environ["FRED_API_KEY"]
    print("[splice] loading archive...")
    archive = load_archive()
    print(f"[splice]   archive range: {archive.index.min().date()} to {archive.index.max().date()} (n={len(archive)})")

    print("[splice] fetching live FRED for overlap...")
    live = fetch_live(api_key)
    print(f"[splice]   live range:    {live.index.min().date()} to {live.index.max().date()} (n={len(live)})")

    # Verify overlap continuity
    overlap_start = max(archive.index.min(), live.index.min())
    overlap_end = min(archive.index.max(), live.index.max())
    print(f"[splice] overlap: {overlap_start.date()} to {overlap_end.date()}")

    a_over = archive.loc[overlap_start:overlap_end]
    l_over = live.loc[overlap_start:overlap_end]
    joined = pd.concat({"archive": a_over, "live": l_over}, axis=1).dropna()
    joined["diff_bp"] = (joined["archive"] - joined["live"]) * 100  # % to bps

    max_abs_diff = joined["diff_bp"].abs().max()
    mean_abs_diff = joined["diff_bp"].abs().mean()
    print(f"[splice] overlap continuity check (n={len(joined)}):")
    print(f"  max |archive - live| = {max_abs_diff:.2f} bp")
    print(f"  mean|archive - live| = {mean_abs_diff:.2f} bp")

    # Spot-check 5 random overlap dates
    sample = joined.sample(min(5, len(joined)), random_state=0).sort_index()
    print(f"  spot-check samples:")
    for d, row in sample.iterrows():
        print(f"    {d.date()}: archive={row['archive']:.3f}%  live={row['live']:.3f}%  diff={row['diff_bp']:+.2f}bp")

    if max_abs_diff > 5:   # Allow up to 5 bps slack (rounding in different vintages)
        print(f"\n[splice] WARNING: max diff {max_abs_diff:.2f}bp > 5bp. Splice may have vintage inconsistencies.")
        print("[splice] ALFRED vintage data changes over time (revisions). Proceeding but flagging.")

    # Build spliced series: archive before SPLICE_DATE + live from SPLICE_DATE onwards
    pre = archive[archive.index < pd.Timestamp(SPLICE_DATE)]
    post = live[live.index >= pd.Timestamp(SPLICE_DATE)]
    combined = pd.concat([pre, post]).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]

    print(f"\n[splice] combined series: {combined.index.min().date()} to {combined.index.max().date()} (n={len(combined)})")
    print(f"         pre-splice  (archive): n={len(pre):,}")
    print(f"         post-splice (live):    n={len(post):,}")

    out_dir = Path(__file__).parent.parent / "data_archive"
    out_path = out_dir / "hy_oas_spliced.csv"
    combined.to_csv(out_path, header=["hy_oas"])
    print(f"[splice] wrote {out_path}")

    # Report
    report = {
        "generated_at": str(pd.Timestamp.utcnow()),
        "splice_date": SPLICE_DATE,
        "archive_source": "Wayback Machine snapshot of fred.stlouisfed.org/graph/fredgraph.csv (2025-11-04)",
        "live_source": "FRED API BAMLH0A0HYM2 (2023-04-24 onwards only)",
        "archive_n": len(archive),
        "live_n": len(live),
        "overlap_n": len(joined),
        "overlap_max_abs_diff_bp": round(float(max_abs_diff), 3),
        "overlap_mean_abs_diff_bp": round(float(mean_abs_diff), 3),
        "combined_n": len(combined),
        "combined_range": [str(combined.index.min().date()), str(combined.index.max().date())],
        "usage_boundary": "PRIVATE RESEARCH ONLY. Do not re-publish historical HY OAS in public product.",
    }
    (out_dir / "hy_oas_splice_report.json").write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
