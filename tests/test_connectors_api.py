"""Tests for the Hermes gateway Connectors API helpers."""

import json
from pathlib import Path

import yaml


def _config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


def test_list_connectors_exposes_only_safe_manifest_payload(tmp_path, monkeypatch):
    from api import connectors

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    payload = connectors.list_connectors()
    ids = {connector["id"] for connector in payload["connectors"]}
    assert {
        "telegram",
        "discord",
        "slack",
        "google_chat",
        "whatsapp",
        "signal",
        "sms",
        "email",
        "homeassistant",
        "mattermost",
        "matrix",
        "dingtalk",
        "feishu",
        "wecom",
        "wecom_callback",
        "weixin",
        "bluebubbles",
        "qqbot",
        "yuanbao",
        "teams",
        "line",
        "ntfy",
        "browser",
        "webhook",
        "api_server",
    } <= ids

    telegram = next(connector for connector in payload["connectors"] if connector["id"] == "telegram")
    assert telegram["configuration_supported"] is True
    assert telegram["status"] == "not_configured"

    slack = next(connector for connector in payload["connectors"] if connector["id"] == "slack")
    assert slack["configuration_supported"] is False
    assert "SLACK_APP_TOKEN" in slack["required_env"]

    google_chat = next(connector for connector in payload["connectors"] if connector["id"] == "google_chat")
    assert google_chat["configuration_supported"] is False
    assert google_chat["toggle_supported"] is False
    assert "GOOGLE_CHAT_SERVICE_ACCOUNT_JSON" in google_chat["required_env"]


def test_dynamic_runtime_managed_platforms_are_listed(tmp_path, monkeypatch):
    from api import connectors

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    monkeypatch.setattr(
        connectors,
        "_discover_runtime_managed_platforms",
        lambda: {
            "future_chat": connectors._runtime_managed_manifest(
                "future_chat",
                {
                    "label": "Future Chat",
                    "required_env": ["FUTURE_CHAT_TOKEN"],
                    "source": "test",
                },
            )
        },
    )

    payload = connectors.list_connectors()
    future = next(connector for connector in payload["connectors"] if connector["id"] == "future_chat")
    assert future["label"] == "Future Chat"
    assert future["configuration_supported"] is False
    assert future["required_env"] == ["FUTURE_CHAT_TOKEN"]


def test_connectors_runtime_reuses_gateway_status_payload(tmp_path, monkeypatch):
    from api import connectors, routes

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    monkeypatch.setattr(
        routes,
        "_gateway_status_payload",
        lambda: {
            "running": True,
            "configured": True,
            "platforms": [{"name": "telegram", "label": "Telegram"}],
            "session_count": 1,
            "health": {"gateway_state": "running", "reason": "test"},
        },
    )

    payload = connectors.list_connectors()
    assert payload["runtime"]["running"] is True
    assert payload["runtime"]["configured"] is True
    assert payload["runtime"]["platforms"] == [{"name": "telegram", "label": "Telegram"}]


def test_save_connector_masks_secret_in_response_and_writes_native_config(tmp_path, monkeypatch):
    from api import connectors

    path = _config_path(tmp_path)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    secret = "123456789:super-secret-token"

    result = connectors.save_connector(
        "telegram",
        {"fields": {"token": secret, "reply_to_mode": "first", "allowed_chats": "123\n456"}},
    )

    serialized = json.dumps(result, sort_keys=True, ensure_ascii=False)
    assert secret not in serialized
    assert "••••••" in serialized

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["platforms"]["telegram"]["token"] == secret
    assert saved["platforms"]["telegram"]["reply_to_mode"] == "first"
    assert saved["platforms"]["telegram"]["extra"]["allowed_chats"] == ["123", "456"]


def test_masked_secret_placeholder_preserves_existing_value(tmp_path, monkeypatch):
    from api import connectors

    path = _config_path(tmp_path)
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    connectors.save_connector("discord", {"fields": {"token": "discord-token-one"}})
    connectors.save_connector(
        "discord",
        {"fields": {"token": "••••••-one", "reply_to_mode": "all"}},
    )

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["platforms"]["discord"]["token"] == "discord-token-one"
    assert saved["platforms"]["discord"]["reply_to_mode"] == "all"


def test_raw_yaml_write_does_not_expand_environment_secret(tmp_path, monkeypatch):
    from api import connectors

    path = _config_path(tmp_path)
    path.write_text(
        "platforms:\n  telegram:\n    token: ${TELEGRAM_BOT_TOKEN}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "real-env-token")

    connectors.save_connector("telegram", {"fields": {"reply_to_mode": "off"}})

    saved_text = path.read_text(encoding="utf-8")
    assert "real-env-token" not in saved_text
    assert "${TELEGRAM_BOT_TOKEN}" in saved_text


def test_toggle_requires_required_fields(tmp_path, monkeypatch):
    from api import connectors

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    try:
        connectors.toggle_connector("telegram", {"enabled": True})
    except connectors.ConnectorError as exc:
        assert "Required connector fields" in str(exc)
    else:
        raise AssertionError("toggle_connector should reject missing required fields")


def test_test_connector_uses_configuration_validation_without_leaking_secrets(tmp_path, monkeypatch):
    from api import connectors

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    connectors.save_connector("api_server", {"fields": {"key": "api-server-key", "port": 8080}})

    result = connectors.test_connector("api_server")
    serialized = json.dumps(result, sort_keys=True)
    assert result["test_level"] == "configuration"
    assert "api-server-key" not in serialized
