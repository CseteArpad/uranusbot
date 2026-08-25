"""
U-2B – Safety Baseline Hardening regressziós csomag.

Négy, az U-2A production auditban bizonyított hibát zár le:

  A. UI trading-settings default fail-open  -> app.py / execution_policy.py
  B. két Freqtrade endpoint egy processzben -> ft_endpoint.py
  C. runner indulhat a Freqtrade előtt      -> deploy/systemd drop-in spec
  D. stratégiai BUY bypass a kapulánc mellett -> SaturnusExecutor.py

Egyetlen teszt sem küld ordert, nem hív systemctl-t, nem ír production state-et,
nem használ valódi credentialt és nem ér el valódi exchange-et vagy Freqtrade-et.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "app")
APP_PY = os.path.join(APP_DIR, "app.py")
STRATEGY_PATH = os.path.join(
    REPO_ROOT, "freqtrade", "user_data", "strategies", "SaturnusExecutor.py"
)

# Az app/ könyvtárat csak HOZZÁFŰZZÜK (a U-0.1 tesztek bevált mintája szerint),
# hogy a repo gyökere elöl maradjon és az `app` továbbra is a csomagot jelentse.
if APP_DIR not in sys.path:
    sys.path.append(APP_DIR)


# --------------------------------------------------------------------------- #
# Közös fixture-ök
# --------------------------------------------------------------------------- #

#: Azok az env-kulcsok, amelyek elszivároghatnának a fejlesztői gépről.
_EXEC_ENV_KEYS = (
    "EXECUTION_ENABLED",
    "EXECUTION_LOG_ONLY",
    "EXECUTION_CONFIRM",
    "FT_URL",
    "URANUS_FT_URL",
    "URANUS_FT_CONFIG",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Minden teszt üres, kiszámítható env-vel indul."""
    for key in _EXEC_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(scope="module")
def web_app_mod():
    """
    Az ``app/app.py`` betöltése egyedi modulnévvel.

    Sima ``import app`` a repo gyökeréből az ``app/`` CSOMAGOT találná meg,
    nem a Flask-modult – ezért használjuk ugyanazt az importlib-mintát, mint a
    U-0.1 biztonsági tesztek.
    """
    spec = importlib.util.spec_from_file_location("u2b_web_app_undertest", APP_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


@pytest.fixture
def policy():
    import execution_policy
    return execution_policy


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    import ft_endpoint
    # Alapból NINCS Freqtrade config: így a tesztek nem a repo (esetleg
    # hiányzó) config.json-jától függenek.
    monkeypatch.setenv("URANUS_FT_CONFIG", str(tmp_path / "nincs-ilyen.json"))
    return ft_endpoint


def _write_ft_config(tmp_path, port, *, host="127.0.0.1", enabled=True):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "api_server": {
                    "enabled": enabled,
                    "listen_ip_address": host,
                    "listen_port": port,
                }
            }
        ),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def strategy_mod():
    """A Freqtrade stratégia betöltése az IStrategy függőség kiváltásával."""
    pytest.importorskip("pandas")

    import types

    if "freqtrade" not in sys.modules:
        ft_pkg = types.ModuleType("freqtrade")
        ft_strategy = types.ModuleType("freqtrade.strategy")

        class _IStrategy:  # minimal stub
            pass

        ft_strategy.IStrategy = _IStrategy
        ft_pkg.strategy = ft_strategy
        sys.modules["freqtrade"] = ft_pkg
        sys.modules["freqtrade.strategy"] = ft_strategy

    spec = importlib.util.spec_from_file_location("u2b_saturnus_executor", STRATEGY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# =========================================================================== #
# PATCH A – SAFE TRADING SETTINGS DEFAULT
# =========================================================================== #

def test_a0_canonical_defaults_are_fail_safe(policy):
    """A kanonikus hármas: tiltott / csak-napló / megerősítés bekapcsolva."""
    assert policy.EXECUTION_ENABLED_DEFAULT is False
    assert policy.EXECUTION_LOG_ONLY_DEFAULT is True
    assert policy.EXECUTION_CONFIRM_DEFAULT is True


def test_a1_missing_execution_env_yields_disabled(web_app_mod, monkeypatch, tmp_path):
    """A1: nincs execution env + a drop-in sem rendelkezik -> tiltott."""
    dropin = tmp_path / "30-canonical-env.conf"
    dropin.write_text(
        "[Service]\n"
        "Environment=PAIR=XRP/USDC\n"
        "Environment=FT_URL=http://127.0.0.1:8090\n",
        encoding="utf-8",
    )

    app_mod = web_app_mod
    monkeypatch.setattr(app_mod, "SETTINGS_ENV_FILE", str(dropin))

    cfg = app_mod.read_trading_settings()
    assert cfg["execution_enabled"] is False
    assert cfg["execution_log_only"] is True


def test_a1b_missing_dropin_file_yields_disabled(web_app_mod, monkeypatch, tmp_path):
    """Nem létező drop-in sem válhat engedélyező alapértelmezéssé."""
    app_mod = web_app_mod
    monkeypatch.setattr(app_mod, "SETTINGS_ENV_FILE", str(tmp_path / "nincs.conf"))

    cfg = app_mod.read_trading_settings()
    assert cfg["execution_enabled"] is False
    assert cfg["execution_log_only"] is True


def test_a2_get_then_post_roundtrip_cannot_enable(web_app_mod, monkeypatch, tmp_path):
    """
    A2: a UI GET-tel lekért objektum visszaküldve NEM válhat engedélyezetté.

    Ez a production forgatókönyv: az operátor megnyitja a beállítás-oldalt,
    átír egy tetszőleges mezőt, és ment.
    """
    dropin = tmp_path / "30-canonical-env.conf"
    dropin.write_text("[Service]\nEnvironment=PAIR=XRP/USDC\n", encoding="utf-8")

    app_mod = web_app_mod
    monkeypatch.setattr(app_mod, "SETTINGS_ENV_FILE", str(dropin))

    roundtrip = app_mod.read_trading_settings()
    text = app_mod.build_settings_dropin(roundtrip)

    assert "Environment=EXECUTION_ENABLED=0" in text
    assert "Environment=EXECUTION_LOG_ONLY=1" in text
    assert "Environment=EXECUTION_ENABLED=1" not in text


def test_a3_explicit_true_only_from_explicit_input(policy):
    """A3: True KIZÁRÓLAG explicit igaz bemenetből keletkezhet."""
    assert policy.coerce_bool(True, False) is True
    assert policy.coerce_bool("1", False) is True
    assert policy.coerce_bool("true", False) is True
    assert policy.coerce_bool("on", False) is True

    # És semmi másból:
    for falsy in (None, "", "0", "false", "no", "off", 0, [], {}, "ture", "igen"):
        assert policy.coerce_bool(falsy, False) is False, falsy


def test_a4_malformed_boolean_is_fail_safe(policy):
    """A4: hibás bool -> a tiltó alapértelmezés, nem a megengedő."""
    assert policy.coerce_bool("maybe", policy.EXECUTION_ENABLED_DEFAULT) is False
    assert policy.coerce_bool(None, policy.EXECUTION_ENABLED_DEFAULT) is False
    # log_only ellenkező irányban fail-safe: hiányzó érték -> marad True
    assert policy.coerce_bool(None, policy.EXECUTION_LOG_ONLY_DEFAULT) is True


def test_a4b_post_payload_string_false_does_not_enable(policy):
    """A `bool("false")` csapda: a régi kód igaznak látta volna."""
    assert bool("false") is True  # a csapda dokumentálása
    assert policy.coerce_bool("false", policy.EXECUTION_ENABLED_DEFAULT) is False


def test_a5_explicit_dropin_true_is_still_honoured(web_app_mod, monkeypatch, tmp_path):
    """A javítás nem veszi el az operátor explicit döntését."""
    dropin = tmp_path / "30-canonical-env.conf"
    dropin.write_text(
        "[Service]\nEnvironment=EXECUTION_ENABLED=1\nEnvironment=EXECUTION_LOG_ONLY=0\n",
        encoding="utf-8",
    )

    app_mod = web_app_mod
    monkeypatch.setattr(app_mod, "SETTINGS_ENV_FILE", str(dropin))

    cfg = app_mod.read_trading_settings()
    assert cfg["execution_enabled"] is True
    assert cfg["execution_log_only"] is False


def test_a6_runner_and_ui_share_one_default_source(policy, monkeypatch):
    """A runner effektív flagjei ugyanabból a modulból jönnek."""
    import tick_runner

    monkeypatch.delenv("EXECUTION_ENABLED", raising=False)
    monkeypatch.delenv("EXECUTION_LOG_ONLY", raising=False)
    monkeypatch.delenv("EXECUTION_CONFIRM", raising=False)

    assert tick_runner._runtime_exec_flags() == (False, True, True)

    ui_defaults = policy.safe_settings_defaults()
    assert ui_defaults["execution_enabled"] is False
    assert ui_defaults["execution_log_only"] is True


def test_a7_typo_in_env_does_not_enable_execution(monkeypatch):
    """Elgépelt env-érték nem kapcsolhat be éles végrehajtást."""
    import tick_runner

    monkeypatch.setenv("EXECUTION_ENABLED", "ture")
    assert tick_runner._runtime_exec_flags()[0] is False

    monkeypatch.setenv("EXECUTION_ENABLED", "1")
    assert tick_runner._runtime_exec_flags()[0] is True


# =========================================================================== #
# PATCH B – SINGLE FREQTRADE URL AUTHORITY
# =========================================================================== #

def test_b1_default_ft_endpoint_is_8090(endpoint):
    """B1: konfiguráció hiányában a kanonikus 8090 jön."""
    assert endpoint.canonical_ft_url() == "http://127.0.0.1:8090"
    assert endpoint.CANONICAL_FT_URL == "http://127.0.0.1:8090"


def test_b2_legacy_8017_is_never_used_implicitly(endpoint, monkeypatch):
    """B2: a Neptunus 8017 sem env-ből, sem configból nem szivároghat be."""
    monkeypatch.setenv("FT_URL", "http://127.0.0.1:8017")

    url, source, rejected = endpoint.resolve(explain=True)
    assert url == "http://127.0.0.1:8090"
    assert source == "canonical_default"
    assert any("foreign_endpoint" in reason for _, _, reason in rejected)


def test_b2b_own_ui_port_is_also_rejected(endpoint, monkeypatch):
    """A saját UI portja (8016) sem Freqtrade – szintén idegen ebben a szerepben."""
    monkeypatch.setenv("FT_URL", "http://127.0.0.1:8016")
    assert endpoint.canonical_ft_url() == "http://127.0.0.1:8090"


def test_b2c_foreign_check_is_loopback_only(endpoint):
    """Távoli hostnál a portszám önmagában nem jelent idegen szolgáltatást."""
    assert endpoint.is_foreign_endpoint("http://127.0.0.1:8017") is True
    assert endpoint.is_foreign_endpoint("http://localhost:8017") is True
    assert endpoint.is_foreign_endpoint("http://10.0.0.5:8017") is False
    assert endpoint.is_foreign_endpoint("http://127.0.0.1:8090") is False


def test_b3_same_process_clients_agree(monkeypatch, tmp_path):
    """
    B3: a UI processz két FT-fogyasztója ugyanazt a címet kapja.

    Pontosan ez tört el a production auditban: a `freqtrade_ui` a 8017-et, az
    `app.py` nézetépítője a 8090-et használta ugyanabban a processzben.
    """
    cfg_path = _write_ft_config(tmp_path, 8090)
    monkeypatch.setenv("URANUS_FT_CONFIG", cfg_path)
    monkeypatch.setenv("FT_URL", "http://127.0.0.1:8017")  # a production hiba

    import ft_endpoint
    import freqtrade_ui

    assert freqtrade_ui.ft_base_url() == "http://127.0.0.1:8090"
    assert ft_endpoint.canonical_ft_url() == "http://127.0.0.1:8090"
    assert freqtrade_ui.ft_base_url() == ft_endpoint.canonical_ft_url()


def test_b4_freqtrade_config_beats_shared_env(endpoint, monkeypatch, tmp_path):
    """Precedencia: az Uranus saját FT configja erősebb a megosztott env-nél."""
    monkeypatch.setenv("URANUS_FT_CONFIG", _write_ft_config(tmp_path, 8091))
    monkeypatch.setenv("FT_URL", "http://127.0.0.1:8099")

    url, source, _ = endpoint.resolve(explain=True)
    assert url == "http://127.0.0.1:8091"
    assert source == "freqtrade_config"


def test_b5_explicit_uranus_override_wins(endpoint, monkeypatch, tmp_path):
    """A kifejezetten Uranus-specifikus felülbírálás a legerősebb."""
    monkeypatch.setenv("URANUS_FT_CONFIG", _write_ft_config(tmp_path, 8091))
    monkeypatch.setenv("URANUS_FT_URL", "http://127.0.0.1:8092")

    url, source, _ = endpoint.resolve(explain=True)
    assert url == "http://127.0.0.1:8092"
    assert source == "URANUS_FT_URL"


def test_b6_shared_env_used_only_without_config(endpoint, monkeypatch):
    """FT config hiányában a megosztott env még használható – ha nem idegen."""
    monkeypatch.setenv("FT_URL", "http://127.0.0.1:8099")
    url, source, _ = endpoint.resolve(explain=True)
    assert url == "http://127.0.0.1:8099"
    assert source == "FT_URL"


@pytest.mark.parametrize(
    "bad", ["", "   ", "nem-url", "ftp://127.0.0.1:8090", "http://", "http://:8090"]
)
def test_b7_invalid_urls_fall_back_to_canonical(endpoint, monkeypatch, bad):
    monkeypatch.setenv("FT_URL", bad)
    assert endpoint.canonical_ft_url() == "http://127.0.0.1:8090"


def test_b8_no_module_level_ft_url_binding():
    """
    Strukturális garancia: a UI FT-kliens nem fagyaszthatja be importáláskor a
    címet – ez tette a production hibát észrevehetetlenné.
    """
    src = open(os.path.join(APP_DIR, "freqtrade_ui.py"), encoding="utf-8").read()
    assert 'FT_URL = os.getenv("FT_URL"' not in src
    assert "ft_endpoint" in src


def test_b9_app_view_does_not_take_endpoint_from_state():
    """A state.json nem lehet konfigurációs authority az FT-címre."""
    src = open(os.path.join(APP_DIR, "app.py"), encoding="utf-8").read()
    assert 'state.get("ft") or {}).get("url")' not in src
    assert 'ft_endpoint.canonical_ft_url()' in src


# =========================================================================== #
# PATCH C – SYSTEMD ORDERING SPEC
# =========================================================================== #

ORDERING_DROPIN = os.path.join(
    REPO_ROOT, "deploy", "systemd", "uranus-runner.service.d", "15-ordering.conf"
)


def test_c1_ordering_dropin_is_version_controlled():
    assert os.path.isfile(ORDERING_DROPIN), "a sorrend-drop-in specifikáció hiányzik"


def test_c2_ordering_dropin_declares_after_and_wants():
    text = open(ORDERING_DROPIN, encoding="utf-8").read()
    directives = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "[Unit]" in directives
    assert "After=uranus-freqtrade.service" in directives
    assert "Wants=uranus-freqtrade.service" in directives


def test_c3_ordering_dropin_avoids_hard_dependency():
    """Requires=/BindsTo= tudatosan elvetve – nincs restart-cascade."""
    text = open(ORDERING_DROPIN, encoding="utf-8").read()
    directives = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(d.startswith("Requires=") for d in directives)
    assert not any(d.startswith("BindsTo=") for d in directives)


def test_c4_ordering_dropin_touches_only_unit_section():
    """Csak [Unit] – így egyetlen meglévő [Service] drop-innel sem ütközik."""
    text = open(ORDERING_DROPIN, encoding="utf-8").read()
    sections = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("[") and line.strip().endswith("]")
    ]
    assert sections == ["[Unit]"]


# =========================================================================== #
# PATCH D – AUTOMATED BUY BYPASS ELIMINATION
# =========================================================================== #

def _df():
    import pandas as pd
    return pd.DataFrame({"close": [1.0, 1.1, 1.2]})


def _make_strategy(strategy_mod, tmp_path, state_payload):
    strat = object.__new__(strategy_mod.UranusExecutor)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state_payload), encoding="utf-8")
    strat.STATE_JSON_PATH = str(state_file)
    strat.EXECUTOR_STATE_PATH = str(tmp_path / "exec_state.json")
    return strat


def _fresh_ts():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_d0_signal_based_entry_is_disabled_by_constant(strategy_mod):
    """A kapcsoló modul-konstans, tehát env-ből nem kapcsolható vissza."""
    assert strategy_mod.SIGNAL_BASED_ENTRY_ENABLED is False

    src = open(STRATEGY_PATH, encoding="utf-8").read()
    assert "SIGNAL_BASED_ENTRY_ENABLED = False" in src
    assert 'getenv("SIGNAL_BASED_ENTRY' not in src


def test_d1_fresh_root_signal_does_not_enter(strategy_mod, tmp_path):
    """D1: friss state["signal"] BUY sem generál belépést."""
    state = {"signal": {"pair": "XRP/USDC", "action": "BUY", "id": "s1", "ts": _fresh_ts()}}
    strat = _make_strategy(strategy_mod, tmp_path, state)

    out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})

    assert int(out["enter_long"].iloc[-1]) == 0
    assert set(out["enter_long"].unique()) == {0}
    assert not os.path.exists(strat.EXECUTOR_STATE_PATH)


def test_d2_fresh_pair_signal_does_not_enter(strategy_mod, tmp_path):
    """D2: friss state["signals"][PAIR] BUY sem generál belépést."""
    state = {"signals": {"XRP/USDC": {"action": "BUY", "id": "s2", "ts": _fresh_ts()}}}
    strat = _make_strategy(strategy_mod, tmp_path, state)

    out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})

    assert int(out["enter_long"].iloc[-1]) == 0
    assert not os.path.exists(strat.EXECUTOR_STATE_PATH)


def test_d2b_repeated_fresh_signal_never_enters(strategy_mod, tmp_path):
    """A one-shot szerződés eltűnt: nincs olyan hívásszám, ami belépést adna."""
    state = {"signals": {"XRP/USDC": {"action": "BUY", "id": "s3", "ts": _fresh_ts()}}}
    strat = _make_strategy(strategy_mod, tmp_path, state)

    for _ in range(5):
        out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
        assert int(out["enter_long"].iloc[-1]) == 0


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"signals": {}},
        {"signals": {"XRP/USDC": {"action": "NONE", "id": "", "ts": ""}}},
        {"signals": {"XRP/USDC": {"action": "BUY", "id": "s4", "ts": "2020-01-01T00:00:00Z"}}},
        {"signals": {"XRP/USDC": {"action": "BUY", "id": "s5", "ts": "nem-datum"}}},
        {"signals": {"XRP/USDC": {"action": "BUY", "id": "s6"}}},
    ],
)
def test_d3_stale_or_missing_signal_never_enters(strategy_mod, tmp_path, state):
    """D3: elavult/hiányzó jel továbbra sem generál belépést."""
    strat = _make_strategy(strategy_mod, tmp_path, state)
    out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert int(out["enter_long"].iloc[-1]) == 0


def test_d3b_unreadable_state_does_not_raise(strategy_mod, tmp_path):
    """A jel-audit olvasása soha nem törhet meg egy Freqtrade tickel."""
    strat = object.__new__(strategy_mod.UranusExecutor)
    strat.STATE_JSON_PATH = str(tmp_path / "nincs-ilyen.json")
    strat.EXECUTOR_STATE_PATH = str(tmp_path / "exec_state.json")

    out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert int(out["enter_long"].iloc[-1]) == 0


def test_d4_runner_force_enter_contract_intact():
    """
    D4: a runner végrehajtási szerződése nem tört el.

    A Freqtrade force entry a `/api/v1/forceenter` RPC-n megy, ami nem a
    stratégia belépési jelét használja – tehát a stratégia passzívvá tétele a
    runner útját nem érinti. Itt azt rögzítjük, hogy a runner változatlanul
    ezen a szerződésen keresztül hajt végre.
    """
    import freqtrade_executor

    src = open(os.path.join(APP_DIR, "freqtrade_executor.py"), encoding="utf-8").read()
    assert hasattr(freqtrade_executor.FreqtradeExecutor, "force_enter")
    assert hasattr(freqtrade_executor.FreqtradeExecutor, "force_exit")
    assert "forceenter" in src

    runner_src = open(os.path.join(APP_DIR, "tick_runner.py"), encoding="utf-8").read()
    assert "ex.force_enter()" in runner_src
    assert "ex.force_exit()" in runner_src


def test_d5_strategy_generates_no_independent_sell(strategy_mod, tmp_path):
    """D5: a stratégia továbbra sem ad önálló SELL jelet."""
    state = {"signals": {"XRP/USDC": {"action": "SELL", "id": "s7", "ts": _fresh_ts()}}}
    strat = _make_strategy(strategy_mod, tmp_path, state)

    out = strat.populate_exit_trend(_df(), {"pair": "XRP/USDC"})
    assert set(out["exit_long"].unique()) == {0}


def test_d6_stoploss_safety_net_unchanged(strategy_mod):
    """A Freqtrade-szintű stoploss védőháló megmaradt."""
    assert strategy_mod.UranusExecutor.stoploss == -0.10
    assert strategy_mod.UranusExecutor.trailing_stop is False


def test_d7_strategy_never_writes_processed_marker(strategy_mod, tmp_path):
    """A `_mark_processed` már nem fut a belépési ágon."""
    state = {"signals": {"XRP/USDC": {"action": "BUY", "id": "s8", "ts": _fresh_ts()}}}
    strat = _make_strategy(strategy_mod, tmp_path, state)
    strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
    assert not os.path.exists(strat.EXECUTOR_STATE_PATH)


def test_d8_entry_path_has_no_enter_long_assignment():
    """Strukturális garancia: sehol nem áll elő `enter_long = 1`."""
    src = open(STRATEGY_PATH, encoding="utf-8").read()
    assert '"enter_long"] = 1' not in src
    assert "'enter_long'] = 1" not in src


# =========================================================================== #
# PRODUCTION SAFETY INVARIANTS (7. fejezet)
# =========================================================================== #

def test_inv1_no_live_activation_from_default_settings(web_app_mod, monkeypatch, tmp_path):
    """NO LIVE ACTIVATION FROM DEFAULT SETTINGS"""
    app_mod = web_app_mod
    monkeypatch.setattr(app_mod, "SETTINGS_ENV_FILE", str(tmp_path / "nincs.conf"))
    text = app_mod.build_settings_dropin(app_mod.read_trading_settings())
    assert "Environment=EXECUTION_ENABLED=0" in text
    assert "Environment=EXECUTION_LOG_ONLY=1" in text


def test_inv2_one_canonical_uranus_ft_endpoint(monkeypatch, tmp_path):
    """ONE CANONICAL URANUS FT ENDPOINT"""
    monkeypatch.setenv("URANUS_FT_CONFIG", _write_ft_config(tmp_path, 8090))
    monkeypatch.setenv("FT_URL", "http://127.0.0.1:8017")

    import ft_endpoint
    import freqtrade_ui

    urls = {ft_endpoint.canonical_ft_url(), freqtrade_ui.ft_base_url()}
    assert urls == {"http://127.0.0.1:8090"}


def test_inv3_runner_ordered_after_freqtrade():
    """RUNNER ORDERED AFTER FREQTRADE"""
    text = open(ORDERING_DROPIN, encoding="utf-8").read()
    assert "After=uranus-freqtrade.service" in text


def test_inv4_no_strategy_bypass_buy_path(strategy_mod, tmp_path):
    """NO STRATEGY BYPASS BUY PATH"""
    for state in (
        {"signal": {"pair": "XRP/USDC", "action": "BUY", "id": "i1", "ts": _fresh_ts()}},
        {"signals": {"XRP/USDC": {"action": "BUY", "id": "i2", "ts": _fresh_ts()}}},
    ):
        strat = _make_strategy(strategy_mod, tmp_path, state)
        out = strat.populate_entry_trend(_df(), {"pair": "XRP/USDC"})
        assert set(out["enter_long"].unique()) == {0}


def test_inv5_live_execution_default_remains_disabled(policy, monkeypatch):
    """LIVE EXECUTION DEFAULT REMAINS DISABLED"""
    import tick_runner

    for key in ("EXECUTION_ENABLED", "EXECUTION_LOG_ONLY", "EXECUTION_CONFIRM"):
        monkeypatch.delenv(key, raising=False)

    enabled, log_only, confirm = tick_runner._runtime_exec_flags()
    assert enabled is False
    assert log_only is True
    assert confirm is True
