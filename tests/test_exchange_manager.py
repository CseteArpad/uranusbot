import pytest

from app.exchange.binance import BinanceAdapter
from app.exchange.exceptions import AdapterNotConfiguredError, UnsupportedExchangeError
from app.exchange.manager import (
    ExchangeManager,
    clear_adapter_cache,
    get_exchange_adapter,
)
from app.exchange.okx import OKXAdapter


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_adapter_cache()
    yield
    clear_adapter_cache()


class TestExchangeManager:
    def test_binance_name_gives_binance_adapter(self):
        adapter = ExchangeManager(exchange_name="binance").get_adapter()
        assert isinstance(adapter, BinanceAdapter)

    def test_okx_name_gives_okx_adapter(self):
        adapter = ExchangeManager(exchange_name="okx").get_adapter()
        assert isinstance(adapter, OKXAdapter)

    def test_myokx_alias_gives_okx_adapter(self):
        adapter = ExchangeManager(exchange_name="myokx").get_adapter()
        assert isinstance(adapter, OKXAdapter)

    @pytest.mark.parametrize("name", ["BINANCE", "Binance", "  binance "])
    def test_case_insensitive_binance(self, name):
        assert isinstance(ExchangeManager(name).get_adapter(), BinanceAdapter)

    @pytest.mark.parametrize("name", ["OKX", "Okx", "okx"])
    def test_case_insensitive_okx(self, name):
        assert isinstance(ExchangeManager(name).get_adapter(), OKXAdapter)

    def test_unknown_exchange_raises(self):
        with pytest.raises(UnsupportedExchangeError):
            ExchangeManager("kraken")

    @pytest.mark.parametrize("bad", [None, "", "   "])
    def test_missing_name_raises(self, bad):
        with pytest.raises(AdapterNotConfiguredError):
            ExchangeManager(bad)

    def test_manager_caches_adapter_instance(self):
        manager = ExchangeManager("binance")
        assert manager.get_adapter() is manager.get_adapter()

    def test_exchange_name_property_is_normalized(self):
        assert ExchangeManager("  BINANCE ").exchange_name == "binance"


class TestGetExchangeAdapter:
    def test_binance_entrypoint(self):
        assert isinstance(get_exchange_adapter("binance"), BinanceAdapter)

    def test_okx_entrypoint(self):
        assert isinstance(get_exchange_adapter("okx"), OKXAdapter)

    def test_unknown_raises(self):
        with pytest.raises(UnsupportedExchangeError):
            get_exchange_adapter("bitfinex")

    def test_cache_returns_same_instance(self):
        assert get_exchange_adapter("okx") is get_exchange_adapter("OKX")

    def test_fresh_bypasses_cache(self):
        first = get_exchange_adapter("binance")
        second = get_exchange_adapter("binance", fresh=True)
        assert first is not second
        assert get_exchange_adapter("binance") is second

    def test_clear_cache_gives_new_instance(self):
        first = get_exchange_adapter("binance")
        clear_adapter_cache()
        assert get_exchange_adapter("binance") is not first
