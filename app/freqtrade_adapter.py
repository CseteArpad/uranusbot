import os
import json
import base64
import logging
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Tuple

# =========================
# Logging
# =========================
LOG_LEVEL = os.getenv("URANUS_ADAPTER_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
log = logging.getLogger("freqtrade_adapter")

# =========================
# Environment / Defaults
# =========================
# Prefer explicit adapter vars, fallback to generic ones.
FT_URL = os.getenv("FREQTRADE_RPC_URL") or os.getenv("FREQTRADE_URL") or "http://127.0.0.1:8090"
FT_USER = os.getenv("FREQTRADE_RPC_USERNAME") or os.getenv("FREQTRADE_USER") or ""
FT_PASS = os.getenv("FREQTRADE_RPC_PASSWORD") or os.getenv("FREQTRADE_PASS") or ""

# If your base URL does not already include /api/v1, we will add it.
API_PREFIX = "/api/v1"


# =========================
# Helpers
# =========================
def _normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        return "http://127.0.0.1:8090"
    return url


def _join_api(url: str, path: str) -> str:
    base = _normalize_base_url(url)

    # If user already passed .../api/v1 in FT_URL, don't add again.
    if base.endswith(API_PREFIX):
        return f"{base}{path if path.startswith('/') else '/' + path}"

    # Add /api/v1
    return f"{base}{API_PREFIX}{path if path.startswith('/') else '/' + path}"


def _auth_header(user: str, password: str) -> Optional[str]:
    user = (user or "").strip()
    password = password or ""
    if not user:
        return None
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _http_json(method: str, path: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Tuple[bool, Dict[str, Any]]:
    """
    Returns: (ok, data)
    ok=False never raises - safe for long-running service.
    """
    url = _join_api(FT_URL, path)
    headers = {"Content-Type": "application/json"}

    auth = _auth_header(FT_USER, FT_PASS)
    if auth:
        headers["Authorization"] = auth

    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method.upper())

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace").strip()
            if not raw:
                return True, {"ok": True, "status": resp.status, "url": url}
            try:
                return True, json.loads(raw)
            except json.JSONDecodeError:
                return True, {"ok": True, "status": resp.status, "url": url, "raw": raw}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, {"ok": False, "error": "HTTPError", "code": getattr(e, "code", None), "reason": str(e), "body": body, "url": url}
    except Exception as e:
        return False, {"ok": False, "error": "Exception", "reason": str(e), "url": url}


# =========================
# Position helpers
# =========================
def _status_open_trade_count(status: Dict[str, Any]) -> Optional[int]:
    # Common keys seen in Freqtrade status responses
    for k in ("open_trade_count", "open_trades", "open_trades_count"):
        if k in status and isinstance(status[k], int):
            return status[k]
    return None


def _in_position_impl(timeout: float = 4.0) -> bool:
    # 1) Try /status
    ok, data = _http_json("GET", "/status", None, timeout=timeout)
    if ok and isinstance(data, dict):
        c = _status_open_trade_count(data)
        if c is not None:
            return c > 0

    # 2) Try /open_trades
    ok, data = _http_json("GET", "/open_trades", None, timeout=timeout)
    if ok:
        # can be list or dict depending on version
        if isinstance(data, list):
            return len(data) > 0
        if isinstance(data, dict):
            # some versions wrap list in "trades"
            trades = data.get("trades")
            if isinstance(trades, list):
                return len(trades) > 0

    # Safe default
    return False


# Exported names the Executor may look for (all call the same implementation)
def ft_in_position(*args, **kwargs) -> bool:
    return _in_position_impl()

def in_position(*args, **kwargs) -> bool:
    return _in_position_impl()

def has_position(*args, **kwargs) -> bool:
    return _in_position_impl()

def is_in_position(*args, **kwargs) -> bool:
    return _in_position_impl()

def position_open(*args, **kwargs) -> bool:
    return _in_position_impl()

def get_in_position(*args, **kwargs) -> bool:
    return _in_position_impl()


# =========================
# Trading helpers
# =========================
def _forcebuy(pair: Optional[str] = None, amount: Optional[float] = None, price: Optional[float] = None, timeout: float = 8.0, **kwargs) -> Dict[str, Any]:
    """
    Best-effort wrapper for Freqtrade forcebuy.
    Payload keys vary by version; we send the common ones.
    """
    payload: Dict[str, Any] = {}
    if pair:
        payload["pair"] = pair
    if amount is not None:
        payload["amount"] = amount
    if price is not None:
        payload["price"] = price

    # Some setups use stake_amount instead of amount
    if "stake_amount" in kwargs and kwargs["stake_amount"] is not None:
        payload["stake_amount"] = kwargs["stake_amount"]

    ok, data = _http_json("POST", "/forcebuy", payload, timeout=timeout)
    if not ok:
        log.warning("forcebuy failed: %s", data)
    return data


def _forcesell(pair: Optional[str] = None, trade_id: Optional[Any] = None, timeout: float = 8.0, **kwargs) -> Dict[str, Any]:
    """
    Best-effort wrapper for Freqtrade forcesell.
    Some versions prefer tradeid/trade_id; we support both.
    """
    payload: Dict[str, Any] = {}

    if trade_id is None:
        trade_id = kwargs.get("trade_id") or kwargs.get("tradeid")

    if trade_id is not None:
        payload["tradeid"] = trade_id  # common in RPC
        payload["trade_id"] = trade_id
    if pair:
        payload["pair"] = pair

    ok, data = _http_json("POST", "/forcesell", payload, timeout=timeout)
    if not ok:
        log.warning("forcesell failed: %s", data)
    return data


# =========================
# Exported BUY callables (Executor candidate list)
# =========================
def ft_buy(*args, **kwargs) -> Dict[str, Any]:
    pair = kwargs.get("pair") or (args[0] if len(args) > 0 else None)
    amount = kwargs.get("amount")
    price = kwargs.get("price")
    return _forcebuy(pair=pair, amount=amount, price=price, **kwargs)

def buy(*args, **kwargs) -> Dict[str, Any]:
    return ft_buy(*args, **kwargs)

def market_buy(*args, **kwargs) -> Dict[str, Any]:
    return ft_buy(*args, **kwargs)

def open_buy(*args, **kwargs) -> Dict[str, Any]:
    return ft_buy(*args, **kwargs)

def open_trade(*args, **kwargs) -> Dict[str, Any]:
    return ft_buy(*args, **kwargs)

def enter_trade(*args, **kwargs) -> Dict[str, Any]:
    return ft_buy(*args, **kwargs)

def create_buy_order(*args, **kwargs) -> Dict[str, Any]:
    return ft_buy(*args, **kwargs)


# =========================
# Exported SELL callables (Executor candidate list)
# =========================
def ft_sell(*args, **kwargs) -> Dict[str, Any]:
    pair = kwargs.get("pair") or (args[0] if len(args) > 0 else None)
    trade_id = kwargs.get("trade_id") or kwargs.get("tradeid")
    return _forcesell(pair=pair, trade_id=trade_id, **kwargs)

def sell(*args, **kwargs) -> Dict[str, Any]:
    return ft_sell(*args, **kwargs)

def market_sell(*args, **kwargs) -> Dict[str, Any]:
    return ft_sell(*args, **kwargs)

def close_trade(*args, **kwargs) -> Dict[str, Any]:
    return ft_sell(*args, **kwargs)

def exit_trade(*args, **kwargs) -> Dict[str, Any]:
    return ft_sell(*args, **kwargs)

def create_sell_order(*args, **kwargs) -> Dict[str, Any]:
    return ft_sell(*args, **kwargs)
