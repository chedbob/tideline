"""Tideline publish orchestrator.

Runs all fetchers, assembles latest.json, uploads to Cloudflare R2.
Local dev: writes to ./out/latest.json and skips R2 upload if creds absent.

Env vars:
  FRED_API_KEY         (required)
  COINGECKO_DEMO_KEY   (optional but recommended)
  R2_ACCOUNT_ID        (upload)
  R2_ACCESS_KEY_ID     (upload)
  R2_SECRET_ACCESS_KEY (upload)
  R2_BUCKET            (upload)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from fetchers import coingecko, fred, perps, yahoo
from compute import regime as regime_compute


def _compute_vix_term_slope(yahoo_data: dict) -> dict:
    """Backwardation flag: VIX > VIX3M = stress. Slope = (VIX3M - VIX) / VIX."""
    try:
        vix = yahoo_data["vix"]["last"]
        vix3m = yahoo_data["vix3m"]["last"]
        slope = (vix3m - vix) / vix
        return {
            "vix": vix,
            "vix3m": vix3m,
            "slope_pct": slope,
            "backwardation": vix > vix3m,
            "regime": "stress" if vix > vix3m else ("calm" if slope > 0.08 else "normal"),
        }
    except (KeyError, TypeError, ZeroDivisionError):
        return {"error": "insufficient_data"}


def _compute_net_liquidity(fred_data: dict) -> dict:
    """Net liquidity = Fed BS − TGA − RRP. Howell framework."""
    try:
        bs = fred_data["fed_balance_sheet"][-1]
        tga = fred_data["tga"][-1]
        rrp = fred_data["rrp"][-1]
        # FRED values are millions for WALCL/TGA, billions for RRP — normalize to $B
        net_b = (bs["value"] / 1000) - (tga["value"] / 1000) - rrp["value"]
        return {
            "net_liquidity_usd_bn": net_b,
            "fed_balance_sheet_bn": bs["value"] / 1000,
            "tga_bn": tga["value"] / 1000,
            "rrp_bn": rrp["value"],
            "as_of": bs["date"],
        }
    except (KeyError, IndexError, TypeError) as exc:
        return {"error": str(exc)}


def _upload_r2(payload: dict) -> bool:
    """Upload to Cloudflare R2 via S3-compatible API. Returns True if uploaded."""
    account = os.environ.get("R2_ACCOUNT_ID")
    akey = os.environ.get("R2_ACCESS_KEY_ID")
    skey = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET")
    if not all([account, akey, skey, bucket]):
        print("[publish] R2 creds absent — skipping upload (local dev mode).")
        return False

    import boto3  # deferred import: optional in dev

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=akey,
        aws_secret_access_key=skey,
        region_name="auto",
    )
    body = json.dumps(payload).encode()
    client.put_object(
        Bucket=bucket,
        Key="latest.json",
        Body=body,
        ContentType="application/json",
        CacheControl="public, max-age=60, s-maxage=300",
    )
    print(f"[publish] uploaded latest.json ({len(body)} bytes) to r2://{bucket}/latest.json")
    return True


def build_payload() -> dict:
    t0 = time.time()
    print("[publish] fetching yahoo...")
    yh = yahoo.fetch_all()
    print(f"[publish]   {len(yh)} tickers in {time.time()-t0:.1f}s")

    print("[publish] fetching fred...")
    t = time.time()
    fr = fred.fetch_all() if os.environ.get("FRED_API_KEY") else {"error": "FRED_API_KEY missing"}
    print(f"[publish]   done in {time.time()-t:.1f}s")

    print("[publish] fetching coingecko...")
    t = time.time()
    cg = coingecko.fetch_all()
    print(f"[publish]   done in {time.time()-t:.1f}s")

    print("[publish] fetching perps (4 venues)...")
    t = time.time()
    pp = perps.fetch_all()
    print(f"[publish]   done in {time.time()-t:.1f}s")

    # --- Regime compute (Zone 0 Faber + Zone 1 4-state) ---
    print("[publish] computing regime state (rule v1)...")
    t = time.time()
    regime_payload = {}
    if os.environ.get("FRED_API_KEY"):
        try:
            regime_payload = regime_compute.run(os.environ["FRED_API_KEY"])
            print(f"[publish]   regime done in {time.time()-t:.1f}s | "
                  f"Zone0={regime_payload['snapshot']['zones']['trend_signal']['state']} "
                  f"Zone1={regime_payload['snapshot']['zones']['regime_state']['state']} "
                  f"log_entries={len(regime_payload['decision_log'])}")
        except Exception as exc:
            regime_payload = {"error": str(exc)}
            print(f"[publish]   regime FAILED: {exc}")
    else:
        regime_payload = {"error": "FRED_API_KEY missing"}

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": 2,
        "rule_version": "v1",
        "regime": regime_payload.get("snapshot") if "error" not in regime_payload else {"error": regime_payload.get("error")},
        "decision_log": regime_payload.get("decision_log", []),
        "panel_meta": regime_payload.get("panel_meta", {}),
        "raw": {
            "yahoo": yh,
            "fred": fr,
            "coingecko": cg,
            "perps": pp,
        },
        "computed": {
            "vix_term_structure": _compute_vix_term_slope(yh),
            "net_liquidity": _compute_net_liquidity(fr) if isinstance(fr, dict) and "error" not in fr else {"error": "fred_unavailable"},
        },
    }
    return payload


def _upload_research_log() -> None:
    """Mirror workers/backtest/research_log.md to R2 alongside latest.json
    so the methodology page can fetch it from the same origin/CDN."""
    log_path = Path(__file__).parent / "backtest" / "research_log.md"
    if not log_path.exists():
        return
    body = log_path.read_bytes()
    # Local mirror for dev
    local_target = Path(__file__).parent / "out" / "research_log.md"
    local_target.write_bytes(body)
    # R2 upload
    account = os.environ.get("R2_ACCOUNT_ID")
    akey = os.environ.get("R2_ACCESS_KEY_ID")
    skey = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET")
    if not all([account, akey, skey, bucket]):
        return
    import boto3
    client = boto3.client("s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=akey, aws_secret_access_key=skey, region_name="auto")
    client.put_object(Bucket=bucket, Key="research_log.md", Body=body,
        ContentType="text/markdown; charset=utf-8",
        CacheControl="public, max-age=60, s-maxage=300")
    print(f"[publish] uploaded research_log.md ({len(body)} bytes)")


def _upload_decision_log_csv(log: list) -> None:
    """Also upload the decision log as CSV for machine-readable public audit."""
    if not log:
        return
    account = os.environ.get("R2_ACCOUNT_ID")
    akey = os.environ.get("R2_ACCESS_KEY_ID")
    skey = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET")
    if not all([account, akey, skey, bucket]):
        return
    import boto3
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["date", "new_state", "trigger", "spy", "vix", "hy_oas"])
    w.writeheader()
    for row in reversed(log):  # oldest first in CSV
        w.writerow(row)
    body = buf.getvalue().encode()
    client = boto3.client("s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=akey, aws_secret_access_key=skey, region_name="auto")
    client.put_object(Bucket=bucket, Key="decision_log.csv", Body=body,
        ContentType="text/csv",
        CacheControl="public, max-age=60, s-maxage=300")
    print(f"[publish] uploaded decision_log.csv ({len(body)} bytes)")


def _sanitize_env() -> None:
    """Strip whitespace from secret env vars. Defends against accidental
    leading/trailing tabs/newlines in GitHub Actions secret values."""
    for k in ("FRED_API_KEY", "COINGECKO_DEMO_KEY",
              "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
              "R2_BUCKET", "R2_PUBLIC_URL"):
        v = os.environ.get(k)
        if v is not None:
            stripped = v.strip()
            if stripped != v:
                print(f"[publish] sanitized {k} (removed whitespace)")
            os.environ[k] = stripped


def main() -> int:
    _sanitize_env()
    payload = build_payload()

    out_dir = Path(__file__).parent / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(payload, indent=2, default=str))
    print(f"[publish] wrote {out_dir / 'latest.json'}")

    # Save decision log CSV locally too
    log = payload.get("decision_log", [])
    if log:
        import csv
        with open(out_dir / "decision_log.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["date", "new_state", "trigger", "spy", "vix", "hy_oas"])
            w.writeheader()
            for row in reversed(log):
                w.writerow(row)
        print(f"[publish] wrote {out_dir / 'decision_log.csv'} ({len(log)} entries)")

    _upload_r2(payload)
    _upload_decision_log_csv(log)
    _upload_research_log()
    return 0


if __name__ == "__main__":
    sys.exit(main())
