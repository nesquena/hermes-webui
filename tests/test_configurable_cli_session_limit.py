"""Regression checks for the configurable non-WebUI sidebar session cap (#3347).

Salvaged/ported fresh from deserted PR #3347 (thanks @fuzhyperblue).

Covers three guarantees the parent task requires:
  (a) the default is 20 — existing behavior is unchanged unless the user raises it,
  (b) a configured value is honored at BOTH cap sites
      (``CLI_VISIBLE_SESSION_LIMIT`` fetch cap in api/models.py AND
       ``CLI_VISIBLE_SESSION_CAP`` visibility cap in api/routes.py) using the
      SAME resolved value, and
  (c) the upper-bound clamp protects sidebar fetch/render performance
      (an absurd value is clamped to 500, a non-positive/blank value falls
      back to the default 20).

The resolver tests are pure unit checks (no disk, no server). The settings-API
test exercises the live test server to prove the value persists and that
out-of-range writes are rejected by the int-range validation.
"""

import json
import pathlib
import urllib.error
import urllib.request

import api.config as config
import api.models as models
import api.routes as routes

from tests._pytest_port import BASE

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
MODELS_PY = (ROOT / "api" / "models.py").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

SETTING = "cli_visible_session_cap"
DEFAULT = 20
MAX = 500


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read()), r.status
    except urllib.error.HTTPError as e:
        return json.loads(e.read()), e.code


# ── (a) default unchanged ────────────────────────────────────────────────────

def test_cli_session_cap_default_is_20():
    """Default must stay 20 so existing behavior is byte-identical."""
    assert config._SETTINGS_DEFAULTS[SETTING] == DEFAULT
    assert config.CLI_VISIBLE_SESSION_CAP_DEFAULT == DEFAULT
    # Empty/missing settings resolve to the legacy default at the resolver.
    assert config.resolve_cli_visible_session_cap({}) == DEFAULT
    assert config.resolve_cli_visible_session_cap(None) == DEFAULT
    # The legacy module-level constants are themselves unchanged at 20.
    assert models.CLI_VISIBLE_SESSION_LIMIT == DEFAULT
    assert routes.CLI_VISIBLE_SESSION_CAP == DEFAULT


# ── (b) honored at BOTH cap sites with the SAME value ────────────────────────

def test_cli_session_cap_honored_at_both_sites(monkeypatch):
    """A configured value flows to BOTH the models fetch cap and the routes
    visibility cap, and both resolve to the SAME number (no fetch/show mismatch).
    """
    configured = 123
    fake_settings = {SETTING: configured}

    # Force every load_settings() reader (models + routes both delegate to the
    # canonical resolver, which calls load_settings() when no dict is passed)
    # to see the configured value.
    monkeypatch.setattr(config, "load_settings", lambda: dict(fake_settings))

    models_site = models._cli_visible_session_limit()      # state.db fetch cap
    routes_site = routes._resolve_cli_session_cap()         # sidebar visibility cap

    assert models_site == configured
    assert routes_site == configured
    # The crucial invariant: both cap sites agree on the SAME resolved value.
    assert models_site == routes_site

    # routes' resolver also honors an explicitly-passed settings dict.
    assert routes._resolve_cli_session_cap(fake_settings) == configured
    assert config.resolve_cli_visible_session_cap(fake_settings) == configured


# ── (c) upper-bound clamp + lower-bound floor ────────────────────────────────

def test_cli_session_cap_upper_bound_clamp():
    """A runaway value is clamped to the documented max so sidebar
    fetch/render performance can't be tanked.
    """
    assert config.CLI_VISIBLE_SESSION_CAP_MAX == MAX
    assert config.resolve_cli_visible_session_cap({SETTING: 100000}) == MAX
    assert config.resolve_cli_visible_session_cap({SETTING: MAX + 1}) == MAX
    # Exactly at the bound is preserved.
    assert config.resolve_cli_visible_session_cap({SETTING: MAX}) == MAX


def test_cli_session_cap_floor_and_invalid_fall_back_to_default():
    """Non-positive / blank / invalid values fall back to the default 20."""
    assert config.resolve_cli_visible_session_cap({SETTING: 0}) == DEFAULT
    assert config.resolve_cli_visible_session_cap({SETTING: -5}) == DEFAULT
    assert config.resolve_cli_visible_session_cap({SETTING: ""}) == DEFAULT
    assert config.resolve_cli_visible_session_cap({SETTING: "   "}) == DEFAULT
    assert config.resolve_cli_visible_session_cap({SETTING: None}) == DEFAULT
    assert config.resolve_cli_visible_session_cap({SETTING: "not-a-number"}) == DEFAULT
    # Numeric strings are accepted.
    assert config.resolve_cli_visible_session_cap({SETTING: "150"}) == 150


def test_cli_session_cap_clamp_propagates_to_both_sites(monkeypatch):
    """A huge configured value is clamped at BOTH sites to the same bound."""
    monkeypatch.setattr(config, "load_settings", lambda: {SETTING: 999999})
    assert models._cli_visible_session_limit() == MAX
    assert routes._resolve_cli_session_cap() == MAX
    assert models._cli_visible_session_limit() == routes._resolve_cli_session_cap()


# ── static wiring (config + both backend sites + UI) ─────────────────────────

def test_cli_session_cap_setting_is_exposed_and_wired():
    # config: default + int-range validation bound
    assert '"cli_visible_session_cap": 20' in CONFIG_PY
    assert '"cli_visible_session_cap": (1, CLI_VISIBLE_SESSION_CAP_MAX)' in CONFIG_PY
    assert "def resolve_cli_visible_session_cap(" in CONFIG_PY
    # models cap site
    assert "def _cli_visible_session_limit()" in MODELS_PY
    assert "limit=visible_session_limit if visible_session_limit is not None else" in MODELS_PY
    assert "_cli_visible_session_limit()" in MODELS_PY
    # routes cap site
    assert "def _resolve_cli_session_cap(" in ROUTES_PY
    assert "cli_cap = _resolve_cli_session_cap()" in ROUTES_PY
    # UI: number field + bounds
    assert 'id="settingsCliVisibleSessionCap"' in INDEX_HTML
    assert 'min="1"' in INDEX_HTML
    assert 'max="500"' in INDEX_HTML
    # panels.js: read + persist
    assert "payload.cli_visible_session_cap=parseInt(cliCapField.value,10)" in PANELS_JS
    assert "settings.cli_visible_session_cap" in PANELS_JS
    assert "body.cli_visible_session_cap=cliVisibleSessionCap" in PANELS_JS
    # boot.js: window mirror
    assert "window._cliVisibleSessionCap=parseInt(s.cli_visible_session_cap||20,10)||20" in BOOT_JS
    # i18n: new keys present in all 14 locale blocks (English fallback fine)
    assert I18N_JS.count("settings_label_cli_session_cap:") == 14
    assert I18N_JS.count("settings_desc_cli_session_cap:") == 14


# ── live settings-API persistence + range validation ─────────────────────────

def test_settings_api_persists_cli_session_cap_and_rejects_out_of_range():
    try:
        d, status = post("/api/settings", {SETTING: 200})
        assert status == 200
        assert d[SETTING] == 200

        d, status = post("/api/settings", {SETTING: "150"})
        assert status == 200
        assert d[SETTING] == 150

        # Out-of-range writes are rejected by _SETTINGS_INT_RANGES validation:
        # the prior valid value (150) is preserved.
        d, status = post("/api/settings", {SETTING: 0})
        assert status == 200
        assert d[SETTING] == 150

        d, status = post("/api/settings", {SETTING: 9999})
        assert status == 200
        assert d[SETTING] == 150
    finally:
        post("/api/settings", {SETTING: DEFAULT})
