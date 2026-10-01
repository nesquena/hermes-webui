"""Regression tests for issue #7056 — login page must hide the password form
when password auth is disabled (e.g. native OIDC configured and
``HERMES_WEBUI_PASSWORD`` unset).

The original bug: ``/api/auth/status`` correctly reports
``password_auth_enabled: false`` when password auth is disabled, but
``/login`` still rendered the password input, the "Sign in" button,
and the "Continue with SSO" entry point — and silently 401-ed every
submit. The fix routes the password input / submit / passkey controls
through a single ``{{PASSWORD_FORM_HTML}}`` placeholder in the
template, which the handler populates with the live markup only when
``is_password_auth_enabled()`` is True.

The OIDC SSO button stays the sole path when password auth is disabled.
"""
from __future__ import annotations

import io
from unittest import mock

import pytest

import api.routes as routes
from api.routes import _LOGIN_PAGE_HTML


# ---------------------------------------------------------------------------
# Source-shape: the template + handler must agree on a single placeholder
# for the password controls, and the handler must call the live predicate
# ---------------------------------------------------------------------------


def test_login_template_uses_password_form_placeholder() -> None:
    """The login template MUST carry a single ``{{PASSWORD_FORM_HTML}}`` placeholder.

    A silent reversion to the previous multi-line ``<input>`` +
    ``<button>`` + ``<button>`` shape fails the suite.
    """
    assert "{{PASSWORD_FORM_HTML}}" in _LOGIN_PAGE_HTML, (
        "the login template must use a {{PASSWORD_FORM_HTML}} placeholder "
        "so the handler can route the password controls through the "
        "``is_password_auth_enabled()`` predicate"
    )


def test_login_template_does_not_render_password_input_inline() -> None:
    """The login template MUST NOT carry a literal ``<input type="password"``.

    The fix replaced the inline input with the placeholder; a silent
    reversion that re-adds the inline input fails the suite.
    """
    assert '<input type="password"' not in _LOGIN_PAGE_HTML, (
        "the login template must not carry an inline <input type=\"password\">; "
        "use the {{PASSWORD_FORM_HTML}} placeholder so the handler can "
        "gate the password form on is_password_auth_enabled()"
    )


def test_login_handler_calls_is_password_auth_enabled() -> None:
    """The /login branch in handle_get MUST call ``is_password_auth_enabled``.

    Pins the contract that the live predicate — not a stale cached
    flag, not a config-only check — gates the password controls. A
    silent reversion to a hard-coded `True` (or any pre-baked
    assumption about the environment) fails the suite.
    """
    src = (routes.__file__).read_text(encoding="utf-8") if hasattr(routes.__file__, "read_text") else open(routes.__file__).read()
    # Find the /login branch
    login_idx = src.index('if parsed.path == "/login":')
    # Find the next return statement that closes the /login branch
    end_idx = src.index("content_type=\"text/html; charset=utf-8\")", login_idx)
    branch = src[login_idx:end_idx]
    assert "is_password_auth_enabled" in branch, (
        "the /login branch must call is_password_auth_enabled() so the "
        "password form visibility tracks /api/auth/status at request time"
    )
    assert "PASSWORD_FORM_HTML" in branch, (
        "the /login branch must populate the {{PASSWORD_FORM_HTML}} "
        "placeholder with the live password form markup"
    )


# ---------------------------------------------------------------------------
# Behavioural: walk the /login handler with a real ``ParsedResult`` and
# a minimal fake handler. We mock the side-effecting helpers (settings
# + OIDC + auth predicate) so the test only covers the placeholder
# logic, not the whole login stack.
# ---------------------------------------------------------------------------


class _FakeHandler:
    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.wfile = io.BytesIO()
        self.command = "GET"
        self.path = "/login"
        self.client_address = ("127.0.0.1", 12345)
        self.request_version = "HTTP/1.1"

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _stub_oidc(enabled: bool):
    """Mock the OIDC predicate the login branch uses to decide SSO button visibility.

    ``_oidc_login_html(parsed)`` is a module-level function the login
    branch calls; the mock has to accept a single positional ``parsed``
    argument and return a stable string. With OIDC enabled the
    fixture returns a marker substring (``oidc-login``) so the
    behavioural tests can assert its presence; with OIDC disabled it
    returns the empty string the real implementation returns.
    """
    def _stub(parsed):
        del parsed
        if enabled:
            return (
                '<a id="oidc-login" class="oidc-login" '
                'href="/api/auth/oidc/start">Continue with SSO</a>'
            )
        return ""

    return mock.patch.object(routes, "_oidc_login_html", _stub)


@pytest.fixture
def patched_login(monkeypatch):
    """Set up the login branch's external dependencies for behavioural tests.

    Yields a (routes, parsed) tuple. Each test patches
    ``is_password_auth_enabled`` and ``_oidc_login_html`` itself so the
    scenario is explicit.
    """
    monkeypatch.setattr(
        routes, "_LOGIN_LOCALE",
        {
            "en": {
                "lang": "en",
                "title": "Sign in",
                "subtitle": "Sign in to continue",
                "placeholder": "Password",
                "btn": "Sign in",
                "invalid_pw": "Invalid password",
                "conn_failed": "Connection failed",
            }
        },
    )
    monkeypatch.setattr(routes, "_resolve_login_locale_key", lambda _lang: "en")
    monkeypatch.setattr(routes, "load_settings", lambda: {"bot_name": "Hermes"})
    from api import updates as _updates
    monkeypatch.setattr(_updates, "WEBUI_VERSION", "test-0.0.0")
    import html as _html
    monkeypatch.setattr(routes, "_html", _html)
    from urllib.parse import urlparse
    yield routes, urlparse("/login?next=/x")


def test_login_renders_password_form_when_password_auth_enabled(patched_login, monkeypatch) -> None:
    routes, parsed = patched_login
    monkeypatch.setattr(
        "api.auth.is_password_auth_enabled", lambda: True, raising=False
    )
    with _stub_oidc(enabled=False):
        handler = _FakeHandler()
        routes.handle_get(handler, parsed)
    html = handler.wfile.getvalue().decode("utf-8", "replace")

    assert '<input type="password"' in html, html
    assert 'id="pw"' in html, html
    assert '<button type="submit"' in html, html
    assert 'id="passkey-login"' in html, html


def test_login_hides_password_form_when_password_auth_disabled(patched_login, monkeypatch) -> None:
    """With password auth disabled (e.g. OIDC-only), the form must NOT render
    the password input / submit / passkey controls. The OIDC SSO entry
    point (when OIDC is enabled) stays the sole path.
    """
    routes, parsed = patched_login
    monkeypatch.setattr(
        "api.auth.is_password_auth_enabled", lambda: False, raising=False
    )
    with _stub_oidc(enabled=True):
        handler = _FakeHandler()
        routes.handle_get(handler, parsed)
    html = handler.wfile.getvalue().decode("utf-8", "replace")

    # The password controls are gone.
    assert '<input type="password"' not in html, html
    assert 'id="pw"' not in html, html
    assert '<button type="submit"' not in html, html
    assert 'id="passkey-login"' not in html, html
    # The OIDC SSO entry point is still present (the user must still
    # be able to log in — just via SSO, not via password). We assert
    # the <a id="oidc-login"> element rather than the bare substring
    # because the template's CSS class ``.oidc-login`` would match
    # the page even when OIDC is disabled.
    assert 'id="oidc-login"' in html, html


def test_login_hides_oidc_button_when_oidc_disabled(patched_login, monkeypatch) -> None:
    """Sanity: with password auth disabled AND OIDC disabled, the page is
    effectively empty — no controls. This pre-existing behaviour is
    unchanged by #7056 (the fix only adds the password-disabled path).
    """
    routes, parsed = patched_login
    monkeypatch.setattr(
        "api.auth.is_password_auth_enabled", lambda: False, raising=False
    )
    with _stub_oidc(enabled=False):
        handler = _FakeHandler()
        routes.handle_get(handler, parsed)
    html = handler.wfile.getvalue().decode("utf-8", "replace")

    assert '<input type="password"' not in html, html
    assert 'id="oidc-login"' not in html, html
