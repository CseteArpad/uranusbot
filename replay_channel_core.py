#!/usr/bin/env python3
# replay_channel_core.py — FÁZIS 1: multi-timeframe pivot channel engine replay
# Reads historical XRP/USDC 1m candles, computes parallel channels per timeframe,
# aggregates CP, derives trend state and candidate action.  No real orders.

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Allow "import channel_engine" when run from the repo root
sys.path.insert(0, str(Path(__file__).parent / "app"))
import channel_engine as ce

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CANDLE_PATH = r"/opt/bots/uranus/freqtrade/user_data/data/binance/XRP_USDC-1m.json"
REPORTS_DIR = "reports"

REPLAY_DAYS = 30

# (name, minutes_per_bar, lookback_bars_of_completed_tf_bars)
TIMEFRAMES: List[Tuple[str, int, int]] = [
    ("1H",   60,   50),
    ("4H",   240,  30),
    ("12H",  720,  20),
    ("1D",   1440, 14),
]

WEIGHTS: Dict[str, float] = {"1H": 0.10, "4H": 0.20, "12H": 0.30, "1D": 0.40}

PIVOT_LEFT  = 3
PIVOT_RIGHT = 3

BUY_ZONE_THRESHOLD  = 0.20
SELL_ZONE_THRESHOLD = 0.80


# ---------------------------------------------------------------------------
# Candle loading
# ---------------------------------------------------------------------------

def _load_candles(path: str) -> List:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Format: [[ts_ms, open, high, low, close, volume], ...]
    return sorted(data, key=lambda c: c[0])


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

def _resample(candles_1m: List, tf_minutes: int) -> List[dict]:
    """Aggregate completed 1m candles into tf-minute OHLCV bars."""
    ms_per_bar = tf_minutes * 60 * 1000
    buckets: Dict[int, dict] = {}
    for c in candles_1m:
        bar_ts = (int(c[0]) // ms_per_bar) * ms_per_bar
        if bar_ts not in buckets:
            buckets[bar_ts] = {
                "ts_ms":  bar_ts,
                "open":   c[1],
                "high":   c[2],
                "low":    c[3],
                "close":  c[4],
                "volume": c[5],
            }
        else:
            b = buckets[bar_ts]
            if c[2] > b["high"]:
                b["high"] = c[2]
            if c[3] < b["low"]:
                b["low"] = c[3]
            b["close"]   = c[4]
            b["volume"] += c[5]
    return sorted(buckets.values(), key=lambda x: x["ts_ms"])


# ---------------------------------------------------------------------------
# Channel cache: precompute channel for every completed TF bar
# ---------------------------------------------------------------------------

def _build_channel_cache(tf_bars: List[dict], lookback: int) -> Dict[int, dict]:
    """
    For bar at index i, build a channel from bars[max(0, i-lookback) : i]
    (all COMPLETED bars before bar i).  The result is keyed by bar ts_ms.
    """
    cache: Dict[int, dict] = {}
    min_needed = PIVOT_LEFT + PIVOT_RIGHT + 2
    for i, bar in enumerate(tf_bars):
        start  = max(0, i - lookback)
        window = tf_bars[start:i]
        if len(window) < min_needed:
            ch = {"valid": False, "reason": "insufficient_history", "n_bars": 0}
        else:
            highs = [b["high"] for b in window]
            lows  = [b["low"]  for b in window]
            ch = ce.build_parallel_channel(highs, lows, PIVOT_LEFT, PIVOT_RIGHT)
        cache[bar["ts_ms"]] = ch
    return cache


# ---------------------------------------------------------------------------
# Per-candle computation
# ---------------------------------------------------------------------------

def _compute_cp(close: float, ch: dict) -> Optional[float]:
    if not ch or not ch.get("valid"):
        return None
    n = ch["n_bars"]
    if n == 0:
        return None
    lo, up = ce.evaluate_channel_at(ch, n - 1)
    try:
        return ce.channel_position(close, lo, up)
    except ValueError:
        return None


def _cpagg(cp_vals: Dict[str, Optional[float]]) -> Optional[float]:
    """Weighted average of available (non-None) CPs; renormalize if some TFs missing."""
    wsum = 0.0
    wtot = 0.0
    for tf, cp in cp_vals.items():
        if cp is not None:
            w = WEIGHTS[tf]
            wsum += w * cp
            wtot += w
    return wsum / wtot if wtot > 0 else None


def _trend_state(cp_vals: Dict[str, Optional[float]]) -> Optional[str]:
    cp_12h = cp_vals.get("12H")
    cp_1d  = cp_vals.get("1D")
    if cp_12h is None or cp_1d is None:
        return None
    if cp_12h > 0.5 and cp_1d > 0.5:
        return "LONG"
    if cp_12h < 0.5 and cp_1d < 0.5:
        return "SHORT"
    return "SIDEWAYS"


def _candidate_action(cpagg_val: Optional[float]) -> Optional[str]:
    if cpagg_val is None:
        return None
    if cpagg_val <= BUY_ZONE_THRESHOLD:
        return "BUY_ZONE"
    if cpagg_val >= SELL_ZONE_THRESHOLD:
        return "SELL_ZONE"
    return "HOLD_ZONE"


def _fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _r(v: Optional[float], places: int = 4) -> Optional[float]:
    return round(v, places) if v is not None else None


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

def _write_txt(path: str, result: dict, rows: List[dict]) -> None:
    cfg   = result["config"]
    summ  = result["summary"]

    def _pct(v):
        return f"{v:.4f}" if v is not None else "N/A"

    lines = [
        "=" * 72,
        "  Uranus Channel Core — FÁZIS 1 Replay",
        "=" * 72,
        "",
        f"  Candle file   : {cfg['candle_path']}",
        f"  Replay window : {cfg['replay_start_utc']}  to  {cfg['replay_end_utc']}",
        f"  Replay days   : {cfg['replay_days']}",
        f"  Pivot left    : {cfg['pivot_left']}   right : {cfg['pivot_right']}",
        "",
        "-" * 72,
        "  Channel validity per timeframe",
        "-" * 72,
    ]
    for tf, _, _ in TIMEFRAMES:
        valid   = summ[f"cp_valid_ticks_{tf}"]
        total   = summ["candles_1m_processed"]
        pct     = 100.0 * valid / total if total else 0.0
        ch_fail = summ[f"channel_build_invalid_{tf}"]
        lines.append(
            f"  {tf:>4s}  cp_valid={valid:>6d}/{total}  ({pct:5.1f}%)  "
            f"build_invalid={ch_fail}"
        )

    lines += [
        "",
        "-" * 72,
        "  Last computed values",
        "-" * 72,
        f"  CP_1H   = {_pct(summ['last_cp_1h'])}",
        f"  CP_4H   = {_pct(summ['last_cp_4h'])}",
        f"  CP_12H  = {_pct(summ['last_cp_12h'])}",
        f"  CP_1D   = {_pct(summ['last_cp_1d'])}",
        f"  CPagg   = {_pct(summ['last_cpagg'])}",
        f"  Trend   = {summ['last_trend_state'] or 'N/A'}",
        f"  Action  = {summ['last_candidate_action'] or 'N/A'}",
        "",
    ]

    def _row_line(r: dict) -> str:
        def _fv(v):
            return f"{v:+.4f}" if v is not None else "  N/A "
        return (
            f"  {r['ts_utc']}  "
            f"{_fv(r['cp_1h'])}  {_fv(r['cp_4h'])}  "
            f"{_fv(r['cp_12h'])}  {_fv(r['cp_1d'])}  "
            f"{_fv(r['cpagg'])}  "
            f"{(r['trend_state'] or 'N/A'):>10s}  "
            f"{(r['candidate_action'] or 'N/A'):>10s}"
        )

    header = (
        "  ts_utc                  CP_1H    CP_4H   CP_12H    CP_1D    CPagg"
        "       TREND      ACTION"
    )

    first20 = result.get("cp_rows_first_20", [])
    last20  = result.get("cp_rows_last_20",  [])

    if first20:
        lines += ["-" * 72, "  First 20 rows", "-" * 72, header]
        lines += [_row_line(r) for r in first20]

    if last20:
        lines += ["", "-" * 72, "  Last 20 rows", "-" * 72, header]
        lines += [_row_line(r) for r in last20]

    lines += ["", "=" * 72]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main replay
# ---------------------------------------------------------------------------

def run() -> dict:
    print(f"Loading candles from {CANDLE_PATH} ...")
    all_candles = _load_candles(CANDLE_PATH)
    print(f"  Total 1m candles in file: {len(all_candles)}")

    # Determine replay window: last REPLAY_DAYS of data
    replay_ms   = REPLAY_DAYS * 24 * 60 * 60 * 1000
    last_ts_ms  = int(all_candles[-1][0])
    replay_start_ms = last_ts_ms - replay_ms

    # Warmup: enough history for the longest TF lookback (1D * 14 = 14 days)
    warmup_ms = max(tf_min * lb for _, tf_min, lb in TIMEFRAMES) * 60 * 1000
    data_start_ms = replay_start_ms - warmup_ms

    working = [c for c in all_candles if int(c[0]) >= data_start_ms]
    replay  = [c for c in working    if int(c[0]) >= replay_start_ms]

    print(f"  Working set : {len(working)} candles  (incl. {warmup_ms // 60000 // 60 // 24}d warmup)")
    print(f"  Replay set  : {len(replay)} candles")

    # Resample & build channel cache per timeframe
    tf_bars:   Dict[str, List[dict]]         = {}
    ch_caches: Dict[str, Dict[int, dict]]    = {}

    for tf_name, tf_min, tf_lb in TIMEFRAMES:
        bars = _resample(working, tf_min)
        tf_bars[tf_name]   = bars
        print(f"  {tf_name:>4s}: {len(bars)} bars  (lookback={tf_lb})")
        ch_caches[tf_name] = _build_channel_cache(bars, tf_lb)

    # Per-TF bar_ms lookup
    bar_ms: Dict[str, int] = {tf: tf_min * 60 * 1000 for tf, tf_min, _ in TIMEFRAMES}

    # Tracking
    cp_valid_ticks:      Dict[str, int] = {tf: 0 for tf, _, _ in TIMEFRAMES}
    channel_build_invalid: Dict[str, int] = {}
    for tf_name, _, _ in TIMEFRAMES:
        channel_build_invalid[tf_name] = sum(
            1 for ch in ch_caches[tf_name].values() if not ch.get("valid")
        )

    rows: List[dict] = []
    last_row: Optional[dict] = None

    print(f"\nReplaying {len(replay)} candles ...")
    for i, candle in enumerate(replay):
        ts_ms = int(candle[0])
        close = float(candle[4])

        cp_vals: Dict[str, Optional[float]] = {}
        for tf_name, _, _ in TIMEFRAMES:
            bms     = bar_ms[tf_name]
            bar_ts  = (ts_ms // bms) * bms
            ch      = ch_caches[tf_name].get(bar_ts)
            cp      = _compute_cp(close, ch)
            cp_vals[tf_name] = cp
            if cp is not None:
                cp_valid_ticks[tf_name] += 1

        agg    = _cpagg(cp_vals)
        trend  = _trend_state(cp_vals)
        action = _candidate_action(agg)

        row = {
            "ts_ms":            ts_ms,
            "ts_utc":           _fmt_ts(ts_ms),
            "cp_1h":            _r(cp_vals.get("1H")),
            "cp_4h":            _r(cp_vals.get("4H")),
            "cp_12h":           _r(cp_vals.get("12H")),
            "cp_1d":            _r(cp_vals.get("1D")),
            "cpagg":            _r(agg),
            "trend_state":      trend,
            "candidate_action": action,
        }
        rows.append(row)
        last_row = row

        if (i + 1) % 5000 == 0:
            print(f"  ... {i + 1}/{len(replay)} ticks  CPagg={_r(agg)}  {action}")

    print(f"  Replay complete. {len(rows)} rows.")

    # Build result dict
    result = {
        "config": {
            "candle_path":      CANDLE_PATH,
            "replay_days":      REPLAY_DAYS,
            "replay_start_utc": _fmt_ts(replay_start_ms),
            "replay_end_utc":   _fmt_ts(last_ts_ms),
            "pivot_left":       PIVOT_LEFT,
            "pivot_right":      PIVOT_RIGHT,
            "weights":          WEIGHTS,
            "timeframes":       {tf: {"minutes": m, "lookback": lb} for tf, m, lb in TIMEFRAMES},
            "buy_zone_threshold":  BUY_ZONE_THRESHOLD,
            "sell_zone_threshold": SELL_ZONE_THRESHOLD,
        },
        "summary": {
            "candles_1m_processed":        len(rows),
            **{f"cp_valid_ticks_{tf}":     cp_valid_ticks[tf]       for tf, _, _ in TIMEFRAMES},
            **{f"channel_build_invalid_{tf}": channel_build_invalid[tf] for tf, _, _ in TIMEFRAMES},
            "last_cp_1h":           last_row["cp_1h"]           if last_row else None,
            "last_cp_4h":           last_row["cp_4h"]           if last_row else None,
            "last_cp_12h":          last_row["cp_12h"]          if last_row else None,
            "last_cp_1d":           last_row["cp_1d"]           if last_row else None,
            "last_cpagg":           last_row["cpagg"]           if last_row else None,
            "last_trend_state":     last_row["trend_state"]     if last_row else None,
            "last_candidate_action":last_row["candidate_action"] if last_row else None,
        },
        "cp_rows_first_20": rows[:20],
        "cp_rows_last_20":  rows[-20:],
    }

    return result


def main() -> None:
    os.makedirs(REPORTS_DIR, exist_ok=True)
    json_path = os.path.join(REPORTS_DIR, "channel_core_xrp_30d.json")
    txt_path  = os.path.join(REPORTS_DIR, "channel_core_xrp_30d.txt")

    result = run()

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\nJSON report: {json_path}")

    _write_txt(txt_path, result, [])
    print(f"TXT  report: {txt_path}")

    # Console summary
    s = result["summary"]
    print("\n" + "=" * 60)
    print("  Channel Core — Final Summary")
    print("=" * 60)
    for tf, _, _ in TIMEFRAMES:
        valid = s[f"cp_valid_ticks_{tf}"]
        total = s["candles_1m_processed"]
        pct   = 100.0 * valid / total if total else 0.0
        print(f"  {tf:>4s}  valid_ticks={valid}/{total}  ({pct:.1f}%)")
    print(f"\n  Last CPagg  = {s['last_cpagg']}")
    print(f"  Last Trend  = {s['last_trend_state']}")
    print(f"  Last Action = {s['last_candidate_action']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
