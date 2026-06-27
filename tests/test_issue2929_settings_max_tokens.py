"""Regression coverage for the Settings-side max_tokens bridge."""

import io
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest


def _function_block(src: str, name: str) -> str:
    marker = re.search(rf"(^|\n)(?:async\s+)?function\s+{re.escape(name)}\(", src)
    assert marker is not None, f"{name}() not found"
    start = marker.start()
    next_marker = re.search(r"\n(?:function\s+\w+\(|async\s+function\s+\w+\()", src[start + 1:])
    end = start + 1 + next_marker.start() if next_marker else len(src)
    return src[start:end]


@pytest.fixture(autouse=True)
def _isolate_config_files(tmp_path, monkeypatch):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(config, "reload_config", lambda: None)
    yield


class _FakeHandler:
    def __init__(self, body_bytes: bytes = b""):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.rfile = io.BytesIO(body_bytes)
        self.headers = {"Content-Length": str(len(body_bytes))}
        self.request = None
        self.client_address = ("127.0.0.1", 0)

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def _write_config(text: str) -> Path:
    import api.config as config

    config_path = config._get_config_path()
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _read_config():
    import api.config as config

    return config._load_yaml_config_file(config._get_config_path())


def test_get_max_tokens_status_prefers_root_then_agent():
    import api.config as config

    _write_config(
        "max_tokens: 256\n"
        "agent:\n"
        "  max_tokens: 512\n"
        "  name: fallback\n"
    )
    assert config.get_max_tokens_status() == {
        "max_tokens": 256,
        "max_tokens_effective": 256,
        "max_tokens_fallback": None,
    }

    _write_config(
        "max_tokens: -1\n"
        "agent:\n"
        "  max_tokens: 512\n"
        "  name: fallback\n"
    )
    assert config.get_max_tokens_status() == {
        "max_tokens": None,
        "max_tokens_effective": None,
        "max_tokens_fallback": None,
    }

    _write_config(
        "agent:\n"
        "  max_tokens: 512\n"
        "  name: fallback\n"
    )
    assert config.get_max_tokens_status() == {
        "max_tokens": None,
        "max_tokens_effective": 512,
        "max_tokens_fallback": 512,
    }

    _write_config(
        "max_tokens: null\n"
        "agent:\n"
        "  max_tokens: 512\n"
        "  name: fallback\n"
    )
    assert config.get_max_tokens_status() == {
        "max_tokens": None,
        "max_tokens_effective": 512,
        "max_tokens_fallback": 512,
    }

    _write_config("max_tokens: true\n")
    assert config.get_max_tokens_status() == {
        "max_tokens": 1,
        "max_tokens_effective": 1,
        "max_tokens_fallback": None,
    }


def test_set_max_tokens_writes_root_override_and_clears_back_to_agent_fallback(monkeypatch):
    import api.config as config

    monkeypatch.setenv("OPENAI_API_KEY", "expanded-secret")
    _write_config(
        "agent:\n"
        "  max_tokens: 512\n"
        "  name: fallback\n"
        "providers:\n"
        "  openai:\n"
        "    api_key: ${OPENAI_API_KEY}\n"
        "metadata:\n"
        "  keep: true\n"
    )

    saved = config.set_max_tokens(768)
    assert saved == {
        "max_tokens": 768,
        "max_tokens_effective": 768,
        "max_tokens_fallback": None,
    }
    data = _read_config()
    assert data["max_tokens"] == 768
    assert data["agent"]["max_tokens"] == 512
    assert data["agent"]["name"] == "fallback"
    assert config._load_yaml_config_file_raw(config._get_config_path())["providers"]["openai"]["api_key"] == "${OPENAI_API_KEY}"
    assert data["metadata"]["keep"] is True

    saved = config.set_max_tokens(None)
    assert saved == {
        "max_tokens": None,
        "max_tokens_effective": 512,
        "max_tokens_fallback": 512,
    }
    data = _read_config()
    assert "max_tokens" not in data
    assert data["agent"]["max_tokens"] == 512
    assert config._load_yaml_config_file_raw(config._get_config_path())["providers"]["openai"]["api_key"] == "${OPENAI_API_KEY}"
    assert data["metadata"]["keep"] is True


def test_set_max_tokens_invalid_non_empty_input_is_a_true_no_op(monkeypatch):
    import api.config as config

    _write_config(
        "agent:\n"
        "  max_tokens: 512\n"
        "  name: fallback\n"
    )

    monkeypatch.setattr(
        config,
        "_save_yaml_config_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected config save")),
    )
    monkeypatch.setattr(
        config,
        "reload_config",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected reload")),
    )

    assert config.set_max_tokens("abc") == {
        "max_tokens": None,
        "max_tokens_effective": 512,
        "max_tokens_fallback": 512,
    }
    assert _read_config()["agent"]["max_tokens"] == 512


def test_set_max_tokens_blank_without_root_override_is_a_true_no_op(monkeypatch):
    import api.config as config

    _write_config(
        "agent:\n"
        "  max_tokens: 512\n"
        "  name: fallback\n"
    )

    monkeypatch.setattr(
        config,
        "_save_yaml_config_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected config save")),
    )
    monkeypatch.setattr(
        config,
        "reload_config",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected reload")),
    )

    assert config.set_max_tokens(None) == {
        "max_tokens": None,
        "max_tokens_effective": 512,
        "max_tokens_fallback": 512,
    }
    assert _read_config()["agent"]["max_tokens"] == 512


def test_get_settings_exposes_max_tokens_from_the_active_profile(monkeypatch):
    import api.config as config
    from api.routes import handle_get

    monkeypatch.setattr(
        "api.routes.load_settings",
        lambda: {"send_key": "enter", "password_hash": "secret"},
    )
    monkeypatch.setattr(
        config,
        "get_max_tokens_status",
        lambda: {
            "max_tokens": 321,
            "max_tokens_effective": 321,
            "max_tokens_fallback": None,
        },
    )

    handler = _FakeHandler()
    handle_get(handler, urlparse("http://example.com/api/settings"))
    payload = handler.json_body()

    assert handler.status == 200
    assert payload["send_key"] == "enter"
    assert payload["max_tokens"] == 321
    assert payload["max_tokens_effective"] == 321
    assert payload["max_tokens_fallback"] is None
    assert "password_hash" not in payload


def test_post_settings_bridges_max_tokens_without_polluting_settings_payload(monkeypatch):
    import api.auth as auth
    from api.routes import handle_post

    captured = {}

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: "")
    monkeypatch.setattr(auth, "verify_session", lambda cookie: False)
    def _fake_save_settings(body):
        captured["body"] = dict(body)
        return {"send_key": body.get("send_key")}

    monkeypatch.setattr("api.routes.save_settings", _fake_save_settings)
    monkeypatch.setattr(
        "api.config.set_max_tokens",
        lambda value: {
            "max_tokens": 777 if value == 123 else None,
            "max_tokens_effective": 777 if value == 123 else None,
            "max_tokens_fallback": None,
        },
    )

    handler = _FakeHandler(json.dumps({"send_key": "enter", "max_tokens": 123}).encode("utf-8"))
    handle_post(handler, urlparse("http://example.com/api/settings"))
    payload = handler.json_body()

    assert handler.status == 200
    assert captured["body"] == {"send_key": "enter"}
    assert payload["send_key"] == "enter"
    assert payload["max_tokens"] == 777
    assert payload["max_tokens_effective"] == 777
    assert payload["max_tokens_fallback"] is None


def test_post_settings_keeps_current_max_tokens_on_unrelated_save(monkeypatch):
    import api.auth as auth
    from api.routes import handle_post

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: "")
    monkeypatch.setattr(auth, "verify_session", lambda cookie: False)
    monkeypatch.setattr("api.routes.save_settings", lambda body: {"language": body.get("language")})
    monkeypatch.setattr(
        "api.config.get_max_tokens_status",
        lambda: {
            "max_tokens": 100,
            "max_tokens_effective": 100,
            "max_tokens_fallback": None,
        },
    )
    monkeypatch.setattr(
        "api.config.set_max_tokens",
        lambda value: (_ for _ in ()).throw(AssertionError(f"unexpected max_tokens write: {value!r}")),
    )

    handler = _FakeHandler(json.dumps({"language": "pl"}).encode("utf-8"))
    handle_post(handler, urlparse("http://example.com/api/settings"))
    payload = handler.json_body()

    assert handler.status == 200
    assert payload["language"] == "pl"
    assert payload["max_tokens"] == 100
    assert payload["max_tokens_effective"] == 100
    assert payload["max_tokens_fallback"] is None


def test_post_settings_does_not_write_max_tokens_before_auth_failures(monkeypatch):
    import api.auth as auth
    from api.routes import handle_post

    saw_set_max_tokens = {"called": False}

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "get_password_hash", lambda: "hash")
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: "")
    monkeypatch.setattr(auth, "verify_session", lambda cookie: False)
    monkeypatch.setattr(auth, "verify_password", lambda current_password: False)
    monkeypatch.setattr(
        "api.config.set_max_tokens",
        lambda value: saw_set_max_tokens.__setitem__("called", True),
    )
    monkeypatch.setattr(
        "api.routes.save_settings",
        lambda body: (_ for _ in ()).throw(AssertionError("save_settings should not run")),
    )

    handler = _FakeHandler(
        json.dumps(
            {
                "send_key": "enter",
                "max_tokens": 123,
                "_clear_password": True,
                "_current_password": "wrong",
            }
        ).encode("utf-8")
    )
    handle_post(handler, urlparse("http://example.com/api/settings"))
    payload = handler.json_body()

    assert handler.status == 403
    assert payload["error"] == "Current password is incorrect."
    assert saw_set_max_tokens["called"] is False


def test_post_settings_does_not_write_max_tokens_when_save_settings_fails(monkeypatch):
    import api.auth as auth
    from api.routes import handle_post

    saw_set_max_tokens = {"called": False}

    monkeypatch.setattr(auth, "is_auth_enabled", lambda: False)
    monkeypatch.setattr(auth, "get_password_hash", lambda: None)
    monkeypatch.setattr(auth, "parse_cookie", lambda handler: "")
    monkeypatch.setattr(auth, "verify_session", lambda cookie: False)
    monkeypatch.setattr(
        "api.routes.save_settings",
        lambda body: (_ for _ in ()).throw(RuntimeError("save_settings failed")),
    )
    monkeypatch.setattr(
        "api.config.set_max_tokens",
        lambda value: saw_set_max_tokens.__setitem__("called", True),
    )

    handler = _FakeHandler(json.dumps({"send_key": "enter", "max_tokens": 123}).encode("utf-8"))
    with pytest.raises(RuntimeError, match="save_settings failed"):
        handle_post(handler, urlparse("http://example.com/api/settings"))

    assert saw_set_max_tokens["called"] is False


def test_settings_panel_wires_max_tokens_for_dirty_state_and_manual_save():
    import pathlib

    panels_js = (pathlib.Path(__file__).parent.parent / "static" / "panels.js").read_text(encoding="utf-8")
    index_html = (pathlib.Path(__file__).parent.parent / "static" / "index.html").read_text(encoding="utf-8")
    i18n_js = (pathlib.Path(__file__).parent.parent / "static" / "i18n.js").read_text(encoding="utf-8")

    load_block = _function_block(panels_js, "loadSettingsPanel")
    load_idx = load_block.find("$('settingsMaxTokens')")
    assert load_idx != -1
    load_window = load_block[load_idx:load_idx + 420]
    assert "settings.max_tokens" in load_window
    assert "_syncSettingsMaxTokensPlaceholder(maxTokensField,settings.max_tokens_fallback)" in load_block.replace(" ", "")
    assert "maxTokensField.dataset.initialValue=maxTokensField.value" in load_block.replace(" ", "")
    assert "maxTokensField.addEventListener('input',_markSettingsDirty" in load_block.replace(" ", "")
    assert "_schedulePreferencesAutosave" not in load_window

    apply_saved_block = _function_block(panels_js, "_applySavedSettingsUi")
    assert "saved&&saved.max_tokens" in apply_saved_block
    assert "_syncSettingsMaxTokensPlaceholder(maxTokensField,saved&&saved.max_tokens_fallback)" in apply_saved_block.replace(" ", "")
    assert "maxTokensField.dataset.initialValue=maxTokensField.value" in apply_saved_block.replace(" ", "")

    autosave_block = _function_block(panels_js, "_autosavePreferencesSettings")
    assert "const maxTokensField=$('settingsMaxTokens');" in autosave_block
    assert "String(maxTokensField.value||'')!==String(maxTokensField.dataset.initialValue||'')" in autosave_block.replace(" ", "")
    compact_autosave = autosave_block.replace(" ", "")
    assert "if(!pwDirty&&!modelDirty)" in compact_autosave
    assert "if(!maxTokensDirty)" in compact_autosave

    prefs_block = _function_block(panels_js, "_preferencesPayloadFromUi")
    assert "settingsMaxTokens" not in prefs_block
    assert "max_tokens" not in prefs_block

    save_block = _function_block(panels_js, "saveSettings")
    assert "settingsMaxTokens" in save_block
    assert "body.max_tokens" in save_block
    assert "initialMaxTokens" in save_block
    assert "constinitialMaxTokens=String(maxTokensField.dataset.initialValue||'').trim();" in save_block.replace(" ", "")
    assert "if(maxTokensRaw!==initialMaxTokens)" in save_block.replace(" ", "")
    assert "body.max_tokens=maxTokensRaw===''?null:maxTokensRaw" in save_block.replace(" ", "")

    assert 'id="settingsMaxTokens"' in index_html
    assert 'data-i18n-placeholder="settings_placeholder_max_tokens_none"' in index_html
    assert "settings_label_max_tokens" in i18n_js
    assert "settings_desc_max_tokens" in i18n_js
    assert "settings_placeholder_max_tokens_none" in i18n_js
    assert "settings_placeholder_max_tokens_fallback" in i18n_js
