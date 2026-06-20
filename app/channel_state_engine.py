# app/channel_state_engine.py
# Last-valid CP state carrier for the channel replay engine.
# Prevents UNKNOWN trend state when a timeframe momentarily loses its channel.
# No live trading, no orders, no state.json access.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

# Timeframes tracked by this engine
_SUPPORTED_TFS = frozenset({"1H", "4H", "12H", "1D"})


@dataclass
class ChannelState:
    """Holds the most recent valid CP and staleness counter per timeframe."""

    # Last known valid channel positions (None until first valid tick arrives)
    last_valid_cp_1h:  Optional[float] = None
    last_valid_cp_4h:  Optional[float] = None
    last_valid_cp_12h: Optional[float] = None
    last_valid_cp_1d:  Optional[float] = None

    # UTC timestamp strings of the last valid CP tick per TF
    last_valid_ts_1h:  Optional[str] = None
    last_valid_ts_4h:  Optional[str] = None
    last_valid_ts_12h: Optional[str] = None
    last_valid_ts_1d:  Optional[str] = None

    # Consecutive ticks where raw CP was None (resets to 0 on a fresh valid CP)
    stale_ticks_1h:  int = 0
    stale_ticks_4h:  int = 0
    stale_ticks_12h: int = 0
    stale_ticks_1d:  int = 0


def update_channel_state(
    state: ChannelState,
    ts_utc: str,
    cp_by_tf: Dict[str, Optional[float]],
) -> None:
    """
    Update *state* in place for the current 1m tick.

    For each TF in cp_by_tf:
    - If cp is not None  → store as last_valid_cp, record ts, reset stale_ticks to 0.
    - If cp is None      → do NOT overwrite last_valid_cp; increment stale_ticks.

    Unknown TF keys (not in {"1H", "4H", "12H", "1D"}) are silently ignored.
    """
    for tf, cp in cp_by_tf.items():
        if tf not in _SUPPORTED_TFS:
            continue
        key = tf.lower()   # "1H" -> "1h", "12H" -> "12h", "1D" -> "1d"
        if cp is not None:
            setattr(state, f"last_valid_cp_{key}",  cp)
            setattr(state, f"last_valid_ts_{key}",   ts_utc)
            setattr(state, f"stale_ticks_{key}",     0)
        else:
            old = getattr(state, f"stale_ticks_{key}", 0)
            setattr(state, f"stale_ticks_{key}", old + 1)


def effective_cp_by_tf(state: ChannelState) -> Dict[str, Optional[float]]:
    """
    Return the best available CP per timeframe.

    Always returns the *last valid* CP even when the current tick's raw CP
    was None.  Returns None only if no valid CP has ever been seen for that TF.
    """
    return {
        "1H":  state.last_valid_cp_1h,
        "4H":  state.last_valid_cp_4h,
        "12H": state.last_valid_cp_12h,
        "1D":  state.last_valid_cp_1d,
    }


def staleness_by_tf(state: ChannelState) -> Dict[str, int]:
    """Return the current consecutive-None tick counter per timeframe."""
    return {
        "1H":  state.stale_ticks_1h,
        "4H":  state.stale_ticks_4h,
        "12H": state.stale_ticks_12h,
        "1D":  state.stale_ticks_1d,
    }
