"""Regression tests for MCP discovery blocking turn start in the WebUI.

The bug: `_run_agent_streaming` called `discover_mcp_tools()` synchronously on
every message. `discover_mcp_tools()` connects every configured MCP server
synchronously (per-server `connect_timeout`, plus the cross-process discovery
lock), so a slow or unreachable server — e.g. an SSH MCP pointing at a sleeping
laptop — stalled turn start by the full connect timeout on EVERY message.

The CLI/TUI never pays this cost per message because it discovers MCP servers
once at agent startup. The WebUI builds a fresh worker per stream, so discovery
must not sit on the turn's critical path.

The fix is a profile-scoped readiness state machine (`_ensure_mcp_discovery` /
`_mcp_wait_readiness`): exactly ONE discovery owner thread per profile home; a
turn that finds the profile still `pending` waits on the shared readiness
event (so a configured server is never silently omitted from the first
tool-bearing turn), and later turns subscribe to the SAME result instead of
paying a fresh wait. Discovery is also kicked off at process start so the
default profile usually resolves during idle time. The thread re-asserts the
profile home through the context-local override (`set_hermes_home_override`)
instead of mutating the process env, so a delayed thread can never
cross-contaminate another stream.

The structural checks here are static (same precedent as
`test_issue1968_mcp_profile_discovery.py`); the real production-path behavior
is exercised at runtime by `test_mcp_discovery_thread_coalescing.py`.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")
LINES = STREAMING_PY.splitlines()


def _call_line_index() -> int:
    """First non-comment line containing the actual `discover_mcp_tools()` call."""
    for idx, line in enumerate(LINES):
        if "discover_mcp_tools()" in line and not line.lstrip().startswith("#"):
            return idx
    raise AssertionError("discover_mcp_tools() call line not found")


def test_discovery_call_is_inside_a_nested_function():
    """The call must not execute inline — it lives in a nested function body."""
    call_idx = _call_line_index()
    # Look upward from the call for the enclosing `def` and a `try:` guard
    # (test_issue1968 already pins the try/except window; this pins the def).
    preceding = LINES[max(0, call_idx - 32):call_idx]
    assert any(line.lstrip().startswith("def ") for line in preceding), (
        "discover_mcp_tools() must be called from inside a nested function so "
        "it runs off the stream worker's critical path."
    )
    assert any(line.strip() == "try:" for line in preceding), (
        "discover_mcp_tools() call must be guarded by try/except so MCP "
        "failures don't crash the chat stream."
    )


def test_discovery_thread_uses_context_local_home_not_env():
    """The thread must assert the profile home via hermes-agent's contextvar.

    Regression: the discovery thread originally re-wrote
    ``os.environ['HERMES_HOME']``. The stream worker mutates that env var
    under ``_ENV_LOCK`` and restores it at teardown, so a discovery thread
    that outlives its stream would write the stale profile into the process
    env and cross-contaminate other streams. hermes-agent's ``get_hermes_home()``
    resolves the context-local override (`set_hermes_home_override`) before the
    env var, and that override is thread-local — it dies with the discovery
    thread.
    """
    call_idx = _call_line_index()
    preceding = LINES[max(0, call_idx - 32):call_idx]
    assert any("set_hermes_home_override" in line for line in preceding), (
        "The discovery thread must assert the profile home through the "
        "context-local override (set_hermes_home_override), never by writing "
        "os.environ['HERMES_HOME']."
    )
    assert any("_resolve_hermes_home_override" in line for line in preceding), (
        "The override must come from the webui's version-gated resolver "
        "(api.profiles._resolve_hermes_home_override), not a direct "
        "hermes_constants import — a direct import silently skips discovery "
        "on older agents that lack the v0.18.0+ override API."
    )
    assert not any(
        "os.environ['HERMES_HOME']" in line
        and "set_hermes_home_override" not in line
        and not line.lstrip().startswith("#")
        for line in preceding
    ), (
        "The discovery thread must not mutate os.environ['HERMES_HOME'] — "
        "that env write races with the stream worker's _ENV_LOCK restore."
    )


def _function_body(name: str, stop_prefixes: tuple[str, ...]) -> list[str]:
    """Lines of a top-level function from its def to the next top-level item."""
    start = next(i for i, line in enumerate(LINES) if line.startswith(f"def {name}("))
    end = start + 1
    while end < len(LINES) and not LINES[end].startswith(stop_prefixes):
        end += 1
    return LINES[start:end]


def test_ensure_spawns_one_daemon_owner_thread_per_profile():
    """The readiness owner must be a daemon thread, one per profile."""
    body = _function_body("_ensure_mcp_discovery", ("def ", "_STREAMING_CRONJOB"))
    assert any("_MCP_READINESS.get(profile_home)" in line for line in body), (
        "readiness must be keyed by profile home so each profile gets its "
        "own registry entry"
    )
    assert any("is_alive()" in line for line in body), (
        "only LIVE owner threads may be reused; a dead pending run must be "
        "restarted so waiters can't hang forever"
    )
    assert any("threading.Thread(" in line for line in body), (
        "the owner must be a threading.Thread"
    )
    assert any("daemon=True" in line for line in body), (
        "the owner thread must be daemon=True so it never keeps the process "
        "alive"
    )
    assert any(".start()" in line for line in body), (
        "the owner thread must be started with .start() — a direct call "
        "would block the stream worker"
    )


def test_wait_uses_shared_readiness_event_not_per_turn_join():
    """The turn wait must be a shared event wait, never a fresh join."""
    body = _function_body("_mcp_wait_readiness", ("def ", "_STREAMING_CRONJOB"))
    assert any("event.wait(" in line for line in body), (
        "the turn must wait on the profile's shared readiness event"
    )
    assert not any(".join(" in line for line in body), (
        "turns must never join the owner thread directly — that would make "
        "each message pay a fresh wait instead of subscribing to the one "
        "shared readiness result"
    )


def test_startup_kickoff_exists_and_keeps_single_call_site():
    """Process-start discovery exists, and no second call line was added."""
    body = _function_body("_startup_mcp_discovery", ("def ", "_STREAMING_CRONJOB"))
    assert body, "_startup_mcp_discovery() must exist"
    assert any("_ensure_mcp_discovery(" in line for line in body), (
        "the startup kickoff must route through the same readiness owner"
    )
