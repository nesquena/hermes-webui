"""Agent-free contract tests for profile-scoped MCP status, inventory and /reload-mcp.

``tests/test_mcp_runtime_profile_scope.py`` drives the real Hermes Agent MCP
runtime; CI has no Hermes Agent checkout, so this file models the small agent
surface WebUI relies on (context-local home override, routed-profile predicate,
scoped registry slots, scoped MCP ledger with adoption and connect cooldowns)
and pins the WebUI side of the contract: which profile is bound, which scope is
reset, what the root profile may see, and the fail-closed paths.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import types
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
import yaml

from api import profiles


class FakeAgent:
    """Minimal model of the hermes-agent MCP surface used by WebUI.

    Mirrors the agent's visibility rules: a routed scope sees the connections it
    owns or adopted; the launch profile's view (scope ``None``) is process-wide.
    """

    def __init__(self, *, pin_supported=True, scoped=True):
        self.override: ContextVar[Optional[str]] = ContextVar("fake_home_override", default=None)
        self.secret: ContextVar[Optional[dict]] = ContextVar("fake_secret_scope", default=None)
        self.pinned = None
        self.servers = {}          # ledger key -> connection
        self.owners = {}           # ledger key -> owner scope
        self.adopters = {}         # ledger key -> {adopter scope}
        self.connecting = set()
        self.errors = {}           # ledger key -> error text
        self.retry_after = {}      # ledger key -> deadline (connect cooldown)
        self.failures = {}         # ledger key -> failure count
        self.lazy = {}             # ledger key -> cfg
        self.global_tools = {}     # tool name -> (toolset, schema)
        self.scoped_tools = {}     # scope -> {tool name -> (toolset, schema)}
        self.calls = []
        self.lock = threading.Lock()
        self.modules = self._build_modules(pin_supported, scoped)

    # hermes_constants
    def home(self):
        return self.override.get() or os.environ.get("HERMES_HOME", "")

    @staticmethod
    def key(path):
        return str(Path(path).resolve())

    def routed(self):
        override = self.override.get()
        anchor = self.pinned or os.environ.get("HERMES_HOME", "")
        return override is not None and self.key(override) != self.key(anchor)

    def scope(self):
        return self.key(self.home()) if self.routed() else None

    def _merged(self, scope):
        return {**self.global_tools, **self.scoped_tools.get(scope, {})}

    def _slot(self, scope):
        return self.global_tools if scope is None else self.scoped_tools.setdefault(scope, {})

    @staticmethod
    def ledger_key(name, scope):
        return name if scope is None else (scope, name)

    def visible(self, key, scope):
        """The agent's ``_server_visible_in_scope``: process-wide for scope ``None``."""
        if scope is None:
            return True
        return self.owners.get(key) == scope or scope in self.adopters.get(key, ())

    def connect(self, name, tools, *, scope):
        key = self.ledger_key(name, scope)
        self.servers[key] = SimpleNamespace(name=name, session=object(), _registered_tool_names=list(tools))
        self.owners[key] = scope
        for tool in tools:
            self._slot(scope)[tool] = (f"mcp-{name}", {"description": f"{tool} ({scope})"})
        return self.servers[key]

    def adopt(self, name, *, owner, adopter):
        """*adopter* shares *owner*'s identical connection (agent: ``_server_tool_scopes``)."""
        key = self.ledger_key(name, owner)
        self.adopters.setdefault(key, set()).add(adopter)
        for tool in self.servers[key]._registered_tool_names:
            self._slot(adopter)[tool] = (f"mcp-{name}", {"description": f"{tool} ({adopter})"})

    def park(self, name, *, scope, error="spawn failed"):
        """A permanent spawn failure the agent parks: task retained in ``_servers``
        without a session, error + backoff recorded under its key."""
        key = self.ledger_key(name, scope)
        self.servers[key] = SimpleNamespace(name=name, session=None, _registered_tool_names=[])
        self.owners[key] = scope
        self.fail(name, scope=scope, error=error)
        return self.servers[key]

    def fail(self, name, *, scope, error="spawn failed"):
        """A connect failure: never in ``_servers``; error + backoff recorded under its key."""
        key = self.ledger_key(name, scope)
        self.errors[key] = error
        self.failures[key] = self.failures.get(key, 0) + 1
        self.retry_after[key] = float("inf")

    def _build_modules(self, pin_supported, scoped):
        agent = self
        hc: Any = types.ModuleType("hermes_constants")
        hc.set_hermes_home_override = lambda path: agent.override.set(None if path is None else str(path))
        hc.reset_hermes_home_override = lambda token: agent.override.reset(token)
        hc.get_hermes_home_override = lambda: agent.override.get()
        hc.hermes_home_key = lambda path=None: agent.key(path if path is not None else agent.home())
        if pin_supported:
            def pin(path):
                agent.pinned = None if path is None else str(path)
            hc.pin_process_hermes_home = pin

        agent_pkg: Any = types.ModuleType("agent")
        agent_pkg.__path__ = []
        secret: Any = types.ModuleType("agent.secret_scope")
        secret.set_secret_scope = lambda mapping: agent.secret.set(dict(mapping))
        secret.reset_secret_scope = lambda token: agent.secret.reset(token)
        secret.current_secret_scope = lambda: agent.secret.get()
        if scoped:
            secret.serves_routed_profile = agent.routed

        tools_pkg: Any = types.ModuleType("tools")
        tools_pkg.__path__ = []
        registry_mod: Any = types.ModuleType("tools.registry")

        class Registry:
            @staticmethod
            def current_scope_key():
                return agent.key(agent.home())

            def _view(self):
                return agent._merged(agent.scope() if scoped else None)

            def get_all_tool_names(self):
                return sorted(self._view())

            def get_toolset_for_tool(self, name):
                return self._view().get(name, (None, None))[0]

            def get_schema(self, name):
                return self._view().get(name, (None, None))[1]

            def snapshot_registration(self, name, *, scope=None):
                slot = agent.global_tools if scope is None else agent.scoped_tools.get(scope, {})
                return slot.get(name)

        registry_mod.registry = Registry()

        scope_mod: Any = types.ModuleType("tools.mcp_tool_scope")
        scope_mod._key_name = lambda key: key[1] if isinstance(key, tuple) else key
        scope_mod._key_scope = lambda key: key[0] if isinstance(key, tuple) else None

        mcp_mod: Any = types.ModuleType("tools.mcp_tool")
        mcp_mod._servers = agent.servers
        mcp_mod._lock = agent.lock
        mcp_mod._server_scope_keys = agent.owners
        mcp_mod._server_tool_scopes = agent.adopters
        mcp_mod._server_connecting = agent.connecting
        mcp_mod._server_connect_errors = agent.errors
        mcp_mod._server_connect_retry_after = agent.retry_after
        mcp_mod._server_connect_failures = agent.failures
        mcp_mod._lazy_server_configs = agent.lazy

        def get_mcp_status(configured=None):
            agent.calls.append(("status", agent.override.get(), dict(configured or {})))
            scope = agent.scope() if scoped else None
            with agent.lock:
                # As the agent: keyed by bare name, last key wins (process-wide for scope None).
                active = {scope_mod._key_name(k): s for k, s in agent.servers.items() if agent.visible(k, scope)}
                errors = {scope_mod._key_name(k): e for k, e in agent.errors.items() if agent.visible(k, scope)}
            rows = []
            for name, cfg in (configured or {}).items():
                enabled = cfg.get("enabled", True) is not False
                server = active.get(name)
                live = server is not None and server.session is not None
                status = ("connected" if live else "disabled" if not enabled
                          else "failed" if name in errors else "configured")
                row = {"name": name, "connected": live, "disabled": not enabled, "status": status,
                       "tools": len(server._registered_tool_names) if live else 0}
                if status == "failed":
                    row["error"] = errors[name]
                rows.append(row)
            return rows

        def shutdown_mcp_servers(*, scope=None, names=None, timeout=15.0):
            agent.calls.append(("shutdown", scope, None if names is None else set(names)))
            wildcard = scope is None and names is None
            with agent.lock:
                selected = [k for k in agent.servers
                            if wildcard or (agent.owners.get(k) == scope
                                            and (names is None or scope_mod._key_name(k) in names))]
                for key in selected:
                    del agent.servers[key]
                    agent.owners.pop(key, None)
                    agent.adopters.pop(key, None)
                # Cooldown/error state: the wildcard drops everything, a scoped call
                # only what it tore down (the agent's ``selected_status``).
                for key in (list(agent.errors) if wildcard else selected):
                    agent.errors.pop(key, None)
                    agent.retry_after.pop(key, None)
                    agent.failures.pop(key, None)

        def register_mcp_servers(servers):
            """The agent's overlay heal for the current scope: a server serving this
            scope through adoption whose config entry is gone (or omitted from
            ``servers`` and absent from the profile's config) loses only this
            profile's overlay; the owner's connection is untouched."""
            agent.calls.append(("register", agent.override.get(), dict(servers)))
            scope = agent.scope() if scoped else None
            if scope is None:
                return []
            cfg = yaml.safe_load(Path(agent.home(), "config.yaml").read_text()) or {}
            configured = cfg.get("mcp_servers") or {}
            with agent.lock:
                for key, adopters in list(agent.adopters.items()):
                    name = scope_mod._key_name(key)
                    if scope in adopters and name not in servers and name not in configured:
                        adopters.discard(scope)
                        slot = agent.scoped_tools.get(scope, {})
                        for tool in [t for t, (ts, _) in slot.items() if ts == f"mcp-{name}"]:
                            del slot[tool]
            return sorted(agent._merged(scope))

        def discover_mcp_tools():
            home = agent.home()
            agent.calls.append(("discover", agent.override.get()))
            cfg = yaml.safe_load(Path(home, "config.yaml").read_text()) or {}
            scope = agent.scope() if scoped else None
            names = []
            if not cfg.get("mcp_servers"):
                return names  # as the agent: nothing configured, no reconcile pass
            for name, srv in (cfg.get("mcp_servers") or {}).items():
                key = agent.ledger_key(name, scope)
                served = any(scope_mod._key_name(k) == name and agent.visible(k, scope) and scope is not None
                             for k in agent.servers) or key in agent.servers
                if served:
                    names += [t for k, s in agent.servers.items()
                              if scope_mod._key_name(k) == name for t in s._registered_tool_names]
                    continue
                if key in agent.retry_after:      # backoff honoured: no retry
                    continue
                if srv.get("fake_fail"):
                    agent.fail(name, scope=scope, error=str(srv["fake_fail"]))
                    continue
                agent.connect(name, srv.get("fake_tools", []), scope=scope)
                names += srv.get("fake_tools", [])
            return names

        mcp_mod.get_mcp_status = get_mcp_status
        mcp_mod.shutdown_mcp_servers = shutdown_mcp_servers
        mcp_mod.discover_mcp_tools = discover_mcp_tools
        mcp_mod.register_mcp_servers = register_mcp_servers
        return {
            "hermes_constants": hc,
            "agent": agent_pkg,
            "agent.secret_scope": secret,
            "tools": tools_pkg,
            "tools.registry": registry_mod,
            "tools.mcp_tool_scope": scope_mod,
            "tools.mcp_tool": mcp_mod,
            "tools.mcp_tool_discovery": mcp_mod,
            "tools.mcp_tool_lifecycle": mcp_mod,
        }


READ = ["mcp__atlassian__jira_get_issue"]
WRITE = READ + ["mcp__atlassian__jira_create_issue"]


def _install(monkeypatch, tmp_path, **agent_kwargs):
    agent = FakeAgent(**agent_kwargs)
    for name, module in agent.modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    base = tmp_path / "home"
    for profile, tools in (("default", None), ("profile-read", READ), ("profile-write", WRITE)):
        home = base if profile == "default" else base / "profiles" / profile
        home.mkdir(parents=True, exist_ok=True)
        servers = {} if tools is None else {"atlassian": {"command": "fake", "fake_tools": tools}}
        (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}))
    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("", "default"))
    monkeypatch.setattr(profiles, "_secret_scope_available", None)
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", None)
    profiles._pin_process_profile_home(base)
    agent.base = base
    return agent


def _configure(agent, profile, servers):
    home = agent.base if profile == "default" else agent.base / "profiles" / profile
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}))


def _as_profile(profile, fn, *args):
    profiles.set_request_profile(profile)
    try:
        return fn(*args)
    finally:
        profiles.clear_request_profile()


def _call(profile, handler_fn):
    handler = MagicMock()
    _as_profile(profile, handler_fn, handler)
    return json.loads(handler.wfile.write.call_args[0][0])


def _reload(profile):
    from api.commands import execute_agent_command
    return _as_profile(profile, execute_agent_command, "/reload-mcp")


def _profile_scope(agent, profile):
    return agent.key(agent.base / "profiles" / profile)


def _servers_by_name(payload):
    return {row["name"]: row for row in payload["servers"]}


def test_init_pins_the_process_profile_home_for_the_agent(monkeypatch, tmp_path):
    agent = _install(monkeypatch, tmp_path)
    assert agent.pinned == str(agent.base)
    assert profiles.get_process_profile_home() == agent.base


def test_process_wide_home_change_repins_in_the_same_step(monkeypatch, tmp_path):
    """``_set_hermes_home`` is the single writer of the process home: startup and
    ``switch_profile(process_wide=True)`` both go through it, so the MCP routing
    anchor (WebUI and agent pin) can never drift from ``HERMES_HOME``."""
    agent = _install(monkeypatch, tmp_path)
    new_home = agent.base / "profiles" / "profile-write"
    profiles._set_hermes_home(new_home)
    assert os.environ["HERMES_HOME"] == str(new_home)
    assert profiles.get_process_profile_home() == new_home
    assert agent.pinned == str(new_home)


def test_status_and_inventory_bind_the_request_profile(monkeypatch, tmp_path):
    from api.routes import _handle_mcp_servers_list, _handle_mcp_tools_list

    agent = _install(monkeypatch, tmp_path)
    agent.connect("atlassian", READ, scope=_profile_scope(agent, "profile-read"))
    agent.connect("atlassian", WRITE, scope=_profile_scope(agent, "profile-write"))

    servers = _call("profile-write", _handle_mcp_servers_list)
    tools = _call("profile-write", _handle_mcp_tools_list)
    assert servers["runtime_scope"] == tools["runtime_scope"] == "profile"
    assert servers["servers"][0]["tool_count"] == 2
    assert {t["name"] for t in tools["tools"]} == set(WRITE)
    # get_mcp_status ran under the profile's home and was handed the displayed config.
    status_calls = [c for c in agent.calls if c[0] == "status"]
    assert all(c[1] == str(agent.base / "profiles" / "profile-write") for c in status_calls)
    assert all(set(c[2]) == {"atlassian"} for c in status_calls)
    # Nothing leaked into the request thread after the calls.
    assert agent.override.get() is None and agent.secret.get() is None

    read_tools = _call("profile-read", _handle_mcp_tools_list)
    assert {t["name"] for t in read_tools["tools"]} == set(READ)
    assert _call("default", _handle_mcp_tools_list)["total"] == 0
    assert not [c for c in agent.calls if c[0] in ("discover", "shutdown")]


def test_inventory_excludes_the_process_profiles_global_slot(monkeypatch, tmp_path):
    from api.routes import _handle_mcp_tools_list

    agent = _install(monkeypatch, tmp_path)
    agent.connect("atlassian", WRITE, scope=None)   # launch profile's unscoped connection
    agent.connect("atlassian", READ, scope=_profile_scope(agent, "profile-read"))
    names = {t["name"] for t in _call("profile-read", _handle_mcp_tools_list)["tools"]}
    assert names == set(READ)


def test_root_profile_status_hides_routed_profiles_runtime(monkeypatch, tmp_path):
    """The agent's launch-profile view is process-wide; WebUI narrows it to the
    root profile's own (unscoped) connections, so a routed profile's live
    connection or connect error never shows up as the root profile's."""
    from api.routes import _handle_mcp_servers_list, _handle_mcp_tools_list

    agent = _install(monkeypatch, tmp_path)
    _configure(agent, "default", {"atlassian": {"command": "fake", "fake_tools": READ}})
    agent.connect("atlassian", WRITE, scope=_profile_scope(agent, "profile-write"))
    agent.fail("atlassian", scope=_profile_scope(agent, "profile-read"), error="bad token")

    servers = _call("default", _handle_mcp_servers_list)
    row = _servers_by_name(servers)["atlassian"]
    assert servers["runtime_scope"] == "profile"
    assert row["status"] == "configured" and row["active"] is False
    assert not row["tool_count"]
    assert _call("default", _handle_mcp_tools_list)["total"] == 0

    # The root profile's own parked task (failed spawn, no session) must not borrow
    # the routed profile's live same-named connection: the agent's process-wide
    # view indexes by bare name and the last key wins.
    agent.park("atlassian", scope=None, error="exec: not found")
    row = _servers_by_name(_call("default", _handle_mcp_servers_list))["atlassian"]
    assert row["status"] == "configured" and row["active"] is False and not row["tool_count"]

    # The root profile's own connection is still reported normally.
    agent.connect("atlassian", READ, scope=None)
    row = _servers_by_name(_call("default", _handle_mcp_servers_list))["atlassian"]
    assert row["status"] == "active" and row["tool_count"] == len(READ)
    assert {t["name"] for t in _call("default", _handle_mcp_tools_list)["tools"]} == set(READ)


def test_adopted_connection_serves_status_inventory_and_reload(monkeypatch, tmp_path):
    """A profile that adopted another profile's identical connection sees it as
    active; its reload counts it and never tears down the owner's connection."""
    from api.routes import _handle_mcp_servers_list, _handle_mcp_tools_list

    agent = _install(monkeypatch, tmp_path)
    read_scope = _profile_scope(agent, "profile-read")
    write_scope = _profile_scope(agent, "profile-write")
    owner_conn = agent.connect("atlassian", READ, scope=read_scope)
    agent.adopt("atlassian", owner=read_scope, adopter=write_scope)

    row = _servers_by_name(_call("profile-write", _handle_mcp_servers_list))["atlassian"]
    assert row["status"] == "active" and row["tool_count"] == len(READ)
    assert {t["name"] for t in _call("profile-write", _handle_mcp_tools_list)["tools"]} == set(READ)

    output = _reload("profile-write")
    assert "Reconnected: atlassian" in output
    assert "Added" not in output and "No MCP servers connected" not in output
    assert ("shutdown", write_scope, {"atlassian"}) in agent.calls
    assert agent.servers[(read_scope, "atlassian")] is owner_conn
    assert write_scope in agent.adopters[(read_scope, "atlassian")]


def test_reload_with_an_empty_config_detaches_adopted_tools_but_keeps_the_owner(monkeypatch, tmp_path):
    """Deleting a profile's last server must stop its tools on reload, even when the
    connection was adopted from another profile: the adopter's overlay goes, the
    owner's connection stays (master's wildcard shutdown stopped the owner too)."""
    from api.routes import _handle_mcp_tools_list

    agent = _install(monkeypatch, tmp_path)
    read_scope = _profile_scope(agent, "profile-read")
    write_scope = _profile_scope(agent, "profile-write")
    owner_conn = agent.connect("atlassian", READ, scope=read_scope)
    agent.adopt("atlassian", owner=read_scope, adopter=write_scope)
    assert {t["name"] for t in _call("profile-write", _handle_mcp_tools_list)["tools"]} == set(READ)

    _configure(agent, "profile-write", {})  # the user deleted the profile's only server
    output = _reload("profile-write")

    assert "Removed: atlassian" in output and "Reconnected" not in output
    assert _call("profile-write", _handle_mcp_tools_list)["total"] == 0
    assert not agent.scoped_tools.get(write_scope)
    assert write_scope not in agent.adopters.get((read_scope, "atlassian"), ())
    # The owner is untouched: same connection, same tools.
    assert agent.servers[(read_scope, "atlassian")] is owner_conn
    assert {t["name"] for t in _call("profile-read", _handle_mcp_tools_list)["tools"]} == set(READ)
    # The teardown stayed scoped to this profile (never the process-wide wildcard) and the
    # overlay reconcile ran under this profile's home before rediscovery.
    shutdowns = [c for c in agent.calls if c[0] == "shutdown"]
    assert shutdowns == [("shutdown", write_scope, {"atlassian"})]
    assert ("register", str(agent.base / "profiles" / "profile-write"), {}) in agent.calls


def test_portable_plugin_tools_are_listed_for_their_profile_without_a_config_entry(monkeypatch, tmp_path):
    """The agent merges plugin-provided (portable) MCP servers into the running config;
    they never appear in the profile's raw ``mcp_servers``. Their tools must still be
    listed for the profile whose registry slot holds them, and for no other profile."""
    from api.routes import _handle_mcp_tools_list, _handle_notes_sources_list

    agent = _install(monkeypatch, tmp_path)
    write_scope = _profile_scope(agent, "profile-write")
    agent.connect("notes-plugin", ["mcp__notes-plugin__search_notes"], scope=write_scope)

    inventory = _call("profile-write", _handle_mcp_tools_list)
    assert [(t["name"], t["server"]) for t in inventory["tools"]] == [
        ("mcp__notes-plugin__search_notes", "notes-plugin")
    ]
    assert inventory["source"] == "tool_registry"
    monkeypatch.setenv("HERMES_WEBUI_EXTERNAL_NOTES_SOURCES", "1")
    notes = _call("profile-write", _handle_notes_sources_list)
    assert [s["name"] for s in notes["sources"]] == ["notes-plugin"]
    assert notes["sources"][0]["tool_count"] == 1
    # Isolation still holds: another profile's slot does not list them.
    assert _call("profile-read", _handle_mcp_tools_list)["total"] == 0
    assert _call("default", _handle_mcp_tools_list)["total"] == 0


def test_skewed_routing_hides_runtime_and_refuses_reload(monkeypatch, tmp_path):
    from api.routes import (
        _handle_mcp_servers_list,
        _handle_mcp_tools_list,
        _handle_notes_sources_list,
    )

    agent = _install(monkeypatch, tmp_path, pin_supported=False)
    _configure(agent, "profile-write", {
        "atlassian": {"command": "fake", "fake_tools": WRITE},
        "joplin": {"command": "fake", "fake_tools": []},   # a notes-drawer source
    })
    agent.connect("atlassian", READ, scope=None)
    monkeypatch.setenv("HERMES_HOME", str(agent.base / "profiles" / "profile-write"))  # turn mirror

    servers = _call("profile-write", _handle_mcp_servers_list)
    assert servers["runtime_scope"] == "unavailable"
    assert servers["servers"][0]["status"] == "configured"
    assert servers["servers"][0]["tool_count"] is None
    assert _call("profile-write", _handle_mcp_tools_list)["total"] == 0
    # The notes drawer reads the same inventory and must be told the runtime is withheld.
    monkeypatch.setenv("HERMES_WEBUI_EXTERNAL_NOTES_SOURCES", "1")
    notes = _call("profile-write", _handle_notes_sources_list)
    assert notes["runtime_scope"] == "unavailable"
    assert [s["name"] for s in notes["sources"]] == ["joplin"]
    assert notes["sources"][0]["active"] is False
    with pytest.raises(RuntimeError, match="could not be confirmed"):
        _reload("profile-write")
    assert not [c for c in agent.calls if c[0] in ("discover", "shutdown")]
    assert "atlassian" in agent.servers


def test_reload_resets_only_the_requested_profile(monkeypatch, tmp_path):
    agent = _install(monkeypatch, tmp_path)
    read_scope = _profile_scope(agent, "profile-read")
    write_scope = _profile_scope(agent, "profile-write")
    read_conn = agent.connect("atlassian", READ, scope=read_scope)
    agent.connect("atlassian", READ, scope=write_scope)  # stale write-profile connection

    output = _reload("profile-write")
    assert "Reconnected: atlassian" in output
    assert ("shutdown", write_scope, {"atlassian"}) in agent.calls
    assert ("discover", str(agent.base / "profiles" / "profile-write")) in agent.calls
    assert agent.servers[(read_scope, "atlassian")] is read_conn
    assert agent.servers[(write_scope, "atlassian")]._registered_tool_names == WRITE


def test_reload_retries_the_profiles_failed_servers_only(monkeypatch, tmp_path):
    """A server that failed to spawn is under connect backoff and absent from the
    ledger, so a scoped shutdown leaves its cooldown alone; the reload must still
    retry it (as the process-wide wildcard did) without touching other owners'."""
    agent = _install(monkeypatch, tmp_path)
    write_scope = _profile_scope(agent, "profile-write")
    agent.fail("atlassian", scope=write_scope, error="exec: not found")
    agent.fail("atlassian", scope=None, error="root still broken")      # root's own backoff

    _configure(agent, "profile-write", {"atlassian": {"command": "fake", "fake_tools": WRITE}})
    output = _reload("profile-write")
    assert "Added: atlassian" in output
    assert ("shutdown", write_scope, set()) in agent.calls
    assert agent.servers[(write_scope, "atlassian")]._registered_tool_names == WRITE
    assert (write_scope, "atlassian") not in agent.errors
    assert "atlassian" in agent.retry_after and "atlassian" in agent.errors  # root untouched


def test_root_profile_reload_never_uses_the_wildcard_shutdown(monkeypatch, tmp_path):
    agent = _install(monkeypatch, tmp_path)
    _configure(agent, "default", {"atlassian": {"command": "fake", "fake_tools": READ}})
    agent.connect("atlassian", READ, scope=None)
    write_conn = agent.connect("atlassian", WRITE, scope=_profile_scope(agent, "profile-write"))

    assert "Reconnected: atlassian" in _reload("default")
    shutdowns = [c for c in agent.calls if c[0] == "shutdown"]
    assert shutdowns == [("shutdown", None, {"atlassian"})]
    assert agent.servers[(_profile_scope(agent, "profile-write"), "atlassian")] is write_conn


def test_legacy_agent_keeps_process_wide_reload_but_reads_the_request_profile(monkeypatch, tmp_path):
    from api.routes import _handle_mcp_servers_list

    agent = _install(monkeypatch, tmp_path, pin_supported=False, scoped=False)
    assert _call("profile-write", _handle_mcp_servers_list)["runtime_scope"] == "legacy_process"
    _reload("profile-write")
    assert ("shutdown", None, None) in agent.calls
    assert ("discover", str(agent.base / "profiles" / "profile-write")) in agent.calls


def test_status_read_survives_an_uninspectable_get_mcp_status(monkeypatch, tmp_path):
    """Signature introspection failing must fall back to the bare call, not blank
    every server's status."""
    from api.routes import _handle_mcp_servers_list

    agent = _install(monkeypatch, tmp_path)
    agent.connect("atlassian", WRITE, scope=_profile_scope(agent, "profile-write"))
    real = agent.modules["tools.mcp_tool"].get_mcp_status

    class Uninspectable:
        @property
        def __signature__(self):
            raise ValueError("no signature")

        def __call__(self, *args, **kwargs):
            assert not args and not kwargs, "bare call expected"
            return real(configured={"atlassian": {"command": "fake"}})

    monkeypatch.setattr(agent.modules["tools.mcp_tool"], "get_mcp_status", Uninspectable())
    row = _servers_by_name(_call("profile-write", _handle_mcp_servers_list))["atlassian"]
    assert row["status"] == "active" and row["tool_count"] == len(WRITE)


def test_ledger_key_helpers_prefer_the_agents_and_fall_back_without_them(monkeypatch, tmp_path):
    from api import mcp_runtime

    agent = _install(monkeypatch, tmp_path)
    assert mcp_runtime.mcp_key_name(("scope-a", "atlassian")) == "atlassian"
    assert mcp_runtime.mcp_key_scope(("scope-a", "atlassian")) == "scope-a"
    assert mcp_runtime.mcp_key_name("atlassian") == "atlassian"
    assert mcp_runtime.mcp_key_scope("atlassian") is None
    monkeypatch.delitem(sys.modules, "tools.mcp_tool_scope")
    assert mcp_runtime.mcp_key_name(("scope-a", "atlassian")) == "atlassian"
    assert mcp_runtime.mcp_key_scope("atlassian") is None
    assert mcp_runtime.accepts_keywords(len, "scope") is False
    assert mcp_runtime.accepts_keywords(agent.modules["tools.mcp_tool"].shutdown_mcp_servers, "scope", "names")


def test_readonly_profile_scope_default_is_unchanged_and_root_is_opt_in(monkeypatch, tmp_path):
    agent = _install(monkeypatch, tmp_path)
    with profiles.profile_env_for_active_request_readonly("t") as bound:
        assert bound is False and agent.override.get() is None
    with profiles.profile_env_for_active_request_readonly("t", include_root=True) as bound:
        assert bound is True and agent.override.get() == str(agent.base)
    assert agent.override.get() is None


def test_runtime_scope_restores_context_after_exception(monkeypatch, tmp_path):
    from api.mcp_runtime import mcp_runtime_scope

    agent = _install(monkeypatch, tmp_path)
    with pytest.raises(KeyError):
        _as_profile("profile-write", lambda: _raise_inside(mcp_runtime_scope, agent))
    assert agent.override.get() is None and agent.secret.get() is None


def _raise_inside(scope_factory, agent):
    with scope_factory("t") as view:
        assert view.trusted and agent.override.get() is not None and agent.secret.get() is not None
        raise KeyError("boom")
