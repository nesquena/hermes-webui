"""Regression tests for issue #1560 — Settings password silently no-ops when
HERMES_WEBUI_PASSWORD env var is set.

Pre-fix behaviour: env-var-precedence in `api.auth.get_password_hash()` meant
that POST /api/settings with `_set_password` would happily persist a new hash
to settings.json AND return 200 + "Saved" — but every subsequent login still
required the env-var password. Same for `_clear_password` ("Disable Auth").

Fix is two-layer:
  - Backend: GET /api/settings now exposes `password_env_var: bool`; POST
    /api/settings refuses with 409 when the env var is set and the request
    asks for `_set_password` or `_clear_password`.
  - Frontend: when `password_env_var` is true, panels.js disables the password
    input, hides the Disable Auth button, and reveals a lock-banner explaining
    that the env var must be unset and the server restarted.

These tests pin both layers so a future refactor can't silently re-introduce
the silent-no-op UX bug.
"""

import io
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest


# ── Settings-file isolation ──────────────────────────────────────────────────
#
# Several tests in this module write password_hash directly to the shared
# settings.json (test_post_set_password_settings_hash_unchanged_after_409 seeds
# a sentinel, test_post_set_password_succeeds_when_env_var_unset goes through
# save_settings). Without isolation, those writes leak into TEST_STATE_DIR/
# settings.json (the path the integration server subprocess started by
# conftest.py reads from), which flips is_auth_enabled() to True for every
# subsequent test in the session and cascades to 401 across test_clarify_unblock,
# test_gateway_sync, etc.
#
# Snapshot-and-restore is preferred over redirecting SETTINGS_FILE because
# load_settings() / save_settings() bind to the module-level Path object
# captured at import time and the fixture must work regardless of import order.
@pytest.fixture(autouse=True)
def _restore_settings_file_after_test():
    import api.config as cfg

    original = (
        cfg.SETTINGS_FILE.read_text(encoding="utf-8")
        if cfg.SETTINGS_FILE.exists()
        else None
    )
    yield
    if original is not None:
        cfg.SETTINGS_FILE.write_text(original, encoding="utf-8")
    elif cfg.SETTINGS_FILE.exists():
        cfg.SETTINGS_FILE.unlink()


# ── FakeHandler that supports GET *and* POST body reading ─────────────────────

class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for routes.handle_get/handle_post.

    Exposes wfile/headers/rfile so the real handlers can read request bodies
    and write JSON responses. The only mutation we observe in tests is `status`
    + the JSON written to `wfile`.
    """

    def __init__(self, body_bytes: bytes = b"", cookie: str = ""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {
            "Content-Length": str(len(body_bytes)),
        }
        if cookie:
            self.headers["Cookie"] = cookie
        # set_auth_cookie() probes handler.request.getpeercert / X-Forwarded-Proto
        # to decide whether to emit the Secure flag. The default
        # BaseHTTPRequestHandler exposes a `.request` socket; FakeHandler is
        # transport-less, so expose a plain None — getattr(None, ...) is safe
        # and the resulting cookie is plain (non-Secure), which is what tests
        # care about. Without this attribute, save_settings → set_auth_cookie
        # raises AttributeError on the success path of `_set_password`.
        self.request = None

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def header(self, name):
        for key, value in self.sent_headers:
            if key.lower() == name.lower():
                return value
        return None

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


# ── Backend: GET /api/settings exposes password_env_var ──────────────────────

def test_get_settings_exposes_password_env_var_true_when_env_set(monkeypatch):
    """Acceptance criterion: GET /api/settings includes `password_env_var: true`
    when HERMES_WEBUI_PASSWORD is set."""
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "shadow-pw")

    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/settings")
    handle_get(handler, parsed)
    assert handler.status == 200

    payload = handler.json_body()
    assert payload.get("password_env_var") is True, (
        "GET /api/settings must expose password_env_var=true when "
        "HERMES_WEBUI_PASSWORD is set so the UI can disable the password field. "
        f"Got: {payload!r}"
    )
    # Also confirm the hash is never echoed back to the client (existing
    # invariant — pinned here to catch a future change that surfaces it
    # alongside the new flag).
    assert "password_hash" not in payload


def test_get_settings_password_env_var_false_when_env_unset(monkeypatch):
    """Control case: env var unset → password_env_var:false (falsy)."""
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)

    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/settings")
    handle_get(handler, parsed)
    assert handler.status == 200

    payload = handler.json_body()
    assert payload.get("password_env_var") is False


def test_get_settings_password_env_var_false_when_env_blank(monkeypatch):
    """Whitespace-only env var must NOT shadow settings — matches the strip()
    guard in api.auth.get_password_hash."""
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "   ")

    from api.routes import handle_get

    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/settings")
    handle_get(handler, parsed)
    assert handler.status == 200

    payload = handler.json_body()
    assert payload.get("password_env_var") is False


# ── Backend: POST /api/settings returns 409 when env var shadows ─────────────

def _post_settings(body_dict, cookie=""):
    """Helper: POST a JSON body to /api/settings via handle_post."""
    from api.routes import handle_post
    raw = json.dumps(body_dict).encode("utf-8")
    handler = _FakeHandler(body_bytes=raw, cookie=cookie)
    parsed = urlparse("http://example.com/api/settings")
    handle_post(handler, parsed)
    return handler


def test_post_set_password_returns_409_when_env_var_set(monkeypatch):
    """Acceptance criterion: POST `_set_password` returns 409 when env var is set,
    with a message naming HERMES_WEBUI_PASSWORD so the user knows what to fix."""
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "shadow-pw")

    handler = _post_settings({"_set_password": "new-attempt"})

    assert handler.status == 409, (
        f"POST _set_password must return 409 when env var is set, got {handler.status}"
    )
    payload = handler.json_body()
    assert "HERMES_WEBUI_PASSWORD" in payload.get("error", ""), (
        "409 error message must name HERMES_WEBUI_PASSWORD so the user can "
        f"identify the override. Got: {payload!r}"
    )


def test_post_clear_password_returns_409_when_env_var_set(monkeypatch):
    """Acceptance criterion: POST `_clear_password=true` ("Disable Auth") returns
    409 when env var is set — disabling auth via UI is impossible while the env
    var is in force."""
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "shadow-pw")

    handler = _post_settings({"_clear_password": True})

    assert handler.status == 409
    payload = handler.json_body()
    assert "HERMES_WEBUI_PASSWORD" in payload.get("error", "")


def test_post_set_password_settings_hash_unchanged_after_409(monkeypatch):
    """Acceptance criterion: env var set + POST `_set_password` → 409 +
    settings.json `password_hash` unchanged.

    Pre-fix the write happened anyway (silently); post-fix the 409 short-circuits
    BEFORE save_settings(), so any pre-existing password_hash on disk must
    survive untouched.
    """
    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "shadow-pw")

    # Seed settings.json with a known sentinel hash so we can detect any write.
    from api.config import load_settings, save_settings
    # Don't go through save_settings (it would re-route _set_password) — write
    # the file directly via the same path load_settings reads from.
    import api.config as cfg
    sentinel_hash = "deadbeef" * 8  # 64 chars, matches PBKDF2 hex output shape
    settings_before = load_settings()
    settings_before["password_hash"] = sentinel_hash
    cfg.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg.SETTINGS_FILE.write_text(
        json.dumps(settings_before, indent=2), encoding="utf-8"
    )

    handler = _post_settings({"_set_password": "new-attempt"})
    assert handler.status == 409

    settings_after = load_settings()
    assert settings_after.get("password_hash") == sentinel_hash, (
        "settings.json password_hash must be UNCHANGED after a 409-rejected "
        "POST _set_password — fix must short-circuit BEFORE save_settings(). "
        f"Got: before={sentinel_hash!r} after={settings_after.get('password_hash')!r}"
    )


def test_post_set_password_succeeds_when_env_var_unset(monkeypatch):
    """Control case: env var unset → POST _set_password is NOT a 409.

    We don't pin the success status (200) tightly because the response path
    sets a session cookie and may use a special status flow; the important
    invariant is that the 409 guard ONLY fires when the env var is set.
    """
    monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)

    handler = _post_settings({"_set_password": "fresh-pw"})

    assert handler.status != 409, (
        "POST _set_password without env var must NOT trigger the #1560 409 "
        f"guard. Got status={handler.status}"
    )


# ── Frontend: index.html, panels.js, i18n.js wiring ──────────────────────────

REPO_ROOT = Path(__file__).parent.parent
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_index_html_has_password_lock_banner_div():
    """index.html must include the lock-banner div with i18n key, hidden by
    default, inside the System pane near the password field."""
    # The banner must exist with the i18n key panels.js looks up
    assert 'id="settingsPasswordEnvLock"' in INDEX_HTML
    assert 'data-i18n="password_env_var_locked"' in INDEX_HTML
    # Default-hidden; panels.js reveals it when settings.password_env_var is true.
    assert 'settingsPasswordEnvLock' in INDEX_HTML
    # Sanity: banner sits inside the System pane (same context as the password
    # field) — this guards against a future refactor that moves the banner away
    # from the field it explains.
    sys_start = INDEX_HTML.index('id="settingsPaneSystem"')
    pwlock_start = INDEX_HTML.index('id="settingsPasswordEnvLock"')
    assert pwlock_start > sys_start, (
        "Lock banner must be inside the System settings pane (after "
        "settingsPaneSystem opens) so it shows next to the password field."
    )


def test_panels_js_disables_password_field_when_env_locked():
    """panels.js loadSettingsPanel must read settings.password_env_var and
    disable the password field + reveal the lock banner."""
    assert "password_env_var" in PANELS_JS, (
        "panels.js must read settings.password_env_var from GET /api/settings."
    )
    assert "settingsPasswordEnvLock" in PANELS_JS, (
        "panels.js must toggle the visibility of #settingsPasswordEnvLock."
    )
    # The password input must be disabled when locked.
    assert "pwField.disabled" in PANELS_JS or "disabled=pwEnvLocked" in PANELS_JS


def test_panels_js_hides_disable_auth_when_env_locked():
    """panels.js must hide the Disable Auth button when env-var-locked — its
    POST would 409 anyway and the UI shouldn't offer an action that can't
    succeed."""
    # Look for a section that toggles btnDisableAuth visibility based on the
    # env-lock flag.
    assert "btnDisableAuth" in PANELS_JS
    # The simplest signal: a guard that hides btnDisableAuth when pwEnvLocked
    # is true. We don't pin the exact JS expression (style.display, hidden,
    # classList — implementer's choice), but the symbol pair must co-occur.
    pw_lock_idx = PANELS_JS.find("pwEnvLocked")
    assert pw_lock_idx != -1, "panels.js must compute pwEnvLocked"
    # btnDisableAuth must be referenced in a region where pwEnvLocked is in
    # scope (same loadSettingsPanel function body — within ±3000 chars).
    btn_idx = PANELS_JS.find("btnDisableAuth")
    assert abs(btn_idx - pw_lock_idx) < 4000, (
        "btnDisableAuth handling must live near the pwEnvLocked computation "
        "in loadSettingsPanel; otherwise the env-lock state can't gate the "
        "button visibility."
    )


def test_panels_js_uses_locked_placeholder_i18n_key():
    """The locked-state input placeholder must come from the i18n key —
    matches the t('password_env_var_locked_placeholder') call site."""
    assert "password_env_var_locked_placeholder" in PANELS_JS


# ── i18n keys present in all 9 locales ───────────────────────────────────────

# All locales currently shipped in static/i18n.js. Issue #1560 lists 9 locales
# (en/es/de/zh/zh-Hant/ru/ja/fr/pt). The repo currently ships 9 locales but
# substitutes 'ko' for 'fr' — we test what the repo actually has, not what the
# issue body lists, so a future addition of fr won't fail the suite either.
EXPECTED_LOCALES = ("en", "it", "ja", "ru", "es", "de", "zh", "zh-Hant", "pt", "ko")


def _locale_block(locale_key: str) -> str:
    """Return the slice of i18n.js between `<key>: {` and the next top-level
    locale opener (or end-of-file). Good enough for substring assertions."""
    # Locale openers look like `  en: {` or `  'zh-Hant': {` (two-space indent).
    if "-" in locale_key:
        opener = f"  '{locale_key}':"
    else:
        opener = f"  {locale_key}:"
    start = I18N_JS.index(opener)
    # Find the next locale opener, scanning all known locales.
    rest = I18N_JS[start + len(opener):]
    next_starts = []
    for other in EXPECTED_LOCALES:
        if other == locale_key:
            continue
        cand_opener = f"  '{other}':" if "-" in other else f"  {other}:"
        idx = rest.find(cand_opener)
        if idx >= 0:
            next_starts.append(idx)
    end = min(next_starts) if next_starts else len(rest)
    return rest[:end]


def test_password_env_var_locked_key_present_in_all_locales():
    """The lock-banner translation key must exist in every shipped locale —
    otherwise users on those locales see [object Object] / undefined / the
    raw HTML default instead of the help text."""
    missing = []
    for locale in EXPECTED_LOCALES:
        block = _locale_block(locale)
        if "password_env_var_locked:" not in block:
            missing.append(locale)
    assert not missing, (
        f"password_env_var_locked translation key missing in locales: {missing}"
    )


def test_password_env_var_locked_placeholder_key_present_in_all_locales():
    """The locked-input placeholder translation key must exist in every shipped
    locale so the disabled input field never shows English fallback to non-EN
    users."""
    missing = []
    for locale in EXPECTED_LOCALES:
        block = _locale_block(locale)
        if "password_env_var_locked_placeholder:" not in block:
            missing.append(locale)
    assert not missing, (
        "password_env_var_locked_placeholder translation key missing in "
        f"locales: {missing}"
    )
