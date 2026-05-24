"""Unit tests for the profile messaging-platform configuration helpers.

Covers the three helpers backing the per-profile platform endpoints:

  * `_list_platforms_for_profile(profile_name) -> dict`
  * `_set_platform_for_profile(profile_name, platform_key, values) -> dict`
  * `_clear_platform_for_profile(profile_name, platform_key) -> dict`

The tests stub the hermes-agent dependency by monkey-patching
`api.profiles._get_platforms_module()` so they run cleanly in
environments without `hermes_cli` on sys.path (which is the default on
this dev machine).
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

# Ensure repo root on path so api.* can import in standalone test invocation.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from api import profiles as profiles_mod  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Fake hermes-agent platforms module — emulates `_all_platforms()` and
# `_platform_status()` shape used by the production code.
# ─────────────────────────────────────────────────────────────────────────────

def _builtin_telegram() -> dict:
    return {
        "key": "telegram",
        "label": "Telegram",
        "emoji": "[tg]",
        "token_var": "TELEGRAM_BOT_TOKEN",
        "vars": [
            {"name": "TELEGRAM_BOT_TOKEN", "prompt": "Bot token",
             "password": True, "help": "From @BotFather"},
            {"name": "TELEGRAM_ALLOWED_USERS", "prompt": "Allowed users",
             "password": False, "help": "Comma-separated handles",
             "optional": True, "is_allowlist": True},
        ],
    }


def _builtin_discord() -> dict:
    return {
        "key": "discord",
        "label": "Discord",
        "emoji": "[dc]",
        "token_var": "DISCORD_BOT_TOKEN",
        "vars": [
            {"name": "DISCORD_BOT_TOKEN", "prompt": "Bot token",
             "password": True, "help": "Bot token from the Discord dev portal"},
        ],
    }


class _FakePluginEntry:
    def __init__(self):
        self.required_env = ["IRC_SERVER", "IRC_NICK", "IRC_PASSWORD"]


def _plugin_irc() -> dict:
    return {
        "key": "irc_plugin",
        "label": "IRC",
        "emoji": "[irc]",
        "token_var": "IRC_SERVER",
        "_registry_entry": _FakePluginEntry(),
    }


def _all_platforms_fake() -> list[dict]:
    return [_builtin_telegram(), _builtin_discord(), _plugin_irc()]


def _platform_status_fake(p: dict) -> str:
    # Status from current os.environ-style ".env" read (we honour what the
    # current write helpers would see). The production helper uses upstream
    # `get_env_value` against `HERMES_HOME/.env`. For tests we don't need to
    # exercise it deeply — return a deterministic value the suite asserts on.
    return "not configured"


@pytest.fixture
def fake_platforms_module(monkeypatch):
    """Substitute the platforms-loading hook with our fake."""
    mod = types.SimpleNamespace(
        _all_platforms=_all_platforms_fake,
        _platform_status=_platform_status_fake,
    )
    monkeypatch.setattr(profiles_mod, "_get_platforms_module", lambda: mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
# Profile-home fixture — creates a real temp profile dir so the cron context
# manager can bracket against a real HERMES_HOME path.
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_profile(tmp_path, monkeypatch):
    """Create a tmp base hermes home + a 'demo' profile dir; patch resolvers.

    Returns (profile_name, profile_home_path).
    """
    base = tmp_path / "hermes_home"
    base.mkdir()
    profile_name = "demo"
    profile_home = base / "profiles" / profile_name
    profile_home.mkdir(parents=True)

    # Patch the module's view of the base home and the named-profile resolver.
    monkeypatch.setattr(profiles_mod, "_DEFAULT_HERMES_HOME", base)
    real_resolver = profiles_mod._resolve_named_profile_home

    def _resolve(name: str) -> Path:
        if name == profile_name:
            return profile_home
        return real_resolver(name)

    monkeypatch.setattr(profiles_mod, "_resolve_named_profile_home", _resolve)
    return profile_name, profile_home


def _env_path(profile_home: Path) -> Path:
    return profile_home / ".env"


def _read_env_lines(profile_home: Path) -> list[str]:
    p = _env_path(profile_home)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines()


def _read_env_dict(profile_home: Path) -> dict:
    out = {}
    for line in _read_env_lines(profile_home):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_list_endpoint_shape_builtin_platform(tmp_profile, fake_platforms_module):
    name, _ = tmp_profile
    out = profiles_mod._list_platforms_for_profile(name)
    assert out["ok"] is True
    assert out["profile"] == name
    platforms = out["platforms"]
    tg = next(p for p in platforms if p["key"] == "telegram")
    assert tg["label"] == "Telegram"
    assert tg["emoji"] == "[tg]"
    assert tg["is_plugin"] is False
    assert tg["status"] in ("configured", "partial", "not_configured")
    assert isinstance(tg["vars"], list)
    names = [v["name"] for v in tg["vars"]]
    assert names == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"]
    # required vs optional
    tok = next(v for v in tg["vars"] if v["name"] == "TELEGRAM_BOT_TOKEN")
    al = next(v for v in tg["vars"] if v["name"] == "TELEGRAM_ALLOWED_USERS")
    assert tok["required"] is True
    assert tok["password"] is True
    assert al["required"] is False
    assert al["password"] is False
    assert al.get("is_allowlist") is True


def test_list_endpoint_shape_plugin_platform_fallback(tmp_profile, fake_platforms_module):
    name, _ = tmp_profile
    out = profiles_mod._list_platforms_for_profile(name)
    irc = next(p for p in out["platforms"] if p["key"] == "irc_plugin")
    assert irc["is_plugin"] is True
    assert irc["required_env"] == ["IRC_SERVER", "IRC_NICK", "IRC_PASSWORD"]
    assert irc["set_env"] == []
    assert "vars" not in irc


def test_list_status_reflects_platform_status_helper(tmp_profile, fake_platforms_module, monkeypatch):
    name, home = tmp_profile

    def _status(p):
        return {"telegram": "configured", "discord": "partially configured",
                "irc_plugin": "not configured"}.get(p["key"], "not configured")

    monkeypatch.setattr(fake_platforms_module, "_platform_status", _status)
    out = profiles_mod._list_platforms_for_profile(name)
    statuses = {p["key"]: p["status"] for p in out["platforms"]}
    assert statuses["telegram"] == "configured"
    assert statuses["discord"] == "partial"
    assert statuses["irc_plugin"] == "not_configured"


def test_list_password_fields_never_return_value(tmp_profile, fake_platforms_module):
    name, home = tmp_profile
    _env_path(home).write_text(
        "TELEGRAM_BOT_TOKEN=secret-123\nTELEGRAM_ALLOWED_USERS=@alice,@bob\n",
        encoding="utf-8",
    )
    out = profiles_mod._list_platforms_for_profile(name)
    tg = next(p for p in out["platforms"] if p["key"] == "telegram")
    tok = next(v for v in tg["vars"] if v["name"] == "TELEGRAM_BOT_TOKEN")
    assert tok["is_set"] is True
    assert "value" not in tok, "password fields MUST NOT round-trip the value"


def test_list_nonpassword_fields_return_value(tmp_profile, fake_platforms_module):
    name, home = tmp_profile
    _env_path(home).write_text(
        "TELEGRAM_ALLOWED_USERS=@alice,@bob\n",
        encoding="utf-8",
    )
    out = profiles_mod._list_platforms_for_profile(name)
    tg = next(p for p in out["platforms"] if p["key"] == "telegram")
    al = next(v for v in tg["vars"] if v["name"] == "TELEGRAM_ALLOWED_USERS")
    assert al["is_set"] is True
    assert al["value"] == "@alice,@bob"


def test_post_validates_platform_key_membership(tmp_profile, fake_platforms_module):
    name, _ = tmp_profile
    with pytest.raises(ValueError):
        profiles_mod._set_platform_for_profile(name, "nope_not_a_platform", {"X": "y"})


def test_post_validates_value_keys_against_schema(tmp_profile, fake_platforms_module):
    name, _ = tmp_profile
    # Built-in: TELEGRAM_BOT_TOKEN is declared, FOO_BAR is not.
    with pytest.raises(ValueError) as ei:
        profiles_mod._set_platform_for_profile(
            name, "telegram", {"TELEGRAM_BOT_TOKEN": "t", "FOO_BAR": "x"})
    assert "FOO_BAR" in str(ei.value)


def test_post_preserves_sibling_env_keys(tmp_profile, fake_platforms_module):
    name, home = tmp_profile
    _env_path(home).write_text(
        "# header comment\n"
        "OPENAI_API_KEY=sk-existing\n"
        "DISCORD_BOT_TOKEN=dc-token\n"
        "\n"
        "TELEGRAM_BOT_TOKEN=old-tg\n",
        encoding="utf-8",
    )
    profiles_mod._set_platform_for_profile(
        name, "telegram",
        {"TELEGRAM_BOT_TOKEN": "new-tg",
         "TELEGRAM_ALLOWED_USERS": "@alice"},
    )
    env = _read_env_dict(home)
    assert env["TELEGRAM_BOT_TOKEN"] == "new-tg"
    assert env["TELEGRAM_ALLOWED_USERS"] == "@alice"
    assert env["OPENAI_API_KEY"] == "sk-existing"
    assert env["DISCORD_BOT_TOKEN"] == "dc-token"
    raw = _env_path(home).read_text(encoding="utf-8")
    assert "# header comment" in raw


def test_post_empty_password_value_means_no_change(tmp_profile, fake_platforms_module):
    name, home = tmp_profile
    _env_path(home).write_text(
        "TELEGRAM_BOT_TOKEN=keep-me\nTELEGRAM_ALLOWED_USERS=@alice\n",
        encoding="utf-8",
    )
    profiles_mod._set_platform_for_profile(
        name, "telegram",
        {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_ALLOWED_USERS": "@bob"},
    )
    env = _read_env_dict(home)
    # Empty password field preserves existing value.
    assert env["TELEGRAM_BOT_TOKEN"] == "keep-me"
    assert env["TELEGRAM_ALLOWED_USERS"] == "@bob"


def test_post_empty_nonpassword_value_removes_key(tmp_profile, fake_platforms_module):
    name, home = tmp_profile
    _env_path(home).write_text(
        "TELEGRAM_BOT_TOKEN=tg\nTELEGRAM_ALLOWED_USERS=@alice\n",
        encoding="utf-8",
    )
    profiles_mod._set_platform_for_profile(
        name, "telegram", {"TELEGRAM_ALLOWED_USERS": ""},
    )
    env = _read_env_dict(home)
    assert env["TELEGRAM_BOT_TOKEN"] == "tg"
    assert "TELEGRAM_ALLOWED_USERS" not in env


def test_delete_removes_only_platform_keys(tmp_profile, fake_platforms_module):
    name, home = tmp_profile
    _env_path(home).write_text(
        "TELEGRAM_BOT_TOKEN=tg\n"
        "TELEGRAM_ALLOWED_USERS=@alice\n"
        "DISCORD_BOT_TOKEN=dc\n"
        "OPENAI_API_KEY=sk\n",
        encoding="utf-8",
    )
    out = profiles_mod._clear_platform_for_profile(name, "telegram")
    assert out["ok"] is True
    assert out["status"] == "not_configured"
    env = _read_env_dict(home)
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "TELEGRAM_ALLOWED_USERS" not in env
    assert env["DISCORD_BOT_TOKEN"] == "dc"
    assert env["OPENAI_API_KEY"] == "sk"


def test_hermes_not_installed_returns_ok_false(tmp_profile, monkeypatch):
    name, _ = tmp_profile

    def _raise():
        raise ImportError("hermes_cli not installed")

    monkeypatch.setattr(profiles_mod, "_get_platforms_module", _raise)
    out_list = profiles_mod._list_platforms_for_profile(name)
    assert out_list == {"ok": False, "message": "hermes-agent not available"}
    out_set = profiles_mod._set_platform_for_profile(name, "telegram", {})
    assert out_set == {"ok": False, "message": "hermes-agent not available"}
    out_clr = profiles_mod._clear_platform_for_profile(name, "telegram")
    assert out_clr == {"ok": False, "message": "hermes-agent not available"}


def test_invalid_profile_name_raises_valueerror(fake_platforms_module):
    # Contains uppercase / disallowed chars per _PROFILE_ID_RE.
    with pytest.raises(ValueError):
        profiles_mod._list_platforms_for_profile("Bad/Name")
    with pytest.raises(ValueError):
        profiles_mod._set_platform_for_profile("Bad/Name", "telegram", {})
    with pytest.raises(ValueError):
        profiles_mod._clear_platform_for_profile("Bad/Name", "telegram")
