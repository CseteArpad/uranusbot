"""
Uranus – OKX_SPOT_ONLY végrehajtási politika: **viselkedési** regressziós csomag.

Tulajdonosi architekturális döntés (2026-09-01)::

    URANUS_EXECUTION_VENUE_POLICY = OKX_SPOT_ONLY
    BINANCE_EXECUTION_ALLOWED     = NO
    BINANCE_PRODUCTION_SUPPORT    = RETIRED

Mit bizonyít ez a csomag – és mit NEM
--------------------------------------
Szándékosan **nincs** benne egyetlen forrásszöveg-vizsgálat sem (``inspect.getsource``,
substring-keresés a kódban). Egy ilyen teszt akkor is zöld maradna, ha a tiltás
kommentbe kerülne, a hívási út pedig megkerülné. Minden állítás **megfigyelhető
viselkedésre** épül: kivétel típusa, verdikt kódja, kilépési kód, vagy az, hogy
egy order-küldő függvény hívása után a hálózati réteget **nem** hívta meg senki.

A hálózati rétegeket ezért fake-eljük, és külön ellenőrizzük, hogy a fake
**meg sem szólalt** – ez bizonyítja, hogy az elutasítás az order elküldése
*előtt* történt, nem utána.

Biztonsági garanciák: nincs valódi hálózat, nincs order, nincs systemctl,
nincs production fájlírás; minden fájlművelet ``tmp_path`` alatt.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.append(APP_DIR)

from app import execution_venue_policy as policy  # noqa: E402


# --------------------------------------------------------------------------- #
# Segédek
# --------------------------------------------------------------------------- #

def make_config(**overrides):
    """Egy érvényes, OKX-es, dry-run Uranus config – felülírható mezőkkel."""
    config = {
        "bot_name": "uranus",
        "dry_run": True,
        "trading_mode": "spot",
        "stake_currency": "USDC",
        "exchange": {
            "name": "okx",
            "pair_whitelist": ["XRP/USDC"],
        },
    }
    for key, value in overrides.items():
        if key == "exchange" and isinstance(value, dict):
            config["exchange"] = {**config["exchange"], **value}
        else:
            config[key] = value
    return config


@pytest.fixture
def write_config(tmp_path, monkeypatch):
    """A megadott configot lemezre írja, és a kanonikus feloldást ráállítja."""

    def _write(config):
        path = tmp_path / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setenv("URANUS_FT_CONFIG", str(path))
        return str(path)

    return _write


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "URANUS_FT_CONFIG", "URANUS_EXCHANGE", "EXCHANGE_NAME",
        "EXECUTION_ENABLED", "EXECUTION_LOG_ONLY", "FT_URL",
        "BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_KEY", "BINANCE_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# 1. A politika maga
# --------------------------------------------------------------------------- #

class TestPolicyInvariants:
    def test_only_okx_is_an_allowed_live_venue(self):
        assert policy.ALLOWED_LIVE_EXECUTION_VENUES == frozenset({"okx"})

    def test_binance_is_retired(self):
        assert "binance" in policy.RETIRED_EXECUTION_VENUES
        assert "binance" not in policy.ALLOWED_LIVE_EXECUTION_VENUES

    def test_okx_live_execution_is_not_authorized_yet(self):
        """A Binance kivezetése NEM jelent OKX élesítési engedélyt."""
        assert policy.LIVE_EXECUTION_AUTHORIZED is False
        assert policy.policy_summary()["OKX_LIVE_EXECUTION_AUTHORIZED"] == "NO"

    @pytest.mark.parametrize(
        "alias", ["binance", "BINANCE", " Binance ", "binanceus", "binanceusdm"]
    )
    def test_binance_aliases_all_resolve_to_the_retired_venue(self, alias):
        assert policy.canonical_venue(alias) == "binance"
        assert policy.is_retired_venue(alias) is True
        assert policy.is_allowed_live_venue(alias) is False

    @pytest.mark.parametrize("alias", ["okx", "OKX", "myokx", " okx "])
    def test_okx_aliases_resolve_to_okx(self, alias):
        assert policy.canonical_venue(alias) == "okx"
        assert policy.is_allowed_live_venue(alias) is True

    @pytest.mark.parametrize("value", [None, "", "   ", "kraken", "coinbase", 42, True])
    def test_unknown_or_missing_venue_is_never_allowed(self, value):
        """Fail-closed: ismeretlen, üres és értelmezhetetlen egyaránt tiltott."""
        assert policy.is_allowed_live_venue(value) is False

    def test_policy_cannot_be_re_enabled_from_the_environment(self, monkeypatch):
        """
        A tiltás modul-konstans. Bármit írunk a környezetbe, a Binance nem
        válhat engedélyezett helyszínné.
        """
        for name, value in [
            ("ALLOWED_LIVE_EXECUTION_VENUES", "binance,okx"),
            ("URANUS_EXECUTION_VENUE_POLICY", "ANY"),
            ("BINANCE_EXECUTION_ALLOWED", "1"),
            ("LIVE_EXECUTION_AUTHORIZED", "1"),
            ("URANUS_ALLOW_BINANCE", "true"),
        ]:
            monkeypatch.setenv(name, value)

        import importlib

        importlib.reload(policy)
        try:
            assert policy.is_allowed_live_venue("binance") is False
            assert policy.LIVE_EXECUTION_AUTHORIZED is False
        finally:
            for name in (
                "ALLOWED_LIVE_EXECUTION_VENUES", "URANUS_EXECUTION_VENUE_POLICY",
                "BINANCE_EXECUTION_ALLOWED", "LIVE_EXECUTION_AUTHORIZED",
                "URANUS_ALLOW_BINANCE",
            ):
                monkeypatch.delenv(name, raising=False)
            importlib.reload(policy)


# --------------------------------------------------------------------------- #
# 2. Az indítási guard (a feladat 8. pontjának tesztmátrixa)
# --------------------------------------------------------------------------- #

class TestStartupGate:
    def test_binance_venue_refuses_startup(self):
        verdict = policy.evaluate_startup(make_config(exchange={"name": "binance"}), {})
        assert verdict.allowed is False
        assert verdict.code == policy.REFUSE_VENUE_RETIRED

    def test_binance_credential_present_refuses_startup(self):
        """Kulcsanyag kivezetett helyszínen -> elutasítás, a helyszín-hibán túl is."""
        verdict = policy.evaluate_startup(
            make_config(exchange={"name": "binance", "key": "k" * 64, "secret": "s" * 64}),
            {},
        )
        assert verdict.allowed is False
        assert policy.REFUSE_BINANCE_CREDENTIAL in verdict.findings

    def test_binance_credential_in_environment_refuses_startup(self):
        verdict = policy.evaluate_startup(make_config(), {"BINANCE_API_KEY": "k" * 64})
        assert verdict.allowed is False
        assert verdict.code == policy.REFUSE_BINANCE_CREDENTIAL

    def test_binance_endpoint_in_config_refuses_startup(self):
        """Az OKX név nem elég, ha a config Binance végpontra mutat."""
        verdict = policy.evaluate_startup(
            make_config(exchange={"name": "okx", "ccxt_config": {"urls": {"api": "https://api.binance.com"}}}),
            {},
        )
        assert verdict.allowed is False
        assert policy.REFUSE_BINANCE_ENDPOINT in verdict.findings

    @pytest.mark.parametrize("venue", [None, "", "   "])
    def test_missing_venue_refuses_startup(self, venue):
        config = make_config()
        config["exchange"]["name"] = venue
        verdict = policy.evaluate_startup(config, {})
        assert verdict.allowed is False
        assert verdict.code == policy.REFUSE_VENUE_MISSING

    def test_missing_config_refuses_startup(self):
        verdict = policy.evaluate_startup(None, {})
        assert verdict.allowed is False
        assert verdict.code == policy.REFUSE_VENUE_MISSING

    @pytest.mark.parametrize("venue", ["kraken", "coinbase", "bybit", "not-a-venue"])
    def test_unknown_venue_refuses_startup(self, venue):
        verdict = policy.evaluate_startup(make_config(exchange={"name": venue}), {})
        assert verdict.allowed is False
        assert verdict.code == policy.REFUSE_VENUE_UNSUPPORTED

    def test_okx_live_without_authorization_refuses_startup(self):
        """OKX + dry_run=false + nincs engedély -> REFUSED."""
        verdict = policy.evaluate_startup(make_config(dry_run=False), {})
        assert verdict.allowed is False
        assert verdict.code == policy.REFUSE_LIVE_NOT_AUTHORIZED

    def test_okx_spot_correct_identity_but_no_authorization_still_refused(self):
        """
        A feladat kifejezett követelménye: helyes identity önmagában NEM elég.
        Minden más rendben van, csak az éles engedély hiányzik.
        """
        config = make_config(dry_run=False, bot_name="uranus", trading_mode="spot")
        config["exchange"]["name"] = "okx"
        verdict = policy.evaluate_startup(config, {})
        assert verdict.allowed is False
        assert verdict.findings == (policy.REFUSE_LIVE_NOT_AUTHORIZED,)

    def test_non_spot_market_type_refuses_startup(self):
        verdict = policy.evaluate_startup(make_config(trading_mode="futures"), {})
        assert verdict.allowed is False
        assert policy.REFUSE_MARKET_TYPE in verdict.findings

    def test_foreign_bot_identity_refuses_startup(self):
        verdict = policy.evaluate_startup(make_config(bot_name="saturnus"), {})
        assert verdict.allowed is False
        assert policy.REFUSE_BOT_IDENTITY in verdict.findings

    def test_okx_dry_run_with_correct_identity_is_allowed(self):
        """A politika nem bénít le mindent: OKX + spot + dry-run indulhat."""
        verdict = policy.evaluate_startup(make_config(), {})
        assert verdict.allowed is True
        assert verdict.code == policy.ALLOW_OK
        assert verdict.venue == "okx"

    def test_all_findings_are_reported_not_just_the_first(self):
        """Egy indítási kísérlet a teljes képet mutassa, ne csak az első hibát."""
        verdict = policy.evaluate_startup(
            make_config(
                dry_run=False,
                trading_mode="futures",
                bot_name="neptunus",
                exchange={"name": "binance", "key": "k" * 64, "secret": "s" * 64},
            ),
            {},
        )
        assert verdict.allowed is False
        assert policy.REFUSE_VENUE_RETIRED in verdict.findings
        assert policy.REFUSE_BINANCE_CREDENTIAL in verdict.findings
        assert policy.REFUSE_MARKET_TYPE in verdict.findings
        assert policy.REFUSE_BOT_IDENTITY in verdict.findings
        assert policy.REFUSE_LIVE_NOT_AUTHORIZED in verdict.findings

    def test_verdict_never_leaks_credential_values(self):
        """A verdikt csak mezőneveket nevez meg, értéket soha."""
        secret = "S3CR3T-must-never-appear-" + "x" * 40
        verdict = policy.evaluate_startup(
            make_config(exchange={"name": "binance", "key": secret, "secret": secret}),
            {"BINANCE_API_KEY": secret},
        )
        blob = json.dumps(verdict.as_dict()) + verdict.log_line()
        assert secret not in blob


# --------------------------------------------------------------------------- #
# 3. Order-küldés előtti kapu
# --------------------------------------------------------------------------- #

class TestAssertLiveExecutionAllowed:
    @pytest.mark.parametrize("venue", ["binance", "BINANCE", "binanceus"])
    def test_binance_always_raises(self, venue):
        with pytest.raises(policy.ExecutionVenueForbidden) as excinfo:
            policy.assert_live_execution_allowed(venue, dry_run=False)
        assert excinfo.value.verdict.code == policy.REFUSE_VENUE_RETIRED

    def test_binance_raises_even_in_dry_run(self):
        """A kivezetés nem a valós pénzről szól: a helyszín maga tiltott."""
        with pytest.raises(policy.ExecutionVenueForbidden):
            policy.assert_live_execution_allowed("binance", dry_run=True)

    @pytest.mark.parametrize("venue", [None, "", "kraken"])
    def test_missing_or_unknown_venue_raises(self, venue):
        with pytest.raises(policy.ExecutionVenueForbidden):
            policy.assert_live_execution_allowed(venue, dry_run=False)

    def test_okx_live_without_authorization_raises(self):
        with pytest.raises(policy.ExecutionVenueForbidden) as excinfo:
            policy.assert_live_execution_allowed("okx", dry_run=False)
        assert excinfo.value.verdict.code == policy.REFUSE_LIVE_NOT_AUTHORIZED

    def test_okx_dry_run_passes(self):
        policy.assert_live_execution_allowed("okx", dry_run=True)  # nem dob


# --------------------------------------------------------------------------- #
# 4. A production order-utak – hívási szintű bizonyítás
# --------------------------------------------------------------------------- #

class TestFreqtradeExecutorOrderPath:
    """
    A ``FreqtradeExecutor`` az elsődleges order-út. A hálózati réteget
    kicseréljük egy számlálóra, és bizonyítjuk, hogy **egyszer sem hívódott meg**:
    az elutasítás az order elküldése előtt történik.
    """

    @pytest.fixture
    def executor(self, monkeypatch):
        from app import freqtrade_executor

        monkeypatch.setenv("EXECUTION_ENABLED", "1")  # a legveszélyesebb beállítás
        monkeypatch.setenv("PAIR", "XRP/USDC")
        ex = freqtrade_executor.FreqtradeExecutor()

        calls = []

        def _never(method, path, payload=None):
            calls.append((method, path))
            raise AssertionError(f"network reached: {method} {path}")

        monkeypatch.setattr(ex, "_request_json", _never)
        return ex, calls

    @pytest.mark.parametrize("venue", ["binance", "binanceus"])
    def test_force_enter_refused_on_binance(self, executor, write_config, venue):
        ex, calls = executor
        write_config(make_config(dry_run=False, exchange={"name": venue}))

        result = ex.force_enter("XRP/USDC")

        assert result.ok is False
        assert result.detail.startswith("venue_policy_refused:")
        assert calls == [], "the order must be refused BEFORE any network call"

    def test_force_exit_refused_on_binance(self, executor, write_config):
        ex, calls = executor
        write_config(make_config(dry_run=False, exchange={"name": "binance"}))

        result = ex.force_exit("XRP/USDC")

        assert result.ok is False
        assert result.detail.startswith("venue_policy_refused:")
        assert calls == []

    def test_force_enter_refused_on_okx_without_live_authorization(self, executor, write_config):
        ex, calls = executor
        write_config(make_config(dry_run=False, exchange={"name": "okx"}))

        result = ex.force_enter("XRP/USDC")

        assert result.ok is False
        assert "LIVE_EXECUTION_NOT_AUTHORIZED" in result.detail
        assert calls == []

    def test_force_enter_refused_when_config_is_unreadable(self, executor, monkeypatch, tmp_path):
        """Fail-closed: hiányzó config nem jelenthet 'valószínűleg rendben'."""
        ex, calls = executor
        monkeypatch.setenv("URANUS_FT_CONFIG", str(tmp_path / "does-not-exist.json"))

        result = ex.force_enter("XRP/USDC")

        assert result.ok is False
        assert result.detail.startswith("venue_policy_refused:")
        assert calls == []


class TestFreqtradeAdapterOrderPath:
    """
    A ``freqtrade_adapter`` a MÁSODIK order-út (``executor.py`` dinamikusan
    importálja). Saját kapuval kell rendelkeznie, különben megkerülhető.
    """

    @pytest.fixture
    def adapter(self, monkeypatch):
        from app import freqtrade_adapter

        calls = []

        def _never(method, path, payload=None, timeout=8.0):
            calls.append((method, path))
            raise AssertionError(f"network reached: {method} {path}")

        monkeypatch.setattr(freqtrade_adapter, "_http_json", _never)
        return freqtrade_adapter, calls

    def test_forcebuy_refused_on_binance(self, adapter, write_config):
        module, calls = adapter
        write_config(make_config(dry_run=False, exchange={"name": "binance"}))

        result = module._forcebuy(pair="XRP/USDC")

        assert result["ok"] is False
        assert result["status"].startswith("venue_policy_refused:")
        assert calls == []

    def test_forcesell_refused_on_binance(self, adapter, write_config):
        module, calls = adapter
        write_config(make_config(dry_run=False, exchange={"name": "binance"}))

        result = module._forcesell(pair="XRP/USDC")

        assert result["ok"] is False
        assert result["status"].startswith("venue_policy_refused:")
        assert calls == []

    def test_forcebuy_refused_on_okx_without_authorization(self, adapter, write_config):
        module, calls = adapter
        write_config(make_config(dry_run=False, exchange={"name": "okx"}))

        result = module._forcebuy(pair="XRP/USDC")

        assert result["ok"] is False
        assert calls == []


class TestNoBinanceFallback:
    """
    Nincs automatikus Binance fallback. Ha az OKX-út nem elérhető, a rendszer
    **elutasít**, nem vált másik tőzsdére.
    """

    def test_unknown_venue_does_not_fall_back_to_a_default(self):
        verdict = policy.evaluate_startup(make_config(exchange={"name": "kraken"}), {})
        assert verdict.allowed is False
        assert verdict.venue is None, "an unresolved venue must stay unresolved"

    def test_missing_venue_does_not_fall_back_to_a_default(self):
        config = make_config()
        del config["exchange"]["name"]
        verdict = policy.evaluate_startup(config, {})
        assert verdict.allowed is False
        assert verdict.venue is None

    def test_adapter_registry_exposes_binance_but_never_for_orders(self):
        """
        A Binance adapter regisztrálva marad (piaci adat), de a rendelési
        felülete a politikára hivatkozva utasít el – nem 'még nincs kész'.
        """
        from app.exchange.manager import get_exchange_adapter

        adapter = get_exchange_adapter("binance", fresh=True)
        with pytest.raises(policy.ExecutionVenueForbidden):
            adapter.create_order("XRP/USDC", "buy", "market", 1.0)


class TestLiquidateDustRetired:
    """
    A közvetlen, aláírt Binance order-út (``liquidate_dust``) kivezetve.
    Ez volt az egyetlen út, amely a Freqtrade-et is megkerülte.
    """

    def test_main_refuses(self):
        from app import liquidate_dust

        with pytest.raises(policy.ExecutionVenueForbidden):
            liquidate_dust.main()

    def test_signed_request_refuses(self):
        from app import liquidate_dust

        with pytest.raises(policy.ExecutionVenueForbidden):
            liquidate_dust.binance_signed_request("k", "s", "POST", "/api/v3/order", {})

    def test_config_loading_refuses_so_no_credential_is_ever_read(self):
        from app import liquidate_dust

        with pytest.raises(policy.ExecutionVenueForbidden):
            liquidate_dust.load_config()


class TestBinanceWalletCredentialPathClosed:
    def test_retired_venue_short_circuits_before_reading_keys(self, tmp_path, monkeypatch):
        from app import binance_wallet

        cfg = tmp_path / "config.json"
        cfg.write_text(
            json.dumps({"exchange": {"name": "binance", "key": "k" * 64, "secret": "s" * 64}}),
            encoding="utf-8",
        )

        def _never(*args, **kwargs):
            raise AssertionError("signed Binance request must not be attempted")

        monkeypatch.setattr(binance_wallet, "_signed_get", _never)

        out = binance_wallet.get_spot_balances_from_freqtrade_config(str(cfg))
        assert out["status"] == "VENUE_RETIRED"
        assert out["balances"] == {}

    def test_unreadable_config_is_treated_as_retired(self, tmp_path):
        from app import binance_wallet

        out = binance_wallet.get_spot_balances_from_freqtrade_config(
            str(tmp_path / "missing.json")
        )
        assert out["status"] == "VENUE_RETIRED"


# --------------------------------------------------------------------------- #
# 5. A preflight guard és a runner indulása
# --------------------------------------------------------------------------- #

class TestVenuePreflight:
    @pytest.fixture
    def preflight(self):
        from app import venue_preflight

        return venue_preflight

    def test_exit_code_is_refusal_on_binance(self, preflight, write_config, capsys):
        path = write_config(make_config(dry_run=False, exchange={"name": "binance"}))
        code = preflight.run(path)
        assert code == policy.EXIT_STARTUP_REFUSED
        assert code != 0

    def test_exit_code_is_refusal_on_okx_live_without_authorization(self, preflight, write_config):
        path = write_config(make_config(dry_run=False, exchange={"name": "okx"}))
        assert preflight.run(path) == policy.EXIT_STARTUP_REFUSED

    def test_exit_code_is_zero_for_okx_dry_run(self, preflight, write_config):
        path = write_config(make_config())
        assert preflight.run(path) == 0

    def test_missing_config_exits_refused(self, preflight, tmp_path):
        assert preflight.run(str(tmp_path / "nope.json")) == policy.EXIT_STARTUP_REFUSED

    def test_json_output_is_machine_readable(self, preflight, write_config, capsys):
        path = write_config(make_config(exchange={"name": "binance"}))
        preflight.run(path, as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"]["allowed"] is False
        assert payload["policy"]["BINANCE_EXECUTION_ALLOWED"] == "NO"


class TestRunnerStartupGuard:
    """A tick_runner el sem indul tiltott helyszínen."""

    @pytest.fixture(scope="class")
    def tr(self):
        import tick_runner  # noqa: F401

        return sys.modules["tick_runner"]

    def test_guard_refuses_binance(self, tr):
        verdict = tr.startup_venue_guard(make_config(exchange={"name": "binance"}), {})
        assert verdict.allowed is False
        assert verdict.code == policy.REFUSE_VENUE_RETIRED

    def test_guard_allows_okx_dry_run(self, tr):
        verdict = tr.startup_venue_guard(make_config(), {})
        assert verdict.allowed is True

    def test_main_loop_exits_with_refusal_code_and_never_ticks(self, tr, monkeypatch):
        """
        Viselkedési bizonyíték: a hurok NEM fut le egyszer sem, és a kilépési
        kód a dedikált ``EXIT_STARTUP_REFUSED`` – amire a systemd
        ``RestartPreventExitStatus`` szűr, hogy ne legyen restart-hurok.
        """
        monkeypatch.setattr(
            tr, "startup_venue_guard",
            lambda *a, **k: policy.StartupVerdict(
                allowed=False,
                code=policy.REFUSE_VENUE_RETIRED,
                reason="test",
                venue="binance",
                findings=(policy.REFUSE_VENUE_RETIRED,),
            ),
        )

        def _never():
            raise AssertionError("run_once must not be reached when startup is refused")

        monkeypatch.setattr(tr, "run_once", _never)

        with pytest.raises(SystemExit) as excinfo:
            tr.main_loop()
        assert excinfo.value.code == policy.EXIT_STARTUP_REFUSED
