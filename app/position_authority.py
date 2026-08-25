"""
Uranus U-3 – Position Authority (döntési réteg).

Fő invariáns
------------
**FREQTRADE COMMUNICATION FAILURE MUST NEVER MEAN FLAT.**

A korábbi ``fetch_open_trade() -> dict | None`` szerződés típushibás volt: két,
gyökeresen eltérő valóságot ugyanazzal az értékkel jelzett –

* sikeres lekérdezés, nulla nyitott trade (jogos FLAT), és
* a lekérdezés meghiúsulása (ismeretlen),

ezért egy hálózati hiba a ``bool(None) -> False`` úton „nincs pozíció”-vá vált,
és a hamis FLAT perzisztálódott (sőt: a flat-ág a ciklus-horgonyt is az aktuális
árra írta, megsemmisítve a nyitott pozíció belépési referenciáját).

Rétegzés
--------
A hálózat, a credential és a válasz-besorolás a ``position_source`` modulban
él. Ez a modul kizárólag azzal foglalkozik, hogy a verdiktből MI KÖVETKEZIK:
reconciliation, állapotírás, végrehajtási kapu. Így a biztonságkritikus döntési
logika mellékhatás- és titokmentes marad, önmagában auditálható.

Bevezetés
---------
``URANUS_POSITION_AUTHORITY_MODE = off | shadow | live`` (alapértelmezés:
``shadow``).

* ``off``    – a modul nem fut; a viselkedés bit-azonos a bevezetés előttivel.
* ``shadow`` – számol, naplóz, kitölti a ``state["position"]`` blokkot, de
  **nem ír legacy mezőt és nem blokkol végrehajtást**.
* ``live``   – a modul az **egyetlen** pozíció-író, és a végrehajtási kapu
  az ő verdiktjére támaszkodik.

A publikus belépési pont (``authority_tick``) SOHA nem dob kivételt.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

import position_source
from position_source import (  # noqa: F401  (kompatibilitási re-export)
    ERR_AUTH,
    ERR_BAD_RESPONSE,
    ERR_DATA_INCONSISTENT,
    ERR_MULTIPLE_TRADES,
    ERR_NETWORK,
    ERR_PAIR_MISMATCH,
    ERR_TIMEOUT,
    ERR_UNKNOWN,
    STATE_FLAT,
    STATE_OPEN,
    STATE_UNKNOWN,
    VERIFIED_STATES,
    PositionFetchResult,
    as_float as _as_float,
    as_int as _as_int,
    fetch_open_position,
)
# --------------------------------------------------------------------------- #
# Események
# --------------------------------------------------------------------------- #

EV_TRANSITION = "POSITION_AUTHORITY_TRANSITION"
EV_RECONCILED = "POSITION_RECONCILED"

REASON_ADOPTED = "POSITION_ADOPTED"
REASON_CLOSED_EXTERNALLY = "POSITION_CLOSED_EXTERNALLY"
REASON_REPLACED = "POSITION_REPLACED"
REASON_AUTHORITY_LOST = "AUTHORITY_LOST"
REASON_AUTHORITY_RECOVERED = "AUTHORITY_RECOVERED"
REASON_STEADY = "STEADY"

#: Hány egymást követő UNKNOWN után jelöljük degradáltnak. A viselkedést NEM
#: változtatja (az UNKNOWN eleve fail-closed), csak a riasztást élesíti.
DEGRADED_AFTER_CONSECUTIVE_UNKNOWN = 3


def get_mode() -> str:
    mode = str(os.getenv("URANUS_POSITION_AUTHORITY_MODE", "shadow")).strip().lower()
    return mode if mode in ("off", "shadow", "live") else "shadow"


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} [position_authority] {msg}", flush=True)


@dataclass(frozen=True)
class PositionSnapshot:
    """A kanonikus pozícióállapot egy tickre."""
    authority_state: str
    observed_at: float
    source: str = "freqtrade_rest"
    pair: Optional[str] = None
    trade_id: Optional[int] = None
    quantity: Optional[float] = None
    entry_price: Optional[float] = None
    entry_price_source: Optional[str] = None
    opened_at: Optional[int] = None
    max_rate: Optional[float] = None
    stake_amount: Optional[float] = None
    last_error: Optional[str] = None

    @property
    def is_verified(self) -> bool:
        return self.authority_state in VERIFIED_STATES

    def trade_dict(self) -> Optional[dict]:
        if self.authority_state != STATE_OPEN:
            return None
        return {
            "trade_id": self.trade_id,
            "pair": self.pair,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "entry_price_source": self.entry_price_source,
            "opened_at": self.opened_at,
        }


@dataclass(frozen=True)
class ReconcileOutcome:
    """A friss verdikt és az utolsó bizonyított állapot összevetésének eredménye."""
    snapshot: PositionSnapshot
    previous_state: Optional[str]
    reason: str
    changed: bool
    reconciled: bool = False
    events: Tuple[dict, ...] = field(default=())


def snapshot_from_result(result: PositionFetchResult, pair: str) -> PositionSnapshot:
    """A fetch-eredmény kanonikus snapshottá alakítása."""
    if result.status != STATE_OPEN:
        return PositionSnapshot(
            authority_state=result.status,
            observed_at=result.observed_at,
            pair=pair if result.status == STATE_FLAT else None,
            last_error=result.error,
        )

    trade = result.trades[0]
    entry = _as_float(trade.get("open_rate"))
    entry_source = "open_rate"
    if entry is None:
        entry = _as_float(trade.get("open_rate_requested"))
        entry_source = "open_rate_requested"

    opened_ms = _as_int(trade.get("open_timestamp"))
    return PositionSnapshot(
        authority_state=STATE_OPEN,
        observed_at=result.observed_at,
        pair=str(trade.get("pair") or pair),
        trade_id=_as_int(trade.get("trade_id")),
        quantity=_as_float(trade.get("amount")),
        entry_price=entry,
        entry_price_source=entry_source,
        opened_at=(opened_ms // 1000) if opened_ms else None,
        max_rate=_as_float(trade.get("max_rate")),
        stake_amount=_as_float(trade.get("stake_amount")),
    )


# --------------------------------------------------------------------------- #
# Reconciliation
# --------------------------------------------------------------------------- #

def _position_block(state: dict) -> dict:
    block = state.get("position")
    if not isinstance(block, dict):
        block = {}
        state["position"] = block
    return block


def last_verified(state: dict) -> Tuple[Optional[str], Optional[dict], Optional[int]]:
    """(állapot, trade, verified_at) az utolsó BIZONYÍTOTT állapotból."""
    lv = _position_block(state).get("last_verified")
    if not isinstance(lv, dict):
        return None, None, None
    st = lv.get("state")
    if st not in VERIFIED_STATES:
        return None, None, None
    trade = lv.get("trade") if isinstance(lv.get("trade"), dict) else None
    return st, trade, _as_int(lv.get("verified_at"))


def reconcile(state: dict, snapshot: PositionSnapshot) -> ReconcileOutcome:
    """
    A friss verdikt összevetése az utolsó bizonyított állapottal.

    A ``last_verified`` KIZÁRÓLAG összehasonlítási alap – soha nem válik
    current authorityvá.
    """
    prev_state, prev_trade, _ = last_verified(state)
    prev_trade_id = _as_int((prev_trade or {}).get("trade_id"))
    new_state = snapshot.authority_state
    events: list[dict] = []

    if new_state == STATE_UNKNOWN:
        reason = REASON_AUTHORITY_LOST if prev_state in VERIFIED_STATES else "AUTHORITY_UNAVAILABLE"
        return ReconcileOutcome(
            snapshot=snapshot, previous_state=prev_state, reason=reason,
            changed=(prev_state != new_state), reconciled=False,
        )

    # Innentől a verdikt bizonyított.
    reason = REASON_STEADY
    reconciled = False

    if prev_state is None:
        reason = REASON_AUTHORITY_RECOVERED if new_state else REASON_STEADY
    elif prev_state == STATE_UNKNOWN:
        reason = REASON_AUTHORITY_RECOVERED
    elif prev_state == STATE_FLAT and new_state == STATE_OPEN:
        reason = REASON_ADOPTED
        reconciled = True
    elif prev_state == STATE_OPEN and new_state == STATE_FLAT:
        reason = REASON_CLOSED_EXTERNALLY
        reconciled = True
    elif prev_state == STATE_OPEN and new_state == STATE_OPEN:
        if prev_trade_id is not None and snapshot.trade_id != prev_trade_id:
            # U-3 senior korrekció: NINCS automatikus adopt. Egy váratlanul
            # kicserélődött trade nem bizonyított folytonosság -> UNKNOWN.
            replaced = PositionSnapshot(
                authority_state=STATE_UNKNOWN,
                observed_at=snapshot.observed_at,
                last_error=ERR_DATA_INCONSISTENT,
            )
            events.append({
                "event": EV_RECONCILED, "reason": REASON_REPLACED,
                "previous_trade_id": prev_trade_id, "observed_trade_id": snapshot.trade_id,
            })
            return ReconcileOutcome(
                snapshot=replaced, previous_state=prev_state, reason=REASON_REPLACED,
                changed=True, reconciled=True, events=tuple(events),
            )

    if reconciled:
        events.append({
            "event": EV_RECONCILED, "reason": reason,
            "previous_state": prev_state, "new_state": new_state,
            "trade_id": snapshot.trade_id,
        })

    return ReconcileOutcome(
        snapshot=snapshot, previous_state=prev_state, reason=reason,
        changed=(prev_state != new_state), reconciled=reconciled, events=tuple(events),
    )


# --------------------------------------------------------------------------- #
# Állapot-írás – EZ AZ EGYETLEN LIVE POZÍCIÓ-ÍRÓ
# --------------------------------------------------------------------------- #

def apply_to_state(state: dict, outcome: ReconcileOutcome, *, write_legacy: bool) -> dict:
    """
    A verdikt beírása a state-be.

    ``UNKNOWN`` esetén a pozíciófüggő mezők ÉRINTETLENEK maradnak: sem a legacy
    ``in_position``, sem a horgonyok (``base``, ``_flat_anchor``, ``peak``,
    ``trough``) nem íródnak. Csak a ``position`` diagnosztikai blokk frissül.
    """
    snap = outcome.snapshot
    block = _position_block(state)
    prev_block_state = block.get("authority_state")

    now_i = int(snap.observed_at or time.time())
    block["authority_state"] = snap.authority_state
    block["source"] = snap.source
    block["observed_at"] = now_i
    block["last_error"] = snap.last_error
    block["reason"] = outcome.reason

    if snap.authority_state == STATE_UNKNOWN:
        prev_unknown = _as_int(block.get("consecutive_unknown")) or 0
        block["consecutive_unknown"] = prev_unknown + 1
        if not block.get("unknown_since"):
            block["unknown_since"] = now_i
        block["degraded"] = block["consecutive_unknown"] >= DEGRADED_AFTER_CONSECUTIVE_UNKNOWN
        # trade/last_verified/last_success_at NEM változik.
    else:
        block["consecutive_unknown"] = 0
        block["unknown_since"] = None
        block["degraded"] = False
        block["last_success_at"] = now_i
        block["trade"] = snap.trade_dict()
        block["last_verified"] = {
            "state": snap.authority_state,
            "verified_at": now_i,
            "trade": snap.trade_dict(),
        }

    block["mode"] = get_mode()

    if write_legacy and snap.is_verified:
        _write_legacy_aliases(state, snap)

    if prev_block_state != snap.authority_state:
        _emit_transition(prev_block_state, snap, outcome)
    for ev in outcome.events:
        _log(f"{ev.get('event')} " + " ".join(
            f"{k}={v}" for k, v in ev.items() if k != "event"
        ))

    return block


def _emit_transition(old: Optional[str], snap: PositionSnapshot, outcome: ReconcileOutcome) -> None:
    _log(
        f"{EV_TRANSITION} ts={int(snap.observed_at)} "
        f"old_state={old or 'NONE'} new_state={snap.authority_state} "
        f"source={snap.source} reason={outcome.reason} "
        f"trade_id={snap.trade_id if snap.trade_id is not None else '-'} "
        f"error={snap.last_error or '-'}"
    )


def _write_legacy_aliases(state: dict, snap: PositionSnapshot) -> None:
    """
    Backward-compatible legacy mezők – KIZÁRÓLAG bizonyított állapotból.

    Szándékosan ugyanazt a mezőkészletet írja, mint a régi
    ``sync_position_from_freqtrade``, hogy a ``rule_engine`` és a UI változatlan
    maradhasson. Ami NEM tartozik ide: a ciklus/recovery kontextus – az
    stratégiai állapot, nem pozíció.
    """
    is_open = snap.authority_state == STATE_OPEN
    prev_in_position = bool(state.get("in_position"))
    prev_trade_id = state.get("active_trade_id")

    state["in_position"] = is_open
    state["ui_in_position"] = is_open

    locks = state.get("locks")
    if not isinstance(locks, dict):
        locks = {}
        state["locks"] = locks
    flags = state.get("flags")
    if not isinstance(flags, dict):
        flags = {}
        state["flags"] = flags

    if is_open:
        state["active_trade_id"] = snap.trade_id
        state["active_trade_pair"] = snap.pair
        stake = snap.stake_amount
        if (stake is None or stake <= 0) and snap.entry_price and snap.quantity:
            stake = snap.entry_price * snap.quantity
        state["live_trade_stake"] = stake if (stake is not None and stake > 0) else None

        if snap.entry_price is not None:
            old_base = _as_float(state.get("base"))
            if old_base is not None and abs(old_base - snap.entry_price) > 1e-12:
                state["previous_base"] = old_base
            elif state.get("previous_base") is None and old_base is not None:
                state["previous_base"] = old_base
            state["base"] = snap.entry_price
            state["base_price"] = snap.entry_price
            state["entry_price"] = snap.entry_price
            state["buy_price"] = snap.entry_price

        cand_peak = snap.max_rate if (snap.max_rate or 0) > 0 else snap.entry_price
        if cand_peak is not None:
            cur_peak = _as_float(state.get("peak"))
            cur_peak = cand_peak if cur_peak is None else max(cur_peak, cand_peak)
            state["peak"] = cur_peak
            state["high"] = cur_peak

        state["trough"] = None
        state["low"] = None
        state["_flat_anchor"] = None
        locks.pop("buy_pending", None)
        return

    # --- verified FLAT ---
    state["active_trade_id"] = None
    state["active_trade_pair"] = None
    state["live_trade_stake"] = None

    if prev_in_position:
        _set_buy_cooldown(state, prev_trade_id)
        locks.pop("buy_pending", None)
        state["peak"] = None
        state["high"] = None
        flags.pop("position_entry_rule", None)
        state["position_entry_rule"] = None

        market = state.get("market") if isinstance(state.get("market"), dict) else {}
        last = _as_float(market.get("last"))
        if last is None:
            last = _as_float(state.get("last"))
        old_base = _as_float(state.get("base"))
        if old_base is not None:
            state["previous_base"] = old_base
        if last is not None:
            state["trough"] = last
            state["low"] = last
            state["base"] = last
            state["_flat_anchor"] = last


def _set_buy_cooldown(state: dict, trade_id: Any) -> None:
    try:
        ttl = int(os.getenv("SELL_REBUY_COOLDOWN_SEC", "180"))
    except (TypeError, ValueError):
        ttl = 180
    if ttl <= 0:
        return
    cooldowns = state.get("cooldowns")
    if not isinstance(cooldowns, dict):
        cooldowns = {}
        state["cooldowns"] = cooldowns
    now_ts = int(time.time())
    cooldowns["buy_until"] = now_ts + ttl
    cooldowns["buy_set_ts"] = now_ts
    cooldowns["buy_reason"] = "AUTHORITY_POST_FLAT_COOLDOWN"
    if trade_id is not None:
        cooldowns["sell_trade_id"] = trade_id


# --------------------------------------------------------------------------- #
# Olvasó segédfüggvények – ezeket használja a végrehajtási kapu
# --------------------------------------------------------------------------- #

def authority_state(state: dict) -> str:
    block = state.get("position")
    if not isinstance(block, dict):
        return STATE_UNKNOWN
    st = block.get("authority_state")
    return st if st in (STATE_OPEN, STATE_FLAT, STATE_UNKNOWN) else STATE_UNKNOWN


def authority_trade_id(state: dict) -> Optional[int]:
    block = state.get("position")
    if not isinstance(block, dict):
        return None
    trade = block.get("trade")
    return _as_int(trade.get("trade_id")) if isinstance(trade, dict) else None


def execution_allowed(state: dict, action: str) -> Tuple[bool, str]:
    """
    A végrehajtási hard gate. ``(engedélyezett, ok)``.

    BUY  -> csak bizonyított FLAT
    SELL -> csak bizonyított OPEN
    UNKNOWN -> egyik sem
    """
    act = str(action or "").strip().upper()
    st = authority_state(state)

    if st == STATE_UNKNOWN:
        return False, "AUTHORITY_UNKNOWN"
    if act == "BUY":
        return (True, "OK") if st == STATE_FLAT else (False, "BUY_REQUIRES_VERIFIED_FLAT")
    if act == "SELL":
        if st != STATE_OPEN:
            return False, "SELL_REQUIRES_VERIFIED_OPEN"
        if authority_trade_id(state) is None:
            return False, "SELL_REQUIRES_VERIFIED_OPEN"
        return True, "OK"
    return True, "NO_ACTION"


# --------------------------------------------------------------------------- #
# Belépési pont
# --------------------------------------------------------------------------- #

def authority_tick(state: dict, pair: str, *, mode: Optional[str] = None) -> Optional[PositionSnapshot]:
    """
    Tickenként egyszer hívandó. SOHA nem dob kivételt.

    ``off``    -> None, a state érintetlen
    ``shadow`` -> a ``position`` blokk frissül, legacy mező NEM
    ``live``   -> a ``position`` blokk és a legacy aliasok is frissülnek
    """
    active_mode = (mode or get_mode())
    if active_mode == "off":
        return None

    try:
        result = fetch_open_position(pair)
        snapshot = snapshot_from_result(result, pair)
        outcome = reconcile(state, snapshot)
        apply_to_state(state, outcome, write_legacy=(active_mode == "live"))
        return outcome.snapshot
    except Exception as exc:  # pragma: no cover - a tick loop soha nem törhet meg
        _log(f"AUTHORITY_TICK_FATAL: {type(exc).__name__}: {exc}")
        try:
            fallback = PositionSnapshot(
                authority_state=STATE_UNKNOWN,
                observed_at=time.time(),
                last_error=ERR_UNKNOWN,
            )
            apply_to_state(
                state,
                ReconcileOutcome(
                    snapshot=fallback, previous_state=None,
                    reason="AUTHORITY_TICK_FATAL", changed=True,
                ),
                write_legacy=False,
            )
            return fallback
        except Exception:
            return None
