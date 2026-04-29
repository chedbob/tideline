"""Yahoo Finance fetcher — VIX complex, sector ETFs, FX, commodities.

Uses unofficial query1 chart endpoint. No key. ~2000/hr soft limit per IP.
Must send a browser User-Agent or Yahoo returns 401.
"""
from __future__ import annotations

import httpx

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Tideline/0.1)"}

TICKERS = {
    # Volatility complex
    "vix": "^VIX",
    "vix3m": "^VIX3M",
    "vix6m": "^VIX6M",
    "vvix": "^VVIX",
    "skew": "^SKEW",
    "move": "^MOVE",
    # Equity indices + style
    "spy": "SPY",
    "qqq": "QQQ",
    "iwm": "IWM",
    "rsp": "RSP",
    "sphb": "SPHB",
    "splv": "SPLV",
    "soxx": "SOXX",
    # Sectors
    "xlk": "XLK", "xlf": "XLF", "xle": "XLE", "xlv": "XLV",
    "xli": "XLI", "xly": "XLY", "xlp": "XLP", "xlu": "XLU",
    "xlre": "XLRE", "xlb": "XLB", "xlc": "XLC",
    # Credit / rates proxies
    "hyg": "HYG", "lqd": "LQD", "tlt": "TLT", "gld": "GLD",
    # FX — the carry canaries
    "dxy": "DX-Y.NYB",
    "usdjpy": "USDJPY=X",
    "audjpy": "AUDJPY=X",
    "cny": "CNY=X",
    # Crypto futures (for CME basis compute)
    "btc_fut": "BTC=F",
}


def _fetch_one(client: httpx.Client, ticker: str, range_: str = "6mo", interval: str = "1d") -> dict:
    r = client.get(
        BASE.format(ticker=ticker),
        params={"range": range_, "interval": interval, "includePrePost": "false"},
        headers=HEADERS,
        timeout=15.0,
    )
    r.raise_for_status()
    data = r.json()["chart"]["result"][0]
    timestamps = data["timestamp"]
    closes = data["indicators"]["quote"][0]["close"]
    series = [
        {"ts": ts, "value": float(c)}
        for ts, c in zip(timestamps, closes) if c is not None
    ]
    meta = data.get("meta", {})
    return {
        "series": series,
        "last": series[-1]["value"] if series else None,
        "prev_close": meta.get("chartPreviousClose"),
        "currency": meta.get("currency"),
    }


def fetch_all() -> dict:
    out: dict = {}
    with httpx.Client(headers=HEADERS) as client:
        for label, ticker in TICKERS.items():
            try:
                out[label] = _fetch_one(client, ticker)
            except Exception as exc:
                out[label] = {"error": str(exc), "ticker": ticker}
    return out


if __name__ == "__main__":
    import json
    data = fetch_all()
    print(json.dumps(
        {k: (v.get("last") if isinstance(v, dict) else v) for k, v in data.items()},
        indent=2,
    ))
