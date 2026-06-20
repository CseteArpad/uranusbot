#!/usr/bin/env python3
"""Uranus - Freqtrade state.json sync (canonical writer)

Goals (2026-01):
- Write ONLY canonical keys used by the UI / rule-engine.
- Preserve any existing `legacy` block (do not rewrite it every run).
- Never re-introduce legacy top-level keys like:
  last_price, current_rate, open_rate, base_price_current, in_position, etc.

This script is executed by systemd timer: uranus-state-sync.timer
"""

import json
import os
import time
import urllib.parse
import urllib.request
from base64 import b64encode
from typing import Any, Dict, Optional, Tuple


# =============================================================================
# CONFIG (paths + defaults)
# =============================================================================
FT_CONFIG = os.getenv(
    "URANUS_FT_CONFIG",
    "/opt/bots/uranus/freqtrade/user_data/config.json",
)
STATE_JSON = os.getenv(
    "URANUS_STATE_JSON",
    "/opt/bots/uranus/state.json",
)

STOPLOSS_COOLDOWN_SECONDS = int(os.getenv("URANUS_STOPLOSS_COOLDOWN_SECONDS", "300"))  # 5 perc

DEFAULT_FT_BASE_URL = os.getenv("URANUS_FT_API_BASE", "http://127.0.0.1:8090").strip()
DEFAULT_FT_USER = os.getenv("URANUS_FT_API_USER", "").strip()
DEFAULT_FT_PASS = os.getenv("URANUS_FT_API_PASS", "").strip()


# =============================================================================
# TIME + JSON IO
# =============================================================================
def now_utc_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def now_epoch() -> int:
    return int(time.time())


def iso_from_epoch(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def load_json(path: str, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# =============================================================================
# HTTP (Basic auth)
# =============================================================================
def http_json(url: str, username: Optional[str], password: Optional[str], timeout: int = 6) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if username and password:
        token = b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


# =============================================================================
# Helpers
# =============================================================================
def normalize_pair(p: Any) -> Any:
    # state-ben lehet XRP/USDC vagy XRPUSDC; egységesítünk "XRP/USDC"-re
    if not p:
        return p
    p = str(p).strip()
    if "/" in p:
        return p
    if p.endswith("USDC") and len(p) > 4:
        return p[:-4] + "/USDC"
    if p.endswith("USDT") and len(p) > 4:
        return p[:-4] + "/USDT"
    return p


def normalize_base_url(u: str) -> str:
    # ha valahonnan ".../api/v1" jön, levágjuk, mert mi hozzáadjuk az endpointoknál
    u = (u or "").strip()
    if not u:
        return DEFAULT_FT_BASE_URL or "http://127.0.0.1:8090"
    u = u.rstrip("/")
    if u.endswith("/api/v1"):
        u = u[:-7]
    return u.rstrip("/")


def pick_pair(state: Dict[str, Any], ft_cfg: Dict[str, Any], status_item: Optional[Dict[str, Any]]) -> str:
    # 0) status pair
    if isinstance(status_item, dict) and status_item.get("pair"):
        return normalize_pair(status_item["pair"]) or "XRP/USDC"

    # 1) state.json pair
    if isinstance(state, dict) and state.get("pair"):
        return normalize_pair(state["pair"]) or "XRP/USDC"

    # 2) config pair_whitelist first
    wl = ft_cfg.get("pair_whitelist")
    if isinstance(wl, list) and wl:
        return normalize_pair(wl[0]) or "XRP/USDC"

    # 3) fallback
    return "XRP/USDC"


def ensure_dict(state: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = state.get(key)
    if not isinstance(v, dict):
        v = {}
        state[key] = v
    return v


def ensure_rpc_block(state: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    rpc = ensure_dict(state, "rpc")
    rpc["url"] = base_url
    rpc.setdefault("last_status_path", "/api/v1/status")
    rpc.setdefault("last_trades_path", "/api/v1/trades")
    return rpc


def preserve_legacy_once(state: Dict[str, Any]) -> None:
    """If `legacy` is missing, store a snapshot of known legacy top-level keys once."""
    if isinstance(state.get("legacy"), dict):
        return

    legacy: Dict[str, Any] = {}
    for k in (
        "last_price",
        "current_rate",
        "open_rate",
        "base_price_current",
        "in_position",
        "last_exit_reason",
        "last_exit_trade_id",
        "last_exit_utc",
        "cooldown_until_utc",
    ):
        if k in state:
            legacy[k] = state.get(k)

    state["legacy"] = legacy


def purge_legacy_top_level_keys(state: Dict[str, Any]) -> None:
    for k in (
        "last_price",
        "current_rate",
        "open_rate",
        "base_price_current",
        "in_position",
        "last_exit_reason",
        "last_exit_trade_id",
        "last_exit_utc",
        "cooldown_until_utc",
    ):
        if k in state:
            state.pop(k, None)


# =============================================================================
# STOPLOSS detection (from trades)
# =============================================================================
_STOPLOSS_TOKENS = ("stoploss", "stop_loss", "stop-loss")


def trade_looks_like_stoploss(tr: Dict[str, Any]) -> bool:
    reason = tr.get("exit_reason") or tr.get("sell_reason") or tr.get("close_reason") or ""
    rl = str(reason).lower()

    if any(tok in rl for tok in _STOPLOSS_TOKENS):
        return True

    has_stop_fields = any(k in tr for k in ("stop_loss_abs", "stop_loss_pct", "stoploss_abs", "stoploss_pct"))
    if has_stop_fields and "stop" in rl:
        return True

    return False


def set_stoploss_cooldown(state: Dict[str, Any], trade_id: str) -> None:
    flags = ensure_dict(state, "flags")
    now = now_epoch()
    until = now + STOPLOSS_COOLDOWN_SECONDS

    flags["last_exit_reason"] = "FT_STOPLOSS"
    flags["last_exit_trade_id"] = str(trade_id)
    flags["last_exit_utc"] = now_utc_iso()
    flags["cooldown_until_epoch"] = until
    flags["cooldown_until_utc"] = iso_from_epoch(until)


# =============================================================================
# Extractors
# =============================================================================
def extract_status_item(status: Any) -> Optional[Dict[str, Any]]:
    # Freqtrade status tipikusan lista
    if isinstance(status, list) and status:
        if isinstance(status[0], dict):
            return status[0]
    elif isinstance(status, dict):
        return status
    return None


def status_to_position_fields(status_item: Optional[Dict[str, Any]]) -> Tuple[bool, Dict[str, Any]]:
    """Return (in_position, position_fields)."""
    if not isinstance(status_item, dict):
        return False, {}

    is_open_val = status_item.get("is_open")
    in_pos = bool(is_open_val is True)

    pos: Dict[str, Any] = {}
    if in_pos:
        pos["in_position"] = True
        pos["entry_price"] = status_item.get("open_rate")
        pos["entry_timestamp_utc"] = status_item.get("open_date") or status_item.get("open_date_utc")
        pos["exit_price"] = None
        pos["exit_timestamp_utc"] = None
        pos["side"] = status_item.get("trade_direction") or status_item.get("side") or status_item.get("direction")
    else:
        pos["in_position"] = False
        pos.setdefault("entry_price", None)
        pos.setdefault("entry_timestamp_utc", None)
        pos.setdefault("exit_price", None)
        pos.setdefault("exit_timestamp_utc", None)
        pos.setdefault("side", None)

    return in_pos, pos


# =============================================================================
# MAIN
# =============================================================================
def main() -> int:
    state = load_json(STATE_JSON, default={})
    if not isinstance(state, dict):
        state = {}

    ft_cfg = load_json(FT_CONFIG, default={})
    if not isinstance(ft_cfg, dict):
        print("ERROR: freqtrade config.json is not a dict", file=os.sys.stderr)
        return 2

    # --- preserve legacy snapshot once, then purge legacy keys forever ---
    preserve_legacy_once(state)

    # --- api_server settings from config.json ---
    api_cfg = ft_cfg.get("api_server") if isinstance(ft_cfg.get("api_server"), dict) else {}
    listen_ip = (api_cfg.get("listen_ip_address") or "127.0.0.1").strip()
    listen_port = api_cfg.get("listen_port") or 8090

    cfg_user = str(api_cfg.get("username") or "").strip()
    cfg_pass = str(api_cfg.get("password") or "").strip()

    # state rpc url (ha van)
    state_rpc = state.get("rpc") if isinstance(state.get("rpc"), dict) else {}
    state_rpc_url = state_rpc.get("url") if isinstance(state_rpc, dict) else ""

    base_url = normalize_base_url(state_rpc_url or DEFAULT_FT_BASE_URL or f"http://{listen_ip}:{listen_port}")

    # auth precedence: ENV > config.json
    user = (DEFAULT_FT_USER or cfg_user).strip() or None
    pwd = (DEFAULT_FT_PASS or cfg_pass).strip() or None

    rpc = ensure_rpc_block(state, base_url)

    # --- stable identity fields ---
    state["updated_utc"] = now_utc_iso()
    state["exchange"] = state.get("exchange") or (ft_cfg.get("exchange", {}) or {}).get("name") or "binance"
    state["pair"] = pick_pair(state, ft_cfg, None)  # temporary; will be overwritten after status if available

    # --- ensure canonical containers exist (do not clobber contents) ---
    ensure_dict(state, "meta")
    ensure_dict(state, "prices")
    ensure_dict(state, "price")
    ensure_dict(state, "position")
    ensure_dict(state, "base")
    ensure_dict(state, "flags")
    ensure_dict(state, "extremes")
    ensure_dict(state, "rules")

    # --- status ---
    status_item: Optional[Dict[str, Any]] = None
    status_snapshot = None  # for meta.ft mirror
    try:
        status = http_json(f"{base_url}{rpc.get('last_status_path','/api/v1/status')}", user, pwd, timeout=6)
        status_item = extract_status_item(status)

        state["rpc_last_status_utc"] = now_utc_iso()
        state["rpc_last_status_ok"] = True
        state["rpc_last_status_code"] = 200
        state["rpc_last_status_error"] = None
    except Exception as e:
        state["rpc_last_status_utc"] = now_utc_iso()
        state["rpc_last_status_ok"] = False
        state["rpc_last_status_code"] = 0
        state["rpc_last_status_error"] = f"{e}"
        status_item = None

    # pair (status has priority)
    pair = pick_pair(state, ft_cfg, status_item)
    state["pair"] = pair

    # position + prices from status
    in_pos, pos_fields = status_to_position_fields(status_item)

    pos = ensure_dict(state, "position")
    pos.update(pos_fields)

    # canonical base.price policy:
    # - if in position: base.price = entry_price (open_rate)
    # - else: do not overwrite existing base.price (that is managed elsewhere in your system)
    base = ensure_dict(state, "base")
    if in_pos and pos.get("entry_price") is not None:
        try:
            base["price"] = float(pos["entry_price"])
        except Exception:
            base["price"] = None

        base["timestamp_utc"] = base.get("timestamp_utc") or now_utc_iso()
        base["type"] = base.get("type") or "entry"
    else:
        base.setdefault("price", base.get("price"))
        base.setdefault("timestamp_utc", base.get("timestamp_utc"))
        base.setdefault("type", base.get("type") or "startup")

    # canonical "price" (latest)
    price_block = ensure_dict(state, "price")
    current_rate = None
    if isinstance(status_item, dict):
        current_rate = status_item.get("current_rate") or status_item.get("close_rate")

    if current_rate is not None:
        try:
            price_block["current"] = float(current_rate)
        except Exception:
            price_block["current"] = None
        price_block["timestamp_utc"] = now_utc_iso()
    else:
        price_block.setdefault("current", price_block.get("current"))
        price_block.setdefault("timestamp_utc", price_block.get("timestamp_utc"))

    # NOTE: we do NOT touch state["prices"].last/prev_last here (tick_runner owns that).

    # --- STOPLOSS event detection from last trade ---
    try:
        q = urllib.parse.urlencode({"limit": 1})
        trades = http_json(f"{base_url}{rpc.get('last_trades_path','/api/v1/trades')}?{q}", user, pwd, timeout=6)

        last_trade = None
        if isinstance(trades, dict) and isinstance(trades.get("trades"), list) and trades["trades"]:
            if isinstance(trades["trades"][0], dict):
                last_trade = trades["trades"][0]
        elif isinstance(trades, list) and trades:
            if isinstance(trades[0], dict):
                last_trade = trades[0]

        if isinstance(last_trade, dict):
            trade_id = last_trade.get("trade_id") or last_trade.get("id") or ""
            trade_id = str(trade_id)

            is_open = bool(last_trade.get("is_open", False))
            if (not is_open) and trade_looks_like_stoploss(last_trade):
                flags = ensure_dict(state, "flags")
                prev_id = str(flags.get("last_exit_trade_id") or "")
                if trade_id and trade_id != prev_id:
                    set_stoploss_cooldown(state, trade_id)

        state["rpc_last_trades_utc"] = now_utc_iso()
        state["rpc_last_trades_ok"] = True
        state["rpc_last_trades_error"] = None

    except Exception as e:
        state["rpc_last_trades_utc"] = now_utc_iso()
        state["rpc_last_trades_ok"] = False
        state["rpc_last_trades_error"] = f"{e}"

    # --- meta.ft compatibility mirror (legacy diagnostics) ---
    # Régi diagnosztikák ezt nézik: meta.ft.last_poll_utc / heartbeat_utc / synced_utc.
    try:
        ping_body = http_json(f"{base_url}/api/v1/ping", user, pwd, timeout=6)
        count_body = http_json(f"{base_url}/api/v1/count", user, pwd, timeout=6)

        meta = ensure_dict(state, "meta")
        ft = meta.get("ft")
        if not isinstance(ft, dict):
            ft = {}

        now = now_utc_iso()
        ft["base_url"] = base_url
        ft["ping"] = {"status_code": 200, "body": ping_body}
        ft["status"] = {"status_code": 200 if state.get("rpc_last_status_ok") else 0, "body": status_snapshot}
        ft["count"] = {"status_code": 200, "body": count_body}
        ft["error"] = None
        ft["ok"] = bool(state.get("rpc_last_status_ok") and state.get("rpc_last_trades_ok"))
        ft["last_poll_utc"] = now
        ft["heartbeat_utc"] = now
        ft["synced_utc"] = now

        meta["ft"] = ft
        state["meta"] = meta
    except Exception as e:
        meta = ensure_dict(state, "meta")
        ft = meta.get("ft")
        if not isinstance(ft, dict):
            ft = {}

        now = now_utc_iso()
        ft["base_url"] = base_url
        ft["error"] = f"{e}"
        ft["ok"] = False
        ft["last_poll_utc"] = now
        ft["heartbeat_utc"] = now

        meta["ft"] = ft
        state["meta"] = meta

    # FORCE_STATUS_FETCH_FOR_HARD_NULL
    # If /status is empty -> force no-position (authoritative).
    try:
        sb = None
        if "status_body" in locals():
            sb = locals().get("status_body")
        if sb is None:
            # fetch /status now (fallback)
            status_body_txt = curl("/status")
            try:
                sb = json.loads(status_body_txt)
            except Exception:
                sb = None

        if isinstance(sb, list) and len(sb) == 0:
            # HARD NULL: no open trades => no position
            pos = ensure_dict(state, "position")
            pos["in_position"] = False
            pos["side"] = None
            pos["entry_price"] = None
            pos["entry_timestamp_utc"] = None
            pos["exit_price"] = None
            pos["exit_timestamp_utc"] = None
            state["position"] = pos
            state["in_position"] = False
            state["reason"] = "FT_STATUS_EMPTY"
            print("DEBUG:: HARD_NULL applied (FT /status empty)")
    except Exception as e:
        print(f"DEBUG:: HARD_NULL check failed: {e}")


    # --- final: purge legacy top-level keys (and do NOT re-add them) ---
    purge_legacy_top_level_keys(state)

    # sync top-level in_position from canonical position block (MUST be after purge)
    state["in_position"] = bool((state.get("position") or {}).get("in_position"))

    # HARD_NULL_ON_EMPTY_STATUS:
    # If Freqtrade /status is an empty list, we MUST be out of position.
    try:
        status_body = None
        # Prefer already-fetched body if present in locals
        if "status_body" in locals():
            status_body = locals().get("status_body")
        elif "ft" in locals() and isinstance(locals().get("ft"), dict):
            sb = locals()["ft"].get("status", {}).get("body")
            status_body = sb

        if isinstance(status_body, list) and len(status_body) == 0:
            pos = state.get("position")
            if not isinstance(pos, dict):
                pos = {}
            pos["in_position"] = False
            pos["side"] = None
            pos["entry_price"] = None
            pos["entry_timestamp_utc"] = None
            pos["exit_price"] = None
            pos["exit_timestamp_utc"] = None
            state["position"] = pos
            state["in_position"] = False
    except Exception:
        pass

    # DEBUG_STATUS_BODY
    try:
        sb = None
        if "status_body" in locals():
            sb = locals().get("status_body")
        elif "ft" in locals() and isinstance(locals().get("ft"), dict):
            sb = locals()["ft"].get("status", {}).get("body")
        pos_dbg = (state.get("position") or {})
        print(f"DEBUG:: status_body type={type(sb).__name__} len={(len(sb) if isinstance(sb,list) else 'NA')} value_head={(sb[:1] if isinstance(sb,list) else sb)}")
        print(f"DEBUG:: pre_write state.in_position={state.get('in_position')} pos.in_position={pos_dbg.get('in_position')} pos.keys={sorted(list(pos_dbg.keys()))}")
    except Exception as e:
        print(f"DEBUG:: status debug failed: {e}")




    atomic_write_json(STATE_JSON, state)
    print(f"OK:: state.json updated (canonical) pair={pair}, in_position={pos.get('in_position')}, base_url={base_url}")
    return 0




if __name__ == "__main__":
    raise SystemExit(main())
