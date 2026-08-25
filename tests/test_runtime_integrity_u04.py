"""
U-0.4 runtime-integritási regressziós tesztek.

Lefedett verdiktek:
  1. TIMESTAMP_STATE_INTEGRITY        (D, E, F)
  2. SHADOW_BALANCE_INTEGRITY         (G, I, J)
  3. SHADOW_PHANTOM_POSITION          (G, H)
  4. LIVE_TRADING_RISK shadow->live   (N)
  5. STALE_STATE_LIVE_EXECUTION_RISK  (A, B, C)
  +  UranusExecutor signal freshness  (K, L, M)

Biztonsági garanciák: egyetlen teszt sem hív valódi Freqtrade order-végpontot,
valódi exchange API-t, valódi systemctl-t vagy a production state writert.
A `freqtrade_executor` modul mérgezett duplikátumként van injektálva: bármely
valódi order-hívás azonnal AssertionError-t okoz.
"""
import importlib.util
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "app")
STRATEGY_PY = os.path.join(
    REPO_ROOT, "freqtrade", "user_data", "strategies", "SaturnusExecutor.py"
)

if APP_DIR not in sys.path:
    sys.path.append(APP_DIR)


def _load(module_name: str, path: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Fixture-ök
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def tr():
    import tick_runner  # noqa: F401  (flat import, app/ a sys.path-on)
    return sys.modules["tick_runner"]


@pytest.fixture(scope="module")
def fresh():
    import runtime_freshness
    return runtime_freshness


@pytest.fixture(scope="module")
def sp():
    import shadow_position
    return shadow_position


@pytest.fixture(scope="module")
def strategy_mod():
    """
    A telepített `freqtrade` csomagban nincs `freqtrade.strategy` almodul, ezért
    az IStrategy bázisosztályt minimális stubbal pótoljuk. A tesztelt logika
    (jel-frissesség) tisztán a modulszintű függvényekben és a metódusokban él,
    a bázisosztálytól nem függ.
    """
    import types

    if "freqtrade.strategy" not in sys.modules:
        pkg = sys.modules.get("freqtrade") or types.ModuleType("freqtrade")
        sys.modules["freqtrade"] = pkg
        stub = types.ModuleType("freqtrade.strategy")
        stub.IStrategy = object
        sys.modules["freqtrade.strategy"] = stub
        pkg.strategy = stub

    return _load("uranus_executor_strategy_undertest", STRATEGY_PY)


@pytest.fixture(autouse=True)
def clean_gate(fresh):
    """Minden teszt zárt gate-tel indul – ez a fail-closed alapállapot."""
    fresh.reset_process_state()
    yield
    fresh.reset_process_state()


class _PoisonedExecutor:
    """Bármely valódi order-hívás azonnal megbuktatja a tesztet."""

    def __init__(self, *a, **k):
        raise AssertionError("FreqtradeExecutor nem példányosítható ebben a tesztben")


@pytest.fixture
def no_real_orders(monkeypatch):
    import types

    fake = types.ModuleType("freqtrade_executor")
    fake.FreqtradeExecutor = _PoisonedExecutor
    monkeypatch.setitem(sys.modules, "freqtrade_executor", fake)
    return fake


@pytest.fixture
def recording_executor(monkeypatch):
    """Rögzítő executor: a hívás tényét méri, valódi ordert nem küld."""
    import types

    calls = []

    class _Rec:
        def __init__(self, *a, **k):
            pass

        def force_enter(self):
            calls.append("force_enter")
            return {"ok": True, "status": 200, "detail": "mock"}

        def force_exit(self, *a, **k):
            calls.append("force_exit")
            return {"ok": True, "status": 200, "detail": "mock"}

    fake = types.ModuleType("freqtrade_executor")
    fake.FreqtradeExecutor = _Rec
    monkeypatch.setitem(sys.modules, "freqtrade_executor", fake)
    return calls


@pytest.fixture
def exec_on(monkeypatch):
    """Éles végrehajtási mód: EXECUTION_ENABLED=1, LOG_ONLY=0, guardrailek nyitva."""
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_LOG_ONLY", "0")
    monkeypatch.setenv("KILL_SWITCH", "0")
    monkeypatch.setenv("MAX_TRADES_PER_DAY", "10")
    monkeypatch.setenv("DAILY_LOSS_CAP_PCT", "99")


def make_candles(close: float, ts_iso: str, count: int = 40):
    """Egyszerű, konstans gyertyasorozat friss időbélyeggel."""
    return [
        {"date": ts_iso, "open": close, "high": close, "low": close,
         "close": close, "volume": 10.0}
        for _ in range(count)
    ]


def buy_decision(tr):
    return tr.normalize_decision(
        action="BUY", rule="BUY_CATASTROPHE",
        reason="last >= catastrophe_buy_level", level="catastrophe_buy_level",
    )


# =========================================================================== #
# A. STARTUP STALE STATE
# =========================================================================== #

def test_a1_startup_with_stale_base_sends_no_order(tr, fresh, monkeypatch, tmp_path, exec_on, no_real_orders):
    """
    Perzisztált régi base + friss ár + EXECUTION_ENABLED=1 + LOG_ONLY=0.
    Elvárt: SEMMILYEN valódi order, és a horgony újrahorgonyzása.
    """
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "pair": "XRP/USDC",
        "in_position": False,
        "base": 1.0,                     # hónapokkal régi horgony
        "updated_utc": "2026-06-21T17:16:55Z",
        "cycle": {},
    }), encoding="utf-8")

    monkeypatch.setattr(tr, "STATE_PATH", str(state_file))
    monkeypatch.setattr(tr, "sync_position_from_freqtrade", lambda state, pair: None)
    monkeypatch.setattr(
        tr, "fetch_candles",
        lambda pair, timeframe, limit=None: make_candles(1.479, tr.now_utc_iso()),
    )
    monkeypatch.setenv("SHADOW_ENABLED", "0")

    ok, last, decision = tr.run_once()

    assert ok is True
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    # A horgony újra a friss árra mutat -> a stale-base okozta katasztrófa eltűnt.
    assert saved["startup_reconcile"]["mode"] == "flat_reanchored"
    assert saved["startup_reconcile"]["previous_base"] == 1.0
    assert saved["base"] == pytest.approx(1.479)

    # A lényeg: a friss horgony mellett a katasztrófa-küszöb (base * 1.10)
    # már NEM sérül, ezért BUY sem keletkezik. A gate ilyenkor armed, tehát ha
    # mégis BUY született volna, a poisoned executor AssertionError-t dobna –
    # a teszt tehát mindkét védelmi réteget bizonyítja.
    assert decision.get("action") == "HOLD"


def test_a2_unarmed_gate_blocks_buy_execution(tr, fresh, exec_on, no_real_orders):
    """A gate a végrehajtás előtt tilt, ha a processz nem bizonyított friss adatot."""
    state = {"pair": "XRP/USDC", "in_position": False}
    decision = buy_decision(tr)

    out, executed = tr.maybe_execute_via_api(state, decision)

    assert executed is False
    detail = state["execution"]["last_result"]["detail"]
    assert detail == "freshness_block:STARTUP_NOT_ARMED"


def test_a3_stale_market_data_blocks_execution(tr, fresh, exec_on, no_real_orders):
    """Friss fetch, de hónapokkal régi gyertya-időbélyeg -> blokk."""
    fresh.mark_market_fetch_ok("2026-06-21T17:16:55Z", "1m")
    fresh.mark_reconciled()

    state = {"pair": "XRP/USDC", "in_position": False}
    out, executed = tr.maybe_execute_via_api(state, buy_decision(tr))

    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "freshness_block:MARKET_DATA_STALE"


def test_a4_invalid_candle_timestamp_blocks_execution(tr, fresh, exec_on, no_real_orders):
    """Értelmezhetetlen gyertya-időbélyeg -> fail-closed blokk."""
    fresh.mark_market_fetch_ok("nem-datum", "1m")
    fresh.mark_reconciled()

    state = {"pair": "XRP/USDC", "in_position": False}
    out, executed = tr.maybe_execute_via_api(state, buy_decision(tr))

    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "freshness_block:MARKET_DATA_TIMESTAMP_INVALID"


def test_a5_not_reconciled_blocks_execution(tr, fresh, exec_on, no_real_orders):
    """Friss adat, de az állapot-újrahorgonyzás még nem futott -> blokk."""
    fresh.mark_market_fetch_ok(tr.now_utc_iso(), "1m")

    state = {"pair": "XRP/USDC", "in_position": False}
    out, executed = tr.maybe_execute_via_api(state, buy_decision(tr))

    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "freshness_block:STALE_STATE_BLOCKED"


def test_a6_stale_base_still_produces_catastrophe_decision(tr):
    """
    Kontroll: a STRATÉGIA változatlan. Elavult horgony mellett a döntési motor
    továbbra is BUY_CATASTROPHE-t ad – tehát a védelmet nem a szabály
    átírása, hanem a horgonyzás és a gate adja.
    """
    state = {
        "pair": "XRP/USDC", "in_position": False,
        "base": 1.0, "last": 1.479, "prev_last": 1.478,
        "market": {"last": 1.479, "prev_last": 1.478},
        "cycle": {},
    }
    tr.ensure_levels(state)
    decision = tr.decision_from_rule_engine(state)

    assert decision.get("action") == "BUY"
    assert decision.get("rule") == "BUY_CATASTROPHE"


# =========================================================================== #
# B. STARTUP AFTER FRESH RECONCILIATION
# =========================================================================== #

def test_b_armed_and_reconciled_allows_execution(tr, fresh, exec_on, recording_executor):
    """Friss adat + megtörtént reconcile -> a gate PASS, a végrehajtás mehet."""
    fresh.mark_market_fetch_ok(tr.now_utc_iso(), "1m")
    fresh.mark_reconciled()

    state = {"pair": "XRP/USDC", "in_position": False}
    out, executed = tr.maybe_execute_via_api(state, buy_decision(tr))

    assert executed is True
    assert recording_executor == ["force_enter"]
    assert state["execution"]["freshness_gate"]["armed"] is True


# =========================================================================== #
# C. OPEN LIVE POSITION
# =========================================================================== #

def test_c_open_live_position_preserves_anchor_and_cycle(tr, fresh):
    """Nyitott LIVE pozíciónál a horgony és a recovery/panic kontextus megmarad."""
    cycle = {
        "recovery_mode": True,
        "required_next_buy_mode": "RECOVERY_OR_LOWER_STANDARD",
        "recovery_anchor_price": 1.60,
    }
    state = {
        "in_position": True,
        "base": 1.60,
        "peak": 1.72,
        "cycle": dict(cycle),
    }

    tr.reconcile_startup_anchor(state, 1.479)

    assert state["base"] == 1.60            # NEM horgonyzunk újra
    assert state["peak"] == 1.72
    assert state["cycle"] == cycle          # a ciklus-kontextus érintetlen
    assert state["startup_reconcile"]["mode"] == "in_position_preserved"
    assert fresh.is_reconciled() is True


def test_c2_reconcile_runs_only_once_per_process(tr, fresh):
    state = {"in_position": False, "base": 1.0, "cycle": {}}
    tr.reconcile_startup_anchor(state, 1.479)
    assert state["base"] == pytest.approx(1.479)

    # Egy későbbi tick már nem horgonyoz újra.
    state["base"] = 1.55
    tr.reconcile_startup_anchor(state, 2.0)
    assert state["base"] == 1.55


def test_c3_no_price_leaves_gate_closed(tr, fresh):
    """Friss ár nélkül nem horgonyzunk, és a gate zárva marad."""
    state = {"in_position": False, "base": 1.0, "cycle": {}}
    tr.reconcile_startup_anchor(state, None)

    assert state["base"] == 1.0
    assert fresh.is_reconciled() is False
    assert "startup_reconcile" not in state


# =========================================================================== #
# Q3 (senior review). MÉRGEZETT PERZISZTÁLT CYCLE / RECOVERY / PANIC KONTEXTUS
# =========================================================================== #

def poisoned_cycle_state(last: float = 1.479) -> dict:
    """
    LIVE flat + hónapokkal régi base + régi panic/recovery kontextus,
    friss piaci árral. A régi referenciaárak szándékosan a mai ár FÖLÖTT vannak.
    """
    return {
        "pair": "XRP/USDC",
        "in_position": False,
        "base": 1.0,
        "last": last, "prev_last": last,
        "market": {"last": last, "prev_last": last},
        "cycle": {
            "recovery_mode": True,
            "recovery_loss_pct": 0.35,
            "recovery_anchor_price": 2.10,
            "required_next_buy_mode": "RECOVERY_OR_LOWER_STANDARD",
            "required_next_sell_mode": "",
            "recovery_target_entry_cap": 1.90,
            "last_panic_sell_price": 2.10,
            "last_panic_buy_price": None,
            "recovery_buy_armed": True,
            "recovery_sell_armed": False,
            "recovery_buy_arm_age": 5,
            "recovery_sell_arm_age": 0,
        },
    }


def test_q3_control_poisoned_state_without_reconcile_yields_catastrophe(tr):
    """
    Kontroll: a mérgezett állapot ÚJRAHORGONYZÁS NÉLKÜL valóban
    BUY_CATASTROPHE-t szül – tehát a teszt-beállítás valós, és a védelmet
    nem a szabály megváltoztatása adja.
    """
    state = poisoned_cycle_state()
    tr.ensure_levels(state)
    tr._mirror_cycle_to_state(state)
    decision = tr.decision_from_rule_engine(state)

    assert decision.get("action") == "BUY"
    assert decision.get("rule") == "BUY_CATASTROPHE"


def test_q3a_poisoned_cycle_cannot_execute_before_arming(tr, fresh, exec_on, no_real_orders):
    """A) Az armolás/reconcile ELŐTT a régi cycle kontextus nem juthat orderig."""
    state = poisoned_cycle_state()
    out, executed = tr.maybe_execute_via_api(state, buy_decision(tr))

    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "freshness_block:STARTUP_NOT_ARMED"


def test_q3b_after_reconcile_stale_cycle_does_not_create_buy(tr, fresh):
    """
    B) Az első armed tick után a megmaradó cycle kontextus önmagában
    NEM hoz létre BUY-t a régi referenciaárakból.
    """
    state = poisoned_cycle_state()
    tr.reconcile_startup_anchor(state, 1.479)
    tr.ensure_levels(state)
    tr._mirror_cycle_to_state(state)
    decision = tr.decision_from_rule_engine(state)

    assert decision.get("action") == "HOLD"

    # A cycle kontextus MEGMARADT (nem töröltük – scope-korlát), csak a
    # horgony frissült.
    assert state["cycle"]["required_next_buy_mode"] == "RECOVERY_OR_LOWER_STANDARD"
    assert state["cycle"]["recovery_target_entry_cap"] == 1.90
    assert state["base"] == pytest.approx(1.479)


@pytest.mark.parametrize("cap", [1.90, 1.50, 1.4795, 1.20])
def test_q3b2_no_buy_for_any_stale_cap_position(tr, fresh, cap):
    """A régi cap bármely elhelyezkedése mellett sem keletkezik BUY."""
    state = poisoned_cycle_state()
    tr.reconcile_startup_anchor(state, 1.479)
    state["cycle"]["recovery_target_entry_cap"] = cap
    tr.ensure_levels(state)
    tr._mirror_cycle_to_state(state)

    assert tr.decision_from_rule_engine(state).get("action") == "HOLD"


def test_q3c_full_tick_with_poisoned_cycle_sends_no_order(tr, fresh, monkeypatch, tmp_path, exec_on, no_real_orders):
    """
    Végponttól végpontig: mérgezett perzisztált állapot + EXECUTION_ENABLED=1
    + LOG_ONLY=0 -> semmilyen valódi order (a poisoned executor buktatna).
    """
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(poisoned_cycle_state()), encoding="utf-8")

    monkeypatch.setattr(tr, "STATE_PATH", str(state_file))
    monkeypatch.setattr(tr, "sync_position_from_freqtrade", lambda state, pair: None)
    monkeypatch.setattr(
        tr, "fetch_candles",
        lambda pair, timeframe, limit=None: make_candles(1.479, tr.now_utc_iso()),
    )
    monkeypatch.setenv("SHADOW_ENABLED", "0")

    ok, _last, decision = tr.run_once()

    assert ok is True
    assert decision.get("action") == "HOLD"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["startup_reconcile"]["mode"] == "flat_reanchored"
    assert saved["cycle"]["required_next_buy_mode"] == "RECOVERY_OR_LOWER_STANDARD"


# =========================================================================== #
# D-E. TIMESTAMP
# =========================================================================== #

def test_d_tick_timestamps_share_one_now(fresh):
    state = {}
    iso = fresh.stamp_tick_timestamps(state, epoch=1_800_000_000)

    assert state["updated_utc"] == iso
    assert state["updated_at"] == iso
    assert state["time"] == iso
    assert state["tick_ts"] == 1_800_000_000
    assert iso.endswith("Z")


def test_e_successful_tick_refreshes_stale_updated_utc(tr, monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "pair": "XRP/USDC", "in_position": False,
        "base": 1.45, "updated_utc": "2026-06-21T17:16:55Z", "cycle": {},
    }), encoding="utf-8")

    monkeypatch.setattr(tr, "STATE_PATH", str(state_file))
    monkeypatch.setattr(tr, "sync_position_from_freqtrade", lambda state, pair: None)
    monkeypatch.setattr(
        tr, "fetch_candles",
        lambda pair, timeframe, limit=None: make_candles(1.479, tr.now_utc_iso()),
    )
    monkeypatch.setenv("EXECUTION_ENABLED", "0")
    monkeypatch.setenv("SHADOW_ENABLED", "0")

    ok, _last, _decision = tr.run_once()
    assert ok is True

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["updated_utc"] != "2026-06-21T17:16:55Z"
    assert saved["updated_utc"] == saved["updated_at"] == saved["time"]
    assert isinstance(saved["tick_ts"], int)


def test_e2_failed_tick_does_not_fake_freshness(tr, monkeypatch, tmp_path):
    """Hibaágon a freshness-timestamp NEM frissül, csak a hibaidő."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "pair": "XRP/USDC", "in_position": False,
        "updated_utc": "2026-06-21T17:16:55Z", "cycle": {},
    }), encoding="utf-8")

    monkeypatch.setattr(tr, "STATE_PATH", str(state_file))
    monkeypatch.setattr(tr, "sync_position_from_freqtrade", lambda state, pair: None)

    def _boom(*a, **k):
        raise RuntimeError("FT down")

    monkeypatch.setattr(tr, "fetch_candles", _boom)
    monkeypatch.setenv("SHADOW_ENABLED", "0")

    ok, _last, decision = tr.run_once()

    assert ok is False
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["updated_utc"] == "2026-06-21T17:16:55Z"   # változatlan
    assert saved["last_error_at"]                            # a hiba ideje külön
    assert decision["rule"] == "API_UNAVAILABLE"


# =========================================================================== #
# F. API HEADER
# =========================================================================== #

@pytest.fixture
def web(monkeypatch):
    """
    Az app.py betöltése, a kimenő hálózat teljes lezárásával.

    A `load_state_view()` egyébként 6 valódi `requests.get` hívást indítana a
    Freqtrade felé; a tesztben ez tilos, ezért mindet megbuktatjuk.
    """
    mod = sys.modules.get("uranus_web_app_u04") or _load(
        "uranus_web_app_u04", os.path.join(APP_DIR, "app.py")
    )

    def _no_network(*a, **k):
        raise AssertionError("valódi HTTP hívás tilos ebben a tesztben")

    # Az app.py függvényen belül importálja a requests-et, ezért magán a
    # requests modulon kell blokkolni – így minden hívási hely le van fedve.
    import requests as _requests

    monkeypatch.setattr(_requests, "get", _no_network)
    monkeypatch.setattr(_requests, "post", _no_network, raising=False)
    monkeypatch.setattr(mod.freqtrade_ui, "snapshot", lambda *a, **k: {})
    mod.app.config["TESTING"] = True
    return mod


def test_f_api_header_uses_canonical_timestamp(web, monkeypatch, tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "pair": "XRP/USDC",
        "updated_utc": "2026-08-24T21:00:00Z",
        "updated_at": "2026-08-24T21:00:00Z",
    }), encoding="utf-8")
    monkeypatch.setattr(web, "STATE_PATH", str(state_file))

    res = web.app.test_client().head("/api/state/head")

    assert res.status_code == 200
    assert res.headers["X-Updated-Utc"] == "2026-08-24T21:00:00Z"


def test_f2_legacy_state_without_updated_utc_falls_back_for_display(web, monkeypatch, tmp_path):
    """Régi state: a view az `updated_at`-ot mutatja – állapotírás nélkül."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "pair": "XRP/USDC", "updated_at": "2026-08-24T21:05:00Z",
    }), encoding="utf-8")
    monkeypatch.setattr(web, "STATE_PATH", str(state_file))

    view = web.load_state_view()
    assert view["updated_utc"] == "2026-08-24T21:05:00Z"

    # A read path nem írhatta be a state-be kanonikus értékként.
    on_disk = json.loads(state_file.read_text(encoding="utf-8"))
    assert "updated_utc" not in on_disk


# =========================================================================== #
# G-J. SHADOW
# =========================================================================== #

@pytest.fixture
def shadow_env(monkeypatch, sp):
    monkeypatch.setenv("SHADOW_ENABLED", "1")
    monkeypatch.setenv("FEE_PCT", "0.1")
    monkeypatch.setenv("SLIPPAGE_PCT", "0")

    # A rule_engine szerződése: a `decision` mező hordozza a szabálynevet
    # (`BUY_*` / `SELL_*`), ebből képzi a shadow a normalizált akciót.
    import rule_engine
    monkeypatch.setattr(
        rule_engine, "decide",
        lambda ctx, cyc: {"decision": "BUY_CATASTROPHE", "reason": "test",
                          "selected_level_name": "catastrophe_buy_level"},
    )
    return sp


def base_shadow_state(**shadow_over):
    shadow = {
        "enabled": True, "in_position": False,
        "equity_usdc": 17.2618, "equity_seed_usdc": 19.25297507,
        "equity_sync_failures": 65997, "ledger": [], "cycle": {},
    }
    shadow.update(shadow_over)
    return {
        "pair": "XRP/USDC", "in_position": False,
        "market": {"last": 1.479, "prev_last": 1.478},
        "shadow": shadow,
    }


def test_g_shadow_flat_balance_fail_blocks_buy(shadow_env, monkeypatch, no_real_orders):
    """A központi lelet: régi pozitív equity + sikertelen sync -> NINCS vétel."""
    sp = shadow_env
    monkeypatch.setattr(sp, "fetch_shadow_equity_from_live",
                        lambda state: (None, "unavailable"))

    state = base_shadow_state()
    sp.maybe_run_shadow_tick(state)

    shadow = state["shadow"]
    assert shadow["in_position"] is False           # nem nyílt phantom pozíció
    assert shadow["equity_usdc"] == 17.2618         # az equity érintetlen
    assert shadow["ledger"] == []
    assert shadow["equity_sync_ok_last_tick"] is False
    assert shadow["equity_sync_failures"] == 65998  # a számláló nő


def test_h_shadow_flat_valid_equity_allows_buy(shadow_env, monkeypatch, no_real_orders):
    sp = shadow_env
    monkeypatch.setattr(sp, "fetch_shadow_equity_from_live",
                        lambda state: (20.0, "ft_balance"))

    state = base_shadow_state()
    sp.maybe_run_shadow_tick(state)

    shadow = state["shadow"]
    assert shadow["in_position"] is True
    assert shadow["equity_sync_failures"] == 0      # sikeres sync -> reset
    assert shadow["entry_price"] == pytest.approx(1.479)
    assert shadow["entry_cost_usdc"] == pytest.approx(20.0)


def test_i_shadow_midposition_balance_fail_keeps_position(shadow_env, monkeypatch, no_real_orders):
    """Pozícióban a balance-hiba nem törheti el a meglévő shadow pozíciót."""
    sp = shadow_env

    def _must_not_call(state):
        raise AssertionError("pozícióban nem szabad balance-t szinkronizálni")

    monkeypatch.setattr(sp, "fetch_shadow_equity_from_live", _must_not_call)

    state = base_shadow_state(
        in_position=True, equity_usdc=0.0, entry_price=1.479,
        entry_qty_base=11.659598, entry_cost_usdc=17.2618, base=1.479, peak=1.479,
    )
    sp.maybe_run_shadow_tick(state)

    shadow = state["shadow"]
    assert shadow["in_position"] is True
    assert shadow["entry_price"] == pytest.approx(1.479)
    assert shadow["equity_sync_failures"] == 65997  # nem nőtt: nem is próbálkozott


def test_j_failure_counter_semantics(sp, monkeypatch):
    shadow = {"in_position": False, "equity_sync_failures": 5}

    monkeypatch.setattr(sp, "fetch_shadow_equity_from_live",
                        lambda state: (None, "unavailable"))
    result = sp._shadow_sync_equity_if_flat({}, shadow)
    assert result == {"attempted": True, "ok": False, "source": "unavailable"}
    assert shadow["equity_sync_failures"] == 6

    monkeypatch.setattr(sp, "fetch_shadow_equity_from_live",
                        lambda state: (12.5, "ft_balance"))
    result = sp._shadow_sync_equity_if_flat({}, shadow)
    assert result["ok"] is True
    assert shadow["equity_sync_failures"] == 0      # sikerre nullázódik
    assert shadow["equity_usdc"] == 12.5


def test_j2_seed_is_not_used_as_spendable_equity(sp, monkeypatch):
    """A seed historikus baseline marad: sikertelen sync mellett nem méretez."""
    sp_mod = sp
    monkeypatch.setattr(sp_mod, "fetch_shadow_equity_from_live",
                        lambda state: (None, "unavailable"))
    shadow = {"in_position": False, "equity_usdc": None,
              "equity_seed_usdc": 19.25297507, "equity_sync_failures": 0}
    sp_mod._shadow_sync_equity_if_flat({}, shadow)

    assert shadow["equity_usdc"] is None            # a seed NEM lett spendable
    assert shadow["equity_seed_usdc"] == 19.25297507


# =========================================================================== #
# N. SHADOW / LIVE IZOLÁCIÓ
# =========================================================================== #

def test_n1_shadow_tick_never_touches_live_executor(shadow_env, monkeypatch, no_real_orders):
    """A poisoned executor bármely használata AssertionError-t okozna."""
    sp = shadow_env
    monkeypatch.setattr(sp, "fetch_shadow_equity_from_live",
                        lambda state: (20.0, "ft_balance"))

    state = base_shadow_state()
    sp.maybe_run_shadow_tick(state)          # nem dobhat

    assert state["shadow"]["in_position"] is True


def test_n2_shadow_module_has_no_order_calls():
    """Strukturális garancia: a shadow modul nem hivatkozik order-útvonalra."""
    src = open(os.path.join(APP_DIR, "shadow_position.py"), encoding="utf-8").read()
    for forbidden in ("force_enter", "force_exit", "freqtrade_executor", "forceenter", "forceexit"):
        assert forbidden not in src, f"tiltott hivatkozás a shadow modulban: {forbidden}"


def test_n3_rule_engine_does_not_read_shadow_state():
    src = open(os.path.join(APP_DIR, "rule_engine.py"), encoding="utf-8").read()
    assert "shadow" not in src.lower()


def test_n4_live_tick_path_does_not_read_shadow_state():
    """A tick_runner csak ÁTADJA a state-et a shadow-nak, vissza nem olvas."""
    src = open(os.path.join(APP_DIR, "tick_runner.py"), encoding="utf-8").read()
    assert 'state["shadow"]' not in src
    assert 'state.get("shadow")' not in src


# =========================================================================== #
# K-M. URANUS EXECUTOR SIGNAL FRESHNESS
# =========================================================================== #

def _make_strategy(strategy_mod, tmp_path, state_payload):
    strat = object.__new__(strategy_mod.UranusExecutor)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_payload), encoding="utf-8")
    strat.STATE_JSON_PATH = str(state_file)
    strat.EXECUTOR_STATE_PATH = str(tmp_path / "exec_state.json")
    return strat


def _df():
    import pandas as pd
    return pd.DataFrame({"close": [1.0, 1.1, 1.2]})


def _signal_state(ts, action="BUY", sig_id="sig-1"):
    return {"signals": {"XRP/USDC": {"action": action, "id": sig_id, "ts": ts}}}


def test_k_stale_signal_blocks_entry(strategy_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("SIGNAL_MAX_AGE_SEC", "300")
    strat = _make_strategy(strategy_mod, tmp_path, _signal_state("2026-06-21T17:16:55Z"))

    out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert int(out["enter_long"].iloc[-1]) == 0
    assert not os.path.exists(strat.EXECUTOR_STATE_PATH)  # nem jelöltük feldolgozottnak


@pytest.mark.parametrize("bad_ts", ["", "AUTO", "nem-datum", None, 0, "0"])
def test_l_missing_or_invalid_signal_ts_blocks_entry(strategy_mod, tmp_path, bad_ts):
    strat = _make_strategy(strategy_mod, tmp_path, _signal_state(bad_ts))
    out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert int(out["enter_long"].iloc[-1]) == 0


def test_m_fresh_signal_never_enters_since_u2b(strategy_mod, tmp_path, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setenv("SIGNAL_MAX_AGE_SEC", "300")
    now_iso = datetime.now(timezone.utc).isoformat()
    strat = _make_strategy(strategy_mod, tmp_path, _signal_state(now_iso))

    # U-2B ÓTA MEGFORDÍTVA: a U-0.4-ben itt még `== 1` állt, mert a stratégia a
    # friss jelre belépett. Az U-2A audit kimutatta, hogy ez az út a teljes
    # Uranus végrehajtási kapuláncot megkerüli (EXECUTION_ENABLED, log-only,
    # freshness gate, guardrail, kill switch), ezért az U-2B Patch D
    # fail-closed módon megszüntette. A frissesség-logika tesztje megmaradt,
    # csak már nem a belépési kimeneten keresztül (lásd `test_m4_*`).
    first = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert int(first["enter_long"].iloc[-1]) == 0

    second = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert int(second["enter_long"].iloc[-1]) == 0

    # A stratégia a jelet nem is jelöli feldolgozottnak többé.
    assert not os.path.exists(strat.EXECUTOR_STATE_PATH)


def test_m2_epoch_signal_ts_is_parsed_but_never_enters(strategy_mod, tmp_path):
    """
    Az epoch-formátumú jel-időbélyeg értelmezése változatlanul működik –
    de U-2B óta egyetlen jelformátum sem vezet belépéshez.
    """
    import time as _t

    now = int(_t.time())
    assert strategy_mod.parse_signal_ts(now) == float(now)
    assert strategy_mod.signal_is_fresh(now, max_age=300)[0] is True

    strat = _make_strategy(strategy_mod, tmp_path, _signal_state(now))
    out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert int(out["enter_long"].iloc[-1]) == 0


def test_m3_exit_trend_still_generates_no_independent_sell(strategy_mod, tmp_path):
    """A kanonikus szerződés változatlan: a strategy nem ad önálló SELL jelet."""
    strat = _make_strategy(strategy_mod, tmp_path, _signal_state("2026-08-24T21:00:00Z", action="SELL"))
    out = strat.populate_exit_trend(_df(), {"pair": "XRP/USDC"})
    assert int(out["exit_long"].iloc[-1]) == 0


def test_m4_signal_freshness_helpers(strategy_mod):
    import time as _t

    ok, _ = strategy_mod.signal_is_fresh(_t.time(), max_age=300)
    assert ok is True

    stale, reason = strategy_mod.signal_is_fresh(_t.time() - 4000, max_age=300)
    assert stale is False and reason.startswith("SIGNAL_STALE")

    invalid, reason = strategy_mod.signal_is_fresh("AUTO")
    assert invalid is False and reason == "SIGNAL_TS_MISSING_OR_INVALID"


# =========================================================================== #
# runtime_freshness egységtesztek
# =========================================================================== #

@pytest.mark.parametrize("raw,expected_kind", [
    (1_800_000_000, "num"), (1_800_000_000_000, "num"), ("1800000000", "num"),
    ("2026-08-24T21:00:00Z", "num"), ("2026-08-24T21:00:00+00:00", "num"),
    ("2026-08-24T21:00:00", "num"),
])
def test_parse_ts_accepts_supported_forms(fresh, raw, expected_kind):
    assert isinstance(fresh.parse_ts(raw), float)


@pytest.mark.parametrize("raw", [None, "", "AUTO", "nem-datum", True, 0, -5, {}])
def test_parse_ts_rejects_invalid(fresh, raw):
    assert fresh.parse_ts(raw) is None


def test_derived_threshold_is_timeframe_driven(fresh, monkeypatch):
    """
    Senior review 1.: a küszöb a timeframe-ből származik, nem fix 900 s.
    1m-en egy 15 perces gyertya már törött adatfolyam, tehát tiltandó.
    """
    monkeypatch.delenv("MARKET_DATA_MAX_AGE_SEC", raising=False)

    fresh.mark_market_fetch_ok(fresh.utc_iso(), "1m")
    assert fresh.effective_max_age_sec() == pytest.approx(180.0)   # 1m * 3

    fresh.mark_market_fetch_ok(fresh.utc_iso(), "4h")
    assert fresh.effective_max_age_sec() == pytest.approx(43200.0)  # 4h * 3

    fresh.mark_market_fetch_ok(fresh.utc_iso(), None)
    assert fresh.effective_max_age_sec() == pytest.approx(300.0)    # ismeretlen tf


def test_timeframe_floor_prevents_false_stale(fresh, monkeypatch):
    """Örökölt kis override nagy timeframe-en nem tilthat hamisan."""
    monkeypatch.setenv("MARKET_DATA_MAX_AGE_SEC", "120")
    fresh.mark_market_fetch_ok(fresh.utc_iso(), "4h")
    assert fresh.effective_max_age_sec() >= 4 * 3600 * 2


def test_15_minute_old_1m_candle_is_stale(fresh, monkeypatch):
    """Senior review 1.: a korábbi 900 s-os default ezt átengedte volna."""
    monkeypatch.delenv("MARKET_DATA_MAX_AGE_SEC", raising=False)
    import time as _t

    fresh.mark_market_fetch_ok(_t.time() - 900, "1m")
    fresh.mark_reconciled()

    ok, reason = fresh.execution_gate_status()
    assert ok is False and reason == "MARKET_DATA_STALE"


@pytest.mark.parametrize("bad_value", ["0", "-1", "nem-szam", ""])
def test_invalid_max_age_cannot_disable_the_gate(fresh, monkeypatch, bad_value):
    """
    Senior review 2.: NO LIVE EXECUTION WITH UNBOUNDED MARKET STALENESS.
    Érvénytelen vagy <=0 konfiguráció NEM kapcsolhatja ki a kor-ellenőrzést;
    ilyenkor a biztonságos származtatott alapértelmezés lép életbe.
    """
    monkeypatch.setenv("MARKET_DATA_MAX_AGE_SEC", bad_value)
    import time as _t

    assert fresh.configured_max_age_sec() is None
    assert fresh.effective_max_age_sec() > 0

    fresh.mark_market_fetch_ok(_t.time() - 86_400, "1m")   # egy napos gyertya
    fresh.mark_reconciled()

    ok, reason = fresh.execution_gate_status()
    assert ok is False and reason == "MARKET_DATA_STALE"


def test_invalid_max_age_still_requires_valid_timestamp(fresh, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_MAX_AGE_SEC", "0")
    fresh.mark_market_fetch_ok("ervenytelen", "1m")
    fresh.mark_reconciled()

    ok, reason = fresh.execution_gate_status()
    assert ok is False and reason == "MARKET_DATA_TIMESTAMP_INVALID"


def test_review2_disabled_age_check_still_blocks_real_order(tr, fresh, monkeypatch, exec_on, no_real_orders):
    """
    KÖTELEZŐ review-teszt:
    EXECUTION_ENABLED=1 + MARKET_DATA_MAX_AGE_SEC=0 + stale candle -> NO force_enter.
    """
    monkeypatch.setenv("MARKET_DATA_MAX_AGE_SEC", "0")
    import time as _t

    fresh.mark_market_fetch_ok(_t.time() - 86_400, "1m")
    fresh.mark_reconciled()

    state = {"pair": "XRP/USDC", "in_position": False}
    out, executed = tr.maybe_execute_via_api(state, buy_decision(tr))

    assert executed is False
    assert state["execution"]["last_result"]["detail"] == "freshness_block:MARKET_DATA_STALE"
