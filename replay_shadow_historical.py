#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
replay_shadow_historical.py — UranusBot historical shadow replay

Replays Freqtrade 1m JSON candle data through the live shadow_position.py
and rule_engine.py logic. No live state.json is read or written. No real orders.

Candle format expected:
    [[timestamp_ms, open, high, low, close, volume], ...]

Default candle path:
    /opt/bots/uranus/freqtrade/user_data/data/binance/XRP_USDC-1m.json

Output:
    reports/shadow_historical_xrp_30d.json
    reports/shadow_historical_xrp_30d.txt

Usage:
    python replay_shadow_historical.py [options]

Examples:
    python replay_shadow_historical.py
    python replay_shadow_historical.py --days 7 --equity 50.0 --verbose
    python replay_shadow_historical.py --candles /tmp/XRP_USDC-1m.json --days 30
    python replay_shadow_historical.py --std-sell 0.01 --std-buy 0.01
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
APP_DIR = SCRIPT_DIR / "app"
DEFAULT_CANDLE_PATH = Path("/opt/bots/uranus/freqtrade/user_data/data/binance/XRP_USDC-1m.json")
REPORTS_DIR = SCRIPT_DIR / "reports"

# ---------------------------------------------------------------------------
# Defaults — mirror live tick_runner defaults exactly
# ---------------------------------------------------------------------------

DEFAULT_EQUITY            = 19.25297507
DEFAULT_DAYS              = 30
DEFAULT_FEE_PCT           = 0.1          # % per side
DEFAULT_SLIPPAGE_PCT      = 0.0          # %
DEFAULT_MA_SHORT_PERIOD   = 5
DEFAULT_MA_LONG_PERIOD    = 20
DEFAULT_PROFIT_BUFFER_PCT = 0.001        # decimal

# Rule engine thresholds (decimal, same as STD_SELL_PCT env var)
DEFAULT_STD_SELL_PCT               = 0.01
DEFAULT_RECOVERY_SELL_RETRACE_PCT  = 0.006
DEFAULT_PANIC_SELL_PCT             = 0.01
DEFAULT_CATASTROPHE_SELL_PCT       = 0.10
DEFAULT_STD_BUY_PCT                = 0.01
DEFAULT_RECOVERY_BUY_REBOUND_PCT   = 0.006
DEFAULT_PANIC_BUY_PCT              = 0.01
DEFAULT_CATASTROPHE_BUY_PCT        = 0.10
DEFAULT_SELL_REVERSAL_MIN_PCT      = 0.0
DEFAULT_MA_SIDEWAYS_BAND_PCT       = 0.0005
DEFAULT_RECOVERY_ARM_TIMEOUT_BARS  = 240

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="UranusBot historical shadow replay",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--candles", default=str(DEFAULT_CANDLE_PATH),
                   help="Path to Freqtrade 1m JSON candle file")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help="Number of days to replay")
    p.add_argument("--equity", type=float, default=DEFAULT_EQUITY,
                   help="Starting equity in USDC")
    p.add_argument("--fee", type=float, default=DEFAULT_FEE_PCT,
                   help="Fee per side in percent (e.g. 0.1 = 0.1%%)")
    p.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE_PCT,
                   help="Slippage in percent")
    p.add_argument("--ma-short", type=int, default=DEFAULT_MA_SHORT_PERIOD,
                   help="MA short period (candles)")
    p.add_argument("--ma-long", type=int, default=DEFAULT_MA_LONG_PERIOD,
                   help="MA long period (candles)")
    p.add_argument("--std-sell", type=float, default=DEFAULT_STD_SELL_PCT,
                   help="STD_SELL_PCT decimal (0.01 = 1%%)")
    p.add_argument("--std-buy", type=float, default=DEFAULT_STD_BUY_PCT,
                   help="STD_BUY_PCT decimal (0.01 = 1%%)")
    p.add_argument("--panic-sell", type=float, default=DEFAULT_PANIC_SELL_PCT,
                   help="PANIC_SELL_PCT decimal")
    p.add_argument("--panic-buy", type=float, default=DEFAULT_PANIC_BUY_PCT,
                   help="PANIC_BUY_PCT decimal")
    p.add_argument("--catastrophe-sell", type=float, default=DEFAULT_CATASTROPHE_SELL_PCT,
                   help="CATASTROPHE_SELL_PCT decimal")
    p.add_argument("--catastrophe-buy", type=float, default=DEFAULT_CATASTROPHE_BUY_PCT,
                   help="CATASTROPHE_BUY_PCT decimal")
    p.add_argument("--recovery-sell-retrace", type=float, default=DEFAULT_RECOVERY_SELL_RETRACE_PCT,
                   help="RECOVERY_SELL_RETRACE_PCT decimal")
    p.add_argument("--recovery-buy-rebound", type=float, default=DEFAULT_RECOVERY_BUY_REBOUND_PCT,
                   help="RECOVERY_BUY_REBOUND_PCT decimal")
    p.add_argument("--sell-reversal-min", type=float, default=DEFAULT_SELL_REVERSAL_MIN_PCT,
                   help="SELL_REVERSAL_MIN_PCT decimal")
    p.add_argument("--ma-sideways-band", type=float, default=DEFAULT_MA_SIDEWAYS_BAND_PCT,
                   help="MA_SIDEWAYS_BAND_PCT decimal")
    p.add_argument("--output-prefix", default="shadow_historical_xrp_30d",
                   help="Output filename prefix (no extension)")
    p.add_argument("--verbose", action="store_true",
                   help="Print per-trade log to stdout")
    return p.parse_args(argv)


def args_to_cfg(args: argparse.Namespace) -> dict:
    return {
        "candle_path":               Path(args.candles),
        "days":                      args.days,
        "start_equity":              args.equity,
        "fee_pct":                   args.fee,
        "slippage_pct":              args.slippage,
        "profit_buffer_pct":         DEFAULT_PROFIT_BUFFER_PCT,
        "ma_short_period":           args.ma_short,
        "ma_long_period":            args.ma_long,
        "std_sell_pct":              args.std_sell,
        "recovery_sell_retrace_pct": args.recovery_sell_retrace,
        "panic_sell_pct":            args.panic_sell,
        "catastrophe_sell_pct":      args.catastrophe_sell,
        "std_buy_pct":               args.std_buy,
        "recovery_buy_rebound_pct":  args.recovery_buy_rebound,
        "panic_buy_pct":             args.panic_buy,
        "catastrophe_buy_pct":       args.catastrophe_buy,
        "sell_reversal_min_pct":     args.sell_reversal_min,
        "ma_sideways_band_pct":      args.ma_sideways_band,
        "recovery_arm_timeout_bars": DEFAULT_RECOVERY_ARM_TIMEOUT_BARS,
        "output_prefix":             args.output_prefix,
    }


# ---------------------------------------------------------------------------
# Env var injection — shadow_position reads these at call time, not import time
# ---------------------------------------------------------------------------

def _apply_cfg_to_env(cfg: dict) -> None:
    """
    Mirror cfg into env vars so shadow_position._env_float() picks them up.
    SHADOW_ENABLED=0 prevents any accidental live shadow activation.
    """
    os.environ["FEE_PCT"]                    = str(cfg["fee_pct"])
    os.environ["SLIPPAGE_PCT"]               = str(cfg["slippage_pct"])
    os.environ["PROFIT_BUFFER_PCT"]          = str(cfg["profit_buffer_pct"])
    os.environ["STD_SELL_PCT"]               = str(cfg["std_sell_pct"])
    os.environ["RECOVERY_SELL_RETRACE_PCT"]  = str(cfg["recovery_sell_retrace_pct"])
    os.environ["PANIC_SELL_PCT"]             = str(cfg["panic_sell_pct"])
    os.environ["CATASTROPHE_SELL_PCT"]       = str(cfg["catastrophe_sell_pct"])
    os.environ["STD_BUY_PCT"]               = str(cfg["std_buy_pct"])
    os.environ["RECOVERY_BUY_REBOUND_PCT"]   = str(cfg["recovery_buy_rebound_pct"])
    os.environ["PANIC_BUY_PCT"]              = str(cfg["panic_buy_pct"])
    os.environ["CATASTROPHE_BUY_PCT"]        = str(cfg["catastrophe_buy_pct"])
    os.environ["SELL_REVERSAL_MIN_PCT"]      = str(cfg["sell_reversal_min_pct"])
    os.environ["MA_SIDEWAYS_BAND_PCT"]       = str(cfg["ma_sideways_band_pct"])
    os.environ["RECOVERY_ARM_TIMEOUT_BARS"]  = str(cfg["recovery_arm_timeout_bars"])
    os.environ["SHADOW_ENABLED"]             = "0"   # never activate live shadow


# ---------------------------------------------------------------------------
# Candle helpers
# ---------------------------------------------------------------------------

def load_candles(path: Path) -> List[List]:
    """Load and sort Freqtrade JSON candles: [[ts_ms, o, h, l, c, v], ...]"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not data:
        raise ValueError(f"Expected non-empty list in {path}")
    data.sort(key=lambda c: c[0])
    return data


def _ts(c) -> int:   return int(c[0])
def _open(c) -> float: return float(c[1])
def _high(c) -> float: return float(c[2])
def _low(c) -> float:  return float(c[3])
def _close(c) -> float: return float(c[4])
def _vol(c) -> float:  return float(c[5])


def ts_to_dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def _sma(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def _sf(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Per-candle state builder
# ---------------------------------------------------------------------------

def _build_market(idx: int, candles: List, closes: List[float], cfg: dict) -> dict:
    c      = candles[idx]
    prev_c = candles[idx - 1] if idx > 0 else c

    ma_s = _sma(closes[: idx + 1], cfg["ma_short_period"])
    ma_l = _sma(closes[: idx + 1], cfg["ma_long_period"])

    market: dict = {
        "last":     _close(c),
        "prev_last": _close(prev_c),
        "high":     _high(c),
        "low":      _low(c),
        "open":     _open(c),
        "volume":   _vol(c),
        "pair":     "XRP/USDC",
        "timeframe": "1m",
        "last_candle_time": _ts(c),
        "updated_at": ts_to_dt(_ts(c)).isoformat().replace("+00:00", "Z"),
    }
    if ma_s is not None:
        market["ma_short"] = ma_s
    if ma_l is not None:
        market["ma_long"] = ma_l
    return market


def _build_flags(market: dict) -> dict:
    ma_s = market.get("ma_short")
    ma_l = market.get("ma_long")
    if ma_s is not None and ma_l is not None:
        return {
            "ma_positive":       ma_s > ma_l,
            "ma_negative":       ma_s < ma_l,
            "ma_filter_enabled": True,
        }
    return {"ma_positive": False, "ma_negative": False, "ma_filter_enabled": True}


def _build_levels(shadow: dict, market: dict, cfg: dict) -> dict:
    """Compute price levels via rule_engine.compute_levels for recovery arming."""
    in_pos = bool(shadow.get("in_position", False))
    base   = _sf(shadow.get("base"))
    peak   = _sf(shadow.get("peak"))
    trough = _sf(shadow.get("trough"))
    last   = _sf(market.get("last"))

    if base is None:
        base = last
    if in_pos and peak is None:
        peak = base if base is not None else last
    if not in_pos and trough is None:
        trough = base if base is not None else last

    fee_per_side = cfg["fee_pct"] / 100.0
    fee_total    = fee_per_side * 2.0
    fee_buffer   = max(cfg["profit_buffer_pct"], fee_total + cfg["slippage_pct"] / 100.0)

    levels_ctx = {
        "base":                    base or (last or 0.0),
        "last":                    last or 0.0,
        "prev_last":               _sf(market.get("prev_last")) or (last or 0.0),
        "peak":                    peak if peak is not None else (base or last or 0.0),
        "trough":                  trough if trough is not None else (base or last or 0.0),
        "in_position":             in_pos,
        "ma_short":                _sf(market.get("ma_short")) or 0.0,
        "ma_long":                 _sf(market.get("ma_long")) or 0.0,
        "fee_pct_per_side":        fee_per_side,
        "fee_total_pct":           fee_total,
        "fee_buffer_pct":          fee_buffer,
        "std_sell_pct":            cfg["std_sell_pct"],
        "recovery_sell_retrace_pct": cfg["recovery_sell_retrace_pct"],
        "panic_sell_pct":          cfg["panic_sell_pct"],
        "catastrophe_sell_pct":    cfg["catastrophe_sell_pct"],
        "std_buy_pct":             cfg["std_buy_pct"],
        "recovery_buy_rebound_pct": cfg["recovery_buy_rebound_pct"],
        "panic_buy_pct":           cfg["panic_buy_pct"],
        "catastrophe_buy_pct":     cfg["catastrophe_buy_pct"],
    }
    computed = rule_engine.compute_levels(levels_ctx) or {}
    return {
        "reference_base":    base,
        "std_sell":          computed.get("std_sell_level"),
        "recovery_sell":     computed.get("recovery_sell_level"),
        "panic_sell":        computed.get("panic_sell_level"),
        "catastrophe_sell":  computed.get("catastrophe_sell_level"),
        "std_buy":           computed.get("std_buy_level"),
        "recovery_buy":      computed.get("recovery_buy_level"),
        "panic_buy":         computed.get("panic_buy_level"),
        "catastrophe_buy":   computed.get("catastrophe_buy_level"),
    }


def _build_state(shadow: dict, market: dict, cfg: dict) -> dict:
    """
    Build a minimal state dict for one replay tick.
    state["in_position"] is always False — replay has no live position.
    The shadow section tracks its own position independently.
    """
    flags  = _build_flags(market)
    levels = _build_levels(shadow, market, cfg)
    return {
        "in_position":     False,   # no live position in replay
        "live_trade_stake": None,
        "market":          market,
        "flags":           flags,
        "levels":          levels,
        "last":            market.get("last"),
        "cycle":           {},
        "shadow":          shadow,
    }


# ---------------------------------------------------------------------------
# Shadow rule engine context builder (no env var reads)
# ---------------------------------------------------------------------------

def _build_shadow_ctx(shadow: dict, market: dict, cfg: dict) -> Tuple[dict, dict]:
    """
    Build (ctx, cycle_ctx) for rule_engine.decide().
    Mirrors shadow_position._shadow_build_ctx but uses cfg params instead of env vars.
    """
    sc = shadow.get("cycle") if isinstance(shadow.get("cycle"), dict) else {}

    in_position = bool(shadow.get("in_position", False))
    base   = _sf(shadow.get("base"))
    peak   = _sf(shadow.get("peak"))
    trough = _sf(shadow.get("trough"))

    last     = _sf(market.get("last"))
    prev_last = _sf(market.get("prev_last"))
    ma_short = _sf(market.get("ma_short"))
    ma_long  = _sf(market.get("ma_long"))

    if last is None:
        last = base
    if prev_last is None:
        prev_last = last
    if in_position and peak is None:
        vals = [v for v in (base, last) if v is not None]
        peak = max(vals) if vals else None
    if not in_position and trough is None:
        vals = [v for v in (base, last) if v is not None]
        trough = min(vals) if vals else None
    if base is None:
        base = last

    fee_per_side = cfg["fee_pct"] / 100.0
    slip         = cfg["slippage_pct"] / 100.0
    buf          = cfg["profit_buffer_pct"]
    fee_total    = fee_per_side * 2.0
    fee_buffer   = max(buf, fee_total + slip)

    required_next_buy_mode  = str(sc.get("required_next_buy_mode")  or "")
    required_next_sell_mode = str(sc.get("required_next_sell_mode") or "")
    recovery_mode      = bool(sc.get("recovery_mode", False))
    recovery_loss_pct  = float(sc.get("recovery_loss_pct") or 0.0)
    recovery_anchor    = _sf(sc.get("recovery_anchor_price"))
    recovery_cap       = _sf(sc.get("recovery_target_entry_cap"))
    last_panic_sell_p  = _sf(sc.get("last_panic_sell_price"))
    last_panic_buy_p   = _sf(sc.get("last_panic_buy_price"))

    panic_context = "NONE"
    if required_next_buy_mode:
        panic_context = "AFTER_SELL_PANIC"
    elif required_next_sell_mode:
        panic_context = "AFTER_BUY_PANIC"

    last_panic_loss = 0.0
    if recovery_anchor is not None and recovery_loss_pct > 0:
        last_panic_loss = recovery_anchor * recovery_loss_pct

    recovery_context = recovery_mode or bool(required_next_buy_mode) or bool(required_next_sell_mode)

    ctx = {
        "base":                     base if base is not None else 0.0,
        "last":                     last if last is not None else 0.0,
        "prev_last":                prev_last if prev_last is not None else (last or 0.0),
        "peak":                     peak if peak is not None else (base or 0.0),
        "trough":                   trough if trough is not None else (base or 0.0),
        "in_position":              in_position,
        "ma_short":                 ma_short if ma_short is not None else 0.0,
        "ma_long":                  ma_long if ma_long is not None else 0.0,
        "fee_pct_per_side":         fee_per_side,
        "fee_total_pct":            fee_total,
        "fee_buffer_pct":           fee_buffer,
        "std_sell_pct":             cfg["std_sell_pct"],
        "recovery_sell_retrace_pct": cfg["recovery_sell_retrace_pct"],
        "panic_sell_pct":           cfg["panic_sell_pct"],
        "catastrophe_sell_pct":     cfg["catastrophe_sell_pct"],
        "std_buy_pct":              cfg["std_buy_pct"],
        "recovery_buy_rebound_pct": cfg["recovery_buy_rebound_pct"],
        "panic_buy_pct":            cfg["panic_buy_pct"],
        "catastrophe_buy_pct":      cfg["catastrophe_buy_pct"],
        "sell_reversal_min_pct":    cfg["sell_reversal_min_pct"],
        "ma_sideways_band_pct":     cfg["ma_sideways_band_pct"],
        "recovery_context":         recovery_context,
        "panic_context":            panic_context,
        "last_panic_loss":          last_panic_loss,
        "last_panic_sell_price":    last_panic_sell_p,
        "last_panic_buy_price":     last_panic_buy_p,
        "recovery_anchor_price":    recovery_anchor,
        "required_next_buy_mode":   "RECOVERY" if required_next_buy_mode else "",
        "required_next_sell_mode":  "RECOVERY" if required_next_sell_mode else "",
        "symbol":                   "XRP/USDC",
        "timeframe":                "1m",
        "expected_recovery_sell_override": None,
    }
    cycle_ctx = {
        "recovery_mode":           recovery_mode,
        "recovery_loss_pct":       recovery_loss_pct,
        "recovery_anchor_price":   recovery_anchor,
        "required_next_buy_mode":  required_next_buy_mode,
        "required_next_sell_mode": required_next_sell_mode,
        "recovery_target_entry_cap": recovery_cap,
        "panic_context":           panic_context,
        "last_panic_loss":         last_panic_loss,
    }
    return ctx, cycle_ctx


# ---------------------------------------------------------------------------
# Profit guard (uses cfg params directly, not env vars)
# ---------------------------------------------------------------------------

def _profit_guard(shadow: dict, last: float, decision: dict, cfg: dict) -> Tuple[bool, str]:
    rule = str(decision.get("rule") or "").strip().upper()
    if rule in ("SELL_PANIC", "SELL_CATASTROPHE"):
        return True, f"panic_allowed:{rule}"

    entry = _sf(shadow.get("entry_price"))
    if entry is None or entry <= 0:
        return False, "SELL_BLOCKED: no entry_price"
    if last is None or last <= 0:
        return False, "SELL_BLOCKED: no last price"

    fee_pct  = cfg["fee_pct"] / 100.0
    slip     = cfg["slippage_pct"] / 100.0
    buf      = cfg["profit_buffer_pct"]
    min_exit = entry * (1.0 + (fee_pct * 2.0) + slip + buf)

    if last < min_exit:
        net_pct = (last / entry) - 1.0 - (fee_pct * 2.0) - slip
        return False, (
            f"SELL_PROFIT_GUARD_BLOCK rule={rule} last={last:.8f} "
            f"entry={entry:.8f} min_exit={min_exit:.8f} net_pct={net_pct:.8f}"
        )
    return True, f"SELL_PROFIT_GUARD_OK rule={rule}"


# ---------------------------------------------------------------------------
# Unrealised PNL (uses cfg params directly)
# ---------------------------------------------------------------------------

def _compute_unrealised(shadow: dict, last: float, cfg: dict) -> None:
    su = shadow.get("unrealised")
    if not isinstance(su, dict):
        su = {}
        shadow["unrealised"] = su

    if not bool(shadow.get("in_position", False)):
        su.update({"gross_usdc": None, "net_usdc": None, "pct": None, "computed_at": None})
        return

    qty        = _sf(shadow.get("entry_qty_base"))
    entry_cost = _sf(shadow.get("entry_cost_usdc"))
    fee_pct    = cfg["fee_pct"] / 100.0

    if qty is None or entry_cost is None or last is None or last <= 0:
        su.update({"gross_usdc": None, "net_usdc": None, "pct": None, "computed_at": None})
        return

    gross_value = qty * last
    gross_usdc  = gross_value - entry_cost
    net_usdc    = gross_value * (1.0 - fee_pct) - entry_cost
    pct         = net_usdc / entry_cost if entry_cost > 0 else 0.0

    su["gross_usdc"]   = gross_usdc
    su["net_usdc"]     = net_usdc
    su["pct"]          = pct
    su["computed_at"]  = int(time.time())


# ---------------------------------------------------------------------------
# Single tick
# ---------------------------------------------------------------------------

def run_tick(shadow: dict, market: dict, cfg: dict) -> Optional[dict]:
    """
    Run one replay tick. Mutates shadow in-place.
    Returns a completed ledger entry dict on SELL, else None.
    """
    last = _sf(market.get("last"))
    if last is None:
        return None

    fee_pct      = cfg["fee_pct"] / 100.0
    slippage_pct = cfg["slippage_pct"] / 100.0

    # 1 — Peak / trough tracking (uses sp directly — no env vars)
    sp._shadow_update_peak_trough(shadow, market)

    # 2 — Recovery arming needs state["levels"] and state["flags"]
    #     Build a minimal state for the sp call
    flags  = _build_flags(market)
    levels = _build_levels(shadow, market, cfg)
    mini_state = {"market": market, "flags": flags, "levels": levels, "last": last}
    sp._shadow_update_recovery_arming(mini_state, shadow)

    # 3 — Build rule engine context from cfg (no env var reads)
    try:
        ctx, cycle_ctx = _build_shadow_ctx(shadow, market, cfg)
    except Exception as exc:
        return None

    # 4 — Rule engine pass (same function as live)
    try:
        raw = rule_engine.decide(ctx, cycle_ctx)
    except Exception:
        return None

    if not isinstance(raw, dict):
        return None

    # 5 — Normalise decision (pure, no env vars)
    decision = sp._shadow_normalise_decision(raw)
    shadow["last_shadow_decision"] = decision

    act    = str(decision.get("action") or "HOLD").upper()
    in_pos = bool(shadow.get("in_position", False))
    ledger_entry = None

    # 6 — Execute shadow trade
    if act == "BUY" and not in_pos:
        equity = _sf(shadow.get("equity_usdc"))
        if equity is not None and equity > 0:
            entry_snap = last
            sp._shadow_record_buy(shadow, last, decision, fee_pct, slippage_pct)
            sp._shadow_apply_cycle_transition(shadow, decision, entry_snap, last)

    elif act == "SELL" and in_pos:
        guard_ok, _guard_msg = _profit_guard(shadow, last, decision, cfg)
        if guard_ok:
            saved_entry = _sf(shadow.get("entry_price"))
            ledger_entry = sp._shadow_record_sell(shadow, last, decision, fee_pct, slippage_pct)
            if ledger_entry:
                sp._shadow_append_ledger(shadow, ledger_entry)
                sp._shadow_recompute_stats(shadow)
                sp._shadow_apply_cycle_transition(shadow, decision, saved_entry, last)

    # 7 — Unrealised PNL
    _compute_unrealised(shadow, last, cfg)
    return ledger_entry


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------

def compute_max_drawdown(equity_curve: List[float]) -> float:
    """Max drawdown % from rolling peak to trough over equity_curve."""
    if not equity_curve:
        return 0.0
    peak   = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * 100.0
            max_dd = max(max_dd, dd)
    return max_dd


# ---------------------------------------------------------------------------
# Main replay
# ---------------------------------------------------------------------------

def run_replay(cfg: dict, verbose: bool = False) -> dict:
    candle_path = cfg["candle_path"]
    days        = cfg["days"]
    start_equity = cfg["start_equity"]

    print(f"[replay] Loading candles from {candle_path} ...", flush=True)
    all_candles = load_candles(candle_path)
    print(f"[replay] Total candles in file: {len(all_candles)}", flush=True)

    # Slice to last N days + warmup
    last_ts_ms    = _ts(all_candles[-1])
    cutoff_ms     = last_ts_ms - days * 24 * 3600 * 1000
    replay_start  = next((i for i, c in enumerate(all_candles) if _ts(c) >= cutoff_ms), 0)
    warmup_start  = max(0, replay_start - cfg["ma_long_period"])

    working       = all_candles[warmup_start:]
    replay_offset = replay_start - warmup_start   # first real replay index in `working`

    n_replay = len(working) - replay_offset
    first_dt = ts_to_dt(_ts(working[replay_offset]))
    last_dt  = ts_to_dt(_ts(working[-1]))

    print(
        f"[replay] Window: {first_dt.date()} → {last_dt.date()} "
        f"({n_replay:,} candles, warmup={replay_offset})",
        flush=True,
    )

    # Precompute all close prices for efficient SMA slicing
    all_closes = [_close(c) for c in working]

    # --- Initialise shadow ---
    shadow: dict = {}
    state_stub: dict = {"shadow": shadow}
    sp._ensure_shadow_section(state_stub)
    shadow = state_stub["shadow"]
    shadow["enabled"]          = True
    shadow["equity_usdc"]      = float(start_equity)
    shadow["equity_seed_usdc"] = float(start_equity)
    shadow["equity_seed_source"] = "replay_config"
    shadow["equity_seed_ts"]   = int(time.time())

    # --- Replay loop ---
    equity_curve: List[float] = [start_equity]
    rule_buy_counter:  Counter = Counter()
    rule_sell_counter: Counter = Counter()
    tick_count = 0
    t0 = time.monotonic()

    for i in range(replay_offset, len(working)):
        market      = _build_market(i, working, all_closes, cfg)
        ledger_entry = run_tick(shadow, market, cfg)
        tick_count  += 1

        # Track equity: when in position use entry_cost + unrealised net
        if bool(shadow.get("in_position", False)):
            entry_cost = _sf(shadow.get("entry_cost_usdc")) or 0.0
            net_u      = _sf((shadow.get("unrealised") or {}).get("net_usdc")) or 0.0
            cur_eq     = entry_cost + net_u
        else:
            cur_eq = _sf(shadow.get("equity_usdc")) or 0.0
        equity_curve.append(cur_eq)

        if ledger_entry:
            rule_buy_counter[str(ledger_entry.get("entry_rule") or "UNKNOWN")]  += 1
            rule_sell_counter[str(ledger_entry.get("exit_rule")  or "UNKNOWN")] += 1
            if verbose:
                pnl_u   = ledger_entry.get("net_pnl_usdc", 0)
                pnl_pct = ledger_entry.get("net_pnl_pct", 0) * 100
                side    = "WIN" if ledger_entry.get("is_win") else "LOSS"
                print(
                    f"  [{side}] trade #{ledger_entry.get('trade_id')} "
                    f"entry={ledger_entry.get('entry_rule')} "
                    f"exit={ledger_entry.get('exit_rule')} "
                    f"pnl={pnl_u:+.4f} USDC ({pnl_pct:+.3f}%)",
                    flush=True,
                )

    elapsed = time.monotonic() - t0
    print(f"[replay] Done: {tick_count:,} ticks in {elapsed:.2f}s", flush=True)

    # --- Metrics ---
    stats   = shadow.get("stats") or {}
    final_eq = _sf(shadow.get("equity_usdc")) or 0.0
    trade_count = stats.get("trade_count", 0)
    net_pnl_usdc = final_eq - start_equity
    net_pnl_pct  = (net_pnl_usdc / start_equity * 100.0) if start_equity > 0 else 0.0
    max_dd       = compute_max_drawdown(equity_curve)

    ledger   = shadow.get("ledger", [])
    first_10 = ledger[:10]
    last_10  = ledger[-10:]

    return {
        "config": {
            "candle_path":                str(candle_path),
            "days":                       days,
            "start_equity_usdc":          start_equity,
            "fee_pct":                    cfg["fee_pct"],
            "slippage_pct":               cfg["slippage_pct"],
            "profit_buffer_pct":          cfg["profit_buffer_pct"],
            "ma_short_period":            cfg["ma_short_period"],
            "ma_long_period":             cfg["ma_long_period"],
            "std_sell_pct":               cfg["std_sell_pct"],
            "recovery_sell_retrace_pct":  cfg["recovery_sell_retrace_pct"],
            "panic_sell_pct":             cfg["panic_sell_pct"],
            "catastrophe_sell_pct":       cfg["catastrophe_sell_pct"],
            "std_buy_pct":                cfg["std_buy_pct"],
            "recovery_buy_rebound_pct":   cfg["recovery_buy_rebound_pct"],
            "panic_buy_pct":              cfg["panic_buy_pct"],
            "catastrophe_buy_pct":        cfg["catastrophe_buy_pct"],
            "sell_reversal_min_pct":      cfg["sell_reversal_min_pct"],
            "ma_sideways_band_pct":       cfg["ma_sideways_band_pct"],
        },
        "replay_window": {
            "first_candle_utc": first_dt.isoformat().replace("+00:00", "Z"),
            "last_candle_utc":  last_dt.isoformat().replace("+00:00", "Z"),
            "candle_count":     n_replay,
            "tick_count":       tick_count,
            "elapsed_sec":      round(elapsed, 3),
        },
        "summary": {
            "start_equity_usdc":   round(start_equity, 8),
            "final_equity_usdc":   round(final_eq, 8),
            "net_pnl_usdc":        round(net_pnl_usdc, 8),
            "net_pnl_pct":         round(net_pnl_pct, 4),
            "trade_count":         trade_count,
            "win_count":           stats.get("win_count", 0),
            "loss_count":          stats.get("loss_count", 0),
            "win_rate_pct":        round(stats.get("win_rate_pct", 0.0), 2),
            "max_drawdown_pct":    round(max_dd, 4),
            "avg_net_pnl_usdc":    round(stats.get("avg_net_pnl_usdc", 0.0), 6),
            "avg_net_pnl_pct":     round(stats.get("avg_net_pnl_pct", 0.0) * 100.0, 4),
            "largest_win_usdc":    round(stats.get("largest_win_usdc", 0.0), 6),
            "largest_loss_usdc":   round(stats.get("largest_loss_usdc", 0.0), 6),
            "largest_win_pct":     round(stats.get("largest_win_pct", 0.0) * 100.0, 4),
            "largest_loss_pct":    round(stats.get("largest_loss_pct", 0.0) * 100.0, 4),
            "total_fees_usdc":     round(stats.get("total_fees_usdc", 0.0), 6),
            "gross_pnl_sum_usdc":  round(stats.get("gross_pnl_sum_usdc", 0.0), 6),
        },
        "rule_distribution": {
            "buy_rules":  dict(rule_buy_counter.most_common()),
            "sell_rules": dict(rule_sell_counter.most_common()),
        },
        "first_10_trades": first_10,
        "last_10_trades":  last_10,
    }


# ---------------------------------------------------------------------------
# Text report formatter
# ---------------------------------------------------------------------------

def _fmt_trade(t: dict) -> List[str]:
    if not t:
        return []
    side     = "WIN" if t.get("is_win") else "LOSS"
    entry_ts = t.get("entry_ts")
    exit_ts  = t.get("exit_ts")
    pnl_usdc = t.get("net_pnl_usdc", 0)
    pnl_pct  = t.get("net_pnl_pct", 0) * 100
    dur      = t.get("duration_sec", 0)
    lines    = [
        f"  #{t.get('trade_id', '?')} [{side}] duration={dur}s",
    ]
    if entry_ts:
        dt = datetime.fromtimestamp(entry_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"    Entry: {dt}  rule={t.get('entry_rule')}  price={t.get('entry_price', 0):.6f}")
    if exit_ts:
        dt = datetime.fromtimestamp(exit_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"    Exit:  {dt}  rule={t.get('exit_rule')}  price={t.get('exit_price', 0):.6f}")
    lines.append(
        f"    Net PNL: {pnl_usdc:+.6f} USDC  ({pnl_pct:+.4f}%)  "
        f"cost={t.get('entry_cost_usdc', 0):.4f} → proceeds={t.get('net_proceeds_usdc', 0):.4f}"
    )
    return lines


def format_txt_report(result: dict) -> str:
    s  = result["summary"]
    c  = result["config"]
    rw = result["replay_window"]
    rd = result["rule_distribution"]

    lines: List[str] = [
        "=" * 64,
        "  UranusBot Shadow Historical Replay Report",
        "=" * 64,
        f"  Period   : {rw['first_candle_utc']}  →  {rw['last_candle_utc']}",
        f"  Days     : {c['days']}",
        f"  Candles  : {rw['candle_count']:,}  (ticks={rw['tick_count']:,}, "
        f"elapsed={rw['elapsed_sec']}s)",
        f"  Data     : {c['candle_path']}",
        "",
        "  ── Equity ──────────────────────────────────────────────",
        f"  Start        : {s['start_equity_usdc']:.8f} USDC",
        f"  Final        : {s['final_equity_usdc']:.8f} USDC",
        f"  Net PNL      : {s['net_pnl_usdc']:+.8f} USDC  ({s['net_pnl_pct']:+.4f}%)",
        f"  Gross PNL    : {s['gross_pnl_sum_usdc']:+.6f} USDC",
        f"  Total Fees   : {s['total_fees_usdc']:.6f} USDC",
        "",
        "  ── Trades ───────────────────────────────────────────────",
        f"  Total        : {s['trade_count']}",
        f"  Wins         : {s['win_count']}",
        f"  Losses       : {s['loss_count']}",
        f"  Win Rate     : {s['win_rate_pct']:.2f}%",
        f"  Avg Net PNL  : {s['avg_net_pnl_usdc']:+.6f} USDC  ({s['avg_net_pnl_pct']:+.4f}%)",
        f"  Largest Win  : {s['largest_win_usdc']:+.6f} USDC  ({s['largest_win_pct']:+.4f}%)",
        f"  Largest Loss : {s['largest_loss_usdc']:+.6f} USDC  ({s['largest_loss_pct']:+.4f}%)",
        f"  Max Drawdown : {s['max_drawdown_pct']:.4f}%",
        "",
        "  ── Parameters ───────────────────────────────────────────",
        f"  Fee/side     : {c['fee_pct']}%",
        f"  Slippage     : {c['slippage_pct']}%",
        f"  STD sell/buy : {c['std_sell_pct']} / {c['std_buy_pct']}",
        f"  Panic s/b    : {c['panic_sell_pct']} / {c['panic_buy_pct']}",
        f"  Catastrophe s/b: {c['catastrophe_sell_pct']} / {c['catastrophe_buy_pct']}",
        f"  MA short/long: {c['ma_short_period']} / {c['ma_long_period']}",
        "",
        "  ── Rule Distribution ─────────────────────────────────────",
        "  BUY rules:",
    ]
    for rule, cnt in rd["buy_rules"].items():
        lines.append(f"    {rule:<35} {cnt}")
    if not rd["buy_rules"]:
        lines.append("    (none)")
    lines.append("  SELL rules:")
    for rule, cnt in rd["sell_rules"].items():
        lines.append(f"    {rule:<35} {cnt}")
    if not rd["sell_rules"]:
        lines.append("    (none)")

    lines += ["", "  ── First 10 Trades ───────────────────────────────────────"]
    for t in result.get("first_10_trades", []):
        lines.extend(_fmt_trade(t))
    if not result.get("first_10_trades"):
        lines.append("    (no trades)")

    lines += ["", "  ── Last 10 Trades ────────────────────────────────────────"]
    for t in result.get("last_10_trades", []):
        lines.extend(_fmt_trade(t))
    if not result.get("last_10_trades"):
        lines.append("    (no trades)")

    lines += ["", "=" * 64]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> None:
    args    = parse_args(argv if argv is not None else sys.argv[1:])
    cfg     = args_to_cfg(args)
    verbose = args.verbose

    # Validate candle file exists before importing shadow_position
    candle_path = cfg["candle_path"]
    if not candle_path.exists():
        print(
            f"[ERROR] Candle file not found: {candle_path}\n"
            f"  Hint: download with freqtrade download-data or pass --candles <path>",
            file=sys.stderr,
        )
        sys.exit(1)

    # Apply cfg to env vars BEFORE importing shadow_position
    _apply_cfg_to_env(cfg)

    # Now safe to import — env vars are already set
    global sp, rule_engine
    sys.path.insert(0, str(APP_DIR))
    import shadow_position as _sp          # noqa: E402
    import rule_engine as _re              # noqa: E402
    sp           = _sp
    rule_engine  = _re

    result = run_replay(cfg, verbose=verbose)

    # Write outputs
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    prefix    = cfg["output_prefix"]
    json_path = REPORTS_DIR / f"{prefix}.json"
    txt_path  = REPORTS_DIR / f"{prefix}.txt"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[replay] JSON report → {json_path}", flush=True)

    txt = format_txt_report(result)
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"[replay] TXT  report → {txt_path}", flush=True)

    # Print summary to stdout
    print("", flush=True)
    s = result["summary"]
    print(
        f"[result] equity {s['start_equity_usdc']:.4f} → {s['final_equity_usdc']:.4f} USDC  "
        f"PNL={s['net_pnl_pct']:+.4f}%  trades={s['trade_count']}  "
        f"win_rate={s['win_rate_pct']:.1f}%  max_dd={s['max_drawdown_pct']:.2f}%",
        flush=True,
    )


# deferred so env vars are set before sp / rule_engine are imported in main()
sp          = None   # type: ignore[assignment]
rule_engine = None   # type: ignore[assignment]


if __name__ == "__main__":
    main()
