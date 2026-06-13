"""Tests for the Hermes gateway Connectors API helpers."""

import json
from pathlib import Path

import yaml


def _config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"


def test_list_connectors_exposes_verified_fallback_when_runtime_metadata_is_unavailable(tmp_path, monkeypatch):
    from api import connectors

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    monkeypatch.setattr(connectors, "_discover_runtime_platform_specs", lambda: {})

    payload = connectors.list_connectors()
    ids = {connector["id"] for connector in payload["connectors"]}
    assert ids == {"telegram", "discord", "api_server"}

    telegram = next(connector for connector in payload["connectors"] if connector["id"] == "telegram")
    assert telegram["configuration_supported"] is True
    assert telegram["category"] == "messaging"
    assert telegram["status"] == "not_configured"

    api_server = next(connector for connector in payload["connectors"] if connector["id"] == "api_server")
    assert api_server["configuration_supported"] is True
    assert api_server["category"] == "developer_api"


def test_runtime_metadata_is_source_of_truth_for_supported_platforms(tmp_path, monkeypatch):
    from api import connectors

    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    monkeypatch.setattr(
        connectors,
        "_discover_runtime_platform_specs",
        lambda: {
            "email": {"source": "gateway.config.Platform"},
            "webhook": {"source": "gateway.config.Platform"},
            "api_server": {"source": "gateway.config.Platform"},
            "future_chat": {
                "label": "Future Chat",
                "required_env": ["FUTURE_CHAT_TOKEN"],
                "source": "gateway.platform_registry",
            },
        },
    )

    payload = connectors.list_connectors()
    ids = {connector["id"] for connector in payload["connectors"]}
    assert {"email", "webhook", "api_server", "future_chat"} <= ids
    assert "google_chat" not in ids

    email = next(connector for connector in payload["connectors"] if connector["id"] == "email")
    assert email["category"] == "messaging"
    assert email["configuration_supported"] is False

    webhook = next(connector for connector in payload["connectors"] if connector["id"] == "webhook")
    assert webhook["category"] == "event_webhook"
    assert webhook["configuration_supported"] is False

    api_server = next(connector for connector in payload["connectors"] if connector["id"] == "api_server")
    assert api_server["category"] == "developer_api"
    assert api_server["configuration_supported"] is True

    future = next(connector for connector in payload["connectors"] if connector["id"] == "future_chat")
    assert future["label"] == "Future Chat"
    assert future["configuration_supported"] is False
    assert future["required_env"] == ["FUTURE_CHAT_TOKEN"]


def test_config_yaml_platforms_are_listed_when_runtime_metadata_is_missing(tmp_path, monkeypatch):
    from api import connectors

    path = _config_path(tmp_path)
    path.write_text(
        "platforms:\n  webhook:\n    enabled: true\n    extra:\n      routes:\n        github-pr: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(_config_path(tmp_path)))
    monkeypatch.setattr(connectors, "_discover_runtime_platform_specs", lambda: {})

    payload = connectors.list_connectors()
    webhook = next(connector for connector in payload["connectors"] if connector["id"] == "webhook")
    assert webhook["category"] == "event_webhook"
    assert webhook["configuration_supported"] is False
    assert webhook["route_count"] == 1


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
