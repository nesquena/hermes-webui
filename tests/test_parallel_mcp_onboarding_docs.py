"""Keep the Parallel Search MCP onboarding notes in step with /reload-mcp.

The guide describes two reload paths: the profile-scoped reload used with
current Hermes Agents, and the process-wide fallback kept for older Agents.
Scoping is not blanket isolation — profiles with an identical route share one
connection, and the config path WebUI reports is not always the one MCP
discovery reads. These checks pin both the wording and the source contract it
describes, so a change to either side fails here instead of leaving the docs
stale.
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
    # A profile can adopt another profile's identical connection, so the owner's
    # reload reaches the adopters too: the docs must not promise plain isolation.
    assert "_server_tool_scopes" in runtime


def test_config_path_override_outranks_the_profile_home():
    """Why the walkthrough excludes a divergent HERMES_CONFIG_PATH.

    Diagnostics report WebUI's resolved config path, where the override
    outranks the profile home, while MCP discovery reads the selected profile
    home's own ``config.yaml``.
    """
    assert 'env_override = os.getenv("HERMES_CONFIG_PATH")' in _repo_text("api/config.py")
    assert '"config_path": str(_get_config_path())' in _repo_text("api/onboarding.py")


def test_parallel_docs_describe_scoped_reload_with_legacy_caveat():
    section = " ".join(_parallel_section().split())
    assert "/reload-mcp` reloads MCP servers for the profile selected in WebUI" in section
    assert "Profiles that share none of those connections are unaffected" in section
    assert "Older Agents without profile-scoped MCP fall back to a process-wide reload" in section
    assert "profiles/<name>/" in section
    assert "With the same profile selected" in section
    # The pre-scoping blanket warning must not come back.
    assert "Do not use this reload workflow for named profiles" not in section
    assert "is currently process-wide" not in section
    # Nor the unconditional promise it replaced: a shared connection is torn
    # down with its owner, and the adopting profiles lose its tools until they
    # rediscover it.
    assert "other profiles' MCP connections and tools keep running" not in section


def test_parallel_docs_qualify_shared_connections_and_waiting():
    section = " ".join(_parallel_section().split())
    assert "the Agent can serve both from one live connection" in section
    assert "deregisters its tools from every adopting profile" in section
    assert "can briefly disconnect and reconnect for a profile you did not reload" in section
    assert (
        "Before either reload below, let active tool calls finish on the profile you are "
        "reloading and on every profile that shares one of its servers" in section
    )
    # Both reloads carry the wait: the one that adds the server and the one that
    # removes it. A shared profile's in-flight tool call breaks on either.
    wait = "on this profile and on every profile that shares one of its servers"
    enable_step, _, disable_step = section.partition("To stop using it")
    assert wait in enable_step
    assert wait in disable_step


def test_parallel_docs_require_the_profile_homes_config_path():
    section = " ".join(_parallel_section().split())
    assert (
        "reported path must be the `config.yaml` inside the selected profile's Hermes home"
        in section
    )
    assert "`HERMES_CONFIG_PATH` overrides the profile home there" in section
    assert "this walkthrough does not apply" in section
    # The disable step carries the same restriction, not just the edit step.
    assert "the restriction above applies to removal as much as to the edit" in section
