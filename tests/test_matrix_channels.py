from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def channels(monkeypatch, tmp_path):
    from api import channels as module

    monkeypatch.setattr(module, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(module, "get_active_profile_name", lambda: "maverick")
    return module


def _valid_payload(**overrides):
    payload = {
        "homeserver": "https://matrix.example.org",
        "user_id": "@maverick:example.org",
        "auth_method": "access_token",
        "access_token": "secret-token",
        "allowed_users": ["@tyler:example.org", "@kendal:example.org"],
        "allowed_rooms": ["!family:example.org"],
        "require_mention": True,
        "session_scope": "room",
        "auto_thread": False,
        "e2ee_mode": "required",
    }
    payload.update(overrides)
    return payload


def test_get_matrix_channel_never_returns_secrets(channels, tmp_path):
    (tmp_path / ".env").write_text(
        "MATRIX_HOMESERVER=https://matrix.example.org\n"
        "MATRIX_USER_ID=@maverick:example.org\n"
        "MATRIX_ACCESS_TOKEN=super-secret\n"
        "MATRIX_PASSWORD=also-secret\n",
        encoding="utf-8",
    )

    result = channels.get_matrix_channel()

    serialized = repr(result)
    assert "super-secret" not in serialized
    assert "also-secret" not in serialized
    assert result["has_access_token"] is True
    assert result["has_password"] is True
    assert result["profile"] == "maverick"


def test_save_matrix_channel_writes_only_active_profile(channels, tmp_path):
    other_home = tmp_path.parent / "other"
    other_home.mkdir()
    (other_home / ".env").write_text("UNCHANGED=yes\n", encoding="utf-8")

    result = channels.save_matrix_channel(_valid_payload())

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MATRIX_ACCESS_TOKEN=secret-token" in env_text
    assert "MATRIX_PASSWORD=" not in env_text
    assert "MATRIX_E2EE_MODE=required" in env_text
    assert "MATRIX_ENCRYPTION=true" in env_text
    assert "MATRIX_ALLOWED_USERS=@tyler:example.org,@kendal:example.org" in env_text
    assert "MATRIX_ALLOWED_ROOMS=!family:example.org" in env_text
    assert "MATRIX_REQUIRE_MENTION=true" in env_text
    assert "MATRIX_AUTO_THREAD=false" in env_text
    assert (other_home / ".env").read_text(encoding="utf-8") == "UNCHANGED=yes\n"
    assert result["profile"] == "maverick"
    assert result["has_access_token"] is True

    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert config["matrix"]["allowed_users"] == [
        "@tyler:example.org",
        "@kendal:example.org",
    ]
    assert config["matrix"]["allowed_rooms"] == ["!family:example.org"]
    assert config["matrix"]["require_mention"] is True
    assert config["matrix"]["session_scope"] == "room"
    assert config["matrix"]["auto_thread"] is False
    assert "e2ee_mode" not in config["matrix"]


def test_blank_secret_preserves_existing_access_token(channels, tmp_path):
    (tmp_path / ".env").write_text(
        "MATRIX_ACCESS_TOKEN=existing-token\nKEEP_ME=yes\n", encoding="utf-8"
    )

    channels.save_matrix_channel(_valid_payload(access_token=""))

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MATRIX_ACCESS_TOKEN=existing-token" in env_text
    assert "KEEP_ME=yes" in env_text


def test_switching_auth_method_removes_old_secret(channels, tmp_path):
    (tmp_path / ".env").write_text(
        "MATRIX_ACCESS_TOKEN=old-token\n", encoding="utf-8"
    )

    channels.save_matrix_channel(
        _valid_payload(
            auth_method="password",
            access_token="",
            password="new-password",
        )
    )

    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "MATRIX_ACCESS_TOKEN" not in env_text
    assert "MATRIX_PASSWORD=new-password" in env_text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("homeserver", "http://matrix.example.org"),
        ("homeserver", "https://matrix.example.org\nINJECTED=yes"),
        ("user_id", "maverick@example.org"),
        ("allowed_users", ["tyler@example.org"]),
        ("allowed_rooms", ["family-room"]),
        ("session_scope", "global"),
        ("e2ee_mode", "sometimes"),
    ],
)
def test_invalid_matrix_configuration_is_rejected(channels, field, value):
    with pytest.raises(ValueError):
        channels.save_matrix_channel(_valid_payload(**{field: value}))


def test_activation_requires_user_allowlist(channels):
    with pytest.raises(ValueError, match="allowed user"):
        channels.save_matrix_channel(_valid_payload(allowed_users=[]))


def test_room_allowlist_is_optional_for_new_direct_messages(channels):
    result = channels.save_matrix_channel(_valid_payload(allowed_rooms=[]))

    assert result["allowed_rooms"] == []


def test_clear_matrix_channel_removes_only_matrix_keys(channels, tmp_path):
    (tmp_path / ".env").write_text(
        "MATRIX_ACCESS_TOKEN=secret\nOPENAI_API_KEY=keep\n", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: test\nmatrix:\n  require_mention: true\n",
        encoding="utf-8",
    )

    result = channels.clear_matrix_channel()

    assert "MATRIX_" not in (tmp_path / ".env").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=keep" in (tmp_path / ".env").read_text(encoding="utf-8")
    config = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "matrix" not in config
    assert config["model"]["default"] == "test"
    assert result["configured"] is False


def test_restart_matrix_gateway_enables_and_targets_request_profile(channels, monkeypatch, tmp_path):
    channels.save_matrix_channel(_valid_payload())
    calls = []
    monkeypatch.setattr(
        channels,
        "restart_profile_gateway",
        lambda profile: calls.append(profile) or {
            "profile": profile,
            "status": "running",
            "managed": True,
        },
    )

    result = channels.restart_matrix_gateway()

    assert calls == ["maverick"]
    assert result["status"] == "running"
    assert result["managed"] is True
    assert "HERMES_WEBUI_MATRIX_GATEWAY_ENABLED=1" in (tmp_path / ".env").read_text()
