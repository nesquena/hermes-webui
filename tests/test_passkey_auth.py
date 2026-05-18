from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def read(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def test_requirements_include_webauthn_dependency():
    assert "webauthn>=2.2.0" in read("requirements.txt")


def test_passkey_login_endpoints_are_public_and_csrf_exempt():
    auth = read("api/auth.py")
    routes = read("api/routes.py")
    for endpoint in (
        "/api/auth/passkey/login/options",
        "/api/auth/passkey/login/verify",
    ):
        assert endpoint in auth
        assert endpoint in routes


def test_passkey_registration_endpoints_remain_authenticated():
    auth = read("api/auth.py")
    assert "/api/auth/passkey/register/options" not in auth
    assert "/api/auth/passkey/register/verify" not in auth


def test_passkey_status_and_login_ui_are_wired():
    routes = read("api/routes.py")
    login_js = read("static/login.js")
    panels_js = read("static/panels.js")
    index_html = read("static/index.html")
    assert "passkeys_enabled" in routes
    assert "passkey_count" in routes
    assert "navigator.credentials.get" in login_js
    assert "navigator.credentials.create" in panels_js
    assert "settingsPasskeyBlock" in index_html
