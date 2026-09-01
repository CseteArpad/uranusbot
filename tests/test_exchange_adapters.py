import pytest

from app import execution_venue_policy
from app.exchange.base import ExchangeAdapter
from app.exchange.binance import BinanceAdapter
from app.exchange.capabilities import ExchangeCapabilities
from app.exchange.okx import OKXAdapter

# OKX_SPOT_ONLY (tulajdonosi dontes, 2026-09-01): a Binance adapter piaci
# adat / provenance celra megmarad, de a RENDELESI kepessegek kikapcsolva.
EXPECTED_BINANCE = ExchangeCapabilities(
    supports_spot=True,
    supports_margin=False,
    supports_public_ohlcv=True,
    supports_balance=True,
    supports_market_orders=False,
    supports_limit_orders=False,
    supports_dust_conversion=False,
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
    # A rendelesi metodusok a Binance adapteren mar NEM skeletonok, hanem
    # veglegesen kivezetettek -> kulon osztalyban ellenorizzuk oket.
    DATA_CALLS = [
        ("get_ohlcv", ("XRP/USDC", "1h")),
        ("get_balances", ()),
    ]
    ORDER_CALLS = [
        ("create_order", ("XRP/USDC", "buy", "market", 1.0)),
        ("cancel_order", ("order-1",)),
        ("convert_dust", (["XRP"],)),
    ]

    @pytest.mark.parametrize("adapter_cls", [BinanceAdapter, OKXAdapter])
    @pytest.mark.parametrize("method,args", DATA_CALLS)
    def test_not_implemented(self, adapter_cls, method, args):
        with pytest.raises(NotImplementedError):
            getattr(adapter_cls(), method)(*args)

    @pytest.mark.parametrize("method,args", ORDER_CALLS)
    def test_okx_order_methods_still_skeleton(self, method, args):
        """Az OKX rendelesi felulet meg nincs implementalva - de NEM kivezetve."""
        with pytest.raises(NotImplementedError):
            getattr(OKXAdapter(), method)(*args)


class TestBinanceOrderSurfaceRetired:
    """
    A Binance adapter rendelesi felulete VEGLEGESEN kivezetve.

    Ez viselkedesi teszt: nem azt bizonyitja, hogy bizonyos szavak szerepelnek
    a forrasban, hanem hogy a hivas ExecutionVenueForbidden-nel elszall - es
    hogy ez MAS hibaosztaly, mint a "meg nincs implementalva" NotImplementedError.
    """

    ORDER_CALLS = [
        ("create_order", ("XRP/USDC", "buy", "market", 1.0)),
        ("cancel_order", ("order-1",)),
        ("convert_dust", (["XRP"],)),
    ]

    @pytest.mark.parametrize("method,args", ORDER_CALLS)
    def test_order_methods_raise_venue_forbidden(self, method, args):
        with pytest.raises(execution_venue_policy.ExecutionVenueForbidden) as excinfo:
            getattr(BinanceAdapter(), method)(*args)
        assert excinfo.value.verdict.code == execution_venue_policy.REFUSE_VENUE_RETIRED
        assert excinfo.value.verdict.venue == "binance"

    @pytest.mark.parametrize("method,args", ORDER_CALLS)
    def test_retirement_is_not_merely_unimplemented(self, method, args):
        """A kivezetes NEM NotImplementedError - a kulonbseg szandekos."""
        with pytest.raises(execution_venue_policy.ExecutionVenueForbidden):
            getattr(BinanceAdapter(), method)(*args)
        assert not issubclass(
            execution_venue_policy.ExecutionVenueForbidden, NotImplementedError
        )

    def test_capabilities_declare_no_order_support(self):
        caps = BinanceAdapter().capabilities
        assert caps.supports_market_orders is False
        assert caps.supports_limit_orders is False
        assert caps.supports_dust_conversion is False

    def test_market_data_surface_survives(self):
        """A piaci adat / provenance ut NEM serul: get_ticker megmarad."""
        caps = BinanceAdapter().capabilities
        assert caps.supports_public_ohlcv is True
        assert callable(BinanceAdapter().get_ticker)
