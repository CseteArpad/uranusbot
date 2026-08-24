"""
U-0.1 security regressziós tesztek.

Lefedett findingek:
  U0-SEC-001  systemd drop-in / directive injection -> RCE
  U0-SEC-002  beégetett credential (fail-closed konfiguráció)
  U0-SEC-003  authentikáció nélküli service-vezérlés
  U0-SEC-004  authentikáció nélküli settings-módosítás
  U0-SEC-005  CSRF-védelem hiánya

Biztonsági garanciák a tesztekben (a feladat O) pontja):
  * a ``guard`` fixture minden ``subprocess.run`` hívást AssertionError-rel
    buktat, tehát valódi ``systemctl`` sosem futhat;
  * a drop-in írása memóriába megy (``_atomic_write_text`` ki van cserélve),
    tehát valódi fájl nem íródik;
  * hálózati hívás nem történik.

Az injekciós teszt szándékosan ÁRTALMATLAN markert használ (``/bin/true``),
sosem valódi káros parancsot.
"""
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(REPO_ROOT, "app")
APP_PY = os.path.join(APP_DIR, "app.py")

# Teszt-token: nem valódi titok, csak a hitelesítési út ellenőrzésére.
TEST_TOKEN = "u0-test-token-not-a-real-secret"

# Ártalmatlan marker: ha a validáció meghibásodna, ez a sor jelenne meg a
# drop-inban. A tesztek arra épülnek, hogy ez SOHA nem történik meg.
INJECTED_DIRECTIVE = "ExecStartPre=/bin/true"


# --------------------------------------------------------------------------- #
# Fixture-ök
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def web_app():
    """
    Az app/app.py betöltése egyedi modulnévvel.

    Az app/ könyvtárat csak *hozzáfűzzük* a sys.path-hoz, hogy a repo gyökere
    elöl maradjon: így a többi teszt ``from app.exchange import ...`` importja
    továbbra is a csomagot találja meg, nem az app.py-t.
    """
    if APP_DIR not in sys.path:
        sys.path.append(APP_DIR)
    spec = importlib.util.spec_from_file_location("uranus_web_app_undertest", APP_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.app.config["TESTING"] = True
    return module


class _FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def guard(web_app, monkeypatch):
    """
    Elszigeteli a mellékhatásokat és rögzíti a hívásokat.

    Alapértelmezésben a ``subprocess.run`` hívás AssertionError-t dob: azok a
    tesztek, amelyek elutasított kérést vizsgálnak, így bizonyítják, hogy
    systemctl egyáltalán nem indult.
    """
    calls = {"writes": [], "subprocess": []}

    def _forbidden_run(*args, **kwargs):
        raise AssertionError(f"subprocess.run nem futhatott volna: {args!r}")

    def _record_write(path, text):
        calls["writes"].append((path, text))

    monkeypatch.setattr(web_app.subprocess, "run", _forbidden_run)
    monkeypatch.setattr(web_app, "_atomic_write_text", _record_write)
    calls["_allow_subprocess"] = lambda: monkeypatch.setattr(
        web_app.subprocess,
        "run",
        lambda *a, **k: calls["subprocess"].append(a[0]) or _FakeCompleted(),
    )
    return calls


@pytest.fixture
def client(web_app, monkeypatch):
    monkeypatch.setenv("URANUS_UI_TOKEN", TEST_TOKEN)
    return web_app.app.test_client()


def bearer():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def valid_payload(**overrides):
    """Teljes, érvényes settings payload (minden mező kötelező a route-ban)."""
    payload = {
        "tick_seconds": 10.0,
        "pair": "XRP/USDC",
        "timeframe": "1m",
        "limit": 200,
        "execution_enabled": False,
        "execution_log_only": True,
        "buy_lock_ttl_sec": 180,
        "kill_switch": False,
        "max_trades_per_day": 6,
        "daily_loss_cap_pct": 2.0,
        "ft_url": "http://127.0.0.1:8090",
        "std_sell_enabled": True,
        "std_sell_pct": 1.0,
        "recovery_sell_retrace_pct": 0.5,
        "recovery_profit_target_pct": 0.1,
        "panic_sell_enabled": True,
        "panic_sell_pct": 5.0,
        "sell_reversal_min_pct": 0.0,
        "catastrophe_sell_pct": 10.0,
        "std_buy_enabled": True,
        "std_buy_pct": 1.0,
        "recovery_buy_rebound_pct": 0.5,
        "panic_buy_enabled": False,
        "panic_buy_pct": 5.0,
        "panic_buy_confirm_ticks": 2,
        "catastrophe_buy_pct": 10.0,
        "ma_filter_enabled": True,
        "ma_period": 20,
        "ma_sideways_band_pct": 0.05,
    }
    payload.update(overrides)
    return payload


def login_session(client):
    """Bejelentkezés session-alapon; visszaadja a CSRF-tokent."""
    res = client.post("/ui/login", json={"token": TEST_TOKEN})
    assert res.status_code == 200, res.data
    token = res.get_json()["csrf_token"]
    assert token
    return token


# --------------------------------------------------------------------------- #
# A) Authentikáció nélküli settings-módosítás
# --------------------------------------------------------------------------- #

def test_a_unauthenticated_settings_post_is_rejected(client, guard):
    res = client.post("/api/settings", json=valid_payload())
    assert res.status_code == 401
    assert res.get_json()["error"] == "unauthorized"
    assert guard["writes"] == []
    assert guard["subprocess"] == []


# --------------------------------------------------------------------------- #
# B–D) Injekciós kísérletek hitelesített kéréssel
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "field,value",
    [
        ("pair", "XRP/USDC\n" + INJECTED_DIRECTIVE),
        ("timeframe", "1m\n" + INJECTED_DIRECTIVE),
        ("ft_url", "http://127.0.0.1:8090\n" + INJECTED_DIRECTIVE),
    ],
)
def test_b_newline_injection_is_rejected(client, guard, field, value):
    res = client.post(
        "/api/settings", json=valid_payload(**{field: value}), headers=bearer()
    )
    assert res.status_code == 400
    assert guard["writes"] == []
    assert guard["subprocess"] == []


@pytest.mark.parametrize("field", ["pair", "timeframe", "ft_url"])
def test_c_carriage_return_injection_is_rejected(client, guard, field):
    payload = valid_payload(**{field: valid_payload()[field] + "\r" + INJECTED_DIRECTIVE})
    res = client.post("/api/settings", json=payload, headers=bearer())
    assert res.status_code == 400
    assert guard["writes"] == []


@pytest.mark.parametrize(
    "bad",
    [
        "XRP/USDC\x00",
        "XRP/USDC\x07",
        "XRP/USDC\x7f",
        # A vezérlőkaraktert beágyazott pozícióban teszteljük: a route saját
        # .strip() hívása a záró whitespace-t (pl. \t, \n) amúgy is levágja,
        # ami önmagában nem sérülékenység – a beágyazott eset a lényeges.
        "XRP\tUSDC",
        "XRP/US\x0bDC",
    ],
)
def test_d_control_characters_are_rejected(client, guard, bad):
    res = client.post("/api/settings", json=valid_payload(pair=bad), headers=bearer())
    assert res.status_code == 400
    assert guard["writes"] == []


# --------------------------------------------------------------------------- #
# E–G) Mező-validáció
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", ["7m", "1y", "60s", "1h;rm", "1H", "1m\x00"])
def test_e_invalid_timeframe_is_rejected(client, guard, bad):
    res = client.post("/api/settings", json=valid_payload(timeframe=bad), headers=bearer())
    assert res.status_code == 400
    assert guard["writes"] == []


@pytest.mark.parametrize("field", ["pair", "timeframe", "ft_url"])
def test_e2_empty_free_text_field_is_rejected_without_side_effects(client, guard, field):
    """
    Az üres mezőt a route korábbi, U-0.1 előtti ellenőrzése fogja meg (HTTP 500),
    a szabad szövegű mezők validátora pedig 400-at ad. A státuszkódot ezért nem
    rögzítjük; a biztonsági követelmény az, hogy NE legyen mellékhatás.
    """
    res = client.post("/api/settings", json=valid_payload(**{field: ""}), headers=bearer())
    assert res.status_code in (400, 500)
    assert guard["writes"] == []
    assert guard["subprocess"] == []


@pytest.mark.parametrize(
    "bad", ["XRPUSDC", "XRP/", "/USDC", "XRP//USDC", "XRP USDC", "XRP/USDC/EUR", "x"]
)
def test_f_invalid_pair_is_rejected(client, guard, bad):
    res = client.post("/api/settings", json=valid_payload(pair=bad), headers=bearer())
    assert res.status_code == 400
    assert guard["writes"] == []


@pytest.mark.parametrize(
    "bad",
    [
        "file:///etc/passwd",
        "ftp://127.0.0.1",
        "http://user:pass@127.0.0.1:8090",
        "http://127.0.0.1:99999",
        "http://",
        "127.0.0.1:8090",
        "http://127.0.0.1:8090?x=1",
        "http://127.0.0.1:8090#frag",
    ],
)
def test_g_invalid_ft_url_is_rejected(client, guard, bad):
    res = client.post("/api/settings", json=valid_payload(ft_url=bad), headers=bearer())
    assert res.status_code == 400
    assert guard["writes"] == []


# --------------------------------------------------------------------------- #
# H–J) Service-vezérlés
# --------------------------------------------------------------------------- #

def test_h_unauthenticated_service_control_is_rejected(client, guard):
    res = client.post("/ui/control/runner/stop")
    assert res.status_code == 401
    assert guard["subprocess"] == []


def test_h2_unauthenticated_service_control_all_is_rejected(client, guard):
    res = client.post("/ui/control/all/stop")
    assert res.status_code == 401
    assert guard["subprocess"] == []


@pytest.mark.parametrize("target", ["runner;rm", "root", "unknown", "RUNNER runner"])
def test_i_invalid_service_target_is_rejected(client, guard, target):
    res = client.post(f"/ui/control/{target}/stop", headers=bearer())
    assert res.status_code == 400
    assert res.get_json()["error"] == "invalid_target"
    assert guard["subprocess"] == []


@pytest.mark.parametrize("action", ["restart;reboot", "mask", "poweroff", "enable"])
def test_j_invalid_service_action_is_rejected(client, guard, action):
    res = client.post(f"/ui/control/runner/{action}", headers=bearer())
    assert res.status_code == 400
    assert res.get_json()["error"] == "invalid_action"
    assert guard["subprocess"] == []


# --------------------------------------------------------------------------- #
# K) Érvényes, hitelesített kérés
# --------------------------------------------------------------------------- #

def test_k_valid_authenticated_settings_request_writes_safe_dropin(client, guard):
    guard["_allow_subprocess"]()
    res = client.post("/api/settings", json=valid_payload(), headers=bearer())
    assert res.status_code == 200, res.data
    assert res.get_json()["ok"] is True

    assert len(guard["writes"]) == 1
    _path, text = guard["writes"][0]
    assert "Environment=PAIR=XRP/USDC" in text
    assert INJECTED_DIRECTIVE not in text
    for line in text.split("\n"):
        if line.strip() and line != "[Service]":
            assert line.startswith("Environment="), line

    # Pontosan a daemon-reload + restart pár futott le, ebben a sorrendben.
    assert len(guard["subprocess"]) == 2
    assert guard["subprocess"][0][-1] == "daemon-reload"
    assert guard["subprocess"][1][-1] == "uranus-runner.service"


# --------------------------------------------------------------------------- #
# L–M) CSRF
# --------------------------------------------------------------------------- #

def test_l_session_mutation_without_csrf_is_rejected(client, guard):
    login_session(client)
    res = client.post("/api/settings", json=valid_payload())
    assert res.status_code == 403
    assert res.get_json()["error"] == "csrf_failed"
    assert guard["writes"] == []
    assert guard["subprocess"] == []


def test_l2_session_mutation_with_wrong_csrf_is_rejected(client, guard):
    login_session(client)
    res = client.post(
        "/api/settings",
        json=valid_payload(),
        headers={"X-CSRF-Token": "wrong-token"},
    )
    assert res.status_code == 403
    assert guard["writes"] == []


def test_m_session_mutation_with_valid_csrf_succeeds(client, guard):
    csrf = login_session(client)
    guard["_allow_subprocess"]()
    res = client.post(
        "/api/settings", json=valid_payload(), headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 200, res.data
    assert len(guard["writes"]) == 1


def test_m2_service_control_with_valid_csrf_succeeds(client, guard):
    csrf = login_session(client)
    guard["_allow_subprocess"]()
    res = client.post("/ui/control/runner/stop", headers={"X-CSRF-Token": csrf})
    assert res.status_code in (200, 500)  # a systemctl mock sikerét nem szimuláljuk
    assert guard["subprocess"], "a hitelesített kérésnek el kellett jutnia a systemctl-ig"


def test_m3_invalid_login_token_is_rejected(client):
    res = client.post("/ui/login", json={"token": "not-the-token"})
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# N) Fail-closed hiányzó konfiguráció esetén
# --------------------------------------------------------------------------- #

def test_n_missing_ui_token_fails_closed(client, guard, monkeypatch):
    monkeypatch.delenv("URANUS_UI_TOKEN", raising=False)
    res = client.post("/api/settings", json=valid_payload(), headers=bearer())
    assert res.status_code == 503
    assert res.get_json()["error"] == "auth_not_configured"
    assert guard["writes"] == []
    assert guard["subprocess"] == []


def test_n2_missing_ui_token_blocks_service_control(client, guard, monkeypatch):
    monkeypatch.delenv("URANUS_UI_TOKEN", raising=False)
    res = client.post("/ui/control/runner/stop", headers=bearer())
    assert res.status_code == 503
    assert guard["subprocess"] == []


def test_n3_missing_ui_token_blocks_login(client, monkeypatch):
    monkeypatch.delenv("URANUS_UI_TOKEN", raising=False)
    res = client.post("/ui/login", json={"token": "anything"})
    assert res.status_code == 503


# --------------------------------------------------------------------------- #
# További state-changing végpontok és a read-only út érintetlensége
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("path", ["/api/rules", "/api/lab-rules"])
def test_other_mutations_require_auth(client, path):
    res = client.post(path, json={})
    assert res.status_code == 401


def test_read_only_endpoint_stays_open(client):
    """Regressziós védelem: a GET olvasási út működése változatlan."""
    res = client.get("/health")
    assert res.status_code == 200


def test_csrf_token_endpoint_requires_session(client):
    res = client.get("/api/csrf-token")
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# A legacy UI (ui_app.py, 8016) ugyanazt a védelmet kapja
# --------------------------------------------------------------------------- #

def test_legacy_ui_service_control_requires_auth(monkeypatch):
    if APP_DIR not in sys.path:
        sys.path.append(APP_DIR)
    monkeypatch.setenv("URANUS_UI_TOKEN", TEST_TOKEN)

    import ui_app

    legacy = ui_app.create_app()
    legacy.config["TESTING"] = True
    legacy_client = legacy.test_client()

    called = []
    monkeypatch.setattr(
        ui_app.subprocess,
        "run",
        lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
            AssertionError("subprocess.run nem futhatott volna")
        ),
    )

    res = legacy_client.post("/ui/control/runner/stop")
    assert res.status_code == 401
    assert called == []
