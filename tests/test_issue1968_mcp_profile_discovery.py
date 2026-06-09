"""Regression test for issue #1968 — non-default profile MCP servers never load.

The bug: `discover_mcp_tools()` was called at the top of `_run_agent_streaming`
before the `HERMES_HOME` env mutation that stamps the per-session profile.
Result: `_load_mcp_config()` always read the default profile's
`~/.hermes/config.yaml`, never the non-default profile's MCP servers.

The fix moves the call past the `_ENV_LOCK` env-mutation block so
`discover_mcp_tools()` runs with the correct `HERMES_HOME` for the session's
profile.

This is a static check (source ordering) rather than a runtime test, because
mocking the entire agent stack to reach the call site would be brittle and
miss the actual lexical ordering that's the load-bearing fix.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
STREAMING_PY = (ROOT / "api" / "streaming.py").read_text(encoding="utf-8")


def _line_of(pattern: str) -> int:
    """Return the 1-indexed line number of the first match for `pattern`."""
    for idx, line in enumerate(STREAMING_PY.splitlines(), start=1):
        if re.search(pattern, line):
            return idx
    raise AssertionError(f"pattern not found in api/streaming.py: {pattern!r}")


def test_discover_mcp_tools_called_after_hermes_home_mutation():
    """The fix for #1968: `discover_mcp_tools()` must execute AFTER the
    `HERMES_HOME = _profile_home` assignment, otherwise non-default profile
    MCP servers are never discovered.
    """
    home_set_line = _line_of(r"os\.environ\['HERMES_HOME'\]\s*=\s*_profile_home")
    discover_call_line = _line_of(r"discover_mcp_tools\(\)\s*$")
    assert discover_call_line > home_set_line, (
        f"discover_mcp_tools() at line {discover_call_line} must be AFTER the "
        f"HERMES_HOME mutation at line {home_set_line} (issue #1968). "
        "Otherwise non-default profile MCP servers never load."
    )


def test_discover_mcp_tools_called_after_env_lock_release():
    """`discover_mcp_tools()` should run AFTER the `_ENV_LOCK` block releases —
    discovery itself can take up to 120s (per `_run_on_mcp_loop` timeout in
    hermes-agent), and holding the env lock across that would serialize all
    concurrent sessions through MCP discovery.

    Lexical check: the discover call must come after the `# Lock released` marker
    that follows the `with _ENV_LOCK:` block.
    """
    lock_release_marker = _line_of(r"# Lock released — agent runs without holding it")
    discover_call_line = _line_of(r"discover_mcp_tools\(\)\s*$")
    assert discover_call_line > lock_release_marker, (
        f"discover_mcp_tools() at line {discover_call_line} should run AFTER "
        f"the _ENV_LOCK release at line {lock_release_marker}, not inside the "
        "lock block (which would serialize MCP discovery across sessions)."
    )


def test_discover_mcp_tools_only_called_once_in_streaming():
    """Sanity check: only one *actual call* to `discover_mcp_tools()` in
    `api/streaming.py` — not counting prose mentions inside comments.

    The fix relocates the existing call rather than adding a second one.  If a
    later refactor reintroduces a pre-mutation call site, this test catches it.
    """
    call_lines = [
        line for line in STREAMING_PY.splitlines()
        if "discover_mcp_tools()" in line
        and not line.lstrip().startswith("#")
    ]
    assert len(call_lines) == 1, (
        f"Expected exactly 1 `discover_mcp_tools()` call line in api/streaming.py "
        f"(comments excluded), found {len(call_lines)}: {call_lines!r}.  A "
        "duplicate call site would re-introduce the #1968 bug if placed before "
        "the HERMES_HOME mutation."
    )


def test_discover_mcp_tools_call_is_inside_try_except():
    """MCP discovery is best-effort — failures must not crash the chat stream.
    Verify the call site is wrapped in `try: ... except Exception: pass`.

    Looks at the 6 lines immediately surrounding the call (which is the actual
    structural block, regardless of how chatty the preceding comment is).
    """
    lines = STREAMING_PY.splitlines()
    call_idx = None
    for idx, line in enumerate(lines):
        if "discover_mcp_tools()" in line and not line.lstrip().startswith("#"):
            call_idx = idx
            break
    assert call_idx is not None, "discover_mcp_tools() call line not found"
    # Look at the 4 lines before and 4 lines after the call.
    block_start = max(0, call_idx - 4)
    block_end = min(len(lines), call_idx + 5)
    block = "\n".join(lines[block_start:block_end])
    assert "try:" in block, (
        f"discover_mcp_tools() at line {call_idx + 1} must be inside a try block "
        "so MCP failures don't crash the chat stream.  Surrounding code:\n" + block
    )
    assert "except" in block, (
        f"discover_mcp_tools() at line {call_idx + 1} must have an except clause."
    )
