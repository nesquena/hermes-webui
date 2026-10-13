"""MCP status, inventory and /reload-mcp follow the request profile.

Two profiles configure a stdio MCP server with the same name (``atlassian``):
one read-only, one with write tools. These tests drive the real Hermes Agent MCP
runtime with a local fake MCP server (no network, no credentials) and check
that each profile sees and reloads only its own connection.

Chat turns are simulated the way ``api/streaming.py`` runs them: the session
profile's home is mirrored into ``os.environ['HERMES_HOME']`` and installed as
the context-local Hermes-home override before ``discover_mcp_tools()``.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

try:
    import mcp  # noqa: F401
    import hermes_constants
    from agent.secret_scope import current_secret_scope, serves_routed_profile  # noqa: F401
    from tools import mcp_tool as mcp_core
    from tools.mcp_tool_discovery import discover_mcp_tools
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
except Exception as exc:  # pragma: no cover - depends on the installed agent
    pytest.skip(
        f"requires hermes-agent with profile-scoped MCP (serves_routed_profile) and the mcp SDK: {exc}",
        allow_module_level=True,
    )

from api import profiles
from api.commands import execute_agent_command
from api.routes import _handle_mcp_servers_list, _handle_mcp_tools_list

requires_process_home_pin = pytest.mark.skipif(
    not hasattr(hermes_constants, "pin_process_hermes_home"),
    reason="streaming turns need hermes_constants.pin_process_hermes_home for profile-scoped MCP keys",
)

SCOPE_PROFILE = "profile"
SCOPE_UNAVAILABLE = "unavailable"
READ_TOOLS = {"mcp__atlassian__jira_get_issue", "mcp__atlassian__confluence_search"}
WRITE_TOOL = "mcp__atlassian__jira_create_issue"

FAKE_SERVER = textwrap.dedent('''
    import os
    try:  # mcp 1.x (CI pins mcp<2)
        from mcp.server.fastmcp import FastMCP
    except ImportError:  # mcp 2.x renamed the high-level server to MCPServer
        from mcp.server import MCPServer as FastMCP

    mcp = FastMCP("fake-atlassian")

    @mcp.tool()
    def jira_get_issue(key: str) -> str:
        """Read a Jira issue (fake)."""
        return key

    @mcp.tool()
    def confluence_search(query: str) -> str:
        """Search Confluence (fake)."""
        return "[]"

    if os.environ.get("READ_ONLY_MODE", "true").lower() == "false":
        @mcp.tool()
        def jira_create_issue(summary: str) -> str:
            """Create a Jira issue (fake, write)."""
            return "created"

    mcp.run()
''')


def _server_cfg(script: Path, *, read_only: bool, account: str) -> dict:
    # Distinct accounts, like distinct Atlassian credentials: identical routes would
    # legitimately share one connection (hermes-agent adopts matching routes).
    return {
        "command": sys.executable,
        "args": [str(script)],
        "env": {"READ_ONLY_MODE": "true" if read_only else "false", "FAKE_ACCOUNT": account},
        "timeout": 30,
        "connect_timeout": 30,
    }


def _write_config(home: Path, servers: dict) -> None:
    import yaml

    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": servers}), encoding="utf-8")


class Env:
    def __init__(self, base: Path, script: Path):
        self.base = base
        self.script = script

    def home(self, profile: str) -> Path:
        return self.base if profile == "default" else self.base / "profiles" / profile

    def configure(self, profile: str, **servers) -> None:
        _write_config(self.home(profile), servers)

    def atlassian(self, profile: str, *, read_only: bool) -> dict:
        return _server_cfg(self.script, read_only=read_only, account=profile)

    def chat_turn(self, profile: str) -> list[str]:
        home = str(self.home(profile))
        old = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = home
        token = hermes_constants.set_hermes_home_override(home)
        try:
            return discover_mcp_tools()
        finally:
            hermes_constants.reset_hermes_home_override(token)
            os.environ["HERMES_HOME"] = old

    def call(self, fn, profile: str) -> dict:
        profiles.set_request_profile(profile)
        try:
            handler = MagicMock()
            fn(handler)
            return json.loads(handler.wfile.write.call_args[0][0])
        finally:
            profiles.clear_request_profile()

    def servers(self, profile: str) -> dict:
        payload = self.call(_handle_mcp_servers_list, profile)
        return {srv["name"]: srv for srv in payload["servers"]} | {"_scope": payload.get("runtime_scope")}

    def tools(self, profile: str) -> dict:
        return self.call(_handle_mcp_tools_list, profile)

    def reload(self, profile: str) -> str:
        profiles.set_request_profile(profile)
        try:
            return execute_agent_command("/reload-mcp")
        finally:
            profiles.clear_request_profile()

    def connection(self, profile: str):
        """The live connection object owned by *profile* for ``atlassian``."""
        key = (hermes_constants.hermes_home_key(self.home(profile)), "atlassian")
        if profile == "default":
            key = "atlassian"
        with mcp_core._lock:
            return mcp_core._servers.get(key)


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    base = tmp_path / "hermes-home"
    base.mkdir()
    script = tmp_path / "fake_atlassian.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(base))
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)  # suite-wide config pin
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name in ("", "default"))
    monkeypatch.setattr(profiles, "_PROCESS_PROFILE_HOME", getattr(profiles, "_PROCESS_PROFILE_HOME", None), raising=False)
    if hasattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME"):
        monkeypatch.setattr(
            hermes_constants, "_PINNED_PROCESS_HERMES_HOME", hermes_constants._PINNED_PROCESS_HERMES_HOME
        )
    # What init_profile_state() does at startup for a WebUI launched on ``base``.
    pin = getattr(profiles, "_pin_process_profile_home", None) or getattr(
        hermes_constants, "pin_process_hermes_home", lambda _home: None
    )
    pin(base)
    shutdown_mcp_servers()

    env = Env(base, script)
    env.configure("default")
    env.configure("profile-read", atlassian=env.atlassian("profile-read", read_only=True))
    env.configure("profile-write", atlassian=env.atlassian("profile-write", read_only=False))
    yield env
    shutdown_mcp_servers()


def _tool_names(payload: dict) -> set[str]:
    return {tool["name"] for tool in payload["tools"]}


@requires_process_home_pin
def test_same_named_servers_stay_isolated_per_profile(mcp_env):
    read_tools = set(mcp_env.chat_turn("profile-read"))
    write_tools = set(mcp_env.chat_turn("profile-write"))
    assert WRITE_TOOL not in read_tools
    assert WRITE_TOOL in write_tools
    assert mcp_env.connection("profile-read") is not mcp_env.connection("profile-write")

    for profile, expect_write in (("profile-read", False), ("profile-write", True)):
        servers = mcp_env.servers(profile)
        inventory = mcp_env.tools(profile)
        names = _tool_names(inventory)
        assert servers["atlassian"]["status"] == "active"
        assert READ_TOOLS <= names
        assert (WRITE_TOOL in names) is expect_write
        # Status counter and inventory describe the same connection.
        assert servers["atlassian"]["tool_count"] == inventory["total"] == len(names)
        assert {tool["server"] for tool in inventory["tools"]} == {"atlassian"}
        assert servers["_scope"] == SCOPE_PROFILE

    # The root profile configures no MCP server and must not see either profile's tools.
    default_servers = mcp_env.servers("default")
    assert set(default_servers) == {"_scope"}
    assert mcp_env.tools("default")["total"] == 0


def test_status_and_inventory_are_passive(mcp_env):
    for profile in ("profile-write", "profile-read", "default"):
        servers = mcp_env.servers(profile)
        inventory = mcp_env.tools(profile)
        if profile != "default":
            assert servers["atlassian"]["status"] == "configured"
            assert not servers["atlassian"]["active"]
            assert inventory["unavailable_servers"] == ["atlassian"]
        assert inventory["total"] == 0
    with mcp_core._lock:
        assert not mcp_core._servers
        assert not mcp_core._server_connecting


@requires_process_home_pin
def test_reload_applies_the_requested_profiles_config_only(mcp_env):
    # profile-write connected while its config was still read-only (stale runtime).
    mcp_env.configure("profile-write", atlassian=mcp_env.atlassian("profile-write", read_only=True))
    mcp_env.chat_turn("profile-read")
    mcp_env.chat_turn("profile-write")
    read_conn = mcp_env.connection("profile-read")
    stale_write_conn = mcp_env.connection("profile-write")

    mcp_env.configure("profile-write", atlassian=mcp_env.atlassian("profile-write", read_only=False))
    stale = mcp_env.servers("profile-write")["atlassian"]
    assert stale["env"]["READ_ONLY_MODE"] == "false"
    assert WRITE_TOOL not in _tool_names(mcp_env.tools("profile-write"))

    output = mcp_env.reload("profile-write")
    assert "Reconnected: atlassian" in output

    assert mcp_env.connection("profile-write") is not stale_write_conn
    assert WRITE_TOOL in _tool_names(mcp_env.tools("profile-write"))
    # The other profile's same-named connection was neither stopped nor replaced.
    assert mcp_env.connection("profile-read") is read_conn
    assert read_conn.session is not None
    read_inventory = mcp_env.tools("profile-read")
    assert WRITE_TOOL not in _tool_names(read_inventory)
    assert mcp_env.servers("profile-read")["atlassian"]["tool_count"] == read_inventory["total"]


@requires_process_home_pin
def test_reloading_an_adopting_profile_keeps_the_owners_connection(mcp_env):
    """Identical routes share one connection; the adopter's reload must not stop it."""
    mcp_env.configure("profile-write", atlassian=mcp_env.atlassian("profile-read", read_only=True))
    mcp_env.chat_turn("profile-read")
    mcp_env.chat_turn("profile-write")
    owner_conn = mcp_env.connection("profile-read")
    assert mcp_env.connection("profile-write") is None  # adopted, not owned
    # The adopted connection serves profile-write: status, inventory and the
    # reload summary all count it (it is not "Added" from the user's point of view).
    assert mcp_env.servers("profile-write")["atlassian"]["status"] == "active"
    assert READ_TOOLS <= _tool_names(mcp_env.tools("profile-write"))

    mcp_env.configure("profile-write", atlassian=mcp_env.atlassian("profile-write", read_only=False))
    output = mcp_env.reload("profile-write")
    assert "Reconnected: atlassian" in output and "Added" not in output
    assert mcp_env.connection("profile-read") is owner_conn
    assert owner_conn.session is not None
    assert WRITE_TOOL in _tool_names(mcp_env.tools("profile-write"))
    assert WRITE_TOOL not in _tool_names(mcp_env.tools("profile-read"))


@requires_process_home_pin
def test_deleting_an_adopting_profiles_last_server_detaches_its_tools_on_reload(mcp_env):
    """A profile that adopted another profile's identical connection deletes its only
    server: its reload must drop its tools (they were callable after reload before)
    while the owner's connection keeps running."""
    mcp_env.configure("profile-write", atlassian=mcp_env.atlassian("profile-read", read_only=True))
    mcp_env.chat_turn("profile-read")
    mcp_env.chat_turn("profile-write")
    owner_conn = mcp_env.connection("profile-read")
    assert mcp_env.connection("profile-write") is None  # adopted, not owned
    assert READ_TOOLS <= _tool_names(mcp_env.tools("profile-write"))

    mcp_env.configure("profile-write")  # no server left
    output = mcp_env.reload("profile-write")

    assert "Removed: atlassian" in output and "Reconnected" not in output
    assert mcp_env.tools("profile-write")["total"] == 0
    from tools.registry import registry
    write_scope = hermes_constants.hermes_home_key(mcp_env.home("profile-write"))
    assert all(registry.snapshot_registration(t, scope=write_scope) is None for t in READ_TOOLS)
    assert mcp_env.connection("profile-read") is owner_conn
    assert owner_conn.session is not None
    assert READ_TOOLS <= _tool_names(mcp_env.tools("profile-read"))


@requires_process_home_pin
def test_root_profile_reload_leaves_named_profiles_running(mcp_env):
    mcp_env.configure("default", atlassian=mcp_env.atlassian("default", read_only=True))
    mcp_env.chat_turn("default")
    mcp_env.chat_turn("profile-write")
    root_conn = mcp_env.connection("default")
    write_conn = mcp_env.connection("profile-write")
    assert root_conn is not None and write_conn is not None

    assert "Reconnected: atlassian" in mcp_env.reload("default")
    assert mcp_env.connection("default") is not root_conn
    assert mcp_env.connection("profile-write") is write_conn
    assert write_conn.session is not None
    assert WRITE_TOOL not in _tool_names(mcp_env.tools("default"))
    assert WRITE_TOOL in _tool_names(mcp_env.tools("profile-write"))


@requires_process_home_pin
def test_root_profile_status_never_shows_a_routed_profiles_connection(mcp_env):
    """The agent's launch-profile view is process-wide; the root profile must only
    report its own (unscoped) connection, never a routed profile's."""
    mcp_env.configure("default", atlassian=mcp_env.atlassian("default", read_only=True))
    mcp_env.chat_turn("profile-write")
    assert mcp_env.connection("profile-write") is not None
    assert mcp_env.connection("default") is None

    root = mcp_env.servers("default")
    assert root["_scope"] == SCOPE_PROFILE
    assert root["atlassian"]["status"] == "configured" and not root["atlassian"]["active"]
    assert not root["atlassian"]["tool_count"]
    assert mcp_env.tools("default")["total"] == 0

    mcp_env.chat_turn("default")
    root = mcp_env.servers("default")
    root_inventory = mcp_env.tools("default")
    assert root["atlassian"]["status"] == "active"
    # Status counter and inventory describe the root profile's own connection
    # (the agent also registers generic resource/prompt tools per server).
    assert root["atlassian"]["tool_count"] == root_inventory["total"]
    assert READ_TOOLS <= _tool_names(root_inventory)
    assert WRITE_TOOL not in _tool_names(root_inventory)


@requires_process_home_pin
def test_reload_retries_a_server_that_failed_to_spawn(mcp_env):
    """A failed spawn sits under the agent's connect backoff (30s+) and never enters
    the ledger, so a scoped shutdown leaves that backoff alone; the profile's reload
    must still retry it, without dropping another profile's backoff."""
    broken = {"command": str(mcp_env.base / "missing-mcp-binary"), "connect_timeout": 5}
    mcp_env.configure("default", atlassian=broken)
    mcp_env.configure("profile-write", atlassian=broken)
    mcp_env.chat_turn("default")
    mcp_env.chat_turn("profile-write")
    # A permanent spawn failure is parked (task retained, no session) or dropped,
    # depending on the agent; either way nothing is connected.
    for profile in ("default", "profile-write"):
        parked = mcp_env.connection(profile)
        assert parked is None or parked.session is None
        assert mcp_env.servers(profile)["atlassian"]["status"] == "configured"
    retry_after = getattr(mcp_core, "_server_connect_retry_after", None)
    if retry_after is None:
        pytest.skip("agent has no connect-retry cooldown ledger")
    write_key = (hermes_constants.hermes_home_key(mcp_env.home("profile-write")), "atlassian")
    with mcp_core._lock:
        assert write_key in retry_after and "atlassian" in retry_after

    mcp_env.configure("profile-write", atlassian=mcp_env.atlassian("profile-write", read_only=False))
    assert "Added: atlassian" in mcp_env.reload("profile-write")
    assert mcp_env.connection("profile-write") is not None
    assert WRITE_TOOL in _tool_names(mcp_env.tools("profile-write"))
    with mcp_core._lock:
        assert write_key not in retry_after
        assert "atlassian" in retry_after  # the root profile's own backoff is untouched
    assert mcp_env.servers("default")["atlassian"]["status"] == "configured"


def test_unconfirmed_scope_hides_runtime_and_refuses_reload(mcp_env, monkeypatch):
    """A live turn's HERMES_HOME mirror on an agent without the pin skews routing."""
    mcp_env.chat_turn("profile-read")
    before = dict(mcp_core._servers)
    if hasattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME"):
        monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    monkeypatch.setenv("HERMES_HOME", str(mcp_env.home("profile-write")))  # mid-turn mirror

    servers = mcp_env.servers("profile-write")
    inventory = mcp_env.tools("profile-write")
    assert servers["atlassian"]["status"] == "configured"
    assert servers["atlassian"]["tool_count"] is None
    assert inventory["total"] == 0

    with pytest.raises(RuntimeError, match="scope could not be confirmed"):
        mcp_env.reload("profile-write")
    assert dict(mcp_core._servers) == before
    assert servers["_scope"] == inventory.get("runtime_scope") == SCOPE_UNAVAILABLE


@requires_process_home_pin
def test_disabled_and_failed_servers_are_reported_per_profile(mcp_env):
    mcp_env.configure(
        "profile-write",
        atlassian=mcp_env.atlassian("profile-write", read_only=False),
        off={**mcp_env.atlassian("profile-write", read_only=False), "enabled": False},
        broken={"command": str(mcp_env.base / "missing-mcp-binary"), "connect_timeout": 5},
        invalid="not-a-mapping",
    )
    mcp_env.chat_turn("profile-write")
    servers = mcp_env.servers("profile-write")
    assert servers["atlassian"]["status"] == "active"
    assert servers["off"]["status"] == "disabled"
    assert servers["broken"]["status"] == "configured"
    assert not servers["broken"]["active"]
    assert servers["invalid"]["status"] == "invalid_config"
    inventory = mcp_env.tools("profile-write")
    assert inventory["unavailable_servers"] == ["broken"]
    assert {tool["server"] for tool in inventory["tools"]} == {"atlassian"}


def test_runtime_scope_is_restored_after_an_exception(mcp_env):
    from api.config import _thread_ctx
    from api.mcp_runtime import mcp_runtime_scope

    previous_env = dict(getattr(_thread_ctx, "env", {}) or {})
    profiles.set_request_profile("profile-write")
    try:
        with pytest.raises(ValueError):
            with mcp_runtime_scope("test") as view:
                assert view.trusted
                assert hermes_constants.get_hermes_home_override() == str(mcp_env.home("profile-write"))
                assert current_secret_scope() is not None
                raise ValueError("boom")
    finally:
        profiles.clear_request_profile()
    assert hermes_constants.get_hermes_home_override() is None
    assert current_secret_scope() is None
    assert dict(getattr(_thread_ctx, "env", {}) or {}) == previous_env
    assert os.environ["HERMES_HOME"] == str(mcp_env.base)


@requires_process_home_pin
def test_concurrent_profile_reads_and_reload_never_cross_profiles(mcp_env):
    mcp_env.chat_turn("profile-read")
    mcp_env.chat_turn("profile-write")
    read_count = mcp_env.servers("profile-read")["atlassian"]["tool_count"]
    write_count = mcp_env.servers("profile-write")["atlassian"]["tool_count"]
    assert read_count != write_count

    barrier = threading.Barrier(3)
    failures: list[str] = []

    def poll(profile: str, expected: int):
        barrier.wait()
        for _ in range(20):
            srv = mcp_env.servers(profile)["atlassian"]
            names = _tool_names(mcp_env.tools(profile))
            allowed = (expected,) if profile == "profile-read" else (expected, 0, None)
            if srv["tool_count"] not in allowed:
                failures.append(f"{profile} saw {srv['tool_count']} tools")
            if profile == "profile-read" and WRITE_TOOL in names:
                failures.append("profile-read inventory leaked a write tool")

    def reload_write():
        barrier.wait()
        mcp_env.reload("profile-write")

    threads = [
        threading.Thread(target=poll, args=("profile-read", read_count)),
        threading.Thread(target=poll, args=("profile-write", write_count)),
        threading.Thread(target=reload_write),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not failures
    assert mcp_env.servers("profile-read")["atlassian"]["tool_count"] == read_count
    assert mcp_env.servers("profile-write")["atlassian"]["tool_count"] == write_count
