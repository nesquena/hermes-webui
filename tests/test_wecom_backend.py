"""Unit tests for api/platforms/wecom.py (mirrors tests/test_feishu_backend.py).

These tests point the active-profile ``.env`` path at a tmp file by
monkeypatching ``get_active_hermes_home`` in the wecom module's namespace, and
exercise both connection modes (``wecom`` WebSocket bot / ``wecom_callback``).
"""
import os
import stat

import pytest

import api.platforms.wecom as wecom


# ── Masked sentinel ────────────────────────────────────────────────────────
MASK = wecom.MASKED_SENTINEL


@pytest.fixture
def env_home(monkeypatch, tmp_path):
    """Point the wecom module's active-profile home at a tmp dir."""
    monkeypatch.setattr(wecom, "get_active_hermes_home", lambda: tmp_path)
    # Default: no agent probe (the common case) so validate() uses the fallback.
    monkeypatch.setattr(wecom, "_get_probe_bot", lambda: None)
    return tmp_path


def _write_env(tmp_path, mapping):
    lines = [f"{k}={v}" for k, v in mapping.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ── get_config: read + masking ─────────────────────────────────────────────


def test_get_config_empty_returns_defaults_and_not_configured(env_home):
    cfg = wecom.get_config()
    assert cfg["configured"] is False
    assert cfg["mode"] == "wecom"
    assert cfg["bot_id"] == ""
    assert cfg["secret_set"] is False
    assert cfg["websocket_url"] == "wss://openws.work.weixin.qq.com"
    assert cfg["dm_policy"] == "open"
    assert cfg["group_policy"] == "open"
    assert cfg["allowed_users"] == ""
    assert cfg["home_channel"] == ""
    # callback defaults
    assert cfg["callback_corp_id"] == ""
    assert cfg["callback_corp_secret_set"] is False
    assert cfg["callback_agent_id"] == ""
    assert cfg["callback_token_set"] is False
    assert cfg["callback_encoding_aes_key_set"] is False
    assert cfg["callback_host"] == "0.0.0.0"
    assert cfg["callback_port"] == "8645"


def test_get_config_ws_mode_masks_secret_and_never_returns_it(env_home):
    _write_env(
        env_home,
        {
            "WECOM_MODE": "wecom",
            "WECOM_BOT_ID": "bot_abc",
            "WECOM_SECRET": "supersecret",
        },
    )
    cfg = wecom.get_config()
    assert cfg["configured"] is True
    assert cfg["mode"] == "wecom"
    assert cfg["bot_id"] == "bot_abc"
    assert cfg["secret_set"] is True
    flat = repr(cfg)
    assert "supersecret" not in flat
    assert "secret" not in cfg  # only *_set booleans, never the raw value


def test_get_config_callback_mode_masks_all_secrets(env_home):
    _write_env(
        env_home,
        {
            "WECOM_MODE": "wecom_callback",
            "WECOM_CALLBACK_CORP_ID": "ww_corp",
            "WECOM_CALLBACK_CORP_SECRET": "corpsecret",
            "WECOM_CALLBACK_AGENT_ID": "1000002",
            "WECOM_CALLBACK_TOKEN": "tok123",
            "WECOM_CALLBACK_ENCODING_AES_KEY": "aeskey123",
        },
    )
    cfg = wecom.get_config()
    assert cfg["configured"] is True
    assert cfg["mode"] == "wecom_callback"
    assert cfg["callback_corp_id"] == "ww_corp"
    assert cfg["callback_agent_id"] == "1000002"
    assert cfg["callback_corp_secret_set"] is True
    assert cfg["callback_token_set"] is True
    assert cfg["callback_encoding_aes_key_set"] is True
    # secrets never leak (under any key)
    flat = repr(cfg)
    for leaked in ("corpsecret", "tok123", "aeskey123"):
        assert leaked not in flat
    assert "callback_corp_secret" not in cfg
    assert "callback_token" not in cfg
    assert "callback_encoding_aes_key" not in cfg


def test_get_config_infers_callback_mode_when_only_corp_set(env_home):
    _write_env(env_home, {"WECOM_CALLBACK_CORP_ID": "ww_corp"})
    cfg = wecom.get_config()
    assert cfg["mode"] == "wecom_callback"


# ── save: WebSocket mode writes the right keys ─────────────────────────────


def test_save_ws_writes_expected_keys_and_returns_fields(env_home):
    res = wecom.save(
        {
            "mode": "wecom",
            "bot_id": "bot_new",
            "secret": "freshsecret",
            "websocket_url": "wss://openws.work.weixin.qq.com",
            "dm_policy": "allowlist",
            "allowed_users": "u1,u2",
            "group_policy": "disabled",
            "home_channel": "chat_123",
        }
    )
    assert res["saved"] is True
    fields = set(res["fields"])
    assert "WECOM_MODE" in fields
    assert "WECOM_BOT_ID" in fields
    assert "WECOM_SECRET" in fields
    assert "WECOM_WEBSOCKET_URL" in fields
    assert "WECOM_DM_POLICY" in fields
    assert "WECOM_ALLOWED_USERS" in fields
    assert "WECOM_GROUP_POLICY" in fields
    assert "WECOM_HOME_CHANNEL" in fields
    # callback keys must NOT be written in ws mode
    assert not any(k.startswith("WECOM_CALLBACK_") for k in fields)

    on_disk = wecom._load_env_file(env_home / ".env")
    assert on_disk["WECOM_BOT_ID"] == "bot_new"
    assert on_disk["WECOM_SECRET"] == "freshsecret"
    assert on_disk["WECOM_DM_POLICY"] == "allowlist"
    assert on_disk["WECOM_GROUP_POLICY"] == "disabled"


def test_save_callback_writes_expected_keys(env_home):
    res = wecom.save(
        {
            "mode": "wecom_callback",
            "callback_corp_id": "ww_corp",
            "callback_corp_secret": "corpsecret",
            "callback_agent_id": "1000002",
            "callback_token": "tok123",
            "callback_encoding_aes_key": "aeskey123",
            "callback_host": "0.0.0.0",
            "callback_port": "9000",
        }
    )
    fields = set(res["fields"])
    assert "WECOM_CALLBACK_CORP_ID" in fields
    assert "WECOM_CALLBACK_CORP_SECRET" in fields
    assert "WECOM_CALLBACK_AGENT_ID" in fields
    assert "WECOM_CALLBACK_TOKEN" in fields
    assert "WECOM_CALLBACK_ENCODING_AES_KEY" in fields
    assert "WECOM_CALLBACK_HOST" in fields
    assert "WECOM_CALLBACK_PORT" in fields
    # ws-only keys must NOT be written in callback mode
    assert "WECOM_BOT_ID" not in fields
    assert "WECOM_SECRET" not in fields

    on_disk = wecom._load_env_file(env_home / ".env")
    assert on_disk["WECOM_CALLBACK_PORT"] == "9000"
    assert on_disk["WECOM_MODE"] == "wecom_callback"


def test_save_env_is_0600(env_home):
    wecom.save({"mode": "wecom", "bot_id": "bot_new", "secret": "freshsecret"})
    mode = stat.S_IMODE(os.stat(env_home / ".env").st_mode)
    assert mode == 0o600


# ── save: secret untouched when masked/blank ──────────────────────────────


def test_save_does_not_write_secret_when_masked(env_home, monkeypatch):
    _write_env(
        env_home,
        {"WECOM_BOT_ID": "bot_abc", "WECOM_SECRET": "existingsecret"},
    )

    captured = {}
    monkeypatch.setattr(
        wecom, "_write_env_file", lambda p, u: captured.update(updates=u)
    )

    res = wecom.save({"mode": "wecom", "bot_id": "bot_abc", "secret": MASK})
    assert "WECOM_SECRET" not in captured["updates"]
    assert "WECOM_SECRET" not in res["fields"]


def test_save_does_not_write_secret_when_blank(env_home, monkeypatch):
    _write_env(
        env_home,
        {"WECOM_BOT_ID": "bot_abc", "WECOM_SECRET": "existingsecret"},
    )

    captured = {}
    monkeypatch.setattr(
        wecom, "_write_env_file", lambda p, u: captured.update(updates=u)
    )

    wecom.save({"mode": "wecom", "bot_id": "bot_abc", "secret": "   "})
    assert "WECOM_SECRET" not in captured["updates"]


def test_save_real_file_keeps_existing_secret_when_masked(env_home):
    _write_env(
        env_home,
        {"WECOM_BOT_ID": "bot_abc", "WECOM_SECRET": "existingsecret"},
    )
    wecom.save({"mode": "wecom", "bot_id": "bot_abc", "secret": MASK})
    on_disk = wecom._load_env_file(env_home / ".env")
    assert on_disk["WECOM_SECRET"] == "existingsecret"


def test_save_callback_keeps_existing_secrets_when_masked(env_home):
    _write_env(
        env_home,
        {
            "WECOM_CALLBACK_CORP_ID": "ww_corp",
            "WECOM_CALLBACK_CORP_SECRET": "corpsecret",
            "WECOM_CALLBACK_AGENT_ID": "1000002",
            "WECOM_CALLBACK_TOKEN": "tok123",
            "WECOM_CALLBACK_ENCODING_AES_KEY": "aeskey123",
        },
    )
    wecom.save(
        {
            "mode": "wecom_callback",
            "callback_corp_id": "ww_corp",
            "callback_corp_secret": MASK,
            "callback_agent_id": "1000002",
            "callback_token": MASK,
            "callback_encoding_aes_key": MASK,
        }
    )
    on_disk = wecom._load_env_file(env_home / ".env")
    assert on_disk["WECOM_CALLBACK_CORP_SECRET"] == "corpsecret"
    assert on_disk["WECOM_CALLBACK_TOKEN"] == "tok123"
    assert on_disk["WECOM_CALLBACK_ENCODING_AES_KEY"] == "aeskey123"


# ── save: required-field enforcement ───────────────────────────────────────


def test_save_ws_missing_secret_raises(env_home):
    with pytest.raises(wecom.WecomConfigError):
        wecom.save({"mode": "wecom", "bot_id": "bot_abc"})  # no secret on disk


def test_save_ws_missing_bot_id_raises(env_home):
    with pytest.raises(wecom.WecomConfigError):
        wecom.save({"mode": "wecom", "secret": "s"})


def test_save_callback_missing_required_raises(env_home):
    with pytest.raises(wecom.WecomConfigError):
        wecom.save(
            {
                "mode": "wecom_callback",
                "callback_corp_id": "ww_corp",
                "callback_corp_secret": "s",
                "callback_agent_id": "1000002",
                # missing token + aes key
            }
        )


def test_save_bad_mode_raises(env_home):
    with pytest.raises(wecom.WecomConfigError):
        wecom.save({"mode": "bogus"})


def test_save_bad_dm_policy_raises(env_home):
    with pytest.raises(wecom.WecomConfigError):
        wecom.save(
            {"mode": "wecom", "bot_id": "b", "secret": "s", "dm_policy": "bogus"}
        )


# ── validate: fallback (no agent probe) ────────────────────────────────────


def test_validate_ws_fallback_ok(env_home):
    res = wecom.validate({"mode": "wecom", "bot_id": "b", "secret": "s"})
    assert res == {"ok": True}


def test_validate_ws_fallback_missing_bot_id(env_home):
    res = wecom.validate({"mode": "wecom", "secret": "s"})
    assert res["ok"] is False
    assert "WECOM_BOT_ID" in res["error"]


def test_validate_callback_fallback_ok(env_home):
    res = wecom.validate(
        {
            "mode": "wecom_callback",
            "callback_corp_id": "ww",
            "callback_corp_secret": "s",
            "callback_agent_id": "1",
            "callback_token": "t",
            "callback_encoding_aes_key": "a",
        }
    )
    assert res == {"ok": True}


def test_validate_callback_fallback_missing_aes_key(env_home):
    res = wecom.validate(
        {
            "mode": "wecom_callback",
            "callback_corp_id": "ww",
            "callback_corp_secret": "s",
            "callback_agent_id": "1",
            "callback_token": "t",
        }
    )
    assert res["ok"] is False
    assert "ENCODING_AES_KEY" in res["error"]


def test_validate_bad_mode(env_home):
    res = wecom.validate({"mode": "bogus"})
    assert res["ok"] is False


def test_validate_uses_agent_probe_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(wecom, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        wecom, "_get_probe_bot", lambda: (lambda payload: {"bot_name": "MyBot"})
    )
    res = wecom.validate({"mode": "wecom", "bot_id": "b", "secret": "s"})
    assert res["ok"] is True
    assert res["bot_name"] == "MyBot"


def test_validate_agent_probe_exception_mapped(monkeypatch, tmp_path):
    monkeypatch.setattr(wecom, "get_active_hermes_home", lambda: tmp_path)

    def boom(payload):
        raise RuntimeError("network down")

    monkeypatch.setattr(wecom, "_get_probe_bot", lambda: boom)
    res = wecom.validate({"mode": "wecom", "bot_id": "b", "secret": "s"})
    assert res["ok"] is False
    assert "network down" in res["error"]


# ── restart_gateway ────────────────────────────────────────────────────────


def test_restart_gateway_cli_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(wecom.shutil, "which", lambda name: None)
    monkeypatch.setattr(wecom, "get_active_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        wecom.Path, "home", staticmethod(lambda: tmp_path / "nohome")
    )
    res = wecom.restart_gateway()
    assert res["ok"] is False
    assert "not found" in res["detail"].lower()


def test_restart_gateway_runs_cli(monkeypatch, tmp_path):
    fake_hermes = tmp_path / "hermes"
    fake_hermes.write_text("#!/bin/sh\n")
    monkeypatch.setattr(wecom.shutil, "which", lambda name: str(fake_hermes))
    monkeypatch.setattr(wecom, "get_active_hermes_home", lambda: tmp_path)

    calls = {}

    class _R:
        returncode = 0
        stdout = "gateway restarted"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["env"] = kwargs.get("env")
        return _R()

    monkeypatch.setattr(wecom.subprocess, "run", fake_run)
    res = wecom.restart_gateway()
    assert res["ok"] is True
    assert calls["cmd"] == [str(fake_hermes), "gateway", "restart"]
    assert calls["env"]["HERMES_HOME"] == str(tmp_path)
