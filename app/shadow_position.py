#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shadow_position.py — Shadow (paper) trading engine for UranusBot.

Entry point: maybe_run_shadow_tick(state)
Called from tick_runner.run_once() AFTER maybe_execute_via_api() and BEFORE _write_state().
All shadow state lives in state["shadow"]. Never touches live execution path.
"""

from __future__ import annotations

import os
import json
import sys
import time
import base64
from datetime import datetime, timezone
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sf(x) -> Optional[float]:
    try:
        return float(x) if x is not None else None
    except Exception:
        return None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        val = os.getenv(name)
        return float(val) if val is not None else float(default)
    except Exception:
        return float(default)


def _shadow_log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{ts} [shadow] {msg}", flush=True)


_SHADOW_LEDGER_MAX = 500


# ---------------------------------------------------------------------------
# State initialisation
# ---------------------------------------------------------------------------

def _ensure_shadow_section(state: dict) -> dict:
    shadow = state.get("shadow")
    if not isinstance(shadow, dict):
        shadow = {}
        state["shadow"] = shadow

    shadow.setdefault("enabled", False)
    shadow.setdefault("in_position", False)
    shadow.setdefault("base", None)
    shadow.setdefault("peak", None)
    shadow.setdefault("trough", None)
    shadow.setdefault("entry_price", None)
    shadow.setdefault("entry_qty_base", None)
    shadow.setdefault("entry_cost_usdc", None)
    shadow.setdefault("entry_fee_usdc", None)
    shadow.setdefault("entry_ts", None)
    shadow.setdefault("entry_rule", None)

    shadow.setdefault("equity_usdc", None)
    shadow.setdefault("equity_seed_usdc", None)
    shadow.setdefault("equity_seed_source", None)
    shadow.setdefault("equity_seed_ts", None)
    shadow.setdefault("equity_last_sync_ts", None)
    shadow.setdefault("equity_last_sync_source", None)
    shadow.setdefault("equity_last_sync_value", None)
    shadow.setdefault("equity_sync_failures", 0)

    if not isinstance(shadow.get("cycle"), dict):
        shadow["cycle"] = {}
    sc = shadow["cycle"]
    sc.setdefault("recovery_mode", False)
    sc.setdefault("recovery_loss_pct", 0.0)
    sc.setdefault("recovery_anchor_price", None)
    sc.setdefault("required_next_buy_mode", None)
    sc.setdefault("required_next_sell_mode", None)
    sc.setdefault("recovery_target_entry_cap", None)
    sc.setdefault("last_panic_sell_price", None)
    sc.setdefault("last_panic_buy_price", None)
    sc.setdefault("recovery_buy_armed", False)
    sc.setdefault("recovery_sell_armed", False)
    sc.setdefault("recovery_buy_arm_age", 0)
    sc.setdefault("recovery_sell_arm_age", 0)

    shadow.setdefault("ledger", [])
    shadow.setdefault("ledger_overflow_count", 0)

    if not isinstance(shadow.get("stats"), dict):
        shadow["stats"] = {}
    ss = shadow["stats"]
    ss.setdefault("trade_count", 0)
    ss.setdefault("win_count", 0)
    ss.setdefault("loss_count", 0)
    ss.setdefault("gross_pnl_sum_usdc", 0.0)
    ss.setdefault("net_pnl_sum_usdc", 0.0)
    ss.setdefault("total_fees_usdc", 0.0)
    ss.setdefault("avg_net_pnl_usdc", 0.0)
    ss.setdefault("avg_net_pnl_pct", 0.0)
    ss.setdefault("win_rate_pct", 0.0)
    ss.setdefault("largest_win_usdc", 0.0)
    ss.setdefault("largest_loss_usdc", 0.0)
    ss.setdefault("largest_win_pct", 0.0)
    ss.setdefault("largest_loss_pct", 0.0)
    ss.setdefault("running_equity_usdc", None)
    ss.setdefault("total_pnl_from_seed_usdc", None)
    ss.setdefault("total_pnl_from_seed_pct", None)
    ss.setdefault("last_updated_ts", None)

    if not isinstance(shadow.get("unrealised"), dict):
        shadow["unrealised"] = {}
    su = shadow["unrealised"]
    su.setdefault("gross_usdc", None)
    su.setdefault("net_usdc", None)
    su.setdefault("pct", None)
    su.setdefault("computed_at", None)

    shadow.setdefault("last_shadow_decision", None)
    shadow.setdefault("last_action", None)
    shadow.setdefault("last_action_ts", None)
    shadow.setdefault("last_action_rule", None)
    shadow.setdefault("reset_ts", None)
    shadow.setdefault("reset_count", 0)

    return shadow


# ---------------------------------------------------------------------------
# Equity seeding and synchronisation
# ---------------------------------------------------------------------------

def _ft_read_stake_config(ft_url: str, headers: dict, ft_timeout: float) -> Optional[float]:
    """
    Read configured stake_amount from FT /api/v1/show_config.
    Returns a positive float if stake_amount is a fixed number, None if unlimited/unreadable.
    """
    from urllib.request import Request, urlopen
    try:
        req = Request(f"{ft_url}/api/v1/show_config", headers=headers)
        with urlopen(req, timeout=ft_timeout) as r:
            body = r.read().decode("utf-8", "replace")
        j = json.loads(body)
        if not isinstance(j, dict):
            return None
        sa = j.get("stake_amount")
        if sa is None:
            return None
        if isinstance(sa, str) and sa.strip().lower() in ("unlimited", "inf", ""):
            return None
        v = float(sa)
        return v if v > 0 else None
    except Exception:
        return None


def _ft_read_open_trade_cost(ft_url: str, headers: dict, ft_timeout: float) -> Optional[float]:
    """
    Read the first open trade's stake cost from FT /api/v1/status.
    Returns a positive float (stake_amount or open_rate×amount) or None.
    """
    from urllib.request import Request, urlopen
    try:
        req = Request(f"{ft_url}/api/v1/status", headers=headers)
        with urlopen(req, timeout=ft_timeout) as r:
            body = r.read().decode("utf-8", "replace")
        j = json.loads(body)
        if not isinstance(j, list) or not j:
            return None
        t = j[0]
        if not isinstance(t, dict):
            return None
        for key in ("stake_amount", "cost"):
            v = t.get(key)
            if v is not None:
                try:
                    fv = float(v)
                    if fv > 0:
                        return fv
                except Exception:
                    pass
        open_rate = t.get("open_rate") or t.get("open_rate_requested")
        amount = t.get("amount")
        if open_rate is not None and amount is not None:
            try:
                cost = float(open_rate) * float(amount)
                if cost > 0:
                    return cost
            except Exception:
                pass
        return None
    except Exception:
        return None


def fetch_shadow_equity_from_live(state: dict) -> Tuple[Optional[float], str]:
    """
    Priority chain:
      0. live in position → state["live_trade_stake"] (stored by tick_runner) or
         FT /api/v1/status trade[0].stake_amount / cost
      1. live flat + FT /api/v1/balance + show_config stake_amount:
         - stake_amount numeric  → min(usdc_free, stake_amount)
         - stake_amount unlimited → usdc_free
      2. Binance REST USDC.free
      3. SHADOW_START_EQUITY_USDC env var
    Returns (amount_or_None, source_string).
    """
    ft_url = os.getenv("FT_URL", "http://127.0.0.1:8090").rstrip("/")
    ft_username = os.getenv("FT_USERNAME", "").strip()
    ft_password = os.getenv("FT_PASSWORD", "").strip()
    ft_timeout = _env_float("FT_TIMEOUT", 8.0)
    live_in_position = bool(state.get("in_position", False))

    headers: dict = {"Accept": "application/json"}
    if ft_username and ft_password:
        tok = base64.b64encode(f"{ft_username}:{ft_password}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {tok}"

    # Step 0 — live is in position: mirror the actual live trade size
    if live_in_position:
        live_stake = _sf(state.get("live_trade_stake"))
        if live_stake is not None and live_stake > 0:
            return live_stake, "live_trade_stake"

        cost = _ft_read_open_trade_cost(ft_url, headers, ft_timeout)
        if cost is not None and cost > 0:
            return cost, "ft_open_trade_cost"

    # Step 1 — FT balance + optional stake_amount cap
    try:
        from urllib.request import Request, urlopen
        req = Request(f"{ft_url}/api/v1/balance", headers=headers)
        with urlopen(req, timeout=ft_timeout) as r:
            body = r.read().decode("utf-8", "replace")

        j = json.loads(body)
        if isinstance(j, dict):
            curmap: dict = {}
            currencies = j.get("currencies")
            if isinstance(currencies, dict):
                curmap = currencies
            elif isinstance(currencies, list):
                for item in currencies:
                    if isinstance(item, dict) and item.get("currency"):
                        curmap[item["currency"]] = item

            def _pick(obj, keys):
                if not isinstance(obj, dict):
                    return None
                for k in keys:
                    v = obj.get(k)
                    if isinstance(v, (int, float)) and float(v) >= 0:
                        return float(v)
                    try:
                        if isinstance(v, str) and v.strip():
                            fv = float(v)
                            if fv >= 0:
                                return fv
                    except Exception:
                        pass
                return None

            usdc_free = None
            if "USDC" in curmap:
                usdc_free = _pick(curmap["USDC"], ("free", "balance", "total"))

            if not live_in_position and usdc_free is not None and usdc_free > 0:
                stake_cfg = _ft_read_stake_config(ft_url, headers, ft_timeout)
                if stake_cfg is not None:
                    return min(usdc_free, stake_cfg), "ft_balance_stake_capped"
                return usdc_free, "ft_balance"

            total = _pick(j, ("total",))
            if total is not None and total > 0:
                return total, "ft_balance_total"

            if "USDC" in curmap:
                usdc_val = _pick(curmap["USDC"], ("balance", "total", "free"))
                if usdc_val is not None and usdc_val > 0:
                    return usdc_val, "ft_balance"
    except Exception:
        pass

    # Step 2 — Binance REST
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        if app_dir not in sys.path:
            sys.path.insert(0, app_dir)
        from binance_wallet import get_spot_balances_from_freqtrade_config  # type: ignore

        candidates = [
            os.path.join(app_dir, "..", "freqtrade", "user_data", "config.json"),
            "/opt/bots/uranus/freqtrade/user_data/config.json",
        ]
        for cfg_path in candidates:
            if os.path.exists(cfg_path):
                result = get_spot_balances_from_freqtrade_config(cfg_path)
                if result.get("status") == "OK":
                    balances = result.get("balances", {})
                    usdc = balances.get("USDC", {})
                    free = usdc.get("free", 0.0)
                    if float(free) > 0:
                        return float(free), "binance_rest"
                break
    except Exception:
        pass

    # Step 3 — env var fallback
    try:
        val = _env_float("SHADOW_START_EQUITY_USDC", 0.0)
        if val > 0:
            return val, "env_var"
    except Exception:
        pass

    return None, "unavailable"


def _shadow_sync_equity_if_flat(state: dict, shadow: dict) -> None:
    if bool(shadow.get("in_position", False)):
        return  # Never sync mid-position — real balance reflects live trade, not shadow

    amount, source = fetch_shadow_equity_from_live(state)

    if amount is not None and amount > 0:
        shadow["equity_usdc"] = amount
        shadow["equity_last_sync_ts"] = int(time.time())
        shadow["equity_last_sync_source"] = source
        shadow["equity_last_sync_value"] = amount
        shadow["equity_sync_failures"] = 0

        if shadow.get("equity_seed_usdc") is None:
            shadow["equity_seed_usdc"] = amount
            shadow["equity_seed_source"] = source
            shadow["equity_seed_ts"] = int(time.time())
    else:
        shadow["equity_sync_failures"] = int(shadow.get("equity_sync_failures") or 0) + 1
        _shadow_log(
            f"SHADOW_BALANCE_READ_FAIL consecutive_failures={shadow['equity_sync_failures']} source={source}"
        )


# ---------------------------------------------------------------------------
# Market tracking
# ---------------------------------------------------------------------------

def _shadow_update_peak_trough(shadow: dict, market: dict) -> None:
    if not isinstance(market, dict):
        return

    last = _sf(market.get("last"))
    high = _sf(market.get("high"))
    low = _sf(market.get("low"))

    if last is None:
        return

    in_pos = bool(shadow.get("in_position", False))

    if in_pos:
        cand = high if high is not None else last
        cur = _sf(shadow.get("peak"))
        shadow["peak"] = max(cur, cand) if cur is not None else cand
        shadow["trough"] = None
    else:
        cand = low if low is not None else last
        cur = _sf(shadow.get("trough"))
        shadow["trough"] = min(cur, cand) if cur is not None else cand
        shadow["peak"] = None


# ---------------------------------------------------------------------------
# Recovery arming (mirrors tick_runner.update_recovery_state — read-only)
# ---------------------------------------------------------------------------

def _shadow_update_recovery_arming(state: dict, shadow: dict) -> None:
    sc = shadow.get("cycle")
    if not isinstance(sc, dict):
        return

    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    last = _sf(market.get("last", state.get("last")))
    if last is None:
        return

    levels = state.get("levels") if isinstance(state.get("levels"), dict) else {}
    flags = state.get("flags") if isinstance(state.get("flags"), dict) else {}

    trend_up = bool(flags.get("ma_positive"))
    trend_down = bool(flags.get("ma_negative"))

    arm_timeout = int(_env_float("RECOVERY_ARM_TIMEOUT_BARS", 240))

    panic_sell = _sf(levels.get("panic_sell"))
    catastrophe_sell = _sf(levels.get("catastrophe_sell"))
    panic_buy = _sf(levels.get("panic_buy"))
    catastrophe_buy = _sf(levels.get("catastrophe_buy"))

    buy_armed = bool(sc.get("recovery_buy_armed", False))
    sell_armed = bool(sc.get("recovery_sell_armed", False))
    buy_age = int(sc.get("recovery_buy_arm_age") or 0)
    sell_age = int(sc.get("recovery_sell_arm_age") or 0)

    if trend_up:
        if (panic_buy is not None and last >= panic_buy) or (catastrophe_buy is not None and last >= catastrophe_buy):
            sell_armed = True
            sell_age = 0

    if trend_down:
        if (panic_sell is not None and last <= panic_sell) or (catastrophe_sell is not None and last <= catastrophe_sell):
            buy_armed = True
            buy_age = 0

    if sell_armed:
        sell_age += 1
        if sell_age > arm_timeout:
            sell_armed = False
            sell_age = 0

    if buy_armed:
        buy_age += 1
        if buy_age > arm_timeout:
            buy_armed = False
            buy_age = 0

    sc["recovery_buy_armed"] = buy_armed
    sc["recovery_sell_armed"] = sell_armed
    sc["recovery_buy_arm_age"] = buy_age
    sc["recovery_sell_arm_age"] = sell_age


# ---------------------------------------------------------------------------
# Rule engine context builder (shadow-specific)
# ---------------------------------------------------------------------------

def _shadow_build_ctx(state: dict, shadow: dict) -> Tuple[dict, dict]:
    """
    Build (ctx, cycle_ctx) for rule_engine.evaluate_rule_engine().
    Position fields come from shadow (NOT live state).
    Market price / MA fields are shared from state["market"] (safe — not position-specific).
    """
    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    sc = shadow.get("cycle") if isinstance(shadow.get("cycle"), dict) else {}

    # Shadow position state
    in_position = bool(shadow.get("in_position", False))
    base = _sf(shadow.get("base"))
    peak = _sf(shadow.get("peak"))
    trough = _sf(shadow.get("trough"))

    # Shared market data
    last = _sf(market.get("last", state.get("last")))
    prev_last = _sf(market.get("prev_last", state.get("prev_last")))
    ma_short = _sf(market.get("ma_short", state.get("ma_short")))
    ma_long = _sf(market.get("ma_long", state.get("ma_long")))

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

    fee_pct_per_side = _env_float("FEE_PCT", 0.1) / 100.0
    slippage_pct = _env_float("SLIPPAGE_PCT", 0.0) / 100.0
    profit_buffer = _env_float("PROFIT_BUFFER_PCT", 0.001)
    fee_total_pct = fee_pct_per_side * 2.0
    fee_buffer_pct = max(profit_buffer, fee_total_pct + slippage_pct)

    required_next_buy_mode = str(sc.get("required_next_buy_mode") or "")
    required_next_sell_mode = str(sc.get("required_next_sell_mode") or "")
    recovery_mode = bool(sc.get("recovery_mode", False))
    recovery_loss_pct = float(sc.get("recovery_loss_pct") or 0.0)
    recovery_anchor_price = _sf(sc.get("recovery_anchor_price"))
    recovery_target_entry_cap = _sf(sc.get("recovery_target_entry_cap"))
    last_panic_sell_price = _sf(sc.get("last_panic_sell_price"))
    last_panic_buy_price = _sf(sc.get("last_panic_buy_price"))

    panic_context = "NONE"
    if required_next_buy_mode:
        panic_context = "AFTER_SELL_PANIC"
    elif required_next_sell_mode:
        panic_context = "AFTER_BUY_PANIC"

    last_panic_loss = 0.0
    if recovery_anchor_price is not None and recovery_loss_pct > 0:
        last_panic_loss = recovery_anchor_price * recovery_loss_pct

    recovery_context = recovery_mode or bool(required_next_buy_mode) or bool(required_next_sell_mode)

    ctx = {
        "base": base if base is not None else 0.0,
        "last": last if last is not None else 0.0,
        "prev_last": prev_last if prev_last is not None else (last or 0.0),
        "peak": peak if peak is not None else (base or 0.0),
        "trough": trough if trough is not None else (base or 0.0),
        "in_position": in_position,
        "ma_short": ma_short if ma_short is not None else 0.0,
        "ma_long": ma_long if ma_long is not None else 0.0,
        "fee_pct_per_side": fee_pct_per_side,
        "fee_total_pct": fee_total_pct,
        "fee_buffer_pct": fee_buffer_pct,
        "std_sell_pct": _env_float("STD_SELL_PCT", 0.01),
        "recovery_sell_retrace_pct": _env_float("RECOVERY_SELL_RETRACE_PCT", 0.006),
        "panic_sell_pct": _env_float("PANIC_SELL_PCT", 0.01),
        "catastrophe_sell_pct": _env_float("CATASTROPHE_SELL_PCT", 0.10),
        "std_buy_pct": _env_float("STD_BUY_PCT", 0.01),
        "recovery_buy_rebound_pct": _env_float("RECOVERY_BUY_REBOUND_PCT", 0.006),
        "panic_buy_pct": _env_float("PANIC_BUY_PCT", 0.01),
        "catastrophe_buy_pct": _env_float("CATASTROPHE_BUY_PCT", 0.10),
        "sell_reversal_min_pct": _env_float("SELL_REVERSAL_MIN_PCT", 0.0),
        "ma_sideways_band_pct": _env_float("MA_SIDEWAYS_BAND_PCT", 0.0005),
        "recovery_context": recovery_context,
        "panic_context": panic_context,
        "last_panic_loss": last_panic_loss,
        "last_panic_sell_price": last_panic_sell_price,
        "last_panic_buy_price": last_panic_buy_price,
        "recovery_anchor_price": recovery_anchor_price,
        "required_next_buy_mode": "RECOVERY" if required_next_buy_mode else "",
        "required_next_sell_mode": "RECOVERY" if required_next_sell_mode else "",
        "symbol": os.getenv("PAIR", "XRP/USDC"),
        "timeframe": os.getenv("TIMEFRAME", "1m"),
        "expected_recovery_sell_override": None,
    }

    cycle_ctx = {
        "recovery_mode": recovery_mode,
        "recovery_loss_pct": recovery_loss_pct,
        "recovery_anchor_price": recovery_anchor_price,
        "required_next_buy_mode": required_next_buy_mode,
        "required_next_sell_mode": required_next_sell_mode,
        "recovery_target_entry_cap": recovery_target_entry_cap,
        "panic_context": panic_context,
        "last_panic_loss": last_panic_loss,
    }

    return ctx, cycle_ctx


# ---------------------------------------------------------------------------
# Profit guard
# ---------------------------------------------------------------------------

def _shadow_profit_guard(shadow: dict, last: float, decision: dict) -> Tuple[bool, str]:
    rule = str(decision.get("rule") or "").strip().upper()

    if rule in ("SELL_PANIC", "SELL_CATASTROPHE"):
        return True, f"panic_allowed:{rule}"

    entry = _sf(shadow.get("entry_price"))
    if entry is None or entry <= 0:
        return False, "SHADOW_SELL_BLOCKED: no entry_price"
    if last is None or last <= 0:
        return False, "SHADOW_SELL_BLOCKED: no last price"

    fee_pct = _env_float("FEE_PCT", 0.1) / 100.0
    slippage = _env_float("SLIPPAGE_PCT", 0.0) / 100.0
    buf = _env_float("PROFIT_BUFFER_PCT", 0.001)

    min_exit = entry * (1.0 + (fee_pct * 2.0) + slippage + buf)
    if last < min_exit:
        net_pct = (last / entry) - 1.0 - (fee_pct * 2.0) - slippage
        return False, (
            f"SHADOW_SELL_PROFIT_GUARD_BLOCK rule={rule} last={last:.8f} "
            f"entry={entry:.8f} min_exit={min_exit:.8f} net_pct={net_pct:.8f}"
        )

    return True, f"SHADOW_SELL_PROFIT_GUARD_OK rule={rule}"


# ---------------------------------------------------------------------------
# Trade accounting
# ---------------------------------------------------------------------------

def _shadow_record_buy(
    shadow: dict, last: float, decision: dict, fee_pct: float, slippage_pct: float
) -> None:
    equity = _sf(shadow.get("equity_usdc"))
    if equity is None or equity <= 0:
        _shadow_log(f"SHADOW_BUY_SKIP equity_usdc={equity}")
        return

    effective_entry = last * (1.0 + slippage_pct)
    buy_fee_usdc = equity * fee_pct
    qty_base = (equity - buy_fee_usdc) / effective_entry

    now = int(time.time())
    shadow["in_position"] = True
    shadow["equity_usdc"] = 0.0
    shadow["base"] = effective_entry
    shadow["entry_price"] = effective_entry
    shadow["entry_qty_base"] = qty_base
    shadow["entry_cost_usdc"] = equity
    shadow["entry_fee_usdc"] = buy_fee_usdc
    shadow["entry_ts"] = now
    shadow["entry_rule"] = decision.get("rule")
    shadow["peak"] = effective_entry
    shadow["trough"] = None
    shadow["last_action"] = "BUY"
    shadow["last_action_ts"] = now
    shadow["last_action_rule"] = decision.get("rule")

    _shadow_log(
        f"SHADOW_BUY entry={effective_entry:.8f} qty={qty_base:.6f} "
        f"cost={equity:.4f} fee={buy_fee_usdc:.4f} rule={decision.get('rule')}"
    )


def _shadow_record_sell(
    shadow: dict, last: float, decision: dict, fee_pct: float, slippage_pct: float
) -> dict:
    qty = _sf(shadow.get("entry_qty_base"))
    entry_price = _sf(shadow.get("entry_price"))
    entry_cost = _sf(shadow.get("entry_cost_usdc"))
    entry_fee = _sf(shadow.get("entry_fee_usdc"))
    entry_ts = shadow.get("entry_ts")
    entry_rule = shadow.get("entry_rule")

    if qty is None or entry_price is None or entry_cost is None:
        _shadow_log("SHADOW_SELL_SKIP: missing entry data")
        return {}

    effective_exit = last * (1.0 - slippage_pct)
    gross_proceeds = qty * effective_exit
    sell_fee_usdc = gross_proceeds * fee_pct
    net_proceeds = gross_proceeds - sell_fee_usdc

    gross_pnl_usdc = gross_proceeds - entry_cost
    total_fees_usdc = (entry_fee or 0.0) + sell_fee_usdc
    net_pnl_usdc = net_proceeds - entry_cost
    net_pnl_pct = net_pnl_usdc / entry_cost if entry_cost > 0 else 0.0

    now = int(time.time())
    rule = str(decision.get("rule") or "")
    duration_sec = (now - entry_ts) if entry_ts else 0
    trade_id = int((shadow.get("stats") or {}).get("trade_count") or 0) + 1

    ledger_entry = {
        "trade_id": trade_id,
        "entry_ts": entry_ts,
        "exit_ts": now,
        "duration_sec": duration_sec,
        "entry_price": entry_price,
        "exit_price": effective_exit,
        "entry_rule": entry_rule,
        "exit_rule": rule,
        "entry_cost_usdc": entry_cost,
        "entry_qty_base": qty,
        "entry_fee_usdc": entry_fee or 0.0,
        "gross_proceeds_usdc": gross_proceeds,
        "sell_fee_usdc": sell_fee_usdc,
        "net_proceeds_usdc": net_proceeds,
        "gross_pnl_usdc": gross_pnl_usdc,
        "total_fees_usdc": total_fees_usdc,
        "net_pnl_usdc": net_pnl_usdc,
        "net_pnl_pct": net_pnl_pct,
        "equity_before_usdc": entry_cost,
        "equity_after_usdc": net_proceeds,
        "is_win": net_pnl_usdc > 0,
        "panic_exit": rule in ("SELL_PANIC", "SELL_CATASTROPHE"),
    }

    shadow["equity_usdc"] = net_proceeds
    shadow["in_position"] = False
    shadow["entry_price"] = None
    shadow["entry_qty_base"] = None
    shadow["entry_cost_usdc"] = None
    shadow["entry_fee_usdc"] = None
    shadow["entry_ts"] = None
    shadow["entry_rule"] = None
    shadow["base"] = effective_exit
    shadow["trough"] = effective_exit
    shadow["peak"] = None
    shadow["last_action"] = "SELL"
    shadow["last_action_ts"] = now
    shadow["last_action_rule"] = rule

    _shadow_log(
        f"SHADOW_SELL exit={effective_exit:.8f} proceeds={net_proceeds:.4f} "
        f"net_pnl={net_pnl_usdc:.4f} ({net_pnl_pct * 100:.4f}%) rule={rule}"
    )

    return ledger_entry


# ---------------------------------------------------------------------------
# Ledger and statistics
# ---------------------------------------------------------------------------

def _shadow_append_ledger(shadow: dict, entry: dict) -> None:
    ledger = shadow.get("ledger")
    if not isinstance(ledger, list):
        ledger = []
        shadow["ledger"] = ledger

    ledger.append(entry)

    overflow = len(ledger) - _SHADOW_LEDGER_MAX
    if overflow > 0:
        del ledger[:overflow]
        shadow["ledger_overflow_count"] = int(shadow.get("ledger_overflow_count") or 0) + overflow


def _shadow_recompute_stats(shadow: dict) -> None:
    ledger = shadow.get("ledger")
    if not isinstance(ledger, list):
        return

    ss = shadow.get("stats")
    if not isinstance(ss, dict):
        ss = {}
        shadow["stats"] = ss

    trade_count = len(ledger)
    win_count = sum(1 for e in ledger if e.get("is_win"))
    loss_count = trade_count - win_count

    gross_pnl_sum = sum(float(e.get("gross_pnl_usdc") or 0.0) for e in ledger)
    net_pnl_sum = sum(float(e.get("net_pnl_usdc") or 0.0) for e in ledger)
    total_fees = sum(float(e.get("total_fees_usdc") or 0.0) for e in ledger)

    avg_net_pnl_usdc = net_pnl_sum / trade_count if trade_count > 0 else 0.0
    pnl_pcts = [float(e.get("net_pnl_pct") or 0.0) for e in ledger]
    avg_net_pnl_pct = sum(pnl_pcts) / trade_count if trade_count > 0 else 0.0
    win_rate_pct = (win_count / trade_count * 100.0) if trade_count > 0 else 0.0

    wins_usdc = [float(e.get("net_pnl_usdc") or 0.0) for e in ledger if e.get("is_win")]
    losses_usdc = [float(e.get("net_pnl_usdc") or 0.0) for e in ledger if not e.get("is_win")]
    wins_pct = [float(e.get("net_pnl_pct") or 0.0) for e in ledger if e.get("is_win")]
    losses_pct = [float(e.get("net_pnl_pct") or 0.0) for e in ledger if not e.get("is_win")]

    running_equity = _sf(shadow.get("equity_usdc"))
    seed = _sf(shadow.get("equity_seed_usdc"))

    total_pnl_from_seed_usdc = None
    total_pnl_from_seed_pct = None
    if running_equity is not None and seed is not None and seed > 0:
        total_pnl_from_seed_usdc = running_equity - seed
        total_pnl_from_seed_pct = total_pnl_from_seed_usdc / seed

    ss["trade_count"] = trade_count
    ss["win_count"] = win_count
    ss["loss_count"] = loss_count
    ss["gross_pnl_sum_usdc"] = gross_pnl_sum
    ss["net_pnl_sum_usdc"] = net_pnl_sum
    ss["total_fees_usdc"] = total_fees
    ss["avg_net_pnl_usdc"] = avg_net_pnl_usdc
    ss["avg_net_pnl_pct"] = avg_net_pnl_pct
    ss["win_rate_pct"] = win_rate_pct
    ss["largest_win_usdc"] = max(wins_usdc) if wins_usdc else 0.0
    ss["largest_loss_usdc"] = min(losses_usdc) if losses_usdc else 0.0
    ss["largest_win_pct"] = max(wins_pct) if wins_pct else 0.0
    ss["largest_loss_pct"] = min(losses_pct) if losses_pct else 0.0
    ss["running_equity_usdc"] = running_equity
    ss["total_pnl_from_seed_usdc"] = total_pnl_from_seed_usdc
    ss["total_pnl_from_seed_pct"] = total_pnl_from_seed_pct
    ss["last_updated_ts"] = int(time.time())


# ---------------------------------------------------------------------------
# Shadow cycle state machine (mirrors _apply_cycle_rules_after_execution)
# ---------------------------------------------------------------------------

def _sc_clear_recovery(sc: dict) -> None:
    sc["recovery_mode"] = False
    sc["recovery_loss_pct"] = 0.0
    sc["recovery_anchor_price"] = None
    sc["required_next_buy_mode"] = None
    sc["required_next_sell_mode"] = None
    sc["recovery_target_entry_cap"] = None


def _sc_set_next_buy_recovery(sc: dict, entry_price: float, exit_price: float) -> None:
    loss_pct = 0.0
    if entry_price is not None and entry_price > 0 and exit_price is not None:
        loss_pct = max(0.0, (float(entry_price) - float(exit_price)) / float(entry_price))
    sc["recovery_mode"] = True
    sc["recovery_loss_pct"] = float(loss_pct)
    sc["recovery_anchor_price"] = float(entry_price)
    sc["required_next_buy_mode"] = "RECOVERY_OR_LOWER_STANDARD"
    sc["required_next_sell_mode"] = None
    sc["recovery_target_entry_cap"] = float(exit_price)


def _sc_set_next_sell_recovery(sc: dict, entry_price: Optional[float]) -> None:
    sc["recovery_mode"] = True
    sc["required_next_buy_mode"] = None
    sc["required_next_sell_mode"] = "RECOVERY_OR_HIGHER_STANDARD"
    sc["recovery_target_entry_cap"] = None
    if entry_price is not None:
        sc["recovery_anchor_price"] = float(entry_price)
    if sc.get("recovery_loss_pct") is None:
        sc["recovery_loss_pct"] = 0.0


def _shadow_apply_cycle_transition(
    shadow: dict, decision: dict, entry_price_before_trade: Optional[float], last: float
) -> None:
    sc = shadow.get("cycle")
    if not isinstance(sc, dict):
        return

    action = str(decision.get("action") or "").strip().upper()
    rule = str(decision.get("rule") or "").strip().upper()

    data = decision.get("data") if isinstance(decision.get("data"), dict) else {}
    min_profitable = _sf(data.get("min_profitable_exit_level"))
    min_recovery = _sf(data.get("min_recovery_exit_level"))

    entry_price = entry_price_before_trade

    if action == "SELL":
        profitable_exit = False
        recovered_exit = False

        if last is not None and min_profitable is not None:
            profitable_exit = last >= min_profitable
        elif entry_price is not None and last is not None:
            profitable_exit = last >= entry_price

        if last is not None and min_recovery is not None:
            recovered_exit = last >= min_recovery

        if rule in ("SELL_PANIC", "SELL_CATASTROPHE"):
            if entry_price is not None and last is not None and not profitable_exit:
                _sc_set_next_buy_recovery(sc, entry_price, last)
                return

        if bool(sc.get("recovery_mode", False)):
            if rule in ("SELL_RECOVERY", "SELL_STANDARD") and (recovered_exit or profitable_exit):
                _sc_clear_recovery(sc)
                return

        if entry_price is not None and last is not None and not profitable_exit:
            _sc_set_next_buy_recovery(sc, entry_price, last)
            return

        if not bool(sc.get("recovery_mode", False)):
            sc["required_next_buy_mode"] = None
            sc["required_next_sell_mode"] = None
            sc["recovery_target_entry_cap"] = None

        return

    if action == "BUY":
        buy_entry = last if last is not None else entry_price

        if rule in ("BUY_PANIC", "BUY_CATASTROPHE"):
            _sc_set_next_sell_recovery(sc, buy_entry)
            return

        if rule in ("BUY_STANDARD", "BUY_RECOVERY"):
            if sc.get("required_next_buy_mode") == "RECOVERY_OR_LOWER_STANDARD":
                sc["required_next_buy_mode"] = None
                sc["recovery_target_entry_cap"] = None
            return


# ---------------------------------------------------------------------------
# Unrealised PNL
# ---------------------------------------------------------------------------

def _shadow_compute_unrealised(shadow: dict, last: float, fee_pct: float) -> None:
    su = shadow.get("unrealised")
    if not isinstance(su, dict):
        su = {}
        shadow["unrealised"] = su

    if not bool(shadow.get("in_position", False)):
        su["gross_usdc"] = None
        su["net_usdc"] = None
        su["pct"] = None
        su["computed_at"] = None
        return

    qty = _sf(shadow.get("entry_qty_base"))
    entry_cost = _sf(shadow.get("entry_cost_usdc"))

    if qty is None or entry_cost is None or last is None or last <= 0:
        su["gross_usdc"] = None
        su["net_usdc"] = None
        su["pct"] = None
        su["computed_at"] = None
        return

    gross_value = qty * last
    gross_usdc = gross_value - entry_cost
    net_usdc = gross_value * (1.0 - fee_pct) - entry_cost
    pct = net_usdc / entry_cost if entry_cost > 0 else 0.0

    su["gross_usdc"] = gross_usdc
    su["net_usdc"] = net_usdc
    su["pct"] = pct
    su["computed_at"] = int(time.time())


# ---------------------------------------------------------------------------
# Decision normalisation
# ---------------------------------------------------------------------------

def _shadow_normalise_decision(raw: dict) -> dict:
    engine_decision = str(
        raw.get("decision") or raw.get("action") or raw.get("last_result") or "HOLD"
    ).strip().upper()

    if engine_decision.startswith("BUY_"):
        action = "BUY"
        rule = engine_decision
    elif engine_decision.startswith("SELL_"):
        action = "SELL"
        rule = engine_decision
    else:
        action = "HOLD"
        rule = engine_decision or "HOLD"

    thresholds = raw.get("thresholds") if isinstance(raw.get("thresholds"), dict) else {}

    return {
        "action": action,
        "rule": rule,
        "reason": str(raw.get("reason") or ""),
        "thresholds": thresholds,
        "candidates": raw.get("candidates"),
        "trend_state": raw.get("trend_state"),
        "panic_context": raw.get("panic_context"),
        "recovery_context": raw.get("recovery_context"),
        "required_next_buy_mode": raw.get("required_next_buy_mode"),
        "required_next_sell_mode": raw.get("required_next_sell_mode"),
        "data": {
            "selected_level_name": raw.get("selected_level_name"),
            "selected_level": raw.get("selected_level"),
            "min_profitable_exit_level": thresholds.get("min_profitable_exit_level"),
            "min_recovery_exit_level": thresholds.get("min_recovery_exit_level"),
        },
    }


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def maybe_run_shadow_tick(state: dict) -> None:
    """
    Called once per tick from tick_runner.run_once() AFTER maybe_execute_via_api()
    and BEFORE _write_state(). Mutates state["shadow"] in-place.
    """
    if not _env_bool("SHADOW_ENABLED", False):
        return

    shadow = _ensure_shadow_section(state)
    shadow["enabled"] = True

    market = state.get("market") if isinstance(state.get("market"), dict) else {}
    last = _sf(market.get("last", state.get("last")))

    fee_pct = _env_float("FEE_PCT", 0.1) / 100.0
    slippage_pct = _env_float("SLIPPAGE_PCT", 0.0) / 100.0

    # 1. Sync equity (only when flat; deferred mid-position)
    _shadow_sync_equity_if_flat(state, shadow)

    # 2. Defer first-time init if equity not yet available
    if shadow.get("equity_usdc") is None:
        _shadow_log("SHADOW_INIT_DEFERRED equity_usdc not available")
        return

    # 3. Defer first seed if live is in_position (real balance reflects live trade)
    if shadow.get("equity_seed_usdc") is None and bool(state.get("in_position", False)):
        _shadow_log("SHADOW_INIT_DEFERRED_LIVE_IN_POSITION waiting for live to be flat")
        return

    # 4. Peak/trough tracking
    _shadow_update_peak_trough(shadow, market)

    # 5. Recovery arming (read-only mirror of live levels/flags)
    _shadow_update_recovery_arming(state, shadow)

    # 6. Validate last price
    if last is None:
        _shadow_log("SHADOW_SKIP no last price")
        return

    # 7. Build shadow-specific rule engine context
    try:
        shadow_ctx, shadow_cycle_ctx = _shadow_build_ctx(state, shadow)
    except Exception as exc:
        _shadow_log(f"SHADOW_CTX_ERROR {type(exc).__name__}: {exc}")
        return

    # 8. Independent rule engine pass (pure function — safe to call twice per tick)
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        if app_dir not in sys.path:
            sys.path.insert(0, app_dir)
        import rule_engine  # type: ignore
        shadow_raw = rule_engine.decide(shadow_ctx, shadow_cycle_ctx)
    except Exception as exc:
        _shadow_log(f"SHADOW_ENGINE_ERROR {type(exc).__name__}: {exc}")
        return

    if not isinstance(shadow_raw, dict):
        _shadow_log(f"SHADOW_ENGINE_BAD_RESULT type={type(shadow_raw)}")
        return

    # 9. Normalise and store decision
    shadow_decision = _shadow_normalise_decision(shadow_raw)
    shadow["last_shadow_decision"] = shadow_decision

    act = str(shadow_decision.get("action") or "HOLD").upper()
    in_pos = bool(shadow.get("in_position", False))

    # 10. Execute shadow trade
    if act == "BUY" and not in_pos:
        equity = _sf(shadow.get("equity_usdc"))
        if equity is None or equity <= 0:
            _shadow_log(f"SHADOW_BUY_DEFERRED equity_usdc={equity}")
        else:
            entry_price_snapshot = last
            _shadow_record_buy(shadow, last, shadow_decision, fee_pct, slippage_pct)
            _shadow_apply_cycle_transition(shadow, shadow_decision, entry_price_snapshot, last)

    elif act == "SELL" and in_pos:
        guard_ok, guard_reason = _shadow_profit_guard(shadow, last, shadow_decision)
        if not guard_ok:
            _shadow_log(guard_reason)
        else:
            saved_entry_price = _sf(shadow.get("entry_price"))
            ledger_entry = _shadow_record_sell(shadow, last, shadow_decision, fee_pct, slippage_pct)
            if ledger_entry:
                _shadow_append_ledger(shadow, ledger_entry)
                _shadow_recompute_stats(shadow)
                _shadow_apply_cycle_transition(shadow, shadow_decision, saved_entry_price, last)

    elif act == "BUY" and in_pos:
        _shadow_log("SHADOW_SKIP_BUY already_in_position")

    elif act == "SELL" and not in_pos:
        _shadow_log("SHADOW_SKIP_SELL not_in_position")

    # 11. Unrealised PNL
    _shadow_compute_unrealised(shadow, last, fee_pct)
