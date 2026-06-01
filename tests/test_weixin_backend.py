"""Unit tests for api/platforms/weixin.py.

The QR login low-level iLink calls are ALWAYS mocked — these tests never touch
the network. We point the active-profile ``.env`` / accounts dir at a tmp dir by
monkeypatching ``get_active_hermes_home`` in the weixin module's namespace.
"""
from __future__ import annotations

import json

import pytest

import api.platforms.weixin as weixin


@pytest.fixture
def env_home(monkeypatch, tmp_path):
    monkeypatch.setattr(weixin, "get_active_hermes_home", lambda: tmp_path)
    # Reset the module-level pending-login registry between tests.
    with weixin._PENDING_LOCK:
        weixin._PENDING.clear()
    return tmp_path


def _fake_run_coro(return_value):
    """Return a ``_run_coro`` stub that closes the (unused) coroutine and yields
    ``return_value`` — avoids 'coroutine was never awaited' warnings."""

    def _runner(coro):
        try:
            coro.close()
        except Exception:
            pass
        return return_value

    return _runner


def _write_env(tmp_path, mapping):
    lines = [f"{k}={v}" for k, v in mapping.items()]
    (tmp_path / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_account(tmp_path, account_id, payload=None):
    acc_dir = tmp_path / "weixin" / "accounts"
    acc_dir.mkdir(parents=True, exist_ok=True)
    (acc_dir / f"{account_id}.json").write_text(
        json.dumps(payload or {"token": "t", "base_url": "https://x"}),
        encoding="utf-8",
    )


# ── get_config ───────────────────────────────────────────────────────────────


def test_get_config_empty_returns_defaults_and_not_configured(env_home):
    cfg = weixin.get_config()
    assert cfg["configured"] is False
    assert cfg["account_id"] == ""
    assert cfg["token_set"] is False
    assert cfg["dm_policy"] == "open"
    assert cfg["group_policy"] == "disabled"
    assert cfg["allowed_users"] == ""
    assert cfg["group_allowed_users"] == ""
    assert cfg["home_channel"] == ""


def test_get_config_detects_account_from_disk(env_home):
    _write_account(env_home, "ilink_bot_123")
    cfg = weixin.get_config()
    assert cfg["configured"] is True
    assert cfg["account_id"] == "ilink_bot_123"


def test_get_config_ignores_context_token_sidecar(env_home):
    acc_dir = env_home / "weixin" / "accounts"
    acc_dir.mkdir(parents=True, exist_ok=True)
    (acc_dir / "abc.context-tokens.json").write_text("{}", encoding="utf-8")
    cfg = weixin.get_config()
    # Only a sidecar present → no real account.
    assert cfg["account_id"] == ""
    assert cfg["configured"] is False


def test_get_config_never_returns_token(env_home):
    _write_env(env_home, {"WEIXIN_ACCOUNT_ID": "a1", "WEIXIN_TOKEN": "supersecrettoken"})
    cfg = weixin.get_config()
    assert cfg["token_set"] is True
    assert cfg["configured"] is True
    flat = repr(cfg)
    assert "supersecrettoken" not in flat
    assert "token" not in cfg  # only token_set, never the raw value


def test_get_config_env_account_preferred(env_home):
    _write_env(env_home, {"WEIXIN_ACCOUNT_ID": "env_acc", "WEIXIN_DM_POLICY": "pairing"})
    cfg = weixin.get_config()
    assert cfg["account_id"] == "env_acc"
    assert cfg["dm_policy"] == "pairing"


# ── save: only access-policy keys, validated ─────────────────────────────────


def test_save_writes_policy_keys(env_home):
    res = weixin.save(
        {
            "dm_policy": "allowlist",
            "allowed_users": "u1, u2",
            "group_policy": "open",
            "group_allowed_users": "g1",
            "home_channel": "chat42",
        }
    )
    assert res["saved"] is True
    assert set(res["fields"]) == {
        "WEIXIN_DM_POLICY",
        "WEIXIN_ALLOWED_USERS",
        "WEIXIN_GROUP_POLICY",
        "WEIXIN_GROUP_ALLOWED_USERS",
        "WEIXIN_HOME_CHANNEL",
    }
    text = (env_home / ".env").read_text(encoding="utf-8")
    assert "WEIXIN_DM_POLICY=allowlist" in text
    assert "WEIXIN_GROUP_ALLOWED_USERS=g1" in text


def test_save_never_writes_credentials(env_home):
    weixin.save(
        {
            "dm_policy": "open",
            "group_policy": "disabled",
            # These must be ignored — save only handles access policy.
            "account_id": "evil",
            "token": "evil-token",
        }
    )
    text = (env_home / ".env").read_text(encoding="utf-8")
    assert "evil-token" not in text
    assert "WEIXIN_TOKEN" not in text
    assert "WEIXIN_ACCOUNT_ID" not in text


def test_save_rejects_bad_dm_policy(env_home):
    with pytest.raises(weixin.WeixinConfigError):
        weixin.save({"dm_policy": "nonsense"})


def test_save_rejects_bad_group_policy(env_home):
    with pytest.raises(weixin.WeixinConfigError):
        weixin.save({"group_policy": "nonsense"})


def test_save_rejects_non_dict(env_home):
    with pytest.raises(weixin.WeixinConfigError):
        weixin.save("not a dict")


# ── start_login: mocked QR fetch ─────────────────────────────────────────────


def test_start_login_unavailable_returns_error(env_home, monkeypatch):
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: False)
    res = weixin.start_login()
    assert "error" in res
    assert "login_id" not in res


def test_start_login_returns_login_id_and_qr(env_home, monkeypatch):
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    monkeypatch.setattr(weixin, "_AGENT_ILINK_BASE_URL", "https://ilink.example")
    monkeypatch.setattr(
        weixin,
        "_run_coro",
        _fake_run_coro({"qrcode": "HEXTOKEN", "qrcode_img_content": "https://scan.me/abc"}),
    )
    # Force the qrcode lib path to be exercised but tolerate absence.
    res = weixin.start_login()
    assert "error" not in res
    assert res["state"] == "waiting"
    assert res["login_id"]
    # Either a rendered PNG or the raw URL must be present.
    assert res.get("qr_png") or res.get("qr_url") == "https://scan.me/abc"
    # The pending registry has the entry.
    with weixin._PENDING_LOCK:
        assert res["login_id"] in weixin._PENDING


def test_start_login_missing_qrcode_token_errors(env_home, monkeypatch):
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    monkeypatch.setattr(weixin, "_run_coro", _fake_run_coro({"qrcode_img_content": "x"}))
    res = weixin.start_login()
    assert "error" in res


# ── poll_status: state-machine mapping with mocked status ────────────────────


def _seed_pending(login_id="lid1", base_url="https://ilink.example"):
    import time

    with weixin._PENDING_LOCK:
        weixin._PENDING[login_id] = {
            "qrcode_value": "HEXTOKEN",
            "qrcode_url": "https://scan.me/abc",
            "base_url": base_url,
            "state": "waiting",
            "created_at": time.time(),
        }
    return login_id


def test_poll_unknown_login_id(env_home):
    res = weixin.poll_status("nope")
    assert res["state"] == "error"


def test_poll_waiting(env_home, monkeypatch):
    lid = _seed_pending()
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    monkeypatch.setattr(weixin, "_run_coro", _fake_run_coro({"status": "wait"}))
    assert weixin.poll_status(lid)["state"] == "waiting"


def test_poll_scanned(env_home, monkeypatch):
    lid = _seed_pending()
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    monkeypatch.setattr(weixin, "_run_coro", _fake_run_coro({"status": "scaned"}))
    assert weixin.poll_status(lid)["state"] == "scanned"


def test_poll_redirect_updates_base_url(env_home, monkeypatch):
    lid = _seed_pending()
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    monkeypatch.setattr(
        weixin,
        "_run_coro",
        _fake_run_coro({"status": "scaned_but_redirect", "redirect_host": "redir.example"}),
    )
    res = weixin.poll_status(lid)
    assert res["state"] == "scanned"
    with weixin._PENDING_LOCK:
        assert weixin._PENDING[lid]["base_url"] == "https://redir.example"


def test_poll_expired_is_sticky(env_home, monkeypatch):
    lid = _seed_pending()
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    monkeypatch.setattr(weixin, "_run_coro", _fake_run_coro({"status": "expired"}))
    assert weixin.poll_status(lid)["state"] == "expired"
    # Second poll returns the sticky terminal state without hitting the network.
    monkeypatch.setattr(
        weixin, "_run_coro", lambda coro: pytest.fail("must not re-poll after expired")
    )
    assert weixin.poll_status(lid)["state"] == "expired"


def test_poll_confirmed_persists_and_writes_env(env_home, monkeypatch):
    lid = _seed_pending()
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    saved = {}

    def fake_save_account(home, *, account_id, token, base_url, user_id=""):
        saved.update(
            home=home, account_id=account_id, token=token, base_url=base_url, user_id=user_id
        )

    monkeypatch.setattr(weixin, "_agent_save_weixin_account", fake_save_account)
    monkeypatch.setattr(
        weixin,
        "_run_coro",
        _fake_run_coro(
            {
                "status": "confirmed",
                "ilink_bot_id": "bot_777",
                "bot_token": "tok_secret",
                "baseurl": "https://confirmed.example",
                "ilink_user_id": "user_9",
            }
        ),
    )

    res = weixin.poll_status(lid)
    assert res["state"] == "confirmed"
    assert res["account_id"] == "bot_777"
    # Agent account store called with the right credentials.
    assert saved["account_id"] == "bot_777"
    assert saved["token"] == "tok_secret"
    # .env mirrors the credentials.
    text = (env_home / ".env").read_text(encoding="utf-8")
    assert "WEIXIN_ACCOUNT_ID=bot_777" in text
    assert "WEIXIN_TOKEN=tok_secret" in text
    assert "WEIXIN_BASE_URL=https://confirmed.example" in text
    # Sticky terminal state on re-poll.
    monkeypatch.setattr(
        weixin, "_run_coro", lambda coro: pytest.fail("must not re-poll after confirmed")
    )
    assert weixin.poll_status(lid)["state"] == "confirmed"


def test_poll_confirmed_incomplete_payload_errors(env_home, monkeypatch):
    lid = _seed_pending()
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)
    monkeypatch.setattr(
        weixin,
        "_run_coro",
        _fake_run_coro({"status": "confirmed", "ilink_bot_id": "", "bot_token": ""}),
    )
    assert weixin.poll_status(lid)["state"] == "error"


def test_poll_network_error_stays_waiting(env_home, monkeypatch):
    lid = _seed_pending()
    monkeypatch.setattr(weixin, "_agent_qr_available", lambda: True)

    def boom(coro):
        try:
            coro.close()
        except Exception:
            pass
        raise RuntimeError("network down")

    monkeypatch.setattr(weixin, "_run_coro", boom)
    res = weixin.poll_status(lid)
    assert res["state"] == "waiting"
