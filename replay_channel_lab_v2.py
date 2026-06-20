#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay_channel_lab_v2.py -- Uranus Channel Lab V2.1

Offline regression-channel strategy research on XRP/USDC 1m historical candles.
No live bot files modified. No state.json touched. No orders placed.

Channel model:
    1. Aggregate 1m candles into 1H bars.
    2. Fit a linear regression over the last `channel_lookback_hours` completed 1H bars.
    3. Upper band = regression_line + std_mult * stddev(residuals).
    4. Lower band = regression_line - std_mult * stddev(residuals).
    5. Channel projected forward to current 1m candle timestamp (no lookahead).

channel_position = (price - lower) / (upper - lower)
    0.0 = at lower band, 1.0 = at upper band.

Trade rules (one position at a time, full-equity sizing):
    Flat:
        STANDARD_BUY  if channel_position <= buy_zone_pct
        PANIC_BUY     if price >= upper_band * (1 + panic_buy_breakout_pct)
    In position:
        STANDARD_SELL if channel_position >= 1 - sell_zone_pct
        PANIC_SELL    if price <= lower_band * (1 - panic_sell_breakout_pct)
    PANIC rules take priority over STANDARD rules.

Output:
    reports/channel_lab_v2_xrp_30d.json
    reports/channel_lab_v2_xrp_30d.txt

Usage examples:
    python replay_channel_lab_v2.py
    python replay_channel_lab_v2.py --days 7 --equity 50.0 --verbose
    python replay_channel_lab_v2.py --lookback 100 --std-mult 1.5 --verbose
    python replay_channel_lab_v2.py --buy-zone 0.20 --sell-zone 0.20
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths and defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_CANDLE_PATH = Path(
    "/opt/bots/uranus/freqtrade/user_data/data/binance/XRP_USDC-1m.json"
)
REPORTS_DIR = SCRIPT_DIR / "reports"

DEFAULT_EQUITY                   = 19.25297507
DEFAULT_DAYS                     = 30
DEFAULT_FEE_PCT                  = 0.1    # percent per side
DEFAULT_SLIPPAGE_PCT             = 0.0    # percent
DEFAULT_BUY_ZONE_PCT             = 0.15
DEFAULT_SELL_ZONE_PCT            = 0.15
DEFAULT_PANIC_BUY_BREAKOUT_PCT   = 0.01
DEFAULT_PANIC_SELL_BREAKOUT_PCT  = 0.01
DEFAULT_CHANNEL_LOOKBACK_HOURS   = 200
DEFAULT_CHANNEL_STD_MULT         = 2.0
DEFAULT_PROFIT_BUFFER_PCT        = 0.001  # extra buffer on top of 2*fee for profit guard

ONE_HOUR_MS = 3_600_000

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Uranus Channel Lab V2 -- regression-channel strategy research",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--candles", default=str(DEFAULT_CANDLE_PATH),
                   help="Freqtrade 1m JSON candle file")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help="Replay window in days (from end of file)")
    p.add_argument("--equity", type=float, default=DEFAULT_EQUITY,
                   help="Starting equity USDC")
    p.add_argument("--fee", type=float, default=DEFAULT_FEE_PCT,
                   help="Fee per side in percent (0.1 = 0.1%%)")
    p.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE_PCT,
                   help="Slippage in percent")
    p.add_argument("--lookback", type=int, default=DEFAULT_CHANNEL_LOOKBACK_HOURS,
                   help="Channel lookback in completed 1H bars")
    p.add_argument("--std-mult", type=float, default=DEFAULT_CHANNEL_STD_MULT,
                   help="Std-dev multiplier for channel band width")
    p.add_argument("--buy-zone", type=float, default=DEFAULT_BUY_ZONE_PCT,
                   help="STANDARD_BUY if channel_position <= this (e.g. 0.15)")
    p.add_argument("--sell-zone", type=float, default=DEFAULT_SELL_ZONE_PCT,
                   help="STANDARD_SELL if channel_position >= 1-this (e.g. 0.15)")
    p.add_argument("--panic-buy-pct", type=float, default=DEFAULT_PANIC_BUY_BREAKOUT_PCT,
                   help="PANIC_BUY if price >= upper * (1 + this)")
    p.add_argument("--panic-sell-pct", type=float, default=DEFAULT_PANIC_SELL_BREAKOUT_PCT,
                   help="PANIC_SELL if price <= lower * (1 - this)")
    p.add_argument("--output-prefix", default="channel_lab_v2_xrp_30d",
                   help="Output filename prefix (no extension)")
    p.add_argument("--verbose", action="store_true",
                   help="Print per-trade log to stdout")
    return p.parse_args(argv)


def _args_to_cfg(args: argparse.Namespace) -> dict:
    return {
        "candle_path":              Path(args.candles),
        "days":                     args.days,
        "start_equity":             args.equity,
        "fee_pct":                  args.fee,
        "slippage_pct":             args.slippage,
        "channel_lookback_hours":   args.lookback,
        "channel_std_mult":         args.std_mult,
        "buy_zone_pct":             args.buy_zone,
        "sell_zone_pct":            args.sell_zone,
        "panic_buy_breakout_pct":   args.panic_buy_pct,
        "panic_sell_breakout_pct":  args.panic_sell_pct,
        "profit_buffer_pct":        DEFAULT_PROFIT_BUFFER_PCT,
        "output_prefix":            args.output_prefix,
    }


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------

def load_candles(path: Path) -> List[List]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError(f"Expected non-empty list in {path}")
    data.sort(key=lambda c: c[0])
    return data


def _ts(c) -> int:      return int(c[0])
def _ts_sec(c) -> int:  return int(c[0]) // 1000
def _open(c) -> float:  return float(c[1])
def _high(c) -> float:  return float(c[2])
def _low(c) -> float:   return float(c[3])
def _close(c) -> float: return float(c[4])
def _vol(c) -> float:   return float(c[5])

def _sf(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None

def _ts_utc(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _dur_str(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m" if h else f"{m}m"


# ---------------------------------------------------------------------------
# 1H candle aggregation
# ---------------------------------------------------------------------------

def _build_hourly(candles_1m: List) -> List[dict]:
    """
    Aggregate 1m candles into 1H OHLCV bars.
    Each bar's ts_ms is the floor of the hour (open time of the 1H bar).
    Only complete 1H bars are included (bars that have received their last 1m close).
    """
    buckets: Dict[int, dict] = {}
    for c in candles_1m:
        h_ts = (_ts(c) // ONE_HOUR_MS) * ONE_HOUR_MS
        if h_ts not in buckets:
            buckets[h_ts] = {
                "ts_ms":  h_ts,
                "open":   _open(c),
                "high":   _high(c),
                "low":    _low(c),
                "close":  _close(c),
                "volume": _vol(c),
            }
        else:
            b = buckets[h_ts]
            if _high(c) > b["high"]: b["high"] = _high(c)
            if _low(c)  < b["low"]:  b["low"]  = _low(c)
            b["close"]  = _close(c)
            b["volume"] += _vol(c)
    return sorted(buckets.values(), key=lambda x: x["ts_ms"])


# ---------------------------------------------------------------------------
# Linear regression channel
# ---------------------------------------------------------------------------

def _linreg(n: int, sx: float, sy: float, sxy: float, sxx: float) -> Tuple[float, float]:
    """Return (slope, intercept) from precomputed sums."""
    denom = n * sxx - sx * sx
    if denom == 0.0:
        return 0.0, sy / n if n > 0 else 0.0
    slope     = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def _compute_channel_params(window: List[dict]) -> Optional[dict]:
    """
    Fit a linear regression to the closing prices of `window` (list of 1H bar dicts).
    Returns a params dict sufficient to project the channel to any future ts_ms,
    or None if the window is too small.
    """
    n = len(window)
    if n < 2:
        return None

    xs = list(range(n))
    ys = [c["close"] for c in window]

    sx  = n * (n - 1) / 2          # sum(0..n-1)
    sxx = n * (n - 1) * (2*n - 1) / 6
    sy  = sum(ys)
    sxy = sum(i * y for i, y in enumerate(ys))

    slope, intercept = _linreg(n, sx, sy, sxy, sxx)

    fitted    = [slope * x + intercept for x in xs]
    residuals = [y - f for y, f in zip(ys, fitted)]
    variance  = sum(r * r for r in residuals) / n
    stddev    = math.sqrt(variance)

    return {
        "slope":     slope,
        "intercept": intercept,
        "stddev":    stddev,
        "x0_ts_ms":  window[0]["ts_ms"],    # ts_ms of x=0 anchor
        "xn_ts_ms":  window[-1]["ts_ms"],   # ts_ms of last data point (x = n-1)
        "n":         n,
    }


def _project_channel(
    params: dict, ts_ms: int, std_mult: float
) -> Tuple[float, float, float, float]:
    """
    Project channel to ts_ms.
    x is expressed in 1H units from x0_ts_ms.
    Returns (mid, upper, lower, x).
    """
    x     = (ts_ms - params["x0_ts_ms"]) / ONE_HOUR_MS
    mid   = params["slope"] * x + params["intercept"]
    band  = std_mult * params["stddev"]
    upper = mid + band
    lower = mid - band
    return mid, upper, lower, x


def _build_channel_cache(
    h_candles: List[dict], lookback: int
) -> Dict[int, Optional[dict]]:
    """
    For each 1H bar, compute channel params from the previous `lookback`
    COMPLETED 1H bars (no lookahead into the current bar).

    Mapping: h_ts_ms -> channel_params or None (insufficient history).
    The params are computed from h_candles[max(0, i-lookback) : i], i.e.
    bars that CLOSED before this hour opened.
    """
    cache: Dict[int, Optional[dict]] = {}
    for i, hc in enumerate(h_candles):
        start  = max(0, i - lookback)
        window = h_candles[start:i]          # excludes current hour
        cache[hc["ts_ms"]] = _compute_channel_params(window) if len(window) >= 2 else None
    return cache


# ---------------------------------------------------------------------------
# Trade accounting (self-contained, no shadow_position dependency)
# ---------------------------------------------------------------------------

def _effective_buy_price(price: float, slippage_pct: float) -> float:
    return price * (1.0 + slippage_pct / 100.0)

def _effective_sell_price(price: float, slippage_pct: float) -> float:
    return price * (1.0 - slippage_pct / 100.0)


def _execute_buy(
    pos: dict, price: float, candle_ts_sec: int, rule: str, cfg: dict
) -> None:
    """Mutate pos in-place to reflect entering a position."""
    equity = pos["equity"]
    if equity is None or equity <= 0:
        return

    fee_dec   = cfg["fee_pct"] / 100.0
    eff_price = _effective_buy_price(price, cfg["slippage_pct"])
    fee_usdc  = equity * fee_dec
    qty       = (equity - fee_usdc) / eff_price

    pos["in_position"]  = True
    pos["equity"]       = 0.0          # USDC is now in the trade
    pos["entry_price"]  = eff_price
    pos["entry_qty"]    = qty
    pos["entry_cost"]   = equity       # total USDC deployed
    pos["entry_fee"]    = fee_usdc
    pos["entry_ts"]     = candle_ts_sec
    pos["entry_rule"]   = rule


def _execute_sell(
    pos: dict, price: float, candle_ts_sec: int, rule: str, cfg: dict
) -> Optional[dict]:
    """
    Close position. Mutates pos and returns a closed-trade ledger entry, or None.
    """
    if not pos.get("in_position"):
        return None

    qty         = pos["entry_qty"]
    entry_price = pos["entry_price"]
    entry_cost  = pos["entry_cost"]
    entry_fee   = pos["entry_fee"]
    entry_ts    = pos["entry_ts"]
    entry_rule  = pos["entry_rule"]

    if qty is None or entry_price is None or entry_cost is None:
        return None

    fee_dec    = cfg["fee_pct"] / 100.0
    eff_price  = _effective_sell_price(price, cfg["slippage_pct"])
    gross      = qty * eff_price
    exit_fee   = gross * fee_dec
    net_proc   = gross - exit_fee
    gross_pnl  = gross - entry_cost
    net_pnl    = net_proc - entry_cost
    net_pnl_pct = net_pnl / entry_cost if entry_cost > 0 else 0.0
    total_fees = entry_fee + exit_fee
    duration   = (candle_ts_sec - entry_ts) if entry_ts else 0

    trade_id = pos.get("trade_count", 0) + 1
    pos["trade_count"] = trade_id

    pos["in_position"] = False
    pos["equity"]      = net_proc
    pos["entry_price"] = None
    pos["entry_qty"]   = None
    pos["entry_cost"]  = None
    pos["entry_fee"]   = None
    pos["entry_ts"]    = None
    pos["entry_rule"]  = None

    return {
        "trade_id":         trade_id,
        "entry_ts":         entry_ts,
        "exit_ts":          candle_ts_sec,
        "duration_sec":     duration,
        "entry_price":      entry_price,
        "exit_price":       eff_price,
        "entry_rule":       entry_rule,
        "exit_rule":        rule,
        "entry_cost_usdc":  entry_cost,
        "gross_proceeds":   gross,
        "net_proceeds_usdc": net_proc,
        "entry_fee_usdc":   entry_fee,
        "exit_fee_usdc":    exit_fee,
        "total_fees_usdc":  total_fees,
        "gross_pnl_usdc":   gross_pnl,
        "net_pnl_usdc":     net_pnl,
        "net_pnl_pct":      net_pnl_pct,
        "is_win":           net_pnl > 0,
        "is_panic":         rule in ("PANIC_SELL",),
    }


def _profit_guard_ok(pos: dict, price: float, rule: str, cfg: dict) -> bool:
    """
    Block STANDARD_SELL if the trade would not cover both fees + profit buffer.
    PANIC_SELL always passes.
    """
    if rule == "PANIC_SELL":
        return True
    entry = pos.get("entry_price")
    if entry is None or entry <= 0 or price is None or price <= 0:
        return False
    fee_dec  = cfg["fee_pct"] / 100.0
    slip_dec = cfg["slippage_pct"] / 100.0
    buf      = cfg["profit_buffer_pct"]
    min_exit = entry * (1.0 + fee_dec * 2.0 + slip_dec + buf)
    return price >= min_exit


# ---------------------------------------------------------------------------
# Channel position and decision
# ---------------------------------------------------------------------------

def _channel_pos(price: float, upper: float, lower: float) -> Optional[float]:
    """Normalised position in [0, 1]. May be outside that range if price breaks band."""
    width = upper - lower
    if width <= 0:
        return None
    return (price - lower) / width


def _decide(
    pos: dict,
    price: float,
    mid: float,
    upper: float,
    lower: float,
    cfg: dict,
) -> Tuple[str, str]:
    """
    V2.1 state machine — strict alternating BUY / SELL sequence.

    STATE = FLAT  (in_position == False)
        allowed : STANDARD_BUY, PANIC_BUY
        forbidden: STANDARD_SELL, PANIC_SELL

    STATE = IN_POSITION  (in_position == True)
        allowed : STANDARD_SELL, PANIC_SELL
        forbidden: STANDARD_BUY, PANIC_BUY

    channel_position < 0 is explicitly allowed for STANDARD_BUY.
    cp is None only when the channel band is zero-width; that blocks
    STANDARD_BUY but PANIC_BUY (price-based) still fires.

    Returns (action, rule):
        action: "BUY" | "SELL" | "HOLD"
        rule:   "STANDARD_BUY" | "PANIC_BUY" | "STANDARD_SELL" | "PANIC_SELL" | ""
    PANIC rules have priority over STANDARD rules within each state.
    """
    in_pos = pos.get("in_position", False)
    cp     = _channel_pos(price, upper, lower)
    buy_z  = cfg["buy_zone_pct"]
    sell_z = cfg["sell_zone_pct"]
    pb_pct = cfg["panic_buy_breakout_pct"]
    ps_pct = cfg["panic_sell_breakout_pct"]

    if not in_pos:
        # --- FLAT: only BUY actions permitted ---
        # PANIC_BUY: price breaks out above upper band
        if price >= upper * (1.0 + pb_pct):
            return "BUY", "PANIC_BUY"
        # STANDARD_BUY: price at or below buy zone (cp < 0 allowed)
        if cp is not None and cp <= buy_z:
            return "BUY", "STANDARD_BUY"
    else:
        # --- IN_POSITION: only SELL actions permitted ---
        # PANIC_SELL: price breaks down below lower band
        if price <= lower * (1.0 - ps_pct):
            return "SELL", "PANIC_SELL"
        # STANDARD_SELL: price at or above sell zone
        if cp is not None and cp >= (1.0 - sell_z):
            return "SELL", "STANDARD_SELL"

    return "HOLD", ""


# ---------------------------------------------------------------------------
# Equity tracking
# ---------------------------------------------------------------------------

def _current_equity(pos: dict, price: float, cfg: dict) -> float:
    """Mark-to-market equity: unrealised net proceeds if in position, else cash."""
    if not pos.get("in_position"):
        return pos["equity"]
    qty       = pos.get("entry_qty") or 0.0
    cost      = pos.get("entry_cost") or 0.0
    fee_dec   = cfg["fee_pct"] / 100.0
    slip_dec  = cfg["slippage_pct"] / 100.0
    eff_price = price * (1.0 - slip_dec)
    gross     = qty * eff_price
    net_proc  = gross * (1.0 - fee_dec)
    # Mark-to-market = net proceeds we'd get right now
    return net_proc


def _compute_max_drawdown(equity_curve: List[float]) -> float:
    if not equity_curve:
        return 0.0
    peak   = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return max_dd


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def _compute_stats(ledger: List[dict], final_equity: float, start_equity: float) -> dict:
    n = len(ledger)
    if n == 0:
        return {
            "trade_count": 0, "win_count": 0, "loss_count": 0,
            "win_rate_pct": 0.0,
            "avg_net_pnl_usdc": 0.0, "avg_net_pnl_pct": 0.0,
            "avg_duration_sec": 0,
            "largest_win_usdc": 0.0, "largest_loss_usdc": 0.0,
            "largest_win_pct": 0.0, "largest_loss_pct": 0.0,
            "total_fees_usdc": 0.0,
            "gross_pnl_sum_usdc": 0.0,
            "net_pnl_sum_usdc": final_equity - start_equity,
        }

    wins   = [t for t in ledger if t["is_win"]]
    losses = [t for t in ledger if not t["is_win"]]

    return {
        "trade_count":       n,
        "win_count":         len(wins),
        "loss_count":        len(losses),
        "win_rate_pct":      len(wins) / n * 100.0,
        "avg_net_pnl_usdc":  sum(t["net_pnl_usdc"] for t in ledger) / n,
        "avg_net_pnl_pct":   sum(t["net_pnl_pct"]  for t in ledger) / n * 100.0,
        "avg_duration_sec":  int(sum(t["duration_sec"] for t in ledger) / n),
        "largest_win_usdc":  max((t["net_pnl_usdc"] for t in wins),   default=0.0),
        "largest_loss_usdc": min((t["net_pnl_usdc"] for t in losses), default=0.0),
        "largest_win_pct":   max((t["net_pnl_pct"]  for t in wins),   default=0.0) * 100.0,
        "largest_loss_pct":  min((t["net_pnl_pct"]  for t in losses), default=0.0) * 100.0,
        "total_fees_usdc":   sum(t["total_fees_usdc"] for t in ledger),
        "gross_pnl_sum_usdc": sum(t["gross_pnl_usdc"] for t in ledger),
        "net_pnl_sum_usdc":  sum(t["net_pnl_usdc"]   for t in ledger),
    }


# ---------------------------------------------------------------------------
# Main replay
# ---------------------------------------------------------------------------

def run_replay(cfg: dict, verbose: bool = False) -> dict:
    candle_path  = cfg["candle_path"]
    days         = cfg["days"]
    start_equity = cfg["start_equity"]
    lookback     = cfg["channel_lookback_hours"]
    std_mult     = cfg["channel_std_mult"]

    print(f"[channel_lab] Loading candles from {candle_path} ...", flush=True)
    all_candles = load_candles(candle_path)
    print(f"[channel_lab] Total 1m candles: {len(all_candles):,}", flush=True)

    # --- Slice replay window (last N days) + channel warmup ---
    last_ts_ms   = _ts(all_candles[-1])
    cutoff_ms    = last_ts_ms - days * 24 * ONE_HOUR_MS
    replay_start = next((i for i, c in enumerate(all_candles) if _ts(c) >= cutoff_ms), 0)

    # We need lookback 1H bars of history before the replay starts.
    # Each 1H bar = 60 1m candles.  Add a safety margin of 10%.
    warmup_1m    = (lookback + 10) * 60
    warmup_start = max(0, replay_start - warmup_1m)
    working      = all_candles[warmup_start:]
    replay_offset = replay_start - warmup_start

    n_replay  = len(working) - replay_offset
    first_dt  = datetime.fromtimestamp(_ts(working[replay_offset]) / 1000, tz=timezone.utc)
    last_dt   = datetime.fromtimestamp(_ts(working[-1]) / 1000, tz=timezone.utc)

    print(
        f"[channel_lab] Replay window: {first_dt.date()} to {last_dt.date()} "
        f"({n_replay:,} candles, warmup={replay_offset:,})",
        flush=True,
    )

    # --- Build 1H candles from the full working set ---
    h_candles = _build_hourly(working)
    print(f"[channel_lab] 1H bars: {len(h_candles):,}  (lookback={lookback})", flush=True)

    # --- Pre-compute channel params for each 1H bar ---
    channel_cache = _build_channel_cache(h_candles, lookback)
    print(f"[channel_lab] Channel cache built ({len(channel_cache):,} entries)", flush=True)

    # --- Build a ts_ms -> channel_params lookup for 1m candles ---
    # For a 1m candle at ts T, the current 1H bar is floor(T / 1H).
    # We look up channel_cache[floor_hour_ts_ms] which was computed from bars
    # BEFORE that hour (no lookahead).
    h_ts_sorted = sorted(channel_cache.keys())

    def _get_channel(ts_ms: int) -> Optional[dict]:
        h_ts = (ts_ms // ONE_HOUR_MS) * ONE_HOUR_MS
        return channel_cache.get(h_ts)

    # --- Initialise position state ---
    pos: dict = {
        "equity":      float(start_equity),
        "in_position": False,
        "entry_price": None,
        "entry_qty":   None,
        "entry_cost":  None,
        "entry_fee":   None,
        "entry_ts":    None,
        "entry_rule":  None,
        "trade_count": 0,
    }

    ledger:       List[dict]  = []
    equity_curve: List[float] = [start_equity]
    rule_buy_ctr:  Counter    = Counter()
    rule_sell_ctr: Counter    = Counter()

    # V2.1 sequence counters — validated at report end
    buy_count       = 0
    sell_count      = 0
    std_buy_count   = 0
    std_sell_count  = 0
    panic_buy_count = 0
    panic_sell_count = 0

    skipped_no_channel = 0
    tick_count         = 0
    t0 = time.monotonic()

    # --- Main loop ---
    for i in range(replay_offset, len(working)):
        candle      = working[i]
        ts_ms       = _ts(candle)
        ts_sec      = _ts_sec(candle)
        price       = _close(candle)

        params = _get_channel(ts_ms)
        if params is None:
            skipped_no_channel += 1
            tick_count += 1
            equity_curve.append(_current_equity(pos, price, cfg))
            continue

        mid, upper, lower, _x = _project_channel(params, ts_ms, std_mult)

        action, rule = _decide(pos, price, mid, upper, lower, cfg)

        entry = None
        if action == "BUY":
            _execute_buy(pos, price, ts_sec, rule, cfg)
            rule_buy_ctr[rule] += 1
            buy_count += 1
            if rule == "STANDARD_BUY":
                std_buy_count += 1
            else:
                panic_buy_count += 1
            if verbose:
                cp = _channel_pos(price, upper, lower)
                print(
                    f"  [BUY ] {_ts_utc(ts_sec)}  rule={rule:<16}  "
                    f"price={price:.6f}  ch_pos={f'{cp:.3f}' if cp is not None else 'N/A'}  "
                    f"lower={lower:.6f}  upper={upper:.6f}",
                    flush=True,
                )

        elif action == "SELL":
            if _profit_guard_ok(pos, price, rule, cfg):
                trade = _execute_sell(pos, price, ts_sec, rule, cfg)
                if trade:
                    ledger.append(trade)
                    rule_sell_ctr[rule] += 1
                    sell_count += 1
                    if rule == "STANDARD_SELL":
                        std_sell_count += 1
                    else:
                        panic_sell_count += 1
                    entry = trade
                    if verbose:
                        side = "WIN " if trade["is_win"] else "LOSS"
                        print(
                            f"  [{side}] {_ts_utc(trade['entry_ts'])} to "
                            f"{_ts_utc(trade['exit_ts'])}  "
                            f"({_dur_str(trade['duration_sec'])})  "
                            f"rule={trade['entry_rule']}/{trade['exit_rule']}  "
                            f"pnl={trade['net_pnl_usdc']:+.4f} USDC "
                            f"({trade['net_pnl_pct']*100:+.3f}%)",
                            flush=True,
                        )

        tick_count  += 1
        equity_curve.append(_current_equity(pos, price, cfg))

    elapsed = time.monotonic() - t0
    print(
        f"[channel_lab] Done: {tick_count:,} ticks in {elapsed:.2f}s  "
        f"(skipped {skipped_no_channel:,} - no channel yet)",
        flush=True,
    )

    # --- V2.1 sequence validation ---
    # Valid iff every BUY has a matching SELL, with at most one open BUY at end.
    seq_valid = (buy_count == sell_count) or (buy_count == sell_count + 1)
    seq_result = "PASS" if seq_valid else "FAIL"
    print(
        f"[channel_lab] Sequence validation: {seq_result}  "
        f"(buys={buy_count} sells={sell_count}  "
        f"std_buy={std_buy_count} panic_buy={panic_buy_count}  "
        f"std_sell={std_sell_count} panic_sell={panic_sell_count})",
        flush=True,
    )

    # --- Final equity ---
    in_pos_at_end = bool(pos.get("in_position", False))
    if in_pos_at_end:
        last_price    = _close(working[-1])
        final_eq      = _current_equity(pos, last_price, cfg)
        entry_cost    = pos.get("entry_cost") or 0.0
        entry_qty     = pos.get("entry_qty") or 0.0
        unrealised    = final_eq - entry_cost
        open_pos_info = {
            "entry_rule":          pos.get("entry_rule"),
            "entry_ts":            pos.get("entry_ts"),
            "entry_ts_utc":        _ts_utc(pos["entry_ts"]) if pos.get("entry_ts") else None,
            "entry_price":         pos.get("entry_price"),
            "entry_cost_usdc":     entry_cost,
            "entry_qty_base":      entry_qty,
            "last_price":          last_price,
            "mark_to_market_usdc": final_eq,
            "unrealised_net_usdc": unrealised,
            "unrealised_pct":      (unrealised / entry_cost) if entry_cost > 0 else 0.0,
        }
        print(
            f"[channel_lab] Ends IN POSITION  entry={pos.get('entry_price'):.6f}  "
            f"last={last_price:.6f}  mark-to-market={final_eq:.6f} USDC",
            flush=True,
        )
    else:
        final_eq      = pos["equity"]
        open_pos_info = None

    stats        = _compute_stats(ledger, final_eq, start_equity)
    net_pnl_usdc = final_eq - start_equity
    net_pnl_pct  = (net_pnl_usdc / start_equity * 100.0) if start_equity > 0 else 0.0
    max_dd       = _compute_max_drawdown(equity_curve)

    return {
        "config": {
            "candle_path":             str(candle_path),
            "days":                    days,
            "start_equity_usdc":       start_equity,
            "fee_pct":                 cfg["fee_pct"],
            "slippage_pct":            cfg["slippage_pct"],
            "channel_lookback_hours":  lookback,
            "channel_std_mult":        std_mult,
            "buy_zone_pct":            cfg["buy_zone_pct"],
            "sell_zone_pct":           cfg["sell_zone_pct"],
            "panic_buy_breakout_pct":  cfg["panic_buy_breakout_pct"],
            "panic_sell_breakout_pct": cfg["panic_sell_breakout_pct"],
            "profit_buffer_pct":       cfg["profit_buffer_pct"],
        },
        "replay_window": {
            "first_candle_utc":  first_dt.isoformat().replace("+00:00", "Z"),
            "last_candle_utc":   last_dt.isoformat().replace("+00:00", "Z"),
            "candle_count_1m":   n_replay,
            "tick_count":        tick_count,
            "skipped_no_channel": skipped_no_channel,
            "elapsed_sec":       round(elapsed, 3),
            "ends_in_position":  in_pos_at_end,
        },
        "summary": {
            "start_equity_usdc":   round(start_equity, 8),
            "final_equity_usdc":   round(final_eq, 8),
            "ends_in_position":    in_pos_at_end,
            "net_pnl_usdc":        round(net_pnl_usdc, 8),
            "net_pnl_pct":         round(net_pnl_pct, 4),
            "trade_count":         stats["trade_count"],
            "win_count":           stats["win_count"],
            "loss_count":          stats["loss_count"],
            "win_rate_pct":        round(stats["win_rate_pct"], 2),
            "avg_net_pnl_usdc":    round(stats["avg_net_pnl_usdc"], 6),
            "avg_net_pnl_pct":     round(stats["avg_net_pnl_pct"], 4),
            "avg_duration_sec":    stats["avg_duration_sec"],
            "avg_duration_str":    _dur_str(stats["avg_duration_sec"]),
            "largest_win_usdc":    round(stats["largest_win_usdc"], 6),
            "largest_loss_usdc":   round(stats["largest_loss_usdc"], 6),
            "largest_win_pct":     round(stats["largest_win_pct"], 4),
            "largest_loss_pct":    round(stats["largest_loss_pct"], 4),
            "total_fees_usdc":     round(stats["total_fees_usdc"], 6),
            "gross_pnl_sum_usdc":  round(stats["gross_pnl_sum_usdc"], 6),
            "max_drawdown_pct":    round(max_dd, 4),
            # V2.1 sequence counts
            "buy_count":           buy_count,
            "sell_count":          sell_count,
            "standard_buy_count":  std_buy_count,
            "standard_sell_count": std_sell_count,
            "panic_buy_count":     panic_buy_count,
            "panic_sell_count":    panic_sell_count,
            "sequence_valid":      seq_valid,
            "sequence_validation": seq_result,
        },
        "open_position": open_pos_info,
        "rule_distribution": {
            "buy_rules":  dict(rule_buy_ctr.most_common()),
            "sell_rules": dict(rule_sell_ctr.most_common()),
        },
        "first_10_trades": ledger[:10],
        "last_10_trades":  ledger[-10:],
    }


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------

def _fmt_trade(t: dict) -> List[str]:
    if not t:
        return []
    side    = "WIN " if t.get("is_win") else "LOSS"
    panic   = " [PANIC]" if t.get("is_panic") else ""
    ets     = t.get("entry_ts")
    xts     = t.get("exit_ts")
    dur     = _dur_str(t.get("duration_sec") or 0)
    pnl_u   = t.get("net_pnl_usdc", 0)
    pnl_pct = t.get("net_pnl_pct", 0) * 100
    lines   = [f"  #{t.get('trade_id','?')} [{side}]{panic}  duration={dur}"]
    if ets:
        lines.append(
            f"    Entry: {_ts_utc(ets)}  rule={t.get('entry_rule')}  "
            f"price={t.get('entry_price', 0):.6f}  cost={t.get('entry_cost_usdc', 0):.4f} USDC"
        )
    if xts:
        lines.append(
            f"    Exit:  {_ts_utc(xts)}  rule={t.get('exit_rule')}  "
            f"price={t.get('exit_price', 0):.6f}  proceeds={t.get('net_proceeds_usdc', 0):.4f} USDC"
        )
    lines.append(
        f"    Net PNL: {pnl_u:+.6f} USDC  ({pnl_pct:+.4f}%)  "
        f"fees={t.get('total_fees_usdc', 0):.5f} USDC"
    )
    return lines


def format_txt_report(result: dict) -> str:
    s  = result["summary"]
    c  = result["config"]
    rw = result["replay_window"]
    rd = result["rule_distribution"]
    op = result.get("open_position")

    pos_flag = "YES (mark-to-market)" if s["ends_in_position"] else "NO (flat)"

    lines: List[str] = [
        "=" * 66,
        "  Uranus Channel Lab V2.1 -- Regression Channel Strategy",
        "=" * 66,
        f"  Period       : {rw['first_candle_utc']}  to  {rw['last_candle_utc']}",
        f"  Days         : {c['days']}",
        f"  Candles (1m) : {rw['candle_count_1m']:,}  "
        f"(skipped {rw['skipped_no_channel']:,} pre-warmup, "
        f"elapsed {rw['elapsed_sec']}s)",
        f"  Data         : {c['candle_path']}",
        "",
        "  -- Channel Parameters ----------------------------------------",
        f"  Lookback     : {c['channel_lookback_hours']} completed 1H bars",
        f"  Std mult     : {c['channel_std_mult']}",
        f"  Buy zone     : channel_position <= {c['buy_zone_pct']}",
        f"  Sell zone    : channel_position >= {1.0 - c['sell_zone_pct']:.2f}",
        f"  Panic buy    : price >= upper * (1 + {c['panic_buy_breakout_pct']})",
        f"  Panic sell   : price <= lower * (1 - {c['panic_sell_breakout_pct']})",
        "",
        "  -- Equity ----------------------------------------------------",
        f"  Start        : {s['start_equity_usdc']:.8f} USDC",
        f"  Final        : {s['final_equity_usdc']:.8f} USDC",
        f"  Ends in pos. : {pos_flag}",
        f"  Net PNL      : {s['net_pnl_usdc']:+.8f} USDC  ({s['net_pnl_pct']:+.4f}%)",
        f"  Gross PNL    : {s['gross_pnl_sum_usdc']:+.6f} USDC",
        f"  Total Fees   : {s['total_fees_usdc']:.6f} USDC",
        f"  Max Drawdown : {s['max_drawdown_pct']:.4f}%",
    ]

    if op:
        unr_pct = (op.get("unrealised_pct") or 0.0) * 100
        lines += [
            "",
            "  -- Open Position at End of Replay ----------------------------",
            f"  Entry rule   : {op.get('entry_rule')}",
            f"  Entry time   : {op.get('entry_ts_utc')}",
            f"  Entry price  : {op.get('entry_price', 0):.6f}",
            f"  Entry cost   : {op.get('entry_cost_usdc', 0):.4f} USDC",
            f"  Last price   : {op.get('last_price', 0):.6f}",
            f"  Unrealised   : {op.get('unrealised_net_usdc', 0):+.6f} USDC  ({unr_pct:+.4f}%)",
            f"  Mark-to-mkt  : {op.get('mark_to_market_usdc', 0):.6f} USDC",
        ]

    lines += [
        "",
        "  -- Trades (closed) -------------------------------------------",
        f"  Total        : {s['trade_count']}",
        f"  Wins         : {s['win_count']}",
        f"  Losses       : {s['loss_count']}",
        f"  Win Rate     : {s['win_rate_pct']:.2f}%",
        f"  Avg PNL      : {s['avg_net_pnl_usdc']:+.6f} USDC  ({s['avg_net_pnl_pct']:+.4f}%)",
        f"  Avg Duration : {s['avg_duration_str']}",
        f"  Largest Win  : {s['largest_win_usdc']:+.6f} USDC  ({s['largest_win_pct']:+.4f}%)",
        f"  Largest Loss : {s['largest_loss_usdc']:+.6f} USDC  ({s['largest_loss_pct']:+.4f}%)",
        "",
        "  -- Fee / Execution -------------------------------------------",
        f"  Fee/side     : {c['fee_pct']}%",
        f"  Slippage     : {c['slippage_pct']}%",
        f"  Profit buf.  : {c['profit_buffer_pct']}",
        "",
        "  -- Rule Distribution -----------------------------------------",
        "  BUY rules:",
    ]
    for rule, cnt in rd["buy_rules"].items():
        lines.append(f"    {rule:<25} {cnt}")
    if not rd["buy_rules"]:
        lines.append("    (none)")
    lines.append("  SELL rules:")
    for rule, cnt in rd["sell_rules"].items():
        lines.append(f"    {rule:<25} {cnt}")
    if not rd["sell_rules"]:
        lines.append("    (none)")

    seq_flag = "PASS" if s["sequence_valid"] else "FAIL"
    lines += [
        "",
        "  -- V2.1 Sequence Validation ----------------------------------",
        f"  Result          : {seq_flag}",
        f"  BUY_COUNT       : {s['buy_count']}",
        f"  SELL_COUNT      : {s['sell_count']}",
        f"  STANDARD_BUY    : {s['standard_buy_count']}",
        f"  STANDARD_SELL   : {s['standard_sell_count']}",
        f"  PANIC_BUY       : {s['panic_buy_count']}",
        f"  PANIC_SELL      : {s['panic_sell_count']}",
        f"  Rule: BUY_COUNT == SELL_COUNT "
        f"{'(open position)' if s['ends_in_position'] else '(flat)'}",
    ]

    lines += ["", "  -- First 10 Trades -------------------------------------------"]
    for t in result.get("first_10_trades", []):
        lines.extend(_fmt_trade(t))
    if not result.get("first_10_trades"):
        lines.append("    (no trades)")

    lines += ["", "  -- Last 10 Trades --------------------------------------------"]
    for t in result.get("last_10_trades", []):
        lines.extend(_fmt_trade(t))
    if not result.get("last_10_trades"):
        lines.append("    (no trades)")

    lines += ["", "=" * 66]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    # Reconfigure stdout/stderr to UTF-8 (Windows cp1250 cannot print box chars)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg  = _args_to_cfg(args)

    candle_path = cfg["candle_path"]
    if not candle_path.exists():
        print(
            f"[ERROR] Candle file not found: {candle_path}\n"
            f"  Pass --candles <path> or download with freqtrade download-data",
            file=sys.stderr,
        )
        sys.exit(1)

    result = run_replay(cfg, verbose=args.verbose)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    prefix    = cfg["output_prefix"]
    json_path = REPORTS_DIR / f"{prefix}.json"
    txt_path  = REPORTS_DIR / f"{prefix}.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[channel_lab] JSON report: {json_path}", flush=True)

    txt = format_txt_report(result)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"[channel_lab] TXT  report: {txt_path}", flush=True)

    print("", flush=True)
    s = result["summary"]
    pos_note = " [IN POSITION - mark-to-market]" if s["ends_in_position"] else ""
    print(
        f"[result] equity {s['start_equity_usdc']:.4f} to {s['final_equity_usdc']:.4f} USDC{pos_note}  "
        f"PNL={s['net_pnl_pct']:+.4f}%  trades={s['trade_count']}  "
        f"win_rate={s['win_rate_pct']:.1f}%  max_dd={s['max_drawdown_pct']:.2f}%  "
        f"avg_dur={s['avg_duration_str']}  "
        f"seq={s['sequence_validation']} (B={s['buy_count']} S={s['sell_count']})",
        flush=True,
    )


if __name__ == "__main__":
    main()
