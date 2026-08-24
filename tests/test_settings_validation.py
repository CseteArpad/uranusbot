"""
U-0.1 – a settings_validation modul egységtesztjei (U0-SEC-001).

Ezek a tesztek tisztán a validációs logikát fedik: nincs Flask, nincs
fájlrendszer, nincs subprocess, nincs hálózat.
"""
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "app")
if APP_DIR not in sys.path:
    sys.path.append(APP_DIR)

from settings_validation import (  # noqa: E402
    ALLOWED_ENV_KEYS,
    MAX_FIELD_LEN,
    SUPPORTED_TIMEFRAMES,
    SettingsValidationError,
    assert_dropin_safe,
    ensure_safe_scalar,
    validate_ft_url,
    validate_pair,
    validate_settings_payload,
    validate_timeframe,
)

INJECTED_DIRECTIVE = "ExecStartPre=/bin/true"


# --------------------------------------------------------------------------- #
# ensure_safe_scalar
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "value",
    ["a\nb", "a\rb", "a\x00b", "a\tb", "a\x1bb", "a\x7fb", "árvíztűrő", "a b"],
)
def test_ensure_safe_scalar_rejects_control_and_non_ascii(value):
    with pytest.raises(SettingsValidationError):
        ensure_safe_scalar("mezo", value)


@pytest.mark.parametrize("value", [None, 42, 3.14, True, [], {}])
def test_ensure_safe_scalar_rejects_non_string(value):
    with pytest.raises(SettingsValidationError):
        ensure_safe_scalar("mezo", value)


def test_ensure_safe_scalar_rejects_overlong_value():
    with pytest.raises(SettingsValidationError):
        ensure_safe_scalar("mezo", "x" * (MAX_FIELD_LEN + 1))


def test_ensure_safe_scalar_accepts_plain_ascii():
    assert ensure_safe_scalar("mezo", "XRP/USDC") == "XRP/USDC"


# --------------------------------------------------------------------------- #
# validate_pair
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("XRP/USDC", "XRP/USDC"),
        ("  XRP/USDC  ", "XRP/USDC"),
        ("xrp/usdc", "XRP/USDC"),
        ("BTC/USDT", "BTC/USDT"),
    ],
)
def test_validate_pair_accepts_canonical_forms(raw, expected):
    assert validate_pair(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "XRP/USDC\n" + INJECTED_DIRECTIVE,
        "XRP/USDC\r" + INJECTED_DIRECTIVE,
        "XRP/USDC\x00",
        "XRPUSDC",          # konkatenált alak a drop-inban nem engedélyezett
        "XRP-USDC",         # OKX-dialektus sem
        "XRP/",
        "/USDC",
        "XRP//USDC",
        "XRP USDC",
        "XRP/USDC/EUR",
        "",
        "   ",
        "X",
    ],
)
def test_validate_pair_rejects_invalid(bad):
    with pytest.raises(SettingsValidationError):
        validate_pair(bad)


def test_validate_pair_blocks_newline_before_reaching_pair_utils():
    """
    Regressziós védelem: a pair_utils NEM alkalmas önmagában injekció-szűrésre.

    A split_pair() a beágyazott sortörést átengedi (a .strip() csak a külső
    whitespace-t vágja); csak akkor bukik el, ha az injektált szövegben
    véletlenül van '/' vagy '-'. Az alábbi direktíva egyiket sem tartalmazza,
    ezért a parser elfogadja – ez bizonyítja, hogy a karakterkészlet-
    ellenőrzésnek MINDIG meg kell előznie a pár-feloldást.
    """
    from exchange import pair_utils

    smuggled = "XRP/USDC\nEnvironment=EVIL"
    # A parser önmagában átengedi – ez pontosan a veszély forrása:
    assert "\n" in pair_utils.normalize_pair(smuggled)
    # A validátor viszont elutasítja:
    with pytest.raises(SettingsValidationError):
        validate_pair(smuggled)


# --------------------------------------------------------------------------- #
# validate_timeframe
# --------------------------------------------------------------------------- #

def test_validate_timeframe_accepts_every_supported_value():
    for tf in SUPPORTED_TIMEFRAMES:
        assert validate_timeframe(tf) == tf


def test_validate_timeframe_is_case_sensitive_for_minute_vs_month():
    assert validate_timeframe("1m") == "1m"
    assert validate_timeframe("1M") == "1M"


@pytest.mark.parametrize(
    "bad", ["7m", "1y", "60s", "", "  ", "1h\n" + INJECTED_DIRECTIVE, "1h;rm", "1H"]
)
def test_validate_timeframe_rejects_invalid(bad):
    with pytest.raises(SettingsValidationError):
        validate_timeframe(bad)


# --------------------------------------------------------------------------- #
# validate_ft_url
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090"),
        ("http://127.0.0.1:8090/", "http://127.0.0.1:8090"),
        ("https://ft.example.com", "https://ft.example.com"),
        ("http://localhost:8080/api-prefix", "http://localhost:8080/api-prefix"),
        ("  http://127.0.0.1:8090  ", "http://127.0.0.1:8090"),
    ],
)
def test_validate_ft_url_accepts_and_canonicalizes(raw, expected):
    assert validate_ft_url(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "http://127.0.0.1:8090\n" + INJECTED_DIRECTIVE,
        "http://user:pass@127.0.0.1:8090",
        "file:///etc/passwd",
        "ftp://127.0.0.1",
        "javascript:alert(1)",
        "http://",
        "127.0.0.1:8090",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:-1",
        "http://127.0.0.1:8090?x=1",
        "http://127.0.0.1:8090#frag",
        "http://exa mple.com",
        "",
    ],
)
def test_validate_ft_url_rejects_invalid(bad):
    with pytest.raises(SettingsValidationError):
        validate_ft_url(bad)


# --------------------------------------------------------------------------- #
# validate_settings_payload
# --------------------------------------------------------------------------- #

def _payload(**overrides):
    base = {
        "pair": "XRP/USDC",
        "timeframe": "1m",
        "ft_url": "http://127.0.0.1:8090",
        "limit": 200,
        "kill_switch": False,
        "daily_loss_cap_pct": 2.0,
    }
    base.update(overrides)
    return base


def test_validate_settings_payload_returns_canonical_copy():
    original = _payload(pair="xrp/usdc", ft_url="http://127.0.0.1:8090/")
    result = validate_settings_payload(original)
    assert result["pair"] == "XRP/USDC"
    assert result["ft_url"] == "http://127.0.0.1:8090"
    # Az eredeti dict nem módosul.
    assert original["pair"] == "xrp/usdc"


@pytest.mark.parametrize("field", ["pair", "timeframe", "ft_url"])
def test_validate_settings_payload_rejects_injection_in_any_free_field(field):
    with pytest.raises(SettingsValidationError):
        validate_settings_payload(_payload(**{field: "x\n" + INJECTED_DIRECTIVE}))


def test_validate_settings_payload_rejects_non_dict():
    with pytest.raises(SettingsValidationError):
        validate_settings_payload("nem dict")


def test_validate_settings_payload_checks_extra_string_fields():
    with pytest.raises(SettingsValidationError):
        validate_settings_payload(_payload(some_extra="a\n" + INJECTED_DIRECTIVE))


# --------------------------------------------------------------------------- #
# assert_dropin_safe
# --------------------------------------------------------------------------- #

def test_assert_dropin_safe_accepts_valid_dropin():
    text = "[Service]\n\nEnvironment=PAIR=XRP/USDC\nEnvironment=KILL_SWITCH=0\n"
    assert assert_dropin_safe(text) == text


@pytest.mark.parametrize(
    "bad",
    [
        "[Service]\n" + INJECTED_DIRECTIVE + "\n",
        "[Service]\nUser=root\n",
        "[Service]\nEnvironment=UNKNOWN_KEY=1\n",
        "[Service]\nEnvironment=PAIR\n",
        "[Service]\r\nEnvironment=PAIR=XRP/USDC\n",
        "[Unit]\nEnvironment=PAIR=XRP/USDC\n",
        "[Service]\nEnvironment=PAIR=XRP/USDC\n\x00",
    ],
)
def test_assert_dropin_safe_rejects_foreign_directives(bad):
    with pytest.raises(SettingsValidationError):
        assert_dropin_safe(bad)


def test_allowed_env_keys_cover_the_free_text_fields():
    for key in ("PAIR", "TIMEFRAME", "FT_URL"):
        assert key in ALLOWED_ENV_KEYS
