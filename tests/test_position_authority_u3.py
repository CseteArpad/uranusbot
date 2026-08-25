"""
U-3 Position Authority regressziós csomag.

Fő invariáns:

    FREQTRADE COMMUNICATION FAILURE MUST NEVER MEAN FLAT.

A csomag lefedi a Phase 2 feladat A–Y mátrixát, valamint a §18 szerinti,
NÉV SZERINT dokumentált eredeti-hiba regressziót
(``test_critical_regression_network_error_must_not_flip_in_position``).

Biztonsági garanciák:
  * nincs valódi hálózati hívás – az ``urlopen`` minden teszben mockolt;
  * nincs valódi order – a ``freqtrade_executor`` mérgezett vagy rögzítő
    duplikátumként van injektálva;
  * nincs systemctl és nincs production state-írás.
"""
from __future__ import annotations

import json
import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "app")

if APP_DIR not in sys.path:
    sys.path.append(APP_DIR)


# --------------------------------------------------------------------------- #
# Fixture-ök
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def pa():
    import position_authority
    return position_authority


@pytest.fixture(scope="module")
def ps():
    """A transport réteg – U-3 split után az ``urlopen`` itt él."""
    import position_source
    return position_source


@pytest.fixture(scope="module")
def tr():
    import tick_runner  # noqa: F401
    return sys.modules["tick_runner"]


@pytest.fixture(scope="module")
def fresh():
    import runtime_freshness
    return runtime_freshness


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, fresh):
    """Tiszta env + zárt freshness gate minden teszt előtt."""
    for key in (
        "URANUS_POSITION_AUTHORITY_MODE", "FT_URL", "URANUS_FT_URL",
        "URANUS_FT_CONFIG", "FT_USERNAME", "FT_PASSWORD",
        "EXECUTION_ENABLED", "EXECUTION_LOG_ONLY", "SELL_REBUY_COOLDOWN_SEC",
    ):
        monkeypatch.delenv(key, raising=False)
    fresh.reset_process_state()
    yield
    fresh.reset_process_state()


class _Resp:
    """Minimális urlopen context manager."""

    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_urlopen(pa, monkeypatch, *, body=None, exc=None, status=200):
    """
    A hálózat mockolása.

    U-3 split után az ``urlopen`` a ``position_source`` transport modulban él;
    a `pa` paramétert a hívási helyek kompatibilitása miatt tartjuk meg.
    """
    import position_source

    def fake(req, timeout=None):
        if exc is not None:
            raise exc
        return _Resp(body if body is not None else "[]", status)
    monkeypatch.setattr(position_source, "urlopen", fake)


def _trade(trade_id=1, pair="XRP/USDC", open_rate=1.5, amount=10.0, **extra):
    t = {
        "trade_id": trade_id,
        "pair": pair,
        "open_rate": open_rate,
        "amount": amount,
        "open_timestamp": 1787600000000,
        "max_rate": open_rate,
        "stake_amount": open_rate * amount,
    }
    t.update(extra)
    return t


def _state_with_verified(pa, state_name, trade=None, extra=None):
    """State a megadott utolsó BIZONYÍTOTT állapottal."""
    st = {"pair": "XRP/USDC", "position": {
        "authority_state": state_name,
        "last_verified": {
            "state": state_name,
            "verified_at": 1787600000,
            "trade": trade,
        },
    }}
    if trade:
        st["position"]["trade"] = trade
    if extra:
        st.update(extra)
    return st


# =========================================================================== #
# A–H · FETCH CONTRACT
# =========================================================================== #

def test_a_empty_list_is_verified_flat(pa, monkeypatch):
    """A. HTTP 200 + [] -> FLAT (és NEM None-szemantika)."""
    _mock_urlopen(pa, monkeypatch, body="[]")
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_FLAT
    assert r.error is None
    assert r.ok is True


def test_b_single_trade_is_verified_open(pa, monkeypatch):
    """B. HTTP 200 + egy valid trade -> OPEN."""
    _mock_urlopen(pa, monkeypatch, body=json.dumps([_trade()]))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_OPEN
    assert r.error is None

    snap = pa.snapshot_from_result(r, "XRP/USDC")
    assert snap.authority_state == pa.STATE_OPEN
    assert snap.trade_id == 1
    assert snap.entry_price == 1.5
    assert snap.entry_price_source == "open_rate"
    assert snap.quantity == 10.0
    assert snap.pair == "XRP/USDC"


def test_c_timeout_is_unknown(pa, monkeypatch):
    """C. timeout -> UNKNOWN / TIMEOUT."""
    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("timed out"))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_TIMEOUT


def test_c2_urlerror_timeout_is_unknown(pa, monkeypatch):
    from urllib.error import URLError
    _mock_urlopen(pa, monkeypatch, exc=URLError(TimeoutError("timed out")))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_TIMEOUT


@pytest.mark.parametrize("code", [401, 403])
def test_d_auth_failure_is_unknown(pa, monkeypatch, code):
    """D. 401/403 -> UNKNOWN / AUTH_ERROR."""
    from urllib.error import HTTPError
    _mock_urlopen(pa, monkeypatch, exc=HTTPError("u", code, "denied", {}, None))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_AUTH
    assert r.http_status == code


def test_e_network_failure_is_unknown(pa, monkeypatch):
    """E. hálózati hiba -> UNKNOWN / NETWORK_ERROR."""
    from urllib.error import URLError
    _mock_urlopen(pa, monkeypatch, exc=URLError(ConnectionRefusedError("refused")))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_NETWORK


@pytest.mark.parametrize("body", ["not json", '{"trades": []}', "null", '["a", "b"]', "123"])
def test_f_malformed_payload_is_unknown(pa, monkeypatch, body):
    """F. értelmezhetetlen válasz -> UNKNOWN / BAD_RESPONSE."""
    _mock_urlopen(pa, monkeypatch, body=body)
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_BAD_RESPONSE


def test_f2_http_500_is_unknown(pa, monkeypatch):
    from urllib.error import HTTPError
    _mock_urlopen(pa, monkeypatch, exc=HTTPError("u", 500, "boom", {}, None))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_BAD_RESPONSE


def test_g_multiple_relevant_trades_is_unknown(pa, monkeypatch):
    """G. >1 releváns trade -> UNKNOWN / MULTIPLE_TRADES."""
    body = json.dumps([_trade(trade_id=1), _trade(trade_id=2)])
    _mock_urlopen(pa, monkeypatch, body=body)
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_MULTIPLE_TRADES


def test_h_pair_mismatch_is_unknown(pa, monkeypatch):
    """H. idegen páron nyitott trade -> UNKNOWN / PAIR_MISMATCH."""
    _mock_urlopen(pa, monkeypatch, body=json.dumps([_trade(pair="BTC/USDC")]))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_PAIR_MISMATCH


@pytest.mark.parametrize("broken", [{"trade_id": None}, {"open_rate": None, "open_rate_requested": None}])
def test_h2_incomplete_trade_is_unknown(pa, monkeypatch, broken):
    """Hiányzó kötelező mező -> UNKNOWN / DATA_INCONSISTENT."""
    t = _trade()
    t.update(broken)
    _mock_urlopen(pa, monkeypatch, body=json.dumps([t]))
    r = pa.fetch_open_position("XRP/USDC")
    assert r.status == pa.STATE_UNKNOWN
    assert r.error == pa.ERR_DATA_INCONSISTENT


def test_h3_open_rate_requested_fallback_is_marked(pa, monkeypatch):
    t = _trade()
    t["open_rate"] = None
    t["open_rate_requested"] = 1.42
    _mock_urlopen(pa, monkeypatch, body=json.dumps([t]))
    snap = pa.snapshot_from_result(pa.fetch_open_position("XRP/USDC"), "XRP/USDC")
    assert snap.entry_price == 1.42
    assert snap.entry_price_source == "open_rate_requested"


def test_single_canonical_endpoint_no_fallback_chain(pa, monkeypatch):
    """§4: EGYETLEN endpoint, nincs hat URL-es lánc."""
    seen = []

    def fake(req, timeout=None):
        seen.append(req.full_url)
        return _Resp("[]")

    import position_source
    monkeypatch.setattr(position_source, "urlopen", fake)
    position_source.fetch_open_position("XRP/USDC", url_base="http://127.0.0.1:8090")
    assert seen == ["http://127.0.0.1:8090/api/v1/status"]

    # Strukturális garancia: a transport modul EGYETLEN REST-útvonalat ismer.
    src = open(os.path.join(APP_DIR, "position_source.py"), encoding="utf-8").read()
    api_paths = {
        line.split('"')[1]
        for line in src.splitlines()
        if line.strip().startswith(("STATUS_PATH", "_PATH")) and '"/api/' in line
    }
    assert api_paths == {"/api/v1/status"}
    assert src.count('"/api/v1/') == 1, "nem maradhat fallback-lánc"


# =========================================================================== #
# I–J · UNKNOWN + last_verified megőrzés
# =========================================================================== #

def test_i_prior_open_plus_timeout_preserves_last_verified_open(pa, monkeypatch):
    """I. előző OPEN + timeout -> UNKNOWN, last_verified OPEN megőrizve."""
    trade = {"trade_id": 7, "pair": "XRP/USDC", "entry_price": 1.4}
    state = _state_with_verified(pa, pa.STATE_OPEN, trade, extra={"in_position": True, "base": 1.4})

    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("timed out"))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert state["position"]["authority_state"] == pa.STATE_UNKNOWN
    assert state["position"]["last_error"] == pa.ERR_TIMEOUT
    assert state["position"]["last_verified"]["state"] == pa.STATE_OPEN
    assert state["position"]["last_verified"]["trade"]["trade_id"] == 7
    assert state["in_position"] is True          # legacy alias ÉRINTETLEN
    assert state["base"] == 1.4                  # horgony ÉRINTETLEN


def test_j_prior_flat_plus_timeout_preserves_last_verified_flat(pa, monkeypatch):
    """J. előző FLAT + timeout -> UNKNOWN, last_verified FLAT megőrizve."""
    state = _state_with_verified(pa, pa.STATE_FLAT, extra={"in_position": False, "base": 1.5})

    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("timed out"))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert state["position"]["authority_state"] == pa.STATE_UNKNOWN
    assert state["position"]["last_verified"]["state"] == pa.STATE_FLAT
    assert state["in_position"] is False
    assert state["base"] == 1.5


def test_last_verified_is_never_current_authority(pa, monkeypatch):
    """A last_verified SOHA nem válhat current authorityvá."""
    state = _state_with_verified(pa, pa.STATE_OPEN, {"trade_id": 7})
    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("t"))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert pa.authority_state(state) == pa.STATE_UNKNOWN
    for act in ("BUY", "SELL"):
        allowed, reason = pa.execution_allowed(state, act)
        assert allowed is False
        assert reason == "AUTHORITY_UNKNOWN"


def test_degraded_flag_after_consecutive_unknown(pa, monkeypatch):
    """T-kiegészítés: N egymást követő UNKNOWN -> degraded, viselkedés változatlan."""
    state = _state_with_verified(pa, pa.STATE_FLAT)
    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("t"))

    for i in range(1, pa.DEGRADED_AFTER_CONSECUTIVE_UNKNOWN + 1):
        pa.authority_tick(state, "XRP/USDC", mode="live")
        assert state["position"]["consecutive_unknown"] == i

    assert state["position"]["degraded"] is True
    assert state["position"]["unknown_since"] is not None
    assert pa.execution_allowed(state, "BUY")[0] is False


# =========================================================================== #
# K–P · EXECUTION GATE
# =========================================================================== #

@pytest.mark.parametrize("act,expected_reason", [
    ("BUY", "AUTHORITY_UNKNOWN"),
    ("SELL", "AUTHORITY_UNKNOWN"),
])
def test_kl_unknown_blocks_both_directions(pa, act, expected_reason):
    """K/L. UNKNOWN -> se BUY, se SELL."""
    state = {"position": {"authority_state": pa.STATE_UNKNOWN}}
    allowed, reason = pa.execution_allowed(state, act)
    assert allowed is False
    assert reason == expected_reason


def test_m_flat_allows_buy(pa):
    """M. verified FLAT + BUY -> a kapu átengedi."""
    state = {"position": {"authority_state": pa.STATE_FLAT}}
    assert pa.execution_allowed(state, "BUY") == (True, "OK")


def test_n_open_allows_sell(pa):
    """N. verified OPEN + SELL -> a kapu átengedi."""
    state = {"position": {"authority_state": pa.STATE_OPEN, "trade": {"trade_id": 5}}}
    assert pa.execution_allowed(state, "SELL") == (True, "OK")


def test_o_open_blocks_buy(pa):
    """O. OPEN + BUY -> blokk."""
    state = {"position": {"authority_state": pa.STATE_OPEN, "trade": {"trade_id": 5}}}
    assert pa.execution_allowed(state, "BUY") == (False, "BUY_REQUIRES_VERIFIED_FLAT")


def test_p_flat_blocks_sell(pa):
    """P. FLAT + SELL -> blokk."""
    state = {"position": {"authority_state": pa.STATE_FLAT}}
    assert pa.execution_allowed(state, "SELL") == (False, "SELL_REQUIRES_VERIFIED_OPEN")


def test_sell_without_trade_id_is_blocked(pa):
    """OPEN, de trade_id nélkül nincs mit eladni -> blokk."""
    state = {"position": {"authority_state": pa.STATE_OPEN, "trade": None}}
    assert pa.execution_allowed(state, "SELL") == (False, "SELL_REQUIRES_VERIFIED_OPEN")


def test_missing_position_block_defaults_to_unknown(pa):
    """Hiányzó position blokk -> UNKNOWN (fail-closed), nem FLAT."""
    assert pa.authority_state({}) == pa.STATE_UNKNOWN
    assert pa.execution_allowed({}, "BUY")[0] is False


# =========================================================================== #
# K/L integrációs változat: a valódi végrehajtási lánc
# =========================================================================== #

@pytest.fixture
def no_real_orders(monkeypatch):
    class _Poison:
        def __init__(self, *a, **k):
            raise AssertionError("valódi order-hívás történt volna")

    fake = types.ModuleType("freqtrade_executor")
    fake.FreqtradeExecutor = _Poison
    monkeypatch.setitem(sys.modules, "freqtrade_executor", fake)
    return fake


@pytest.fixture
def recording_executor(monkeypatch):
    calls = []

    class _Rec:
        def __init__(self, *a, **k):
            pass

        def force_enter(self):
            calls.append("force_enter")
            return types.SimpleNamespace(ok=True, http_status=200, detail="mock", response={})

        def force_exit(self, *a, **k):
            calls.append("force_exit")
            return types.SimpleNamespace(ok=True, http_status=200, detail="mock", response={})

        def confirm_open_trades(self):
            return types.SimpleNamespace(ok=True, http_status=200, detail="mock", response={})

    fake = types.ModuleType("freqtrade_executor")
    fake.FreqtradeExecutor = _Rec
    monkeypatch.setitem(sys.modules, "freqtrade_executor", fake)
    return calls


@pytest.fixture
def armed(monkeypatch, fresh):
    """Éles végrehajtási mód + nyitott freshness gate + tág guardrail."""
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_LOG_ONLY", "0")
    monkeypatch.setenv("KILL_SWITCH", "0")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "10")
    monkeypatch.setenv("DAILY_LOSS_CAP_PCT", "99")
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", "live")
    fresh.mark_market_fetch_ok(__import__("time").time(), "1m")
    fresh.mark_reconciled()


def _buy(tr):
    return tr.normalize_decision(action="BUY", rule="BUY_STANDARD", reason="test", level="none")


def _sell(tr):
    return tr.normalize_decision(action="SELL", rule="SELL_STD", reason="test", level="none")


def test_k_integration_unknown_blocks_force_enter(tr, pa, armed, no_real_orders):
    """K. UNKNOWN + BUY -> nincs force_enter (a poisoned executor bizonyítja)."""
    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_UNKNOWN}}
    out, executed = tr.maybe_execute_via_api(state, _buy(tr))
    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "position_block:AUTHORITY_UNKNOWN"


def test_l_integration_unknown_blocks_force_exit(tr, pa, armed, no_real_orders):
    """L. UNKNOWN + SELL -> nincs force_exit."""
    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_UNKNOWN}}
    out, executed = tr.maybe_execute_via_api(state, _sell(tr))
    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "position_block:AUTHORITY_UNKNOWN"


def test_o_integration_open_blocks_buy(tr, pa, armed, no_real_orders):
    state = {"pair": "XRP/USDC",
             "position": {"authority_state": pa.STATE_OPEN, "trade": {"trade_id": 3}}}
    out, executed = tr.maybe_execute_via_api(state, _buy(tr))
    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "position_block:BUY_REQUIRES_VERIFIED_FLAT"


def test_p_integration_flat_blocks_sell(tr, pa, armed, no_real_orders):
    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_FLAT}}
    out, executed = tr.maybe_execute_via_api(state, _sell(tr))
    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "position_block:SELL_REQUIRES_VERIFIED_OPEN"


def test_m_integration_flat_buy_reaches_executor(tr, pa, armed, recording_executor):
    """M. verified FLAT + BUY -> a kapu átengedi (mock executor, nincs valódi order)."""
    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_FLAT}}
    out, executed = tr.maybe_execute_via_api(state, _buy(tr))
    assert executed is True
    assert recording_executor == ["force_enter"]


def test_gate_order_position_before_guardrail(tr, pa, monkeypatch, fresh, no_real_orders):
    """A pozíció-kapu a guardrail ELŐTT fut: UNKNOWN + kill switch -> position_block."""
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_LOG_ONLY", "0")
    monkeypatch.setenv("KILL_SWITCH", "1")
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", "live")
    fresh.mark_market_fetch_ok(__import__("time").time(), "1m")
    fresh.mark_reconciled()

    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_UNKNOWN}}
    out, executed = tr.maybe_execute_via_api(state, _buy(tr))
    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "position_block:AUTHORITY_UNKNOWN"


def test_shadow_mode_does_not_block_execution(tr, pa, monkeypatch, fresh, recording_executor):
    """Shadow módban a kapu NEM blokkol – a bevezetés viselkedés-semleges."""
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_LOG_ONLY", "0")
    monkeypatch.setenv("KILL_SWITCH", "0")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "10")
    monkeypatch.setenv("DAILY_LOSS_CAP_PCT", "99")
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", "shadow")
    fresh.mark_market_fetch_ok(__import__("time").time(), "1m")
    fresh.mark_reconciled()

    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_UNKNOWN}}
    out, executed = tr.maybe_execute_via_api(state, _buy(tr))
    assert executed is True


# =========================================================================== #
# Q–S · STARTUP ÉS FAGYASZTÁS
# =========================================================================== #

def test_q_startup_unknown_blocks_reconcile_and_execution(tr, pa, fresh):
    """Q. friss piac + UNKNOWN pozíció -> nincs reconcile, a gate zárva marad."""
    fresh.mark_market_fetch_ok(__import__("time").time(), "1m")
    assert fresh.is_reconciled() is False

    state = {"pair": "XRP/USDC", "base": 1.0, "in_position": True}
    tr.reconcile_startup_anchor(state, 1.5, pa.STATE_UNKNOWN)

    assert fresh.is_reconciled() is False
    assert state["startup_reconcile"]["mode"] == "blocked_authority_unknown"
    assert state["base"] == 1.0
    ok, reason = fresh.execution_gate_status()
    assert ok is False
    assert reason == fresh.REASON_NOT_RECONCILED


def test_q2_startup_verified_flat_reanchors(tr, pa, fresh):
    fresh.mark_market_fetch_ok(__import__("time").time(), "1m")
    state = {"pair": "XRP/USDC", "base": 1.0, "in_position": True}
    tr.reconcile_startup_anchor(state, 1.5, pa.STATE_FLAT)

    assert state["startup_reconcile"]["mode"] == "flat_reanchored"
    assert state["base"] == 1.5
    assert fresh.is_reconciled() is True


def test_q3_startup_verified_open_preserves_anchor(tr, pa, fresh):
    fresh.mark_market_fetch_ok(__import__("time").time(), "1m")
    state = {"pair": "XRP/USDC", "base": 1.4, "in_position": False}
    tr.reconcile_startup_anchor(state, 1.5, pa.STATE_OPEN)

    assert state["startup_reconcile"]["mode"] == "in_position_preserved"
    assert state["base"] == 1.4
    assert fresh.is_reconciled() is True


def test_r_unknown_does_not_flat_reanchor(pa, monkeypatch):
    """R. UNKNOWN -> nincs flat-újrahorgonyzás."""
    state = _state_with_verified(pa, pa.STATE_OPEN, {"trade_id": 9}, extra={
        "in_position": True, "base": 1.4, "base_price": 1.4,
        "_flat_anchor": None, "peak": 1.45, "trough": None,
        "market": {"last": 1.9},
    })
    before = {k: state[k] for k in ("base", "base_price", "_flat_anchor", "peak", "trough")}

    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("t"))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    for k, v in before.items():
        assert state[k] == v, f"{k} megváltozott UNKNOWN alatt"


def test_s_unknown_leaves_cycle_and_position_state_unchanged(pa, monkeypatch):
    """S. UNKNOWN -> a pozíciófüggő ciklus-állapot változatlan."""
    state = _state_with_verified(pa, pa.STATE_OPEN, {"trade_id": 9}, extra={
        "in_position": True,
        "cycle": {"recovery_mode": True, "recovery_anchor_price": 1.3},
        "position_entry_rule": "BUY_STANDARD",
        "active_trade_id": 9,
        "cooldowns": {},
        "flags": {"position_entry_rule": "BUY_STANDARD"},
    })
    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("t"))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert state["cycle"] == {"recovery_mode": True, "recovery_anchor_price": 1.3}
    assert state["position_entry_rule"] == "BUY_STANDARD"
    assert state["active_trade_id"] == 9
    assert state["cooldowns"] == {}
    assert state["flags"]["position_entry_rule"] == "BUY_STANDARD"


def test_s2_run_once_freezes_position_dependent_updates(tr, pa, monkeypatch, tmp_path, fresh):
    """A run_once UNKNOWN alatt nem hívja a pozíciófüggő frissítőket."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "pair": "XRP/USDC", "in_position": True, "base": 1.4,
        "position": {"authority_state": pa.STATE_OPEN,
                     "last_verified": {"state": pa.STATE_OPEN, "verified_at": 1,
                                       "trade": {"trade_id": 9}}},
    }), encoding="utf-8")

    monkeypatch.setattr(tr, "STATE_PATH", str(state_file))
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", "live")
    monkeypatch.setenv("SHADOW_ENABLED", "0")
    _mock_urlopen(pa, monkeypatch, exc=TimeoutError("t"))
    monkeypatch.setattr(
        tr, "fetch_candles",
        lambda pair, timeframe, limit=None: [
            {"date": tr.now_utc_iso(), "open": 1.9, "high": 1.9, "low": 1.9,
             "close": 1.9, "volume": 1.0} for _ in range(40)
        ],
    )

    called = []
    monkeypatch.setattr(tr, "update_peak_trough", lambda *a, **k: called.append("peak_trough"))
    monkeypatch.setattr(tr, "update_recovery_state", lambda *a, **k: called.append("recovery"))

    ok, last, decision = tr.run_once()

    assert ok is True
    assert called == [], "pozíciófüggő frissítő futott UNKNOWN alatt"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["position"]["authority_state"] == pa.STATE_UNKNOWN
    assert saved["in_position"] is True          # legacy alias megőrizve
    assert saved["base"] == 1.4                  # horgony megőrizve
    # A piaci mező viszont frissülhetett – market-only forrás.
    assert saved["market"]["last"] == 1.9


# =========================================================================== #
# T–V · RECONCILIATION
# =========================================================================== #

def test_t_recovery_unknown_to_open(pa, monkeypatch):
    """T. UNKNOWN -> OPEN visszatérés."""
    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_UNKNOWN}}
    _mock_urlopen(pa, monkeypatch, body=json.dumps([_trade(trade_id=4)]))
    snap = pa.authority_tick(state, "XRP/USDC", mode="live")

    assert snap.authority_state == pa.STATE_OPEN
    assert state["position"]["authority_state"] == pa.STATE_OPEN
    assert state["position"]["last_verified"]["state"] == pa.STATE_OPEN
    assert state["in_position"] is True
    assert state["active_trade_id"] == 4


def test_u_recovery_unknown_to_flat(pa, monkeypatch):
    """U. UNKNOWN -> FLAT visszatérés."""
    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_UNKNOWN}}
    _mock_urlopen(pa, monkeypatch, body="[]")
    snap = pa.authority_tick(state, "XRP/USDC", mode="live")

    assert snap.authority_state == pa.STATE_FLAT
    assert state["position"]["last_verified"]["state"] == pa.STATE_FLAT
    assert state["in_position"] is False


def test_reconcile_flat_to_open_is_adoption(pa, monkeypatch):
    """REST OPEN + local FLAT -> OPEN + reconcile esemény."""
    state = _state_with_verified(pa, pa.STATE_FLAT, extra={"in_position": False})
    _mock_urlopen(pa, monkeypatch, body=json.dumps([_trade(trade_id=11)]))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert state["position"]["authority_state"] == pa.STATE_OPEN
    assert state["position"]["reason"] == pa.REASON_ADOPTED
    assert state["in_position"] is True


def test_reconcile_open_to_flat_is_external_close(pa, monkeypatch):
    """REST FLAT + local OPEN -> FLAT + reconcile + re-buy cooldown."""
    state = _state_with_verified(
        pa, pa.STATE_OPEN, {"trade_id": 11},
        extra={"in_position": True, "base": 1.4, "market": {"last": 1.6}},
    )
    _mock_urlopen(pa, monkeypatch, body="[]")
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert state["position"]["authority_state"] == pa.STATE_FLAT
    assert state["position"]["reason"] == pa.REASON_CLOSED_EXTERNALLY
    assert state["in_position"] is False
    assert state["cooldowns"]["buy_reason"] == "AUTHORITY_POST_FLAT_COOLDOWN"
    assert state["base"] == 1.6          # verified FLAT -> újrahorgonyzás engedett


def test_reconcile_same_trade_stays_open(pa, monkeypatch):
    state = _state_with_verified(pa, pa.STATE_OPEN, {"trade_id": 11}, extra={"in_position": True})
    _mock_urlopen(pa, monkeypatch, body=json.dumps([_trade(trade_id=11)]))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert state["position"]["authority_state"] == pa.STATE_OPEN
    assert state["position"]["reason"] == pa.REASON_STEADY


def test_no_auto_adopt_on_replaced_trade(pa, monkeypatch):
    """
    §8 senior korrekció: NINCS automatikus adopt.

    Ha a bizonyított trade helyett MÁS trade_id jelenik meg, az nem bizonyított
    folytonosság -> UNKNOWN, execution blokkolva, evidence.
    """
    state = _state_with_verified(
        pa, pa.STATE_OPEN, {"trade_id": 11}, extra={"in_position": True, "base": 1.4}
    )
    _mock_urlopen(pa, monkeypatch, body=json.dumps([_trade(trade_id=99, open_rate=1.8)]))
    snap = pa.authority_tick(state, "XRP/USDC", mode="live")

    assert snap.authority_state == pa.STATE_UNKNOWN
    assert state["position"]["reason"] == pa.REASON_REPLACED
    assert state["in_position"] is True         # legacy NEM íródik át
    assert state["base"] == 1.4                 # horgony NEM követi az új trade-et
    assert pa.execution_allowed(state, "BUY")[0] is False
    assert pa.execution_allowed(state, "SELL")[0] is False


def test_v_multiple_trades_no_silent_selection(pa, monkeypatch):
    """V. több trade -> UNKNOWN, egyiket sem választjuk ki."""
    state = _state_with_verified(pa, pa.STATE_FLAT, extra={"in_position": False})
    body = json.dumps([_trade(trade_id=1), _trade(trade_id=2)])
    _mock_urlopen(pa, monkeypatch, body=body)
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert state["position"]["authority_state"] == pa.STATE_UNKNOWN
    assert state["position"]["last_error"] == pa.ERR_MULTIPLE_TRADES
    assert state["position"].get("trade") in (None, {})
    assert state.get("active_trade_id") is None
    assert state["in_position"] is False


# =========================================================================== #
# W · SHADOW IZOLÁCIÓ
# =========================================================================== #

def test_w_shadow_open_cannot_make_authority_open(pa, monkeypatch):
    """W. shadow.in_position=True + FT verified FLAT -> LIVE authority FLAT."""
    state = {"pair": "XRP/USDC", "shadow": {"in_position": True, "entry_price": 1.1}}
    _mock_urlopen(pa, monkeypatch, body="[]")
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert pa.authority_state(state) == pa.STATE_FLAT
    assert state["in_position"] is False
    assert state["shadow"]["in_position"] is True   # a shadow érintetlen


def test_w2_shadow_flat_cannot_hide_authority_open(pa, monkeypatch):
    """W. shadow.in_position=False + FT verified OPEN -> LIVE authority OPEN."""
    state = {"pair": "XRP/USDC", "shadow": {"in_position": False}}
    _mock_urlopen(pa, monkeypatch, body=json.dumps([_trade(trade_id=6)]))
    pa.authority_tick(state, "XRP/USDC", mode="live")

    assert pa.authority_state(state) == pa.STATE_OPEN
    assert state["in_position"] is True
    assert state["shadow"]["in_position"] is False


def test_w3_authority_module_never_reads_shadow_state():
    """
    Strukturális garancia: az authority modul nem OLVASSA a shadow state-et.

    A "shadow" szó legitim módon szerepel a modulban a bevezetési mód neveként
    (``off | shadow | live``), ezért nem a szó jelenlétét, hanem a shadow state
    tényleges elérését tiltjuk.
    """
    src = open(os.path.join(APP_DIR, "position_authority.py"), encoding="utf-8").read()
    for forbidden in ('state["shadow"]', "state.get('shadow')", 'state.get("shadow")',
                      "['shadow']", 'shadow_position'):
        assert forbidden not in src, f"tiltott shadow-hozzáférés: {forbidden}"


# =========================================================================== #
# X · LEGACY ALIAS
# =========================================================================== #

def test_x_legacy_in_position_not_overwritten_on_fetch_failure(pa, monkeypatch):
    """X. lekérdezési hiba nem írhatja át a legacy in_position mezőt."""
    for prior in (True, False):
        state = _state_with_verified(
            pa, pa.STATE_OPEN if prior else pa.STATE_FLAT,
            {"trade_id": 3} if prior else None,
            extra={"in_position": prior, "ui_in_position": prior},
        )
        _mock_urlopen(pa, monkeypatch, exc=TimeoutError("t"))
        pa.authority_tick(state, "XRP/USDC", mode="live")

        assert state["in_position"] is prior
        assert state["ui_in_position"] is prior
        assert state["position"]["authority_state"] == pa.STATE_UNKNOWN


def test_x2_shadow_mode_writes_no_legacy_alias(pa, monkeypatch):
    """Shadow mód: a position blokk frissül, legacy mező NEM."""
    state = {"pair": "XRP/USDC", "in_position": True, "base": 1.4}
    _mock_urlopen(pa, monkeypatch, body="[]")
    pa.authority_tick(state, "XRP/USDC", mode="shadow")

    assert state["position"]["authority_state"] == pa.STATE_FLAT
    assert state["in_position"] is True     # NEM íródott át
    assert state["base"] == 1.4


def test_x3_off_mode_touches_nothing(pa, monkeypatch):
    state = {"pair": "XRP/USDC", "in_position": True}
    _mock_urlopen(pa, monkeypatch, body="[]")
    assert pa.authority_tick(state, "XRP/USDC", mode="off") is None
    assert "position" not in state
    assert state["in_position"] is True


def test_x4_legacy_state_without_position_block_starts_unknown(pa):
    """§19 migráció: a legacy boolean SOHA nem válik verified FLAT/OPEN-né."""
    legacy = {"pair": "XRP/USDC", "in_position": False, "base": 1.5}
    assert pa.authority_state(legacy) == pa.STATE_UNKNOWN
    assert pa.last_verified(legacy) == (None, None, None)
    assert pa.execution_allowed(legacy, "BUY")[0] is False

    legacy_open = {"pair": "XRP/USDC", "in_position": True}
    assert pa.authority_state(legacy_open) == pa.STATE_UNKNOWN


# =========================================================================== #
# Y · NINCS VALÓDI MELLÉKHATÁS
# =========================================================================== #

def test_y_no_real_network_or_systemctl_in_module():
    src = open(os.path.join(APP_DIR, "position_authority.py"), encoding="utf-8").read()
    for forbidden in ("systemctl", "subprocess", "force_enter", "force_exit", "forceenter"):
        assert forbidden not in src, f"tiltott hivatkozás: {forbidden}"


def test_y2_authority_tick_never_raises(pa, monkeypatch):
    """A belépési pont soha nem dobhat – a tick loop nem törhet meg."""
    def boom(*a, **k):
        raise RuntimeError("katasztrófa")

    monkeypatch.setattr(pa, "fetch_open_position", boom)
    state = {"pair": "XRP/USDC", "in_position": True}
    snap = pa.authority_tick(state, "XRP/USDC", mode="live")

    assert snap is not None
    assert snap.authority_state == pa.STATE_UNKNOWN
    assert state["in_position"] is True


# =========================================================================== #
# §18 · AZ EREDETI CRITICAL BUG NÉVSZERINTI REGRESSZIÓJA
# =========================================================================== #

def test_critical_regression_network_error_must_not_flip_in_position(
    pa, tr, monkeypatch, tmp_path, fresh, no_real_orders
):
    """
    U-3 CRITICAL REGRESSION — a Freqtrade kommunikációs hibája SOHA nem FLAT.

    Kiinduló állapot : in_position=True, bizonyított OPEN trade, base=1.4
    Esemény          : a pozíció-lekérdezés hálózati hibát dob
    RÉGI HIBA        : in_position=False, base a friss árra írva, perzisztálva
    ÚJ ELVÁRÁS       : authority_state=UNKNOWN
                       legacy in_position marad True
                       last_verified OPEN megőrizve
                       NINCS flat-újrahorgonyzás
                       NINCS éles order
    """
    from urllib.error import URLError

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "pair": "XRP/USDC",
        "in_position": True,
        "ui_in_position": True,
        "active_trade_id": 42,
        "base": 1.4,
        "base_price": 1.4,
        "entry_price": 1.4,
        "peak": 1.45,
        "trough": None,
        "_flat_anchor": None,
        "cycle": {"recovery_mode": True},
        "position": {
            "authority_state": pa.STATE_OPEN,
            "trade": {"trade_id": 42, "pair": "XRP/USDC", "entry_price": 1.4},
            "last_verified": {
                "state": pa.STATE_OPEN,
                "verified_at": 1787600000,
                "trade": {"trade_id": 42, "pair": "XRP/USDC", "entry_price": 1.4},
            },
        },
    }), encoding="utf-8")

    monkeypatch.setattr(tr, "STATE_PATH", str(state_file))
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", "live")
    monkeypatch.setenv("SHADOW_ENABLED", "0")
    # Éles végrehajtási mód: ha bármi átcsúszna, a poisoned executor buktat.
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_LOG_ONLY", "0")

    _mock_urlopen(pa, monkeypatch, exc=URLError(ConnectionRefusedError("connection refused")))
    monkeypatch.setattr(
        tr, "fetch_candles",
        lambda pair, timeframe, limit=None: [
            {"date": tr.now_utc_iso(), "open": 1.95, "high": 1.95, "low": 1.95,
             "close": 1.95, "volume": 1.0} for _ in range(40)
        ],
    )

    ok, last_close, decision = tr.run_once()
    assert ok is True

    saved = json.loads(state_file.read_text(encoding="utf-8"))

    # 1) az authority ismeretlen, nem FLAT
    assert saved["position"]["authority_state"] == pa.STATE_UNKNOWN
    assert saved["position"]["last_error"] == pa.ERR_NETWORK

    # 2) a legacy alias NEM billen át hamis FLAT-re  <-- ez volt az eredeti bug
    assert saved["in_position"] is True
    assert saved["ui_in_position"] is True
    assert saved["active_trade_id"] == 42

    # 3) az utolsó bizonyított állapot megőrizve
    assert saved["position"]["last_verified"]["state"] == pa.STATE_OPEN
    assert saved["position"]["last_verified"]["trade"]["trade_id"] == 42

    # 4) nincs flat-újrahorgonyzás: a horgony a nyitott pozícióé marad
    assert saved["base"] == 1.4
    assert saved["base_price"] == 1.4
    assert saved["entry_price"] == 1.4
    assert saved["_flat_anchor"] is None
    assert saved["peak"] == 1.45
    assert saved["cycle"] == {"recovery_mode": True}

    # 5) nincs éles order. Két, egymást erősítő bizonyíték:
    #    a) a poisoned executor puszta példányosítása AssertionError-t dobna,
    #       tehát ha idáig eljutottunk, valódi order-hívás nem történt;
    #    b) az executor-hívást rögzítő `last_call` kulcs létre sem jött.
    assert "last_call" not in saved.get("execution", {})
    assert executed_would_be_blocked(tr, pa, saved)


def executed_would_be_blocked(tr, pa, saved_state: dict) -> bool:
    """
    Külön bizonyíték: még egy KIKÉNYSZERÍTETT BUY döntés sem jutna ki
    ebben az állapotban – a pozíció-kapu blokkolja.
    """
    probe = dict(saved_state)
    decision = tr.normalize_decision(
        action="BUY", rule="BUY_STANDARD", reason="regression probe", level="none"
    )
    _out, executed = tr.maybe_execute_via_api(probe, decision)
    detail = probe["execution"]["last_result"]["detail"]
    return (executed is False) and detail.startswith(
        ("position_block:", "freshness_block:", "execution_disabled")
    )
