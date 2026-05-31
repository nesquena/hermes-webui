"""Unit tests for api/platforms/feishu.py (Task 1).

These tests mock the agent probe and point the active-profile ``.env`` path at a
tmp file by monkeypatching ``get_active_hermes_home`` in the feishu module's
namespace.
"""
import os
import stat

import pytest

import api.platforms.feishu as feishu


# ── Masked sentinel ────────────────────────────────────────────────────────
MASK = feishu.MASKED_SENTINEL


@pytest.fixture
def env_home(monkeypatch, tmp_path):
    """Point the feishu module's active-profile home at a tmp dir."""
    monkeypatch.setattr(feishu, "get_active_hermes_home", lambda: tmp_path)
    return tmp_path


def _write_env(tmp_path, mapping):
    lines = [f"{k}={v}" for k, v in mapping.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── get_config: read + masking ─────────────────────────────────────────────


def test_get_config_empty_returns_defaults_and_not_configured(env_home):
    cfg = feishu.get_config()
    assert cfg["configured"] is False
    assert cfg["app_id"] == ""
    assert cfg["app_secret_set"] is False
    assert cfg["domain"] == "feishu"
    assert cfg["connection_mode"] == "websocket"
    assert cfg["webhook_host"] == "127.0.0.1"
    assert cfg["webhook_port"] == "8765"
    assert cfg["webhook_path"] == "/feishu/webhook"
    assert cfg["verification_token_set"] is False
    assert cfg["encrypt_key_set"] is False
    assert cfg["allow_all_users"] is False
    assert cfg["require_mention"] is True
    assert cfg["group_policy"] == "open"
    assert cfg["allowed_users"] == ""
    assert cfg["home_channel"] == ""


def test_get_config_masks_secrets_and_never_returns_them(env_home):
    _write_env(
        env_home,
        {
            "FEISHU_APP_ID": "cli_abc",
            "FEISHU_APP_SECRET": "supersecret",
            "FEISHU_VERIFICATION_TOKEN": "vtok",
            "FEISHU_ENCRYPT_KEY": "ekey",
        },
    )
    cfg = feishu.get_config()
    # configured because both app_id and app_secret present
    assert cfg["configured"] is True
    assert cfg["app_id"] == "cli_abc"
    assert cfg["app_secret_set"] is True
    assert cfg["verification_token_set"] is True
    assert cfg["encrypt_key_set"] is True
    # secrets never leak (under any key)
    flat = repr(cfg)
    assert "supersecret" not in flat
    assert "vtok" not in flat
    assert "ekey" not in flat
    assert "app_secret" not in cfg
    assert "verification_token" not in cfg
    assert "encrypt_key" not in cfg


def test_get_config_app_id_without_secret_not_configured(env_home):
    _write_env(env_home, {"FEISHU_APP_ID": "cli_abc"})
    cfg = feishu.get_config()
    assert cfg["configured"] is False
    assert cfg["app_id"] == "cli_abc"
    assert cfg["app_secret_set"] is False


def test_get_config_parses_bools_leniently(env_home):
    _write_env(
        env_home,
        {
            "FEISHU_ALLOW_ALL_USERS": "true",
            "FEISHU_REQUIRE_MENTION": "false",
        },
    )
    cfg = feishu.get_config()
    assert cfg["allow_all_users"] is True
    assert cfg["require_mention"] is False


# ── save: writes the right keys ────────────────────────────────────────────


def test_save_writes_expected_keys_and_returns_fields(env_home):
    res = feishu.save(
        {
            "app_id": "cli_new",
            "app_secret": "freshsecret",
            "domain": "lark",
            "connection_mode": "websocket",
            "allow_all_users": True,
            "require_mention": False,
            "group_policy": "disabled",
            "allowed_users": "u1,u2",
            "home_channel": "oc_123",
        }
    )
    assert res["saved"] is True
    fields = set(res["fields"])
    assert "FEISHU_APP_ID" in fields
    assert "FEISHU_APP_SECRET" in fields
    assert "FEISHU_DOMAIN" in fields
    assert "FEISHU_CONNECTION_MODE" in fields
    assert "FEISHU_ALLOW_ALL_USERS" in fields
    assert "FEISHU_REQUIRE_MENTION" in fields
    assert "FEISHU_GROUP_POLICY" in fields
    assert "FEISHU_ALLOWED_USERS" in fields
    assert "FEISHU_HOME_CHANNEL" in fields

    on_disk = feishu._load_env_file(env_home / ".env")
    assert on_disk["FEISHU_APP_ID"] == "cli_new"
    assert on_disk["FEISHU_APP_SECRET"] == "freshsecret"
    assert on_disk["FEISHU_DOMAIN"] == "lark"
    # bools written as true/false strings
    assert on_disk["FEISHU_ALLOW_ALL_USERS"] == "true"
    assert on_disk["FEISHU_REQUIRE_MENTION"] == "false"
    assert on_disk["FEISHU_GROUP_POLICY"] == "disabled"


def test_save_env_is_0600(env_home):
    feishu.save({"app_id": "cli_new", "app_secret": "freshsecret"})
    mode = stat.S_IMODE(os.stat(env_home / ".env").st_mode)
    assert mode == 0o600


# ── save: secret untouched when masked/blank ──────────────────────────────


def test_save_does_not_write_secret_when_masked(env_home, monkeypatch):
    _write_env(
        env_home,
        {"FEISHU_APP_ID": "cli_abc", "FEISHU_APP_SECRET": "existingsecret"},
    )

    captured = {}

    def fake_write(env_path, updates):
        captured["updates"] = updates

    monkeypatch.setattr(feishu, "_write_env_file", fake_write)

    res = feishu.save({"app_id": "cli_abc", "app_secret": MASK})
    assert "FEISHU_APP_SECRET" not in captured["updates"]
    assert "FEISHU_APP_SECRET" not in res["fields"]


def test_save_does_not_write_secret_when_blank(env_home, monkeypatch):
    _write_env(
        env_home,
        {"FEISHU_APP_ID": "cli_abc", "FEISHU_APP_SECRET": "existingsecret"},
    )

    captured = {}
    monkeypatch.setattr(
        feishu, "_write_env_file", lambda p, u: captured.update(updates=u)
    )

    feishu.save({"app_id": "cli_abc", "app_secret": "   "})
    assert "FEISHU_APP_SECRET" not in captured["updates"]


def test_save_real_file_keeps_existing_secret_when_masked(env_home):
    _write_env(
        env_home,
        {"FEISHU_APP_ID": "cli_abc", "FEISHU_APP_SECRET": "existingsecret"},
    )
    feishu.save({"app_id": "cli_abc", "app_secret": MASK})
    on_disk = feishu._load_env_file(env_home / ".env")
    assert on_disk["FEISHU_APP_SECRET"] == "existingsecret"


def test_save_missing_required_when_not_already_set_raises(env_home):
    with pytest.raises(feishu.FeishuConfigError):
        feishu.save({"app_id": "cli_abc"})  # no secret, none on disk


def test_save_bad_enum_raises(env_home):
    with pytest.raises(feishu.FeishuConfigError):
        feishu.save({"app_id": "a", "app_secret": "s", "domain": "bogus"})


# ── save: webhook fields only in webhook mode ──────────────────────────────


def test_save_webhook_fields_written_in_webhook_mode(env_home, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feishu, "_write_env_file", lambda p, u: captured.update(updates=u)
    )
    feishu.save(
        {
            "app_id": "a",
            "app_secret": "s",
            "connection_mode": "webhook",
            "webhook_host": "0.0.0.0",
            "webhook_port": "9000",
            "webhook_path": "/hook",
            "verification_token": "vt",
            "encrypt_key": "ek",
        }
    )
    u = captured["updates"]
    assert u["FEISHU_WEBHOOK_HOST"] == "0.0.0.0"
    assert u["FEISHU_WEBHOOK_PORT"] == "9000"
    assert u["FEISHU_WEBHOOK_PATH"] == "/hook"
    assert u["FEISHU_VERIFICATION_TOKEN"] == "vt"
    assert u["FEISHU_ENCRYPT_KEY"] == "ek"


def test_save_webhook_fields_not_written_in_websocket_mode(env_home, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        feishu, "_write_env_file", lambda p, u: captured.update(updates=u)
    )
    feishu.save(
        {
            "app_id": "a",
            "app_secret": "s",
            "connection_mode": "websocket",
            "webhook_host": "0.0.0.0",
            "verification_token": "vt",
        }
    )
    u = captured["updates"]
    assert "FEISHU_WEBHOOK_HOST" not in u
    assert "FEISHU_WEBHOOK_PORT" not in u
    assert "FEISHU_WEBHOOK_PATH" not in u
    assert "FEISHU_VERIFICATION_TOKEN" not in u
    assert "FEISHU_ENCRYPT_KEY" not in u


# ── validate: maps probe result ────────────────────────────────────────────


def test_validate_success_maps_probe_dict(monkeypatch):
    monkeypatch.setattr(
        feishu,
        "_get_probe_bot",
        lambda: (lambda app_id, app_secret, domain: {
            "bot_name": "MyBot",
            "bot_open_id": "ou_1",
        }),
    )
    res = feishu.validate("a", "s", "feishu")
    assert res == {"ok": True, "bot_name": "MyBot", "bot_open_id": "ou_1"}


def test_validate_none_maps_to_error(monkeypatch):
    monkeypatch.setattr(
        feishu, "_get_probe_bot", lambda: (lambda *a, **k: None)
    )
    res = feishu.validate("a", "s", "feishu")
    assert res["ok"] is False
    assert res.get("error")


def test_validate_probe_unavailable(monkeypatch):
    monkeypatch.setattr(feishu, "_get_probe_bot", lambda: None)
    res = feishu.validate("a", "s", "feishu")
    assert res == {"ok": False, "error": "agent unavailable"}


def test_validate_probe_exception_mapped(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(feishu, "_get_probe_bot", lambda: boom)
    res = feishu.validate("a", "s", "feishu")
    assert res["ok"] is False
    assert "network down" in res["error"]


# ── restart_gateway ────────────────────────────────────────────────────────


def test_restart_gateway_cli_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(feishu.shutil, "which", lambda name: None)
    monkeypatch.setattr(feishu, "get_active_hermes_home", lambda: tmp_path)
    # ensure fallback path does not exist
    monkeypatch.setattr(
        feishu.Path, "home", staticmethod(lambda: tmp_path / "nohome")
    )
    res = feishu.restart_gateway()
    assert res["ok"] is False
    assert "not found" in res["detail"].lower()


def test_restart_gateway_runs_cli(monkeypatch, tmp_path):
    fake_hermes = tmp_path / "hermes"
    fake_hermes.write_text("#!/bin/sh\n")
    monkeypatch.setattr(feishu.shutil, "which", lambda name: str(fake_hermes))
    monkeypatch.setattr(feishu, "get_active_hermes_home", lambda: tmp_path)

    calls = {}

    class _R:
        returncode = 0
        stdout = "gateway restarted"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["env"] = kwargs.get("env")
        return _R()

    monkeypatch.setattr(feishu.subprocess, "run", fake_run)
    res = feishu.restart_gateway()
    assert res["ok"] is True
    assert calls["cmd"] == [str(fake_hermes), "gateway", "restart"]
    assert calls["env"]["HERMES_HOME"] == str(tmp_path)
