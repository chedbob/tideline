"""Live regime computation for Tideline.

Runs rule/v1.py against current data and returns the state today,
when it last transitioned, and the full state history for the
methodology decision log.

Every call reprocesses the full 1997-present panel from scratch.
This is deterministic, idempotent, and robust to missed cron runs.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import numpy as np
import pandas as pd

import hashlib
from pathlib import Path

from rule import v1 as rule_v1
from rule.v1 import (
    RegimeContext, evaluate as regime_evaluate, faber_signal,
    dwell_for, PERCENTILE_WINDOW_DAYS, NFCI_RELEASE_LAG_DAYS,
    classify_hy, classify_vix, classify_curve, classify_nfci,
)


def _rule_fingerprint() -> str:
    """SHA256 of rule/v1.py — tamper-evident integrity for every published payload."""
    rule_path = Path(rule_v1.__file__)
    return hashlib.sha256(rule_path.read_bytes()).hexdigest()

START = "1997-01-01"
FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Tideline-Publish/0.2)"}

# ----------------------------------------------------------------------
# Data fetch
# ----------------------------------------------------------------------

def _fred(client: httpx.Client, sid: str, key: str, retries: int = 3) -> pd.Series:
    last_err = None
    for attempt in range(retries):
        try:
            r = client.get(FRED_BASE, params={
                "series_id": sid, "api_key": key, "file_type": "json",
                "observation_start": START, "sort_order": "asc"}, timeout=30)
            r.raise_for_status()
            recs = [(o["date"], float(o["value"])) for o in r.json()["observations"] if o["value"] not in (".", "")]
            if not recs:
                raise ValueError(f"empty FRED result for {sid}")
            dates, vals = zip(*recs)
            return pd.Series(vals, index=pd.to_datetime(dates), name=sid)
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"FRED fetch failed for {sid}: {last_err}")


def _yahoo(client: httpx.Client, ticker: str, start: str = START, retries: int = 3) -> pd.Series:
    p1 = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    p2 = int(time.time())
    last_err = None
    for attempt in range(retries):
        try:
            r = client.get(YAHOO.format(ticker=ticker),
                params={"period1": p1, "period2": p2, "interval": "1d"},
                headers=HEADERS, timeout=30)
            r.raise_for_status()
            data = r.json()["chart"]["result"][0]
            ts = pd.to_datetime(data["timestamp"], unit="s", utc=True).tz_convert("America/New_York").normalize().tz_localize(None)
            closes = data["indicators"]["quote"][0]["close"]
            s = pd.Series(closes, index=ts, name=ticker).dropna()
            return s[~s.index.duplicated(keep="last")]
        except Exception as e:
            last_err = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Yahoo fetch failed for {ticker}: {last_err}")


def build_panel(api_key: str) -> pd.DataFrame:
    with httpx.Client() as fc, httpx.Client(headers=HEADERS) as yc:
        hy = _fred(fc, "BAA10Y", api_key)
        vix = _fred(fc, "VIXCLS", api_key)
        curve = _fred(fc, "T10Y3M", api_key)
        nfci = _fred(fc, "NFCI", api_key)
        spy = _yahoo(yc, "SPY")
        try:
            vix3m_yh = _yahoo(yc, "^VIX3M")
        except Exception:
            vix3m_yh = pd.Series(dtype=float)

    df = pd.DataFrame({"spy": spy})
    df["hy_oas"] = hy.reindex(df.index, method="ffill")
    df["vix"] = vix.reindex(df.index, method="ffill")
    df["curve_3m10y"] = curve.reindex(df.index, method="ffill")
    nfci_daily = nfci.reindex(df.index, method="ffill")
    df["nfci_lagged"] = nfci_daily.shift(NFCI_RELEASE_LAG_DAYS)

    vix3m_daily = vix3m_yh.reindex(df.index) if len(vix3m_yh) else pd.Series(index=df.index, dtype=float)
    vix3m_proxy = df["vix"].rolling(63, min_periods=20).mean()
    df["vix3m"] = vix3m_daily.combine_first(vix3m_proxy)

    # SPY moving averages for Faber
    df["ma_50"] = df["spy"].rolling(50).mean()
    df["ma_200"] = df["spy"].rolling(200).mean()

    df = df.dropna(subset=["spy", "hy_oas", "vix", "curve_3m10y"])
    return df


# ----------------------------------------------------------------------
# Features + percentiles
# ----------------------------------------------------------------------

def _add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["hy_oas_5d_change"] = df["hy_oas"].diff(5)
    df["vix_1d_change"] = df["vix"].diff(1)
    df["vix_3d_change"] = df["vix"].diff(3)
    df["vix_5d_change"] = df["vix"].diff(5)
    df["nfci_1w_change"] = df["nfci_lagged"].diff(5)
    return df


def _rolling_percentiles(df: pd.DataFrame) -> dict:
    window = PERCENTILE_WINDOW_DAYS
    levels = [10, 15, 25, 30, 50, 70, 75, 85, 90]

    def pc(s: pd.Series) -> dict:
        out = {}
        prior = s.shift(1)
        for lvl in levels:
            out[lvl] = prior.rolling(window, min_periods=252).quantile(lvl/100)
        return out

    return {
        "hy_5d":    pc(df["hy_oas_5d_change"]),
        "vix_3d":   pc(df["vix_3d_change"]),
        "vix_5d":   pc(df["vix_5d_change"]),
        "hy_level": pc(df["hy_oas"]),
        "vix_level": pc(df["vix"]),
    }


# ----------------------------------------------------------------------
# Run state machine
# ----------------------------------------------------------------------

def run_state_machine(df: pd.DataFrame, pct: dict) -> pd.DataFrame:
    states, triggers = [], []
    current = "NORMAL"
    dwell = 0

    for i, (date, row) in enumerate(df.iterrows()):
        def p(key, level):
            v = pct[key][level].iloc[i] if i < len(pct[key][level]) else None
            return float(v) if v is not None and not pd.isna(v) else 0

        pct_hy_5d = {x: p("hy_5d", x) for x in pct["hy_5d"]}
        pct_vix_3d = {x: p("vix_3d", x) for x in pct["vix_3d"]}
        pct_vix_5d = {x: p("vix_5d", x) for x in pct["vix_5d"]}
        pct_hy_lvl = {x: p("hy_level", x) for x in pct["hy_level"]}
        pct_vix_lvl = {x: p("vix_level", x) for x in pct["vix_level"]}

        if pct_hy_5d.get(85, 0) == 0 and i < 252:
            states.append(current); triggers.append(None); dwell += 1; continue

        if pd.isna(row.get("nfci_lagged")) or pd.isna(row.get("nfci_1w_change")):
            states.append(current); triggers.append(None); dwell += 1; continue

        v5chg = row["vix_5d_change"] if not pd.isna(row["vix_5d_change"]) else 0

        ctx = RegimeContext(
            hy_oas=row["hy_oas"], vix=row["vix"],
            vix3m=row["vix3m"] if not pd.isna(row["vix3m"]) else None,
            curve_3m10y=row["curve_3m10y"], nfci_lagged=row["nfci_lagged"],
            hy_oas_5d_change=row["hy_oas_5d_change"] if not pd.isna(row["hy_oas_5d_change"]) else 0,
            vix_1d_change=row["vix_1d_change"] if not pd.isna(row["vix_1d_change"]) else 0,
            vix_3d_change=row["vix_3d_change"] if not pd.isna(row["vix_3d_change"]) else 0,
            vix_5d_change=v5chg,
            nfci_1w_change=row["nfci_1w_change"] if not pd.isna(row["nfci_1w_change"]) else 0,
            vix_5d_ago=row["vix"] - v5chg,
            days_in_state=dwell,
            pct_hy_5d=pct_hy_5d, pct_vix_3d=pct_vix_3d, pct_vix_5d=pct_vix_5d,
            pct_hy_level=pct_hy_lvl, pct_vix_level=pct_vix_lvl,
        )

        # Determine dwell for the specific candidate transition
        candidate_primary = rule_v1.primary_transition(current, ctx)
        req_dwell = dwell_for(current, candidate_primary)

        new_state, trigger = regime_evaluate(current, ctx, req_dwell)

        if new_state != current:
            current = new_state
            dwell = 1
        else:
            dwell += 1
        states.append(current)
        triggers.append(trigger)

    out = df.copy()
    out["state"] = states
    out["trigger"] = triggers
    return out


# ----------------------------------------------------------------------
# Assemble payload
# ----------------------------------------------------------------------

def build_regime_snapshot(df_states: pd.DataFrame, today_row: pd.Series, pct: dict, i_today: int) -> dict:
    """Extract the snapshot fields that go into latest.json."""
    state = today_row["state"]
    state_series = df_states["state"]
    # last transition
    changes = state_series.ne(state_series.shift())
    last_change_idx = changes[changes & (state_series.index <= today_row.name)].index
    last_transition_date = str(last_change_idx[-1].date()) if len(last_change_idx) else None
    days_in_state = (df_states.index <= today_row.name).sum() - (df_states.index[df_states["state"] == state].min().index.min() if False else 0)
    # simpler: how many consecutive days of the same state ending today
    tail = state_series.loc[:today_row.name]
    days_in = 1
    for v in reversed(tail.iloc[:-1].tolist()):
        if v == state:
            days_in += 1
        else:
            break

    spy = float(today_row["spy"])
    ma50 = float(today_row["ma_50"])
    ma200 = float(today_row["ma_200"])

    faber = faber_signal(spy, ma50, ma200)

    # Percentile of today's HY OAS within trailing 5y (for Zone 2)
    def current_pct(series_key, level_key):
        pct_p = pct[series_key][level_key]
        if i_today >= len(pct_p) or pd.isna(pct_p.iloc[i_today]):
            return None
        return float(pct_p.iloc[i_today])

    hy = float(today_row["hy_oas"])
    vix = float(today_row["vix"])
    curve = float(today_row["curve_3m10y"])
    nfci = float(today_row["nfci_lagged"]) if not pd.isna(today_row.get("nfci_lagged")) else None

    # Reverse-compute percentile-of-today using window comparison
    hy_hist = df_states["hy_oas"].iloc[max(0, i_today - PERCENTILE_WINDOW_DAYS):i_today]
    vix_hist = df_states["vix"].iloc[max(0, i_today - PERCENTILE_WINDOW_DAYS):i_today]
    hy_pct_today = round(float((hy_hist < hy).mean()), 3) if len(hy_hist) else None
    vix_pct_today = round(float((vix_hist < vix).mean()), 3) if len(vix_hist) else None

    return {
        "rule_version": rule_v1.RULE_VERSION,
        "rule_lineage": rule_v1.RULE_LINEAGE,
        "rule_sha256": _rule_fingerprint(),
        "data_as_of": str(today_row.name.date()),
        "zones": {
            "trend_signal": {
                "state": faber,
                "evidence": {
                    "spy_close": round(spy, 2),
                    "ma_50": round(ma50, 2),
                    "ma_200": round(ma200, 2),
                    "spy_vs_200": round(spy / ma200 - 1, 4),
                    "ma50_vs_200": round(ma50 / ma200 - 1, 4),
                },
                "historical_claim": rule_v1.FABER_CLAIM,
            },
            "regime_state": {
                "state": state,
                "days_in_state": int(days_in),
                "last_transition_date": last_transition_date,
                "last_transition_trigger": (df_states["trigger"].dropna().iloc[-1] if df_states["trigger"].dropna().size else None),
                "description": {
                    "EASY":     "Calm conditions, credit tight, vol low",
                    "NORMAL":   "Baseline conditions, no active stress signal",
                    "ELEVATED": "At least one stress signal firing, heightened vigilance",
                    "STRESS":   "Multiple simultaneous stress signals, historically associated with drawdowns",
                }.get(state, ""),
            },
            "components": {
                "hy_oas":      {"value": round(hy, 2), "unit": "%", "percentile_5y": hy_pct_today, "state": classify_hy(hy, hy_pct_today or 0.5)},
                "vix":         {"value": round(vix, 2), "percentile_5y": vix_pct_today, "state": classify_vix(vix)},
                "curve_3m10y": {"value": round(curve, 2), "unit": "%", "state": classify_curve(curve)},
                "nfci_lagged": {"value": round(nfci, 2) if nfci is not None else None, "state": classify_nfci(nfci) if nfci is not None else None, "note": f"released with {NFCI_RELEASE_LAG_DAYS}-day lag"},
            },
        },
        "baseline_references": {
            "spy_20d_up_rate_1997_2026": 0.631,
            "spy_20d_down_rate_1997_2026": 0.369,
        },
        "disclaimer": "Descriptive regime tracker. Not investment advice. Past performance does not predict future results.",
    }


def _tide_score(faber: str, regime: str) -> int:
    """Mirrors web/index.html computeTideScore — 0..100."""
    s = 50
    if faber == "GREEN":
        s += 22
    elif faber == "CAUTION":
        s -= 22
    s += {"EASY": 14, "NORMAL": 0, "ELEVATED": -8, "STRESS": -22}.get(regime, 0)
    return max(0, min(100, s))


def compute_tide_history(df_states: pd.DataFrame, days: int = 90) -> list[dict]:
    """Daily Tide Score + state + forward returns + verdict for the last N days.

    Each entry:
      date, tide_score, faber, regime, spy_close,
      spy_5d_fwd_return, spy_20d_fwd_return,
      spy_5d_outcome   ('UP' / 'DOWN' / 'FLAT' / null),
      spy_20d_outcome  (...),
      verdict_5d       ('hit' / 'miss' / null)   — based on Faber call
      verdict_20d      (...)

    Verdict logic (matches the bootstrap-validated Faber claim):
      GREEN   → predicts UP  → hit if forward return > 0
      CAUTION → predicts DOWN → hit if forward return < 0
      NEUTRAL → no call      → verdict null
    """
    df = df_states.copy()
    spy = df["spy"]
    df["spy_5d_fwd"]  = spy.shift(-5)  / spy - 1
    df["spy_20d_fwd"] = spy.shift(-20) / spy - 1

    df["faber"] = [faber_signal(s, m50, m200) for s, m50, m200 in
                   zip(df["spy"], df["ma_50"], df["ma_200"])]

    tail = df.tail(days)
    out = []
    for date, row in tail.iterrows():
        faber = row["faber"]
        regime = row["state"]
        score = _tide_score(faber, regime)

        def bucket(r, threshold=0.005):
            if pd.isna(r):
                return None
            if r > threshold:  return "UP"
            if r < -threshold: return "DOWN"
            return "FLAT"

        out_5d  = bucket(row["spy_5d_fwd"])
        out_20d = bucket(row["spy_20d_fwd"])

        def verdict(call_state, outcome):
            if outcome is None:
                return None  # not yet resolved
            if call_state == "GREEN":
                return "hit" if outcome == "UP" else ("miss" if outcome == "DOWN" else "flat")
            if call_state == "CAUTION":
                return "hit" if outcome == "DOWN" else ("miss" if outcome == "UP" else "flat")
            return None  # NEUTRAL = no call

        out.append({
            "date": str(date.date()),
            "tide_score": score,
            "faber": faber,
            "regime": regime,
            "spy_close": round(float(row["spy"]), 2),
            "spy_5d_fwd_return": None if pd.isna(row["spy_5d_fwd"]) else round(float(row["spy_5d_fwd"]) * 100, 2),
            "spy_20d_fwd_return": None if pd.isna(row["spy_20d_fwd"]) else round(float(row["spy_20d_fwd"]) * 100, 2),
            "spy_5d_outcome": out_5d,
            "spy_20d_outcome": out_20d,
            "verdict_5d": verdict(faber, out_5d),
            "verdict_20d": verdict(faber, out_20d),
        })
    return out


def compute_decision_log(df_states: pd.DataFrame) -> list[dict]:
    """Return list of every state transition, newest first."""
    changes = df_states["state"].ne(df_states["state"].shift())
    log_rows = df_states[changes].iloc[1:]  # skip first row (initial state, not a transition)
    log = []
    for date, row in log_rows.iterrows():
        log.append({
            "date": str(date.date()),
            "new_state": row["state"],
            "trigger": row["trigger"],
            "spy": round(float(row["spy"]), 2),
            "vix": round(float(row["vix"]), 2),
            "hy_oas": round(float(row["hy_oas"]), 2),
        })
    log.reverse()  # newest first
    return log


def run(api_key: str) -> dict:
    """Main entry — returns full regime payload for publish.py to serialize."""
    df = build_panel(api_key)
    df = _add_features(df)
    pct = _rolling_percentiles(df)
    df_states = run_state_machine(df, pct)

    i_today = len(df_states) - 1
    today_row = df_states.iloc[-1]
    snapshot = build_regime_snapshot(df_states, today_row, pct, i_today)
    decision_log = compute_decision_log(df_states)
    tide_history = compute_tide_history(df_states, days=180)  # ~6 months

    return {
        "snapshot": snapshot,
        "decision_log": decision_log,
        "tide_history": tide_history,
        "panel_meta": {
            "start": str(df_states.index.min().date()),
            "end": str(df_states.index.max().date()),
            "days": int(len(df_states)),
        },
    }
