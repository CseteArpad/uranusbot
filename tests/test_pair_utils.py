import pytest

from app.exchange import pair_utils
from app.exchange.exceptions import InvalidPairError


class TestNormalizePair:
    @pytest.mark.parametrize(
        "raw",
        ["XRP/USDC", "xrp/usdc", " XRP/USDC ", "XRP-USDC", "XRPUSDC", "xrpusdc"],
    )
    def test_all_dialects_normalize_to_slash(self, raw):
        assert pair_utils.normalize_pair(raw) == "XRP/USDC"

    def test_other_quotes(self):
        assert pair_utils.normalize_pair("BTCUSDT") == "BTC/USDT"
        assert pair_utils.normalize_pair("SOL-USDC") == "SOL/USDC"

    @pytest.mark.parametrize("bad", ["", None, "/", "XRP/", "/USDC", "XRP"])
    def test_invalid_raises(self, bad):
        with pytest.raises(InvalidPairError):
            pair_utils.normalize_pair(bad)

    def test_unknown_concatenated_quote_raises(self):
        with pytest.raises(InvalidPairError):
            pair_utils.normalize_pair("XRPFOO")


class TestSymbolConversion:
    def test_to_binance_symbol(self):
        assert pair_utils.to_binance_symbol("XRP/USDC") == "XRPUSDC"
        assert pair_utils.to_binance_symbol("XRP-USDC") == "XRPUSDC"

    def test_to_okx_inst_id(self):
        assert pair_utils.to_okx_inst_id("XRP/USDC") == "XRP-USDC"
        assert pair_utils.to_okx_inst_id("XRPUSDC") == "XRP-USDC"
