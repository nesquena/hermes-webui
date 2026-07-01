"""Regression + security coverage for the safe config.yaml viewer (#2929).

Salvaged from PR #3228 (author @AJV20) and extended with the three
maintainer-required fixes plus a redaction-completeness pass:

  fix #1  numeric/bool secrets under a sensitive key path are masked (the
          key-path check runs BEFORE the numeric/bool passthrough). This is
          covered by a NON-VACUOUS test that fails if the two branches are
          reordered.
  fix #2  the endpoint returns only the config basename (``filename``) and
          never the absolute server path.
  fix #3  a UI note documents that the value-level scrub of secrets pasted
          into non-secret keys only runs when ``api_redact_enabled`` is on.

Security contract: no credential value — of ANY type — under any sensitive
key path may appear in the /api/config/safe response.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import api.routes as routes

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


# ── FakeHandler (mirrors test_gateway_status_agent_health._FakeHandler) ───────

class _FakeHandler:
    """Minimal BaseHTTPRequestHandler stand-in for routes.handle_get."""

    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


def _call_safe_config(monkeypatch, config: dict, config_path: Path | None = None):
    """Invoke handle_get for /api/config/safe and return the FakeHandler."""
    monkeypatch.setattr(routes, "get_config", lambda: config)
    if config_path is not None:
        monkeypatch.setattr(routes, "_get_config_path", lambda: config_path)
    handler = _FakeHandler()
    parsed = urlparse("http://example.com/api/config/safe")
    routes.handle_get(handler, parsed)
    return handler


# ── Unit: the redactor ───────────────────────────────────────────────────────

def test_redact_config_masks_secret_key_paths_and_prefilters_plain_strings(monkeypatch):
    calls = []
    monkeypatch.setattr(
        routes,
        "_redact_text",
        lambda text: calls.append(text) or text.replace("ghp_sensitive", "[REDACTED]"),
    )

    safe: dict[str, Any] = routes._redact_config_for_display({
        "providers": {"openai": {"api_key": "***", "model": "gpt-5.5"}},
        "gateway": {"api_key": 1234567890, "enabled": True},
        "platforms": {"telegram": {"token": False}},
        "webui": {"dashboard": {"public_url": "https://example.test"}},
        "notes": "contains ghp_sensitive token",
        "items": [{"password": "***"}],
    })

    assert safe["providers"]["openai"]["api_key"] == "[REDACTED]"
    assert safe["gateway"]["api_key"] == "[REDACTED]"
    assert safe["gateway"]["enabled"] is True
    assert safe["platforms"]["telegram"]["token"] == "[REDACTED]"
    assert safe["providers"]["openai"]["model"] == "gpt-5.5"
    assert safe["webui"]["dashboard"]["public_url"] == "https://example.test"
    assert safe["notes"] == "contains [REDACTED] token"
    assert safe["items"][0]["password"] == "[REDACTED]"
    # The masked-by-path values must never even be handed to _redact_text.
    assert "***" not in calls


def test_numeric_secret_under_sensitive_path_is_redacted_fix1_nonvacuous():
    """Maintainer fix #1 (NON-VACUOUS).

    A numeric value under a sensitive key path must be masked. With the buggy
    ordering (numeric/bool passthrough before the key-path check) this leaks the
    raw integer 12345 — so this assertion fails without the fix, proving it is
    not vacuous.
    """
    safe = routes._redact_config_for_display({"providers": {"x": {"token": 12345}}})
    assert safe["providers"]["x"]["token"] == "[REDACTED]"
    # The raw number must not survive anywhere in the serialized output.
    assert "12345" not in json.dumps(safe)


def test_bool_secret_under_sensitive_path_is_redacted():
    safe = routes._redact_config_for_display({"x": {"secret": True}})
    assert safe["x"]["secret"] == "[REDACTED]"


def test_empty_and_none_secret_values_are_left_as_is():
    """Empty/absent secrets are not turned into a misleading [REDACTED]."""
    safe = routes._redact_config_for_display({"a": {"api_key": ""}, "b": {"token": None}})
    assert safe["a"]["api_key"] == ""
    assert safe["b"]["token"] is None


def test_redaction_completeness_no_secret_of_any_type_leaks():
    """The security contract: drive a representative config carrying secrets of
    several types under many sensitive key paths and assert none leak."""
    sentinels = {
        "k_api_key": "sk-live-AAAAAAAAAAAA",
        "k_apikey": "AIzaXXXXXXXXXXXX",
        "k_token": "ghp_token_value_zzz",
        "k_token_num": 9988776655,            # numeric secret
        "k_secret": "shhh-secret-value",
        "k_password": "hunter2password",
        "k_passwd": "pwpwpwpw",
        "k_passphrase": "open sesame phrase",
        "k_credential": "cred-blob-value",
        "k_cookie": "session=abc123cookie",
        "k_private_key": "-----BEGIN PRIVATE KEY-----abc",
        "k_client_secret": "cs_live_clientsecret",
        "k_access_key": "AKIAEXAMPLEACCESSKEY",
        "k_refresh_token": "refresh_tok_value",
        "k_bearer": "Bearer aaa.bbb.ccc",
        "k_auth": "Basic dXNlcjpwYXNz",
        "k_webhook": "https://hooks.example/T000/B000/secrettoken",
        "k_session_key": "sesskeyvalue",
        "k_signature": "x-amz-sig-value",
        "k_bool_secret": True,                # bool secret
    }
    config = {
        "providers": {
            "openai": {"api_key": sentinels["k_api_key"], "model": "gpt-5.5"},
            "google": {"apikey": sentinels["k_apikey"]},
        },
        "gateway": {
            "token": sentinels["k_token"],
            "auth_token": sentinels["k_token_num"],
            "secret": sentinels["k_secret"],
            "enabled": True,           # non-secret bool: must survive
            "port": 8080,              # non-secret int: must survive
        },
        "auth": {
            "password": sentinels["k_password"],
            "passwd": sentinels["k_passwd"],
            "passphrase": sentinels["k_passphrase"],
            "bearer": sentinels["k_bearer"],
            "value": sentinels["k_auth"],         # under 'auth' ancestor
        },
        "oauth": {
            "client_secret": sentinels["k_client_secret"],
            "refresh_token": sentinels["k_refresh_token"],
        },
        "aws": {"access_key": sentinels["k_access_key"], "region": "us-east-1"},
        "store": {
            "credential": sentinels["k_credential"],
            "cookie": sentinels["k_cookie"],
            "private_key": sentinels["k_private_key"],
            "session_key": sentinels["k_session_key"],
            "signature": sentinels["k_signature"],
            "x_secret": sentinels["k_bool_secret"],
        },
        "integrations": [
            {"webhook": sentinels["k_webhook"]},
            {"name": "ok", "url": "https://example.test/ping"},
        ],
    }

    safe = routes._redact_config_for_display(config)
    blob = json.dumps(safe)

    leaked = [
        name for name, val in sentinels.items()
        if val is not True and str(val) in blob
    ]
    # Bool True can't be detected via substring (it serializes as `true`);
    # assert the masked location directly instead.
    assert safe["store"]["x_secret"] == "[REDACTED]"
    assert not leaked, f"secret(s) leaked into safe config: {leaked}"

    # Non-secret scalars must survive untouched.
    assert safe["gateway"]["enabled"] is True
    assert safe["gateway"]["port"] == 8080
    assert safe["aws"]["region"] == "us-east-1"
    assert safe["providers"]["openai"]["model"] == "gpt-5.5"
    assert safe["integrations"][1]["url"] == "https://example.test/ping"


def test_non_secret_session_knobs_are_not_over_redacted():
    """The completeness pass must not over-redact non-credential 'session'
    knobs (regression guard against a naive bare 'session' fragment)."""
    safe = routes._redact_config_for_display({
        "gateway": {
            "session_ttl_seconds": 3600,
            "max_live_sessions": 5,
            "group_sessions_per_user": True,
        }
    })
    assert safe["gateway"]["session_ttl_seconds"] == 3600
    assert safe["gateway"]["max_live_sessions"] == 5
    assert safe["gateway"]["group_sessions_per_user"] is True


# ── Endpoint behavior ─────────────────────────────────────────────────────────

def test_endpoint_returns_redacted_yaml_and_basename_only(monkeypatch, tmp_path):
    cfg_path = tmp_path / "deep" / "home" / "config.yaml"
    config = {"providers": {"openai": {"api_key": "sk-live-SECRETVALUE", "model": "gpt"}}}
    handler = _call_safe_config(monkeypatch, config, config_path=cfg_path)
    payload = handler.get_json()

    assert handler.status == 200
    assert payload["ok"] is True
    assert payload["read_only"] is True
    assert payload["filename"] == "config.yaml"
    assert payload["redacted_count"] >= 1
    assert "sk-live-SECRETVALUE" not in payload["text"]
    assert "[REDACTED]" in payload["text"]


def test_endpoint_response_contains_no_absolute_path_fix2(monkeypatch, tmp_path):
    """Maintainer fix #2: the absolute server path must never reach the client."""
    cfg_path = tmp_path / "secret-home-dir" / "config.yaml"
    handler = _call_safe_config(monkeypatch, {"a": 1}, config_path=cfg_path)
    payload = handler.get_json()

    # No 'path' field at all.
    assert "path" not in payload
    # The absolute path string must appear nowhere in the serialized response.
    raw = json.dumps(payload)
    assert str(cfg_path) not in raw
    assert str(cfg_path.parent) not in raw
    assert "secret-home-dir" not in raw


# ── Static wiring assertions (salvaged from #3228, adapted to master) ─────────

def test_safe_config_endpoint_is_get_only_read_only_and_uses_active_config_path():
    assert '"/api/config/safe"' in ROUTES_PY
    endpoint_idx = ROUTES_PY.index('if parsed.path == "/api/config/safe"')
    settings_idx = ROUTES_PY.index('"/api/settings"', endpoint_idx)
    block = ROUTES_PY[endpoint_idx:settings_idx]
    assert "_safe_config_yaml_text()" in block
    assert "_get_config_path()" in block
    assert '"path": str(cfg_path)' not in block
    assert '"read_only": True' in block
    assert '"filename": cfg_path.name' in block


def test_system_settings_mounts_read_only_safe_config_viewer():
    assert 'id="safeConfigText"' in INDEX_HTML
    assert 'onclick="loadSafeConfig(true)"' in INDEX_HTML
    assert 'onclick="copySafeConfig()"' in INDEX_HTML
    assert "read-only" in INDEX_HTML
    assert ".safe-config-viewer" in STYLE_CSS


def test_redaction_disabled_doc_note_present_fix3():
    """Maintainer fix #3: the panel documents the api_redact_enabled caveat."""
    assert 'data-i18n="safe_config_redact_note"' in INDEX_HTML
    assert "api_redact_enabled" in INDEX_HTML


def test_safe_config_frontend_loads_and_copies_redacted_yaml():
    assert "async function loadSafeConfig" in PANELS_JS
    assert "api('/api/config/safe')" in PANELS_JS
    assert "loadSafeConfig();" in PANELS_JS
    assert "async function copySafeConfig" in PANELS_JS
    assert "navigator.clipboard.writeText" in PANELS_JS


def test_safe_config_i18n_and_changelog_entries_exist():
    for key in [
        "safe_config_title",
        "safe_config_desc",
        "safe_config_redact_note",
        "safe_config_refresh",
        "safe_config_copy",
        "safe_config_meta",
        "safe_config_copied",
    ]:
        assert key in I18N_JS
    assert "safe, read-only config.yaml viewer" in CHANGELOG
    assert "#2929" in CHANGELOG
