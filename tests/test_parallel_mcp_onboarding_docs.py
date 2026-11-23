"""Keep the Parallel Search MCP onboarding notes in step with /reload-mcp.

The guide describes two reload paths: the profile-scoped reload used with
current Hermes Agents, and the process-wide fallback kept for older Agents.
These checks pin both the wording and the source contract it describes, so a
change to either side fails here instead of leaving the docs stale.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _repo_text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def _parallel_section() -> str:
    doc = _repo_text("docs/onboarding.md")
    start = doc.index("## Optional web search with Parallel Search MCP")
    end = doc.index("\n## ", start + 1)
    return doc[start:end]


def test_reload_command_keeps_scoped_and_legacy_paths():
    commands = _repo_text("api/commands.py")
    assert 'mcp_runtime_scope("/reload-mcp")' in commands
    assert "shutdown_mcp_servers(scope=view.registry_scope, names=owned_names)" in commands
    assert 'accepts_keywords(shutdown_mcp_servers, "scope", "names")' in commands
    # Older Agents keep the process-wide shutdown.
    assert "shutdown_mcp_servers()" in commands

    runtime = _repo_text("api/mcp_runtime.py")
    assert 'SCOPE_PROFILE = "profile"' in runtime
    assert 'SCOPE_LEGACY = "legacy_process"' in runtime


def test_parallel_docs_describe_scoped_reload_with_legacy_caveat():
    section = " ".join(_parallel_section().split())
    assert "/reload-mcp` reloads MCP servers for the profile selected in WebUI" in section
    assert "other profiles' MCP connections and tools keep running" in section
    assert "Older Agents without profile-scoped MCP fall back to a process-wide reload" in section
    assert "profiles/<name>/" in section
    assert "With the same profile selected" in section
    # The pre-scoping blanket warning must not come back.
    assert "Do not use this reload workflow for named profiles" not in section
    assert "is currently process-wide" not in section
