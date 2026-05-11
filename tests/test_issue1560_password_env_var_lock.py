"""Tests for issue #1560 — Settings password silently no-ops when HERMES_WEBUI_PASSWORD env var is set.

Root cause: HERMES_WEBUI_PASSWORD takes precedence in api.auth.get_password_hash(),
but the UI had no way to know — POST /api/settings happily wrote password_hash to
settings.json, returned 200 + "Saved" toast, while every subsequent login still
required the env-var password.

Fix: surface env-var precedence in GET /api/settings (`password_env_var: bool`),
refuse the write loudly (409) when shadowed, disable the field + show help-text
banner in the UI, with i18n keys in all 9 locales.
"""

import json
import os
import pathlib
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).parent.parent


def _read(rel_path):
    return (REPO / rel_path).read_text(encoding='utf-8')


# ── Backend (api/routes.py) ───────────────────────────────────────────────


def test_get_settings_surfaces_password_env_var_flag():
    """GET /api/settings handler must include `password_env_var: bool(env)`."""
    src = _read('api/routes.py')
    # Locate the GET /api/settings block (by handler comment + path string)
    start = src.index('if parsed.path == "/api/settings":')
    # Block ends at next top-level `if parsed.path == ...` or `if parsed.path.startswith`
    end = src.index('if parsed.path', start + 50)
    block = src[start:end]

    assert 'password_env_var' in block, \
        'GET /api/settings must expose password_env_var so UI can disable the field'
    assert 'HERMES_WEBUI_PASSWORD' in block, \
        'GET /api/settings must read HERMES_WEBUI_PASSWORD env var'


def test_post_settings_refuses_set_password_when_env_var_shadowed():
    """POST /api/settings with _set_password must return 409 when env var is set."""
    src = _read('api/routes.py')
    # The guard lives near the POST /api/settings handler; locate it via the
    # canonical error-message substring (defense-in-depth comment + bad() call).
    assert 'HERMES_WEBUI_PASSWORD env var is set' in src, \
        'POST /api/settings must refuse with a clear message naming the env var'
    assert '409' in src, 'POST /api/settings must use HTTP 409 for env-var conflict'


def test_post_settings_refuses_clear_password_when_env_var_shadowed():
    """POST /api/settings with _clear_password=true must also be refused."""
    src = _read('api/routes.py')
    # Same guard must cover both paths
    assert '_clear_password' in src
    # Find the guard and verify it tests both flags
    guard_idx = src.index('HERMES_WEBUI_PASSWORD env var is set')
    # Look back ~2KB for the conditional that triggers the guard
    window = src[max(0, guard_idx - 2000):guard_idx]
    assert 'requested_password' in window or '_set_password' in window
    assert 'requested_clear_password' in window or '_clear_password' in window, \
        'guard must cover both _set_password and _clear_password'


# ── Frontend: lock UI elements (static/index.html) ────────────────────────


def test_settings_html_has_password_env_lock_banner():
    """The settings password block must include a hidden lock banner element."""
    html = _read('static/index.html')
    assert 'id="settingsPasswordEnvLock"' in html, \
        'settingsPasswordEnvLock banner element required (revealed when env var set)'
    assert 'data-i18n="password_env_var_locked"' in html, \
        'banner must use the i18n key password_env_var_locked'


# ── Frontend: env-locked logic (static/panels.js) ─────────────────────────


def test_panels_js_disables_password_when_env_locked():
    """panels.js must disable the password field and show the banner when password_env_var is true."""
    src = _read('static/panels.js')
    assert 'password_env_var' in src, \
        'panels.js must read settings.password_env_var from GET /api/settings'
    assert 'settingsPasswordEnvLock' in src, \
        'panels.js must toggle the settingsPasswordEnvLock banner'
    # The disable logic should set pwField.disabled
    assert 'pwField.disabled' in src or 'disabled=pwEnvLocked' in src.replace(' ', ''), \
        'password field must be disabled when env-locked'


def test_panels_js_hides_disable_auth_button_when_env_locked():
    """The Disable Auth button must be hidden when env var shadows the settings password."""
    src = _read('static/panels.js')
    # When env-locked, btnDisableAuth should be set display:none
    # We verify by locating the env-locked block and checking it touches btnDisableAuth
    idx = src.index('pwEnvLocked')
    # Look in a window after the first env-locked reference for btnDisableAuth handling
    window = src[idx:idx + 3000]
    assert 'btnDisableAuth' in window, \
        'Disable Auth button must be hidden in the env-locked code path'


# ── i18n: keys present in all 9 locales (static/i18n.js) ──────────────────


LOCALES = ['en', 'ja', 'ru', 'es', 'de', 'zh', 'zh-Hant', 'pt', 'ko']


def _split_locales(i18n_src):
    """Split i18n.js into per-locale source slices.

    Locale block headers look like `  en: {` or `  'zh-Hant': {`. We slice each
    block from its header to the next sibling header at the same indentation.
    """
    import re
    pattern = re.compile(r"^  ['\"]?([\w\-]+)['\"]?: \{$", re.MULTILINE)
    matches = list(pattern.finditer(i18n_src))
    blocks = {}
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(i18n_src)
        blocks[name] = i18n_src[start:end]
    return blocks


def test_i18n_password_env_var_locked_in_all_locales():
    """Every locale must define the password_env_var_locked banner string."""
    src = _read('static/i18n.js')
    blocks = _split_locales(src)
    missing = [loc for loc in LOCALES if loc not in blocks
               or 'password_env_var_locked:' not in blocks[loc]]
    assert not missing, \
        f"Locales missing password_env_var_locked: {missing}"


def test_i18n_password_env_var_locked_placeholder_in_all_locales():
    """Every locale must define the password_env_var_locked_placeholder string."""
    src = _read('static/i18n.js')
    blocks = _split_locales(src)
    missing = [loc for loc in LOCALES
               if loc not in blocks
               or 'password_env_var_locked_placeholder:' not in blocks[loc]]
    assert not missing, \
        f"Locales missing password_env_var_locked_placeholder: {missing}"


def test_i18n_locked_string_mentions_env_var_name_in_all_locales():
    """Each locale's banner must literally mention HERMES_WEBUI_PASSWORD so users can find it."""
    src = _read('static/i18n.js')
    blocks = _split_locales(src)
    for loc in LOCALES:
        block = blocks.get(loc, '')
        # Find the password_env_var_locked entry
        idx = block.find('password_env_var_locked:')
        assert idx != -1, f"{loc}: missing password_env_var_locked"
        # Take the rest of that line (the message string)
        line_end = block.index('\n', idx)
        line = block[idx:line_end]
        assert 'HERMES_WEBUI_PASSWORD' in line, \
            f"{loc}: banner must literally name HERMES_WEBUI_PASSWORD"


# ── Live HTTP smoke test (env var NOT set in pytest) ──────────────────────


def test_get_settings_returns_password_env_var_false_when_unset(monkeypatch):
    """When HERMES_WEBUI_PASSWORD is not set in the test process,
    GET /api/settings must include `password_env_var: False`."""
    # Test the unset branch explicitly. Some suite neighbors intentionally set
    # HERMES_WEBUI_PASSWORD while exercising the locked-password path.
    monkeypatch.delenv('HERMES_WEBUI_PASSWORD', raising=False)
    # The conftest server inherits a sanitized env; verify this process is clean.
    assert not os.getenv('HERMES_WEBUI_PASSWORD', '').strip(), \
        'this test requires HERMES_WEBUI_PASSWORD to be unset'

    from tests._pytest_port import BASE
    req = urllib.request.Request(BASE + '/api/settings')
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        payload = json.loads(e.read())

    assert 'password_env_var' in payload, \
        'GET /api/settings must always include password_env_var key'
    assert payload['password_env_var'] is False, \
        'env var unset => password_env_var must be False'
