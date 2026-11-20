"""Regression tests for #9143 review: listing providers decides an OAuth plugin
account from the stored credential-pool rows in a read-only named-profile scope,
and labels it OAuth only when the counted row is an OAuth row.

Unlike ``test_issue9143_plugin_oauth_stale_state.py`` these tests do not replace
``get_auth_status()`` with a dictionary. A fake Agent reads and writes real
``auth.json`` files:

* ``hermes_cli.auth.read_credential_pool`` reads the active profile's stored rows
  (with the global-root fallback) and never writes, like the Agent's.
* ``hermes_cli.auth.get_auth_status`` and ``agent.credential_pool.load_pool`` run a
  stand-in for the Agent's full loader: the plugin profile's
  ``seed_pool_credentials`` hook adds rows, rows the hook no longer produces are
  pruned, and the result is saved back to ``auth.json`` (the extension point the
  maintainers reproduced the write with). The status follows
  ``get_plugin_oauth_auth_status()``.
"""

from __future__ import annotations

import json
import sys
import time
import types
from types import SimpleNamespace

import pytest

import api.config as config
import api.profiles as profiles
from api.plugin_providers import invalidate_plugin_model_provider_cache

SEED_SOURCE = "seed:env"


def _ms_from_now(delta_ms: int) -> int:
    return int(time.time() * 1000) + delta_ms


def _oauth_row(**overrides):
    row = {
        "id": "row-oauth",
        "label": "solstice account",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:device_code",
        "access_token": "access-token-1",
        "refresh_token": None,
        "base_url": "https://api.solstice.invalid/v1",
        "expires_at": None,
        "expires_at_ms": _ms_from_now(3_600_000),
        "last_status": None,
    }
    row.update(overrides)
    return row


def _api_key_row(**overrides):
    row = {
        "id": "row-api-key",
        "label": "solstice key",
        "auth_type": "api_key",
        "priority": 0,
        "source": "manual",
        "access_token": "solstice-api-key-1",
        "refresh_token": None,
        "base_url": "https://api.solstice.invalid/v1",
        "expires_at": None,
        "expires_at_ms": None,
        "last_status": None,
    }
    row.update(overrides)
    return row


def _iso_from_now(delta_s: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta_s))


class _FakeAgent:
    """Stand-in for the Agent's pool code, backed by real ``auth.json`` files."""

    def __init__(self, root, named, seeded_rows):
        self.root = root
        self.named = named
        self.home = named
        self.seeded_rows = seeded_rows
        self.loader_calls: list[str] = []

    # -- storage -----------------------------------------------------------
    def write_pool(self, home, rows):
        (home / "auth.json").write_text(
            json.dumps({"credential_pool": {"solstice": rows}}, indent=2), encoding="utf-8"
        )

    def _read_pool(self, home):
        try:
            payload = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        return list((payload.get("credential_pool") or {}).get("solstice") or [])

    # -- hermes_cli.auth.read_credential_pool: read only, root as fallback --
    def read_credential_pool(self, provider_id=None):
        if provider_id != "solstice":
            return []
        rows = self._read_pool(self.home)
        return rows or self._read_pool(self.root)

    # -- the Agent's full loader: seed, prune, save --------------------------
    def load_pool_entries(self, provider_id):
        self.loader_calls.append(provider_id)
        rows = self._read_pool(self.home)
        seeded = list(self.seeded_rows())
        seeded_sources = {row["source"] for row in seeded}
        rows = [r for r in rows if not str(r.get("source", "")).startswith("seed:") or r["source"] in seeded_sources]
        have = {r.get("source") for r in rows}
        for row in seeded:
            if row["source"] not in have:
                rows.append({
                    "id": "row-seeded",
                    "label": row["source"],
                    "auth_type": "api_key",
                    "priority": len(rows),
                    "source": row["source"],
                    "access_token": row["access_token"],
                    "refresh_token": None,
                    "base_url": "",
                    "expires_at": None,
                    "expires_at_ms": None,
                })
        self.write_pool(self.home, rows)
        return rows

    def get_auth_status(self, provider_id):
        if provider_id != "solstice":
            return {}
        rows = [r for r in self.load_pool_entries(provider_id) if (r.get("access_token") or r.get("agent_key") or "").strip()]
        now_ms = int(time.time() * 1000)
        live = [r for r in rows if r.get("expires_at_ms") is None or int(r["expires_at_ms"]) > now_ms]
        refreshable = [r for r in rows if r.get("refresh_token")] if not live else []
        return {
            "configured": True,
            "provider": provider_id,
            "logged_in": bool(live),
            "needs_refresh": bool(refreshable),
            "accounts": len(rows),
            "base_url": "",
            "hint": "",
        }


@pytest.fixture
def named_profile(monkeypatch, tmp_path):
    """Named profile active under a root home, with a plugin that overrides ``seed_pool_credentials``."""
    root = tmp_path / "root"
    named = root / "profiles" / "work"
    named.mkdir(parents=True)
    state = {"refresh": True, "seeded": [{"source": SEED_SOURCE, "access_token": "seeded-token"}], "env_vars": ()}
    agent = _FakeAgent(root, named, lambda: state["seeded"])

    def build_profile():
        profile = SimpleNamespace(
            name="solstice",
            display_name="Solstice",
            env_vars=state["env_vars"],
            auth_type="oauth_external",
            aliases=(),
            seed_pool_credentials=lambda **_kwargs: state["seeded"],
        )
        if state["refresh"]:
            profile.refresh_credential = lambda entry: None
        return profile

    fake_providers = types.ModuleType("providers")
    fake_providers.list_providers = lambda: [build_profile()]
    monkeypatch.setitem(sys.modules, "providers", fake_providers)

    fake_pkg = types.ModuleType("hermes_cli")
    fake_pkg.__path__ = []
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: []
    fake_models.provider_model_ids = lambda pid: []
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = agent.get_auth_status
    fake_auth.read_credential_pool = agent.read_credential_pool
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_pkg)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)

    fake_cp = types.ModuleType("agent.credential_pool")
    fake_cp.load_pool = lambda pid: SimpleNamespace(entries=lambda: [SimpleNamespace(**r) for r in agent.load_pool_entries(pid)])
    fake_agent_pkg = types.ModuleType("agent")
    fake_agent_pkg.__path__ = []
    fake_agent_pkg.credential_pool = fake_cp
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.credential_pool", fake_cp)

    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: named)
    monkeypatch.setenv("HERMES_HOME", str(named))
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", True, raising=False)
    config._CREDENTIAL_POOL_CACHE.clear()
    invalidate_plugin_model_provider_cache()

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    config.cfg["model"] = {}
    config._cfg_mtime = 0.0
    config.invalidate_models_cache()
    state["agent"] = agent
    try:
        yield state
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config.invalidate_models_cache()
        config._CREDENTIAL_POOL_CACHE.clear()
        invalidate_plugin_model_provider_cache()


def _card(state):
    from api.providers import get_providers

    result = get_providers()
    return next((p for p in result["providers"] if p["id"] == "solstice"), None)


def _auth_bytes(agent):
    return {
        "named": (agent.named / "auth.json").read_bytes() if (agent.named / "auth.json").exists() else None,
        "root": (agent.root / "auth.json").read_bytes() if (agent.root / "auth.json").exists() else None,
    }


# --- item 1: a listing never writes the credential store ---------------------


def test_listing_leaves_named_and_root_auth_json_untouched_when_plugin_seeds(named_profile):
    """The plugin's ``seed_pool_credentials`` would add a row if the Agent loader ran."""
    agent = named_profile["agent"]
    agent.write_pool(agent.named, [_oauth_row()])
    agent.write_pool(agent.root, [_oauth_row(id="row-root", access_token="root-token")])
    before = _auth_bytes(agent)

    card = _card(named_profile)

    assert _auth_bytes(agent) == before
    assert agent.loader_calls == []
    assert card is not None
    assert card["has_key"] is True


def test_listing_leaves_auth_json_untouched_when_plugin_would_prune_a_row(named_profile):
    """A row from a seed source the plugin no longer produces would be pruned by the loader."""
    agent = named_profile["agent"]
    named_profile["seeded"] = []
    agent.write_pool(agent.named, [_oauth_row(), _api_key_row(id="row-stale", source=SEED_SOURCE)])
    agent.write_pool(agent.root, [])
    before = _auth_bytes(agent)

    card = _card(named_profile)

    assert _auth_bytes(agent) == before
    assert agent.loader_calls == []
    assert card is not None
    assert card["has_key"] is True


# --- item 1: stored rows decide the account -----------------------------------


@pytest.mark.parametrize(
    ("rows", "has_key"),
    [
        pytest.param([_oauth_row()], True, id="unexpired-access-token"),
        pytest.param([_oauth_row(expires_at_ms=None, expires_at=_iso_from_now(3600))], True, id="unexpired-iso-expiry"),
        pytest.param([_oauth_row(expires_at_ms=None, access_token="", agent_key="agent-key-1")], True, id="agent-key-only"),
        pytest.param(
            [_oauth_row(expires_at_ms=_ms_from_now(-3_600_000), refresh_token="refresh-token-1")],
            True,
            id="expired-with-refresh-token",
        ),
        pytest.param(
            [_oauth_row(expires_at_ms=None, expires_at=_iso_from_now(-3600), refresh_token="refresh-token-1")],
            True,
            id="expired-iso-with-refresh-token",
        ),
        pytest.param([_oauth_row(expires_at_ms=_ms_from_now(-3_600_000))], False, id="expired-no-refresh-token"),
        pytest.param([_oauth_row(access_token="", refresh_token="refresh-token-1")], False, id="no-token"),
        pytest.param(
            [
                _oauth_row(id="a", expires_at_ms=_ms_from_now(-3_600_000), refresh_token="refresh-token-1"),
                _oauth_row(id="b", access_token="", agent_key=""),
            ],
            True,
            id="expired-refreshable-next-to-empty-row",
        ),
    ],
)
def test_read_only_scope_decides_pooled_oauth_account_from_stored_rows(named_profile, rows, has_key):
    agent = named_profile["agent"]
    agent.write_pool(agent.named, rows)
    before = _auth_bytes(agent)

    card = _card(named_profile)

    assert agent.loader_calls == []
    assert _auth_bytes(agent) == before
    assert card is not None
    assert card["has_key"] is has_key
    if has_key:
        assert card["key_source"] == "oauth"
        assert card["is_oauth"] is True
    else:
        assert card["key_source"] == "none"


def test_read_only_scope_expired_refresh_token_needs_a_plugin_refresh_hook(named_profile):
    """Without ``refresh_credential`` the Agent cannot renew the row, so it does not count."""
    named_profile["refresh"] = False
    agent = named_profile["agent"]
    agent.write_pool(
        agent.named, [_oauth_row(expires_at_ms=_ms_from_now(-3_600_000), refresh_token="refresh-token-1")]
    )

    card = _card(named_profile)

    assert agent.loader_calls == []
    assert card is not None
    assert card["has_key"] is False


# --- item 2: only an OAuth row gets the OAuth label ----------------------------


def test_read_only_scope_api_key_row_keeps_the_api_key_card(named_profile):
    """``hermes auth add solstice --type api-key`` stores an ``auth_type: api_key`` row."""
    named_profile["env_vars"] = ("SOLSTICE_API_KEY",)
    agent = named_profile["agent"]
    agent.write_pool(agent.named, [_api_key_row()])
    before = _auth_bytes(agent)

    card = _card(named_profile)

    assert _auth_bytes(agent) == before
    assert agent.loader_calls == []
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "config_yaml"
    assert card["is_oauth"] is False
    assert card["configurable"] is True


def test_api_key_row_keeps_the_api_key_card_outside_the_read_only_scope(named_profile, monkeypatch):
    """The Agent's status says signed in; the counted stored row is an API-key row."""
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", False)
    named_profile["env_vars"] = ("SOLSTICE_API_KEY",)
    named_profile["seeded"] = []
    agent = named_profile["agent"]
    agent.write_pool(agent.named, [_api_key_row()])

    card = _card(named_profile)

    assert agent.loader_calls, "outside the read-only scope the Agent's status still decides"
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "config_yaml"
    assert card["is_oauth"] is False


def test_oauth_row_reports_oauth_outside_the_read_only_scope(named_profile, monkeypatch):
    """Control: outside the read-only scope the Agent's status decides, as before."""
    monkeypatch.setattr(config._thread_ctx, "block_process_env_fallback", False)
    named_profile["seeded"] = []
    agent = named_profile["agent"]
    agent.write_pool(agent.named, [_oauth_row()])

    card = _card(named_profile)

    assert agent.loader_calls, "outside the read-only scope the Agent's status still decides"
    assert card is not None
    assert card["has_key"] is True
    assert card["key_source"] == "oauth"
    assert card["is_oauth"] is True


def test_mixed_oauth_and_api_key_rows_keep_the_api_key_card(named_profile):
    """Counted rows of both kinds: not every counted row is OAuth, so the master card stays."""
    named_profile["env_vars"] = ("SOLSTICE_API_KEY",)
    agent = named_profile["agent"]
    agent.write_pool(agent.named, [_oauth_row(), _api_key_row()])

    card = _card(named_profile)

    assert agent.loader_calls == []
    assert card is not None
    assert card["has_key"] is True
    assert card["is_oauth"] is False
