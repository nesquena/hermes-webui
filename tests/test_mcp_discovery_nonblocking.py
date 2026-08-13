"""Regression test for MCP discovery blocking turn start in the WebUI.

The bug: `_run_agent_streaming` called `discover_mcp_tools()` synchronously on
every message. `discover_mcp_tools()` connects every configured MCP server
synchronously (per-server `connect_timeout`, plus the cross-process discovery
lock), so a slow or unreachable server — e.g. an SSH MCP pointing at a sleeping
laptop — stalled turn start by the full connect timeout on EVERY message.

The CLI/TUI never pays this cost per message because it discovers MCP servers
once at agent startup. The WebUI builds a fresh worker per stream, so discovery
must not sit on the turn's critical path.

The fix runs discovery on a daemon background thread. hermes-agent's per-turn
prologue (`refresh_agent_mcp_tools`) folds tools from servers that finish
connecting mid-turn into the current turn's first API call, so no tools are
lost — the turn just starts immediately.

This is a static check (same precedent as
`test_issue1968_mcp_profile_discovery.py`): mocking the entire agent stack to
reach the call site would be brittle and would miss the actual structural
shape that's the load-bearing fix — the call must live in a function that is
spawned (not invoked inline) as a daemon `threading.Thread` target.
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
    preceding = LINES[max(0, call_idx - 12):call_idx]
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
    that outlives its stream (the whole point of backgrounding it — an
    unreachable server holds the thread for the full connect timeout) would
    write the stale profile into the process env and cross-contaminate other
    streams. hermes-agent's ``get_hermes_home()`` resolves the context-local
    override (`set_hermes_home_override`) before the env var, and that
    override is thread-local — it dies with the discovery thread.
    """
    call_idx = _call_line_index()
    preceding = LINES[max(0, call_idx - 16):call_idx]
    assert any("set_hermes_home_override" in line for line in preceding), (
        "The discovery thread must assert the profile home through the "
        "context-local override (set_hermes_home_override), never by writing "
        "os.environ['HERMES_HOME']."
    )
    assert not any(
        "os.environ['HERMES_HOME']" in line
        and "set_hermes_home_override" not in line
        for line in preceding
    ), (
        "The discovery thread must not mutate os.environ['HERMES_HOME'] — "
        "that env write races with the stream worker's _ENV_LOCK restore."
    )


def test_discovery_runs_in_a_daemon_background_thread():
    """The nested function must be spawned as a daemon thread, never joined."""
    call_idx = _call_line_index()
    tail = LINES[call_idx + 1:]
    spawn_idx = next(
        (
            call_idx + 1 + i
            for i, line in enumerate(tail)
            if "threading.Thread(" in line
        ),
        None,
    )
    assert spawn_idx is not None, (
        "No threading.Thread(...) found after the discover_mcp_tools() "
        "call — the discovery function must be run on a background thread."
    )
    spawn_block = LINES[spawn_idx:spawn_idx + 10]
    assert any("target=" in line for line in spawn_block), (
        "MCP discovery thread must declare its target (the nested discovery "
        "function)."
    )
    assert any("daemon=True" in line for line in spawn_block), (
        "MCP discovery thread must be daemon=True so it never keeps the "
        "process alive."
    )
    assert any(".start()" in line for line in spawn_block), (
        "MCP discovery thread must be started with .start() — a direct call "
        "or a .join() would block the stream worker."
    )
