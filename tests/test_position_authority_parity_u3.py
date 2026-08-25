"""
U-3 pre-commit review – OFF vs SHADOW mód paritás + rétegzési határ.

Miért kötelező ez
-----------------
A rollout alapértelmezése ``URANUS_POSITION_AUTHORITY_MODE=shadow``. A shadow
bevezetés csak akkor biztonságos, ha **semmilyen rejtett viselkedésváltozást**
nem hoz: a legacy pozíció-sync, a döntés, a végrehajtási jogosultság és a
startup-horgonyzás bit-azonos marad, és kizárólag az új ``state["position"]``
evidence-blokk épül fel mellette.

Fontos, kimondandó következmény: **shadow módban az eredeti fail-open hiba még
ÉL.** A shadow nem javít, csak megfigyel – a javítás a ``live`` módban lép
életbe. Ezt a C) eset explicit módon rögzíti.

Biztonsági garanciák: nincs valódi hálózat, order, systemctl vagy production
state-írás – minden I/O mockolt, a state tmp_path alatt él.
"""
from __future__ import annotations

import copy
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
# Fixture-ök és segédek
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def pa():
    import position_authority
    return position_authority


@pytest.fixture(scope="module")
def ps():
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
def _clean(monkeypatch, fresh):
    for key in (
        "URANUS_POSITION_AUTHORITY_MODE", "FT_URL", "URANUS_FT_URL",
        "FT_USERNAME", "FT_PASSWORD", "EXECUTION_ENABLED", "EXECUTION_LOG_ONLY",
        "SHADOW_ENABLED", "KILL_SWITCH",
    ):
        monkeypatch.delenv(key, raising=False)
    fresh.reset_process_state()
    yield
    fresh.reset_process_state()


class _Resp:
    def __init__(self, body: str, status: int = 200):
        self._body = body.encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


#: Felső szintű blokkok, amelyekre a paritás szándékosan nem értelmezhető.
VOLATILE_TOP = (
    "position",          # az ÚJ blokk – szándékosan csak shadow/live módban épül
    "execution",         # végrehajtási diagnosztika; külön, célzott teszt fedi (E)
)

#: Faliórától függő mezőnevek. Két külön ``run_once()`` hívás óhatatlanul
#: átléphet egy másodperchatárt, ezért ezeket REKURZÍVAN kiszűrjük – a
#: kézzel felsorolt lista konstrukció szerint hiányos volt (a hibát a
#: ``market.updated_at`` és a ``last_decision`` beágyazott időbélyegei
#: mutatták meg egy intermittáló bukásban).
_WALL_CLOCK_KEYS = ("ts", "time", "timestamp", "updated_at", "updated_utc", "checked_at")
_WALL_CLOCK_SUFFIXES = ("_ts", "_at", "_utc", "_time", "_until")


def _is_wall_clock(key: str) -> bool:
    if key in _WALL_CLOCK_KEYS:
        return True
    return any(key.endswith(suffix) for suffix in _WALL_CLOCK_SUFFIXES)


def _scrub(value):
    """Rekurzívan eltávolít minden falióra-jellegű mezőt."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _is_wall_clock(k)}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def normalize(state: dict) -> dict:
    """A legacy, végrehajtás-releváns state az idő-/futásfüggő mezők nélkül."""
    out = copy.deepcopy(state)
    for key in VOLATILE_TOP:
        out.pop(key, None)
    return _scrub(out)


BASE_STATE = {
    "pair": "XRP/USDC",
    "timeframe": "1m",
    "in_position": False,
    "base": 1.50,
    "base_price": 1.50,
    "_flat_anchor": 1.50,
    "trough": 1.49,
    "peak": None,
    "cycle": {"recovery_mode": False},
}

OPEN_STATE = {
    "pair": "XRP/USDC",
    "timeframe": "1m",
    "in_position": True,
    "active_trade_id": 42,
    "base": 1.40,
    "base_price": 1.40,
    "entry_price": 1.40,
    "peak": 1.45,
    "trough": None,
    "_flat_anchor": None,
    "cycle": {"recovery_mode": False},
}

TRADE = {
    "trade_id": 42, "pair": "XRP/USDC", "open_rate": 1.40, "amount": 10.0,
    "open_timestamp": 1787600000000, "max_rate": 1.45, "stake_amount": 14.0,
    "is_open": True,
}


def run_tick(tr, ps, fresh, monkeypatch, tmp_path, *, mode, initial, ft_body, ft_exc=None):
    """
    Egy teljes ``run_once()`` futtatása adott módban, azonos külvilággal.

    Ugyanaz a Freqtrade-valóság kerül a LEGACY sync és az AUTHORITY elé is:
    a ``tick_runner.urlopen`` és a ``position_source.urlopen`` egyaránt mockolt.
    """
    fresh.reset_process_state()
    state_file = tmp_path / f"state_{mode}.json"
    state_file.write_text(json.dumps(initial), encoding="utf-8")

    monkeypatch.setattr(tr, "STATE_PATH", str(state_file))
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", mode)
    monkeypatch.setenv("SHADOW_ENABLED", "0")

    def fake_urlopen(req, timeout=None):
        if ft_exc is not None:
            raise ft_exc
        return _Resp(ft_body)

    monkeypatch.setattr(tr, "urlopen", fake_urlopen)
    monkeypatch.setattr(ps, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        tr, "fetch_candles",
        lambda pair, timeframe, limit=None: [
            {"date": "2026-08-25T07:00:00Z", "open": 1.52, "high": 1.53,
             "low": 1.51, "close": 1.52, "volume": 100.0} for _ in range(40)
        ],
    )

    ok, last, decision = tr.run_once()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    return ok, decision, saved


@pytest.fixture
def no_real_orders(monkeypatch):
    class _Poison:
        def __init__(self, *a, **k):
            raise AssertionError("valódi order-hívás történt volna")

    fake = types.ModuleType("freqtrade_executor")
    fake.FreqtradeExecutor = _Poison
    monkeypatch.setitem(sys.modules, "freqtrade_executor", fake)
    return fake


# =========================================================================== #
# 1 · RÉTEGZÉSI HATÁR (complexity split ellenőrzése)
# =========================================================================== #

def test_layering_authority_has_no_transport_or_credentials():
    """Az authority réteg mellékhatás- és titokmentes: nincs benne hálózat."""
    src = open(os.path.join(APP_DIR, "position_authority.py"), encoding="utf-8").read()
    for forbidden in ("urlopen", "urllib", "base64", "Request(", "HTTPError", "URLError",
                      'getenv("FT_PASSWORD"', 'getenv("FT_USERNAME"'):
        assert forbidden not in src, f"transport/credential szivárgás az authorityben: {forbidden}"


def _imported_modules(filename: str) -> set[str]:
    """A fájl tényleges import-jai (AST alapján, nem szövegkeresés)."""
    import ast

    tree = ast.parse(open(os.path.join(APP_DIR, filename), encoding="utf-8").read())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_layering_dependency_is_one_way():
    """
    ``authority -> source``, fordítva soha.

    Az ellenőrzés AST-alapú: a ``position_source`` docstringje szándékosan
    MEGNEVEZI a határ másik oldalát, ezért a puszta szövegkeresés hamis
    riasztást adna – a lényeg az, hogy ne IMPORTÁLJA.
    """
    assert "position_source" in _imported_modules("position_authority.py")
    assert "position_authority" not in _imported_modules("position_source.py")


def test_layering_source_imports_no_app_state_modules():
    """A transport réteg nem függ a runner/state világtól."""
    imported = _imported_modules("position_source.py")
    for forbidden in ("tick_runner", "state_schema", "rule_engine", "shadow_position"):
        assert forbidden not in imported


def test_layering_source_has_no_state_persistence():
    """A transport réteg nem ír state-et és nem ismer végrehajtási politikát."""
    src = open(os.path.join(APP_DIR, "position_source.py"), encoding="utf-8").read()
    for forbidden in ('state["', "state.get(", "execution_allowed", "apply_to_state",
                      "in_position", "last_verified"):
        assert forbidden not in src, f"policy/persistence szivárgás a transportban: {forbidden}"


def test_classify_is_pure(ps):
    """A besorolás tiszta függvény: azonos bemenet -> azonos kimenet."""
    trades = ({"trade_id": 1, "pair": "XRP/USDC", "open_rate": 1.5, "amount": 2.0},)
    a = ps.classify(trades, "XRP/USDC", 200, 1000.0)
    b = ps.classify(trades, "XRP/USDC", 200, 1000.0)
    assert a == b
    assert a.status == ps.STATE_OPEN


# =========================================================================== #
# 2 · OFF vs SHADOW PARITÁS
# =========================================================================== #

def test_a_parity_verified_flat(tr, ps, fresh, monkeypatch, tmp_path, no_real_orders):
    """A) FT verified FLAT: off és shadow legacy state-je azonos."""
    body = "[]"
    ok_off, dec_off, s_off = run_tick(
        tr, ps, fresh, monkeypatch, tmp_path, mode="off", initial=BASE_STATE, ft_body=body)
    ok_sh, dec_sh, s_sh = run_tick(
        tr, ps, fresh, monkeypatch, tmp_path, mode="shadow", initial=BASE_STATE, ft_body=body)

    assert ok_off is ok_sh is True
    assert normalize(s_off) == normalize(s_sh)
    assert s_off["in_position"] == s_sh["in_position"] is False
    # ...és CSAK az új blokk épült shadow-ban:
    assert "position" not in s_off
    assert s_sh["position"]["authority_state"] == "FLAT"


def test_b_parity_verified_open(tr, ps, fresh, monkeypatch, tmp_path, no_real_orders):
    """B) FT verified OPEN: off és shadow legacy state-je azonos."""
    body = json.dumps([TRADE])
    ok_off, dec_off, s_off = run_tick(
        tr, ps, fresh, monkeypatch, tmp_path, mode="off", initial=OPEN_STATE, ft_body=body)
    ok_sh, dec_sh, s_sh = run_tick(
        tr, ps, fresh, monkeypatch, tmp_path, mode="shadow", initial=OPEN_STATE, ft_body=body)

    assert normalize(s_off) == normalize(s_sh)
    assert s_off["in_position"] == s_sh["in_position"] is True
    assert s_off["active_trade_id"] == s_sh["active_trade_id"] == 42
    assert s_off["base"] == s_sh["base"] == 1.40
    assert "position" not in s_off
    assert s_sh["position"]["authority_state"] == "OPEN"
    assert s_sh["position"]["trade"]["trade_id"] == 42


def test_c_parity_fetch_failure_shadow_changes_nothing_yet(
    tr, ps, fresh, monkeypatch, tmp_path, no_real_orders
):
    """
    C) FT fetch failure: a production viselkedés shadow-ban MÉG NEM változik.

    Ez a legfontosabb paritás-eset, és egyben a legfontosabb figyelmeztetés:
    shadow módban az eredeti fail-open hiba MÉG ÉL – a legacy sync továbbra is
    ``in_position=False``-t ír. A shadow nem javít, csak láthatóvá teszi az
    UNKNOWN-t az új blokkban. A javítás a ``live`` módban lép életbe.
    """
    from urllib.error import URLError
    exc = URLError(ConnectionRefusedError("refused"))

    ok_off, dec_off, s_off = run_tick(
        tr, ps, fresh, monkeypatch, tmp_path, mode="off",
        initial=OPEN_STATE, ft_body="[]", ft_exc=exc)
    ok_sh, dec_sh, s_sh = run_tick(
        tr, ps, fresh, monkeypatch, tmp_path, mode="shadow",
        initial=OPEN_STATE, ft_body="[]", ft_exc=exc)

    # A legacy viselkedés bit-azonos a két módban.
    assert normalize(s_off) == normalize(s_sh)

    # És igen: shadow-ban is megtörténik a régi (hibás) FLAT-re billenés.
    assert s_off["in_position"] is False
    assert s_sh["in_position"] is False

    # Az egyetlen különbség: az új blokk kimondja, hogy valójában UNKNOWN.
    assert "position" not in s_off
    assert s_sh["position"]["authority_state"] == "UNKNOWN"
    assert s_sh["position"]["last_error"] == "NETWORK_ERROR"


def test_c2_live_mode_actually_fixes_it(tr, ps, fresh, monkeypatch, tmp_path, no_real_orders):
    """Kontraszt a C)-hez: ugyanaz a hiba live módban MÁR nem billent FLAT-re."""
    from urllib.error import URLError
    exc = URLError(ConnectionRefusedError("refused"))

    initial = dict(OPEN_STATE)
    initial["position"] = {
        "authority_state": "OPEN",
        "last_verified": {"state": "OPEN", "verified_at": 1787600000,
                          "trade": {"trade_id": 42}},
    }
    ok, dec, s_live = run_tick(
        tr, ps, fresh, monkeypatch, tmp_path, mode="live",
        initial=initial, ft_body="[]", ft_exc=exc)

    assert s_live["position"]["authority_state"] == "UNKNOWN"
    assert s_live["in_position"] is True      # <-- a javítás
    assert s_live["base"] == 1.40


def test_d_parity_decision_output(tr, ps, fresh, monkeypatch, tmp_path, no_real_orders):
    """D) azonos market + state mellett off és shadow ugyanazt a döntést adja."""
    for initial, body in ((BASE_STATE, "[]"), (OPEN_STATE, json.dumps([TRADE]))):
        _o, dec_off, _s = run_tick(
            tr, ps, fresh, monkeypatch, tmp_path, mode="off", initial=initial, ft_body=body)
        _o, dec_sh, _s = run_tick(
            tr, ps, fresh, monkeypatch, tmp_path, mode="shadow", initial=initial, ft_body=body)

        assert dec_off.get("action") == dec_sh.get("action")
        assert dec_off.get("rule") == dec_sh.get("rule")
        assert dec_off.get("reason") == dec_sh.get("reason")
        assert dec_off.get("level") == dec_sh.get("level")


def test_e_parity_execution_eligibility(tr, ps, fresh, monkeypatch, tmp_path, no_real_orders):
    """
    E) végrehajtási jogosultság paritás.

    A pozíció-kapu kizárólag ``live`` módban enforce-olható, ezért off és shadow
    között a végrehajtási eredménynek azonosnak kell lennie.
    """
    from urllib.error import URLError
    exc = URLError(ConnectionRefusedError("refused"))
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_LOG_ONLY", "1")   # log-only: nincs API-hívás

    results = {}
    for mode in ("off", "shadow"):
        _o, _d, saved = run_tick(
            tr, ps, fresh, monkeypatch, tmp_path, mode=mode,
            initial=OPEN_STATE, ft_body="[]", ft_exc=exc)
        results[mode] = saved.get("execution", {}).get("last_result", {}).get("detail")

    assert results["off"] == results["shadow"]
    assert results["shadow"] != "position_block:AUTHORITY_UNKNOWN"


def test_e2_shadow_writes_no_position_gate_evidence(tr, pa, fresh, monkeypatch):
    """Shadow módban a végrehajtási ág nem tesz be pozíció-kapu bizonyítékot."""
    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    monkeypatch.setenv("EXECUTION_LOG_ONLY", "0")
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", "shadow")
    fresh.mark_market_fetch_ok(__import__("time").time(), "1m")
    fresh.mark_reconciled()

    import tick_runner as t
    state = {"pair": "XRP/USDC", "position": {"authority_state": pa.STATE_UNKNOWN}}
    decision = t.normalize_decision(action="BUY", rule="BUY_STANDARD", reason="x", level="none")

    class _Rec:
        def __init__(self, *a, **k):
            pass

        def force_enter(self):
            return types.SimpleNamespace(ok=True, http_status=200, detail="mock", response={})

        def confirm_open_trades(self):
            return types.SimpleNamespace(ok=True, http_status=200, detail="mock", response={})

    fake = types.ModuleType("freqtrade_executor")
    fake.FreqtradeExecutor = _Rec
    monkeypatch.setitem(sys.modules, "freqtrade_executor", fake)

    _out, executed = t.maybe_execute_via_api(state, decision)
    assert executed is True
    assert "position_authority" not in state["execution"]


def test_f_parity_startup_anchor_behaviour(tr, ps, fresh, monkeypatch, tmp_path, no_real_orders):
    """A startup-horgonyzás viselkedése off és shadow között azonos."""
    for initial, body in ((BASE_STATE, "[]"), (OPEN_STATE, json.dumps([TRADE]))):
        _o, _d, s_off = run_tick(
            tr, ps, fresh, monkeypatch, tmp_path, mode="off", initial=initial, ft_body=body)
        _o, _d, s_sh = run_tick(
            tr, ps, fresh, monkeypatch, tmp_path, mode="shadow", initial=initial, ft_body=body)

        assert s_off["startup_reconcile"]["mode"] == s_sh["startup_reconcile"]["mode"]
        assert s_off["base"] == s_sh["base"]
        assert s_off["_flat_anchor"] == s_sh["_flat_anchor"]


# =========================================================================== #
# 3 · MODE CONFIG SAFETY
# =========================================================================== #

@pytest.mark.parametrize("raw", [None, "", "   ", "LIVE_", "liv", "l i v e", "1", "true",
                                 "on", "enabled", "Live!", "shadow ", "OFF!"])
def test_invalid_or_missing_mode_never_activates_live(pa, monkeypatch, raw):
    """Se hiányzó, se elgépelt, se érvénytelen env nem aktiválhat live módot."""
    if raw is None:
        monkeypatch.delenv("URANUS_POSITION_AUTHORITY_MODE", raising=False)
    else:
        monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", raw)

    mode = pa.get_mode()
    assert mode != "live", f"{raw!r} live módot aktivált"
    assert mode == "shadow"


@pytest.mark.parametrize("raw,expected", [
    ("live", "live"), ("LIVE", "live"), ("  live  ", "live"),
    ("off", "off"), ("OFF", "off"), ("shadow", "shadow"),
])
def test_valid_modes_are_accepted_case_insensitively(pa, monkeypatch, raw, expected):
    """A live mód KIZÁRÓLAG explicit, pontos értékkel érhető el."""
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", raw)
    assert pa.get_mode() == expected


def test_default_mode_is_shadow(pa, monkeypatch):
    monkeypatch.delenv("URANUS_POSITION_AUTHORITY_MODE", raising=False)
    assert pa.get_mode() == "shadow"


def test_off_mode_leaves_state_bit_identical(pa, monkeypatch):
    monkeypatch.setenv("URANUS_POSITION_AUTHORITY_MODE", "off")
    state = {"pair": "XRP/USDC", "in_position": True, "base": 1.4}
    before = copy.deepcopy(state)
    assert pa.authority_tick(state, "XRP/USDC") is None
    assert state == before
