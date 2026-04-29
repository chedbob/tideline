"""FRED fetcher — Fed liquidity, credit spreads, rates, FCI.

Endpoint: https://api.stlouisfed.org/fred/series/observations
Requires FRED_API_KEY env var. Rate limit ~120 req/min (we use 10).
"""
from __future__ import annotations

import os
from datetime import date, timedelta

import httpx

BASE = "https://api.stlouisfed.org/fred/series/observations"

SERIES = {
    "fed_balance_sheet": "WALCL",
    "tga": "WTREGEN",
    "rrp": "RRPONTSYD",
    "m2_weekly": "WM2NS",
    "hy_oas": "BAMLH0A0HYM2",
    "ig_oas": "BAMLC0A0CM",
    "real_yield_10y": "DFII10",
    "breakeven_5y5y": "T5YIFR",
    "curve_3m10y": "T10Y3M",
    "nfci": "NFCI",
    "dxy_broad": "DTWEXBGS",
}


def _fetch_series(client: httpx.Client, series_id: str, api_key: str, lookback_days: int = 400) -> list[dict]:
    start = (date.today() - timedelta(days=lookback_days)).isoformat()
    r = client.get(
        BASE,
        params={
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start,
            "sort_order": "asc",
        },
        timeout=20.0,
    )
    r.raise_for_status()
    obs = r.json().get("observations", [])
    return [{"date": o["date"], "value": float(o["value"])} for o in obs if o["value"] not in (".", "")]


def fetch_all() -> dict:
    api_key = os.environ["FRED_API_KEY"]
    out: dict = {}
    with httpx.Client() as client:
        for label, sid in SERIES.items():
            try:
                out[label] = _fetch_series(client, sid, api_key)
            except Exception as exc:
                out[label] = {"error": str(exc)}
    return out


if __name__ == "__main__":
    import json
    data = fetch_all()
    print(json.dumps({k: (v[-1] if isinstance(v, list) and v else v) for k, v in data.items()}, indent=2))
