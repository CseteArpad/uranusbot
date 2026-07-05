import pytest

from app.exchange.base import ExchangeAdapter
from app.exchange.binance import BinanceAdapter
from app.exchange.capabilities import ExchangeCapabilities
from app.exchange.okx import OKXAdapter

EXPECTED_BINANCE = ExchangeCapabilities(
    supports_spot=True,
    supports_margin=False,
    supports_public_ohlcv=True,
    supports_balance=True,
    supports_market_orders=True,
    supports_limit_orders=True,
    supports_dust_conversion=True,
)

EXPECTED_OKX = ExchangeCapabilities(
    supports_spot=True,
    supports_margin=False,
    supports_public_ohlcv=True,
    supports_balance=True,
    supports_market_orders=True,
    supports_limit_orders=True,
    supports_dust_conversion=False,
)


class TestCapabilities:
    def test_binance_capabilities(self):
        assert BinanceAdapter().capabilities == EXPECTED_BINANCE

    def test_okx_capabilities(self):
        assert OKXAdapter().capabilities == EXPECTED_OKX

    def test_okx_has_no_dust_conversion(self):
        assert OKXAdapter().capabilities.supports_dust_conversion is False

    def test_capabilities_frozen(self):
        with pytest.raises(Exception):
            BinanceAdapter().capabilities.supports_spot = False


class TestAdapterBasics:
    @pytest.mark.parametrize("adapter_cls", [BinanceAdapter, OKXAdapter])
    def test_is_exchange_adapter(self, adapter_cls):
        assert isinstance(adapter_cls(), ExchangeAdapter)

    @pytest.mark.parametrize("adapter_cls", [BinanceAdapter, OKXAdapter])
    def test_normalize_pair(self, adapter_cls):
        adapter = adapter_cls()
        assert adapter.normalize_pair("xrp-usdc") == "XRP/USDC"
        assert adapter.normalize_pair("XRPUSDC") == "XRP/USDC"

    def test_native_symbol_formats(self):
        assert BinanceAdapter().to_exchange_symbol("XRP/USDC") == "XRPUSDC"
        assert OKXAdapter().to_exchange_symbol("XRP/USDC") == "XRP-USDC"


class TestSkeletonMethodsRaise:
    # get_ticker a FÁZIS 7.5.3a-ban implementálva lett - kikerült a listából.
    CALLS = [
        ("get_ohlcv", ("XRP/USDC", "1h")),
        ("get_balances", ()),
        ("create_order", ("XRP/USDC", "buy", "market", 1.0)),
        ("cancel_order", ("order-1",)),
        ("convert_dust", (["XRP"],)),
    ]

    @pytest.mark.parametrize("adapter_cls", [BinanceAdapter, OKXAdapter])
    @pytest.mark.parametrize("method,args", CALLS)
    def test_not_implemented(self, adapter_cls, method, args):
        with pytest.raises(NotImplementedError):
            getattr(adapter_cls(), method)(*args)
