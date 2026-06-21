"""
Fetch multi-year 1h OHLCV from Binance for XRPUSDC/BTCUSDC/ETHUSDC/SOLUSDC,
then resample to 4h / 12h / 1d.

Usage:
    venv/bin/python tools/fetch_historical_candles.py

Features:
  - Backs up existing data/candles/ before overwriting
  - Resumable: picks up from last existing timestamp per symbol
  - Rate-limit safe: 0.25 s sleep between requests (~240 req/min, well under 1200)
  - Deduplication + UTC sort before save
  - Resample 1h → 4h / 12h / 1d via pandas

No live trading. No orders. Offline data collection only.
"""
from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SYMBOLS    = ["XRPUSDC", "BTCUSDC", "ETHUSDC", "SOLUSDC"]
START_DATE = "2021-01-01"           # inclusive
BASE_URL   = "https://api.binance.com/api/v3/klines"
INTERVAL   = "1h"
LIMIT      = 1000                   # max per request
SLEEP_SEC  = 0.25                   # between requests

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "candles"

RESAMPLE_MAP = {
    "4h":  "4h",
    "12h": "12h",
    "1d":  "1D",
}

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _backup_existing() -> None:
    if not DATA_DIR.exists() or not any(DATA_DIR.iterdir()):
        return
    ts  = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    dst = DATA_DIR.parent / f"candles_backup_{ts}"
    shutil.copytree(DATA_DIR, dst)
    print(f"Backup: {dst}")


def _existing_end_ms(symbol: str) -> int | None:
    """Return ms timestamp of last candle in existing 1h CSV, or None."""
    path = DATA_DIR / f"{symbol}_{INTERVAL}.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, usecols=["timestamp"])
        if df.empty:
            return None
        last_ts = pd.to_datetime(df["timestamp"]).max()
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        # Add 1 ms so we don't re-download the last candle
        return int(last_ts.timestamp() * 1000) + 1
    except Exception:
        return None


def _load_existing_rows(symbol: str) -> list[list]:
    """Load already-downloaded rows from 1h CSV as raw list."""
    path = DATA_DIR / f"{symbol}_{INTERVAL}.csv"
    if not path.exists():
        return []
    df = pd.read_csv(path)
    df.columns = [c.lower().strip() for c in df.columns]
    df = df[COLUMNS]
    return df.values.tolist()


def _fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    """
    Fetch all 1h candles for symbol between start_ms and end_ms.
    Returns list of [ts_iso, open, high, low, close, volume].
    """
    rows: list[list] = []
    cur_start = start_ms

    while cur_start < end_ms:
        params = {
            "symbol":    symbol,
            "interval":  INTERVAL,
            "startTime": cur_start,
            "endTime":   end_ms,
            "limit":     LIMIT,
        }
        try:
            resp = requests.get(BASE_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as exc:
            print(f"  [WARN] request failed: {exc} — retrying in 5 s")
            time.sleep(5)
            continue

        if not data:
            break

        for candle in data:
            open_time_ms = int(candle[0])
            ts = datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc)
            rows.append([
                ts.strftime("%Y-%m-%d %H:%M:%S"),
                float(candle[1]),   # open
                float(candle[2]),   # high
                float(candle[3]),   # low
                float(candle[4]),   # close
                float(candle[5]),   # volume
            ])

        # Advance past last returned candle
        last_open_ms = int(data[-1][0])
        cur_start = last_open_ms + 1

        if len(data) < LIMIT:
            break

        time.sleep(SLEEP_SEC)

    return rows


def _merge_and_dedup(existing: list[list], new_rows: list[list]) -> pd.DataFrame:
    """Combine, deduplicate on timestamp, sort ascending."""
    all_rows = existing + new_rows
    if not all_rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(all_rows, columns=COLUMNS)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = (
        df.drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    df["timestamp"] = df["timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return df


def _save_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def _resample_and_save(df_1h: pd.DataFrame, symbol: str) -> dict[str, int]:
    """Resample 1h DataFrame to 4h / 12h / 1d and save. Returns row counts."""
    counts: dict[str, int] = {}

    ts = pd.to_datetime(df_1h["timestamp"], utc=True)
    df = df_1h.copy()
    df.index = ts

    for label, rule in RESAMPLE_MAP.items():
        agg = df.resample(rule, closed="left", label="left").agg(
            open=("open",   "first"),
            high=("high",   "max"),
            low =("low",    "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        ).dropna(subset=["open"])

        agg = agg.reset_index().rename(columns={"timestamp": "timestamp"})
        # The index after resample is the timestamp column
        agg.columns = [c if c != "index" else "timestamp" for c in agg.columns]
        # rename the DatetimeIndex column
        if agg.columns[0] != "timestamp":
            agg = agg.rename(columns={agg.columns[0]: "timestamp"})
        agg["timestamp"] = pd.to_datetime(agg["timestamp"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        agg = agg[COLUMNS]

        path = DATA_DIR / f"{symbol}_{label}.csv"
        _save_csv(agg, path)
        counts[label] = len(agg)

    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Uranus Range Lab — FÁZIS 7A historikus letöltés")
    print(f"Időszak: {START_DATE} → {datetime.now(timezone.utc).date()}")
    print("=" * 60)

    _backup_existing()

    start_ms = int(
        datetime.strptime(START_DATE, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp() * 1000
    )
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    summary_rows: list[dict] = []

    for symbol in SYMBOLS:
        print(f"\n--- {symbol} ---")

        # Resumable: start from last existing candle if present
        resume_ms = _existing_end_ms(symbol)
        effective_start = resume_ms if resume_ms else start_ms

        if resume_ms:
            existing = _load_existing_rows(symbol)
            print(f"  Meglévő: {len(existing)} sor — folytatás innen: "
                  f"{datetime.fromtimestamp(resume_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')}")
        else:
            existing = []
            print(f"  Új letöltés: {START_DATE}–tól")

        new_rows = _fetch_klines(symbol, effective_start, end_ms)
        print(f"  Letöltve: {len(new_rows)} új sor")

        df_1h = _merge_and_dedup(existing, new_rows)
        if df_1h.empty:
            print(f"  [WARN] {symbol} — nincsenek adatok, kihagyva")
            continue

        # Save 1h
        path_1h = DATA_DIR / f"{symbol}_{INTERVAL}.csv"
        _save_csv(df_1h, path_1h)

        ts_series = pd.to_datetime(df_1h["timestamp"])
        row_from  = ts_series.min().strftime("%Y-%m-%d")
        row_to    = ts_series.max().strftime("%Y-%m-%d")
        n_1h      = len(df_1h)
        print(f"  1h: {n_1h} sor  ({row_from} → {row_to})")

        # Resample
        counts = _resample_and_save(df_1h, symbol)
        for tf, cnt in counts.items():
            print(f"  {tf}: {cnt} sor")
            summary_rows.append({
                "symbol":    symbol,
                "timeframe": tf,
                "rows":      cnt,
                "from":      row_from,
                "to":        row_to,
            })

        summary_rows.append({
            "symbol":    symbol,
            "timeframe": "1h",
            "rows":      n_1h,
            "from":      row_from,
            "to":        row_to,
        })

    # Print summary table
    print("\n" + "=" * 60)
    print("SUMMARY")
    print(f"{'symbol':<12} {'timeframe':<10} {'rows':>7}  {'from':<12} {'to':<12}")
    print("-" * 60)
    tf_order = {"1h": 0, "4h": 1, "12h": 2, "1d": 3}
    summary_rows.sort(key=lambda r: (r["symbol"], tf_order.get(r["timeframe"], 99)))
    for r in summary_rows:
        print(f"{r['symbol']:<12} {r['timeframe']:<10} {r['rows']:>7}  {r['from']:<12} {r['to']:<12}")
    print("=" * 60)
    print(f"\nFájlok helye: {DATA_DIR}")


if __name__ == "__main__":
    main()
