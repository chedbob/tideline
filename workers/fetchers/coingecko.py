"""CoinGecko fetcher — BTC.D, ETH.D, stablecoin supply, major prices.

Free tier with demo key: 30 req/min stable. Without key: ~10/min, flaky.
Set COINGECKO_DEMO_KEY env var.
"""
from __future__ import annotations

import os

import httpx

BASE = "https://api.coingecko.com/api/v3"


def _headers() -> dict:
    key = os.environ.get("COINGECKO_DEMO_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def fetch_all() -> dict:
    out: dict = {}
    with httpx.Client(headers=_headers(), timeout=20.0) as client:
        try:
            g = client.get(f"{BASE}/global").json()["data"]
            out["btc_dominance"] = g["market_cap_percentage"].get("btc")
            out["eth_dominance"] = g["market_cap_percentage"].get("eth")
            out["total_mcap_usd"] = g["total_market_cap"].get("usd")
            out["total_volume_usd"] = g["total_volume"].get("usd")
            out["mcap_change_24h_pct"] = g.get("market_cap_change_percentage_24h_usd")
        except Exception as exc:
            out["global_error"] = str(exc)

        try:
            coins = client.get(
                f"{BASE}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": "bitcoin,ethereum,solana,tether,usd-coin,dai",
                    "sparkline": "false",
                    "price_change_percentage": "24h,7d,30d",
                },
            ).json()
            out["coins"] = {
                c["id"]: {
                    "price_usd": c["current_price"],
                    "market_cap": c["market_cap"],
                    "change_24h": c.get("price_change_percentage_24h_in_currency"),
                    "change_7d": c.get("price_change_percentage_7d_in_currency"),
                    "change_30d": c.get("price_change_percentage_30d_in_currency"),
                }
                for c in coins
            }
        except Exception as exc:
            out["coins_error"] = str(exc)

    return out


if __name__ == "__main__":
    import json
    print(json.dumps(fetch_all(), indent=2, default=str))
