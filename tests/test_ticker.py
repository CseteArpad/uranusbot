"""
FÁZIS 7.5.3a – adapter get_ticker tesztek.

Minden HTTP hívás mockolt (requests.get patch) – élő hálózat TILOS.
"""
import pytest
import requests

from app.exchange.binance import BinanceAdapter
from app.exchange.exceptions import ExchangeError, TickerFetchError
from app.exchange.okx import OKXAdapter


class FakeResponse:
    def __init__(self, payload=None, status_code=200, json_error=False):
        self.payload = payload
        self.status_code = status_code
        self._json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._json_error:
            raise ValueError("not a JSON body")
        return self.payload


class RecordingGet:
    """requests.get helyettesítő: rögzíti a hívást, előre beállított választ ad."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def __call__(self, url, params=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "params": params or {}, "timeout": timeout})
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture
def patch_get(monkeypatch):
    def _patch(response=None, exc=None):
        fake = RecordingGet(response=response, exc=exc)
        monkeypatch.setattr(requests, "get", fake)
        return fake

    return _patch


BINANCE_OK = {"symbol": "XRPUSDC", "price": "0.5234"}
OKX_OK = {
    "code": "0",
    "msg": "",
    "data": [{"instId": "XRP-USDC", "last": "0.5241", "askPx": "0.5242"}],
}


class TestBinanceGetTicker:
    def test_ok_response_gives_correct_dict(self, patch_get):
        fake = patch_get(FakeResponse(BINANCE_OK))
        tick = BinanceAdapter().get_ticker("XRP/USDC")

        assert tick["pair"] == "XRP/USDC"
        assert tick["symbol"] == "XRPUSDC"
        assert tick["price"] == pytest.approx(0.5234)
        assert isinstance(tick["price"], float)
        assert isinstance(tick["ts"], int)
        assert tick["ts_utc"].endswith("Z")
        assert tick["source"] == "binance"

        assert fake.calls[0]["url"] == "https://api.binance.com/api/v3/ticker/price"
        assert fake.calls[0]["params"] == {"symbol": "XRPUSDC"}
        assert fake.calls[0]["timeout"] is not None

    @pytest.mark.parametrize("raw", ["XRP-USDC", "xrpusdc"])
    def test_pair_dialects_are_normalized(self, patch_get, raw):
        fake = patch_get(FakeResponse(BINANCE_OK))
        tick = BinanceAdapter().get_ticker(raw)
        assert tick["pair"] == "XRP/USDC"
        assert fake.calls[0]["params"] == {"symbol": "XRPUSDC"}

    def test_http_error_raises_ticker_fetch_error(self, patch_get):
        patch_get(FakeResponse({}, status_code=502))
        with pytest.raises(TickerFetchError):
            BinanceAdapter().get_ticker("XRP/USDC")

    def test_network_error_raises_ticker_fetch_error(self, patch_get):
        patch_get(exc=requests.ConnectionError("connection refused"))
        with pytest.raises(TickerFetchError):
            BinanceAdapter().get_ticker("XRP/USDC")

    @pytest.mark.parametrize(
        "payload",
        [{}, {"symbol": "XRPUSDC"}, {"price": "not-a-number"}, ["unexpected"], None],
    )
    def test_invalid_payload_raises_ticker_fetch_error(self, patch_get, payload):
        patch_get(FakeResponse(payload))
        with pytest.raises(TickerFetchError):
            BinanceAdapter().get_ticker("XRP/USDC")

    def test_non_json_body_raises_ticker_fetch_error(self, patch_get):
        patch_get(FakeResponse(json_error=True))
        with pytest.raises(TickerFetchError):
            BinanceAdapter().get_ticker("XRP/USDC")

    def test_ticker_fetch_error_is_exchange_error(self, patch_get):
        patch_get(exc=requests.ConnectionError("down"))
        with pytest.raises(ExchangeError):
            BinanceAdapter().get_ticker("XRP/USDC")


class TestOKXGetTicker:
    def test_ok_response_gives_correct_dict(self, patch_get):
        fake = patch_get(FakeResponse(OKX_OK))
        tick = OKXAdapter().get_ticker("XRP/USDC")

        assert tick["pair"] == "XRP/USDC"
        assert tick["symbol"] == "XRP-USDC"
        assert tick["price"] == pytest.approx(0.5241)
        assert isinstance(tick["ts"], int)
        assert tick["ts_utc"].endswith("Z")
        assert tick["source"] == "okx"

        assert fake.calls[0]["url"] == "https://www.okx.com/api/v5/market/ticker"
        assert fake.calls[0]["params"] == {"instId": "XRP-USDC"}
        assert fake.calls[0]["timeout"] is not None

    @pytest.mark.parametrize("raw", ["XRPUSDC", "xrp/usdc"])
    def test_pair_dialects_are_normalized(self, patch_get, raw):
        fake = patch_get(FakeResponse(OKX_OK))
        tick = OKXAdapter().get_ticker(raw)
        assert tick["pair"] == "XRP/USDC"
        assert fake.calls[0]["params"] == {"instId": "XRP-USDC"}

    def test_http_error_raises_ticker_fetch_error(self, patch_get):
        patch_get(FakeResponse({}, status_code=500))
        with pytest.raises(TickerFetchError):
            OKXAdapter().get_ticker("XRP/USDC")

    def test_network_error_raises_ticker_fetch_error(self, patch_get):
        patch_get(exc=requests.Timeout("timed out"))
        with pytest.raises(TickerFetchError):
            OKXAdapter().get_ticker("XRP/USDC")

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            None,
            {"code": "51001", "msg": "instrument not exist", "data": []},
            {"code": "0", "data": []},
            {"code": "0", "data": [{}]},
            {"code": "0", "data": [{"last": ""}]},
            {"code": "0", "data": [{"last": "not-a-number"}]},
            {"code": "0", "data": "unexpected"},
        ],
    )
    def test_invalid_payload_raises_ticker_fetch_error(self, patch_get, payload):
        patch_get(FakeResponse(payload))
        with pytest.raises(TickerFetchError):
            OKXAdapter().get_ticker("XRP/USDC")

    def test_non_json_body_raises_ticker_fetch_error(self, patch_get):
        patch_get(FakeResponse(json_error=True))
        with pytest.raises(TickerFetchError):
            OKXAdapter().get_ticker("XRP/USDC")
