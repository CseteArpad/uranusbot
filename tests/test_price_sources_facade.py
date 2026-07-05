"""
FÁZIS 7.5.3b – price_sources facade + binance_spot_rest shim tesztek.

Az adapter mockolt; egy autouse fixture garantálja, hogy egyetlen teszt
sem indíthat valódi HTTP-hívást (requests.get azonnal elhasal).
"""
import pytest
import requests

from app import binance_spot_rest, price_sources
from app.exchange.binance import BinanceAdapter
from app.exchange.exceptions import ExchangeError, TickerFetchError
from app.exchange.manager import clear_adapter_cache
from app.exchange.okx import OKXAdapter


@pytest.fixture(autouse=True)
def no_real_http(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("real HTTP call attempted from facade test")

    monkeypatch.setattr(requests, "get", _forbidden)
    monkeypatch.setattr(requests, "post", _forbidden)
    clear_adapter_cache()
    yield
    clear_adapter_cache()


class FakeAdapter:
    """Adapter-helyettesítő: rögzíti a hívást, fix ticket ad vagy kivételt dob."""

    def __init__(self, name="binance", symbol="XRPUSDC", price=0.52, exc=None):
        self.name = name
        self.exc = exc
        self.calls = []
        self._tick = {
            "pair": "XRP/USDC",
            "symbol": symbol,
            "price": price,
            "ts": 1751700000,
            "ts_utc": "2026-07-05T18:00:00Z",
            "source": name,
        }

    def get_ticker(self, pair):
        self.calls.append(pair)
        if self.exc is not None:
            raise self.exc
        return dict(self._tick)


@pytest.fixture
def patch_adapter(monkeypatch):
    """price_sources.get_exchange_adapter cseréje; visszaadja a rögzítőt."""

    def _patch(adapter):
        requested = []

        def fake_get_exchange_adapter(name, fresh=False):
            requested.append(name)
            return adapter

        monkeypatch.setattr(
            price_sources, "get_exchange_adapter", fake_get_exchange_adapter
        )
        return requested

    return _patch


class TestPriceSourcesFacade:
    def test_get_tick_returns_correct_dict(self, patch_adapter):
        adapter = FakeAdapter()
        patch_adapter(adapter)
        tick = price_sources.get_tick("XRP/USDC")

        assert tick == {
            "pair": "XRP/USDC",
            "symbol": "XRPUSDC",
            "price": 0.52,
            "ts": 1751700000,
            "ts_utc": "2026-07-05T18:00:00Z",
            "source": "binance",
        }
        assert adapter.calls == ["XRP/USDC"]

    def test_get_current_price_returns_float(self, patch_adapter):
        patch_adapter(FakeAdapter(price=0.777))
        price = price_sources.get_current_price("XRP/USDC")
        assert isinstance(price, float)
        assert price == pytest.approx(0.777)

    def test_get_price_alias(self, patch_adapter):
        patch_adapter(FakeAdapter(price=1.23))
        assert price_sources.get_price("XRP/USDC") == pytest.approx(1.23)

    def test_default_exchange_is_binance(self, patch_adapter, monkeypatch):
        monkeypatch.delenv("URANUS_EXCHANGE", raising=False)
        monkeypatch.delenv("EXCHANGE_NAME", raising=False)
        requested = patch_adapter(FakeAdapter())
        price_sources.get_tick("XRP/USDC")
        assert requested == ["binance"]

    def test_explicit_okx_argument(self, patch_adapter):
        requested = patch_adapter(FakeAdapter(name="okx", symbol="XRP-USDC"))
        tick = price_sources.get_tick("XRP/USDC", exchange="okx")
        assert requested == ["okx"]
        assert tick["source"] == "okx"

    def test_env_override_selects_exchange(self, patch_adapter, monkeypatch):
        monkeypatch.setenv("URANUS_EXCHANGE", "okx")
        requested = patch_adapter(FakeAdapter(name="okx"))
        price_sources.get_tick("XRP/USDC")
        assert requested == ["okx"]

    def test_adapter_error_propagates(self, patch_adapter):
        patch_adapter(FakeAdapter(exc=TickerFetchError("boom")))
        with pytest.raises(TickerFetchError):
            price_sources.get_tick("XRP/USDC")
        with pytest.raises(ExchangeError):
            price_sources.get_current_price("XRP/USDC")

    def test_real_adapters_resolve_without_network(self):
        # a manager valódi adaptert ad vissza - hálózat nélkül (nincs hívás)
        assert isinstance(
            price_sources.get_exchange_adapter("binance"), BinanceAdapter
        )
        assert isinstance(price_sources.get_exchange_adapter("okx"), OKXAdapter)


class TestBinanceSpotRestShim:
    def test_fetch_last_price_success(self, patch_adapter):
        requested = patch_adapter(FakeAdapter(price=0.52))
        out = binance_spot_rest.fetch_last_price("XRP/USDC")

        assert out["ok"] is True
        assert out["pair"] == "XRP/USDC"
        assert out["symbol"] == "XRPUSDC"
        assert out["last"] == pytest.approx(0.52)
        assert out["source"] == "binance_spot_rest"
        assert out["error"] is None
        assert requested == ["binance"]  # a shim mindig binance-t kér

    def test_fetch_last_price_error_gives_ok_false(self, patch_adapter):
        patch_adapter(FakeAdapter(exc=TickerFetchError("HTTP 502")))
        out = binance_spot_rest.fetch_last_price("XRP/USDC")

        assert out["ok"] is False
        assert out["last"] is None
        assert "HTTP 502" in out["error"]
        assert "TickerFetchError" in out["error"]

    def test_fetch_last_price_never_raises_on_exchange_error(self, patch_adapter):
        patch_adapter(FakeAdapter(exc=ExchangeError("generic failure")))
        out = binance_spot_rest.fetch_last_price("XRP/USDC")
        assert out["ok"] is False

    def test_get_current_price_alias_success(self, patch_adapter):
        patch_adapter(FakeAdapter(price=2.5))
        assert binance_spot_rest.get_current_price("XRP/USDC") == pytest.approx(2.5)
        assert binance_spot_rest.get_price("XRP/USDC") == pytest.approx(2.5)

    def test_get_current_price_raises_runtime_error_on_failure(self, patch_adapter):
        # örökölt viselkedés: a régi shim RuntimeError-t dobott hibánál
        patch_adapter(FakeAdapter(exc=TickerFetchError("down")))
        with pytest.raises(RuntimeError):
            binance_spot_rest.get_current_price("XRP/USDC")

    def test_shim_has_no_direct_http_logic(self):
        import inspect

        src = inspect.getsource(binance_spot_rest)
        assert "api.binance.com" not in src
        assert "requests.get" not in src
