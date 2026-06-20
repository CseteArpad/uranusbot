# executor.py
# Uranus – Executor
# Feladata: RuleDecision -> Freqtrade végrehajtás
# NINCS döntési logika itt!

from typing import Optional, Callable, Any
from datetime import datetime, timezone
import importlib

from rule_engine import RuleDecision


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_callable(mod: Any, candidates: list[str]) -> Callable:
    """
    Visszaadja az első létező callable attribútumot a modulból.
    """
    for name in candidates:
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    raise ImportError(
        f"No suitable callable found in module {getattr(mod, '__name__', mod)} "
        f"candidates={candidates}"
    )


def _call_flex(fn: Callable, pair: str) -> Any:
    """
    Meghívás rugalmasan: először pair=..., ha nem megy, akkor pozicionálisan.
    """
    try:
        return fn(pair=pair)
    except TypeError:
        return fn(pair)


def _load_adapter():
    """
    Preferált: freqtrade_adapter
    Fallback: ft_api
    """
    try:
        m = importlib.import_module("freqtrade_adapter")
        src = "freqtrade_adapter"
        return m, src
    except Exception:
        m = importlib.import_module("ft_api")
        src = "ft_api"
        return m, src


# --- Adapter betöltés + függvények feloldása (névfüggetlen) ---
_ADAPTER_MOD, ADAPTER_SOURCE = _load_adapter()

_FT_BUY = _resolve_callable(
    _ADAPTER_MOD,
    [
        "ft_buy",
        "buy",
        "market_buy",
        "open_buy",
        "open_trade",
        "enter_trade",
        "create_buy_order",
    ],
)

_FT_SELL = _resolve_callable(
    _ADAPTER_MOD,
    [
        "ft_sell",
        "sell",
        "market_sell",
        "close_sell",
        "close_trade",
        "exit_trade",
        "create_sell_order",
    ],
)

_FT_IN_POSITION = _resolve_callable(
    _ADAPTER_MOD,
    [
        "ft_in_position",
        "in_position",
        "has_position",
        "is_in_position",
        "position_open",
        "get_in_position",
    ],
)


class UranusExecutor:
    """
    Vékonyréteg:
    - fogad egy RuleDecision-t
    - meghívja a megfelelő Freqtrade akciót
    """

    @staticmethod
    def execute(
        *,
        decision: RuleDecision,
        pair: str,
        price: float,
        state: dict,
        log: Callable[[dict], None],
    ) -> Optional[str]:

        if not decision.is_action():
            return None

        action = decision.action
        reason = decision.reason

        # Build a snapshot of the current state for this pair.  The global
        # state structure stores per-pair data under state["pairs"][pair].
        # We enrich the log entry with base, peak, trough and flags so that
        # each decision is recorded with its relevant trading context.
        pair_state = (state.get("pairs") or {}).get(pair, {})

        log({
            "ts": now_iso(),
            "pair": pair,
            "action": action,
            "price": price,
            "reason": reason,
            "adapter": ADAPTER_SOURCE,
            # Persist the current trading context for debugging/analysis
            "base": pair_state.get("base"),
            "peak": pair_state.get("peak"),
            "trough": pair_state.get("trough"),
            "flags": pair_state.get("flags"),
            "in_position": pair_state.get("in_position"),
        })

        if action == "BUY":
            _call_flex(_FT_BUY, pair)
            state["last_action"] = {
                "action": "BUY",
                "price": price,
                "reason": reason,
                "ts": now_iso(),
            }
            return "BUY"

        if action == "SELL":
            _call_flex(_FT_SELL, pair)
            state["last_action"] = {
                "action": "SELL",
                "price": price,
                "reason": reason,
                "ts": now_iso(),
            }
            return "SELL"

        return None

    @staticmethod
    def sync_in_position(pair: str) -> bool:
        """
        Forrásigazság: Freqtrade
        """
        try:
            val = _call_flex(_FT_IN_POSITION, pair)
            return bool(val)
        except Exception:
            return False
