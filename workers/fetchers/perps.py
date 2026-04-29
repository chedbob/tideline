"""Aggregate perp funding + open interest across Binance, Bybit, OKX, Hyperliquid.

All endpoints are public (no auth, no keys). OI-weighted funding is the
institutional read — one venue's funding alone is noise.
"""
from __future__ import annotations

import httpx

TIMEOUT = 15.0


def _binance(client: httpx.Client, symbol: str = "BTCUSDT") -> dict:
    premium = client.get(
        "https://fapi.binance.com/fapi/v1/premiumIndex",
        params={"symbol": symbol},
        timeout=TIMEOUT,
    ).json()
    oi = client.get(
        "https://fapi.binance.com/fapi/v1/openInterest",
        params={"symbol": symbol},
        timeout=TIMEOUT,
    ).json()
    mark = float(premium["markPrice"])
    oi_contracts = float(oi["openInterest"])
    return {
        "funding_rate_8h": float(premium["lastFundingRate"]),
        "mark_price": mark,
        "oi_notional_usd": oi_contracts * mark,
    }


def _bybit(client: httpx.Client, symbol: str = "BTCUSDT") -> dict:
    r = client.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear", "symbol": symbol},
        timeout=TIMEOUT,
    ).json()
    t = r["result"]["list"][0]
    mark = float(t["markPrice"])
    oi = float(t["openInterest"])
    return {
        "funding_rate_8h": float(t["fundingRate"]),
        "mark_price": mark,
        "oi_notional_usd": oi * mark,
    }


def _okx(client: httpx.Client, inst: str = "BTC-USDT-SWAP") -> dict:
    fr = client.get(
        "https://www.okx.com/api/v5/public/funding-rate",
        params={"instId": inst},
        timeout=TIMEOUT,
    ).json()["data"][0]
    oi = client.get(
        "https://www.okx.com/api/v5/public/open-interest",
        params={"instId": inst},
        timeout=TIMEOUT,
    ).json()["data"][0]
    return {
        "funding_rate_8h": float(fr["fundingRate"]),
        "oi_notional_usd": float(oi.get("oiCcy", 0)) * float(fr.get("markPx", 0) or 0),
    }


def _hyperliquid(client: httpx.Client, coin: str = "BTC") -> dict:
    meta = client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
        timeout=TIMEOUT,
    ).json()
    names = [a["name"] for a in meta[0]["universe"]]
    idx = names.index(coin)
    ctx = meta[1][idx]
    mark = float(ctx["markPx"])
    return {
        "funding_rate_1h": float(ctx["funding"]),
        "mark_price": mark,
        "oi_notional_usd": float(ctx["openInterest"]) * mark,
    }


def fetch_all() -> dict:
    out: dict = {"btc": {}, "eth": {}}
    with httpx.Client() as client:
        for coin_key, binance_sym, hl_coin in [
            ("btc", "BTCUSDT", "BTC"),
            ("eth", "ETHUSDT", "ETH"),
        ]:
            for venue, fn in [
                ("binance", lambda c=binance_sym: _binance(client, c)),
                ("bybit", lambda c=binance_sym: _bybit(client, c)),
                ("okx", lambda c=f"{coin_key.upper()}-USDT-SWAP": _okx(client, c)),
                ("hyperliquid", lambda c=hl_coin: _hyperliquid(client, c)),
            ]:
                try:
                    out[coin_key][venue] = fn()
                except Exception as exc:
                    out[coin_key][venue] = {"error": str(exc)}

    # OI-weighted funding (normalize all to 8h annualized)
    for coin in ("btc", "eth"):
        venues = out[coin]
        total_oi = 0.0
        weighted = 0.0
        for v, d in venues.items():
            if "error" in d:
                continue
            oi = d.get("oi_notional_usd") or 0
            fr_8h = d.get("funding_rate_8h")
            if fr_8h is None and "funding_rate_1h" in d:
                fr_8h = d["funding_rate_1h"] * 8
            if oi and fr_8h is not None:
                weighted += oi * fr_8h
                total_oi += oi
        out[coin]["aggregate"] = {
            "oi_weighted_funding_8h": (weighted / total_oi) if total_oi else None,
            "oi_weighted_funding_annualized": (weighted / total_oi * 3 * 365) if total_oi else None,
            "total_oi_usd": total_oi,
        }
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_all(), indent=2, default=str))
