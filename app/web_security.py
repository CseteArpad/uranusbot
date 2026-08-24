"""
Uranus U-0.1 – központi authentikáció és CSRF-védelem (U0-SEC-003/004/005).

Egyetlen ``before_request`` hook véd MINDEN state-changing kérést; a védelem
nincs route-onként duplikálva. A read-only (GET/HEAD) végpontok szándékosan
nyitva maradnak, mert a UI olvasási útvonala változatlan működést igényel.

Két auth-modell, explicit módon elkülönítve:

  * **API / operátori kliens** – ``Authorization: Bearer <token>`` vagy
    ``X-Uranus-Token: <token>`` fejléc. Böngésző cross-origin módon nem tud
    egyedi fejlécet küldeni preflight nélkül, ezért itt CSRF-token nem kell.
  * **Böngésző** – session cookie (``POST /ui/login`` után). Mivel a cookie-t a
    böngésző automatikusan csatolja, itt a mutációhoz KÖTELEZŐ a sessionhöz
    kötött, véletlenszerű CSRF-token az ``X-CSRF-Token`` fejlécben.

Fail-closed viselkedés:
    Ha az ``URANUS_UI_TOKEN`` nincs beállítva, semmilyen mutáció nem hajtható
    végre (HTTP 503). Nincs beégetett alapértelmezett jelszó, és nincs olyan
    üzemmód, amelyben a védelem csendben kikapcsol.

Státuszkódok:
    503 – a szerver nincs beállítva (hiányzó ``URANUS_UI_TOKEN``)
    401 – hiányzó vagy érvénytelen hitelesítő adat
    403 – hitelesített kérés, de hiányzó/érvénytelen CSRF-token
"""
from __future__ import annotations

import hmac
import os
import secrets

from flask import jsonify, request, session

TOKEN_ENV = "URANUS_UI_TOKEN"
SECRET_KEY_ENV = "URANUS_UI_SECRET_KEY"

SESSION_AUTH_KEY = "uranus_authenticated"
CSRF_SESSION_KEY = "uranus_csrf_token"

CSRF_HEADER = "X-CSRF-Token"
TOKEN_HEADER = "X-Uranus-Token"

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Ezek az útvonalak mutáció esetén sem igényelnek meglévő auth-ot
#: (a bejelentkezés maga, illetve a kijelentkezés).
AUTH_EXEMPT_PATHS = frozenset({"/ui/login", "/ui/logout"})

_LOGIN_PAGE = """<!doctype html>
<html lang="hu"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Uranus Bot - Bejelentkezes</title>
<style>
 body{background:#0b1220;color:#e7eefc;font-family:system-ui,sans-serif;
      display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
 form{background:#101b33;padding:28px 32px;border-radius:12px;
      border:1px solid rgba(255,255,255,.08);min-width:300px}
 h1{font-size:1.1rem;margin:0 0 16px}
 input{width:100%;padding:9px 10px;border-radius:7px;border:1px solid rgba(255,255,255,.14);
       background:#0c1730;color:#e7eefc;font-size:1rem;box-sizing:border-box}
 button{margin-top:14px;width:100%;padding:9px;border-radius:7px;border:0;
        background:#2b6cb0;color:#fff;font-size:1rem;cursor:pointer}
 p{color:#9db0d1;font-size:.85rem;margin:14px 0 0}
</style></head>
<body><form method="post" action="/ui/login">
<h1>Uranus Bot - Bejelentkezes</h1>
<label for="token">Operator token</label>
<input id="token" name="token" type="password" autocomplete="current-password" autofocus>
<button type="submit">Belepes</button>
<p>A token forrasa az URANUS_UI_TOKEN kornyezeti valtozo.</p>
</form></body></html>
"""


# --------------------------------------------------------------------------- #
# Konfiguráció
# --------------------------------------------------------------------------- #

def configured_token() -> str:
    """
    Az operátori token a környezetből. Kérésenként olvassuk (nem import-időben),
    hogy a konfiguráció service-restart nélkül is konzisztens legyen, és hogy a
    tesztek monkeypatch-elni tudják. Beégetett alapértelmezés NINCS.
    """
    return os.environ.get(TOKEN_ENV, "") or ""


def is_auth_configured() -> bool:
    return bool(configured_token())


def resolve_secret_key() -> str:
    """
    Flask session-kulcs. Ha nincs konfigurálva, folyamatonként generálunk egy
    véletlen kulcsot: ilyenkor a sessionök egy újraindítást nem élnek túl
    (újra be kell jelentkezni), de beégetett titok soha nem kerül a kódba.
    """
    configured = os.environ.get(SECRET_KEY_ENV, "") or ""
    return configured if configured else secrets.token_urlsafe(48)


# --------------------------------------------------------------------------- #
# Token- és CSRF-kezelés
# --------------------------------------------------------------------------- #

def _constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(str(left or ""), str(right or ""))


def _presented_token() -> str:
    """Bearer vagy egyedi fejléc – ebben a sorrendben."""
    header = request.headers.get("Authorization", "") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return (request.headers.get(TOKEN_HEADER, "") or "").strip()


def issue_csrf_token() -> str:
    """Új, sessionhöz kötött CSRF-token. Minden bejelentkezéskor friss."""
    token = secrets.token_urlsafe(32)
    session[CSRF_SESSION_KEY] = token
    return token


def current_csrf_token() -> str:
    """A session aktuális CSRF-tokenje (üres string, ha nincs bejelentkezve)."""
    if not session.get(SESSION_AUTH_KEY):
        return ""
    return session.get(CSRF_SESSION_KEY, "") or ""


def is_session_authenticated() -> bool:
    return bool(session.get(SESSION_AUTH_KEY))


def _has_valid_header_token() -> bool:
    expected = configured_token()
    if not expected:
        return False
    presented = _presented_token()
    return bool(presented) and _constant_time_equals(presented, expected)


# --------------------------------------------------------------------------- #
# Központi kikényszerítés
# --------------------------------------------------------------------------- #

def _json_error(code: int, error: str, message: str):
    response = jsonify({"ok": False, "error": error, "message": message})
    response.status_code = code
    return response


def enforce_request_security():
    """
    ``before_request`` hook. ``None`` visszatérés = a kérés mehet tovább.

    Csak a state-changing metódusokat védi; a GET/HEAD olvasási út érintetlen.
    """
    if request.method not in MUTATING_METHODS:
        return None
    if request.path in AUTH_EXEMPT_PATHS:
        return None

    if not is_auth_configured():
        return _json_error(
            503,
            "auth_not_configured",
            f"A(z) {TOKEN_ENV} nincs beallitva, ezert minden allapotvaltoztatas tiltott.",
        )

    if _has_valid_header_token():
        return None

    if is_session_authenticated():
        expected = session.get(CSRF_SESSION_KEY, "") or ""
        presented = (request.headers.get(CSRF_HEADER, "") or "").strip()
        if not expected or not presented or not _constant_time_equals(presented, expected):
            return _json_error(
                403,
                "csrf_failed",
                f"Hianyzo vagy ervenytelen {CSRF_HEADER} fejlec.",
            )
        return None

    return _json_error(
        401,
        "unauthorized",
        "Hitelesites szukseges: Bearer token vagy bejelentkezett session.",
    )


# --------------------------------------------------------------------------- #
# Telepítés a Flask alkalmazásba
# --------------------------------------------------------------------------- #

def install_security(app) -> None:
    """
    Regisztrálja a központi védelmet és a bejelentkezési végpontokat.

    Idempotens: ugyanarra az app-példányra kétszer hívva nem duplikál.
    """
    if getattr(app, "_uranus_security_installed", False):
        return

    if not app.secret_key:
        app.secret_key = resolve_secret_key()
    app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    app.config.setdefault("SESSION_COOKIE_SAMESITE", "Strict")

    app.before_request(enforce_request_security)

    @app.context_processor
    def _inject_csrf_token():
        # A sablonok innen kapják a <meta name="csrf-token"> értékét.
        try:
            return {"csrf_token": current_csrf_token()}
        except Exception:
            return {"csrf_token": ""}

    @app.get("/ui/login")
    def security_login_page():
        return _LOGIN_PAGE, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.post("/ui/login")
    def security_login():
        expected = configured_token()
        if not expected:
            return _json_error(
                503,
                "auth_not_configured",
                f"A(z) {TOKEN_ENV} nincs beallitva.",
            )

        presented = ""
        if request.is_json:
            payload = request.get_json(silent=True) or {}
            presented = str(payload.get("token", "") or "")
        if not presented:
            presented = str(request.form.get("token", "") or "")
        if not presented:
            presented = _presented_token()

        if not presented or not _constant_time_equals(presented, expected):
            return _json_error(401, "unauthorized", "Ervenytelen token.")

        session.clear()
        session[SESSION_AUTH_KEY] = True
        csrf = issue_csrf_token()

        if request.form.get("token"):
            # Böngészős űrlap: vissza a kezelőfelületre.
            from flask import redirect

            return redirect("/")
        return jsonify({"ok": True, "csrf_token": csrf})

    @app.post("/ui/logout")
    def security_logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/csrf-token")
    def security_csrf_token():
        if not is_session_authenticated():
            return _json_error(401, "unauthorized", "Nincs bejelentkezett session.")
        return jsonify({"ok": True, "csrf_token": current_csrf_token()})

    app._uranus_security_installed = True
