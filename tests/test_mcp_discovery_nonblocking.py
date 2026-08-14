"""Regression tests for MCP discovery blocking turn start in the WebUI.

The bug: `_run_agent_streaming` called `discover_mcp_tools()` synchronously on
every message. `discover_mcp_tools()` connects every configured MCP server
synchronously (per-server `connect_timeout`, plus the cross-process discovery
lock), so a slow or unreachable server — e.g. an SSH MCP pointing at a sleeping
laptop — stalled turn start by the full connect timeout on EVERY message.

The CLI/TUI never pays this cost per message because it discovers MCP servers
once at agent startup. The WebUI builds a fresh worker per stream, so discovery
must not sit on the turn's critical path.

The fix runs discovery through `_run_mcp_discovery_background`: a coalesced
daemon thread (one per profile home, so messages can't accumulate lock-waiting
daemons) that the stream worker joins for a bounded window BEFORE the agent
builds/snapshots its tools. Reachable servers that connect within the window
land in THIS turn's snapshot (first-turn completeness); slower or unreachable
servers keep the thread running in the background and their tools land on the
next turn via hermes-agent's between-turns prologue refresh. The thread
re-asserts the stream's profile home through the context-local override
(`set_hermes_home_override`) instead of mutating the process env, so a delayed
thread can never cross-contaminate another stream.

The structural checks here are static (same precedent as
`test_issue1968_mcp_profile_discovery.py`); the real production-path behavior
of `_run_mcp_discovery_background` is exercised at runtime by
`test_mcp_discovery_thread_coalescing.py`.
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
    that outlives its stream would write the stale profile into the process
    env and cross-contaminate other streams. hermes-agent's ``get_hermes_home()``
    resolves the context-local override (`set_hermes_home_override`) before the
    env var, and that override is thread-local — it dies with the discovery
    thread.
    """
    call_idx = _call_line_index()
    preceding = LINES[max(0, call_idx - 16):call_idx]
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


def _helper_body() -> list[str]:
    """Lines of `_run_mcp_discovery_background` from its def to its end."""
    start = next(
        i
        for i, line in enumerate(LINES)
        if line.startswith("def _run_mcp_discovery_background(")
    )
    end = start + 1
    while end < len(LINES) and not (
        LINES[end].startswith("def ") or LINES[end].startswith("_STREAMING_CRONJOB")
    ):
        end += 1
    return LINES[start:end]


def test_discovery_thread_is_daemon_and_bounded_join():
    """The helper must spawn a daemon thread and only ever JOIN with a bound.

    The stream worker must never block on the full discovery duration: the
    join has to be `join(timeout=...)` (bounded), not a bare `.join()`, and
    the thread must be `daemon=True` so it never keeps the process alive.
    """
    body = _helper_body()
    assert any("threading.Thread(" in line for line in body), (
        "_run_mcp_discovery_background must construct a threading.Thread."
    )
    thread_block = body
    assert any("daemon=True" in line for line in thread_block), (
        "MCP discovery thread must be daemon=True so it never keeps the "
        "process alive."
    )
    assert any(".start()" in line for line in thread_block), (
        "MCP discovery thread must be started with .start() — a direct call "
        "would block the stream worker."
    )
    assert any(".join(timeout=" in line for line in body), (
        "The stream worker's wait for discovery must be a BOUNDED join "
        "(join(timeout=...)) — a bare .join() would block the turn for the "
        "full connect timeout, recreating the original bug."
    )


def test_discovery_threads_coalesced_per_profile():
    """One live discovery thread per profile home — never one per message."""
    body = _helper_body()
    assert any("_MCP_DISCOVERY_THREADS.get(profile_home)" in line for line in body), (
        "The helper must look up an existing live thread by profile home "
        "before spawning, so rapid messages don't accumulate one "
        "lock-waiting discovery daemon each."
    )
    assert any("is_alive()" in line for line in body), (
        "Only LIVE discovery threads may be reused; finished threads must be "
        "replaced by a fresh spawn."
    )
