"""Regression coverage for issue #2453 agent-source boundary docs/warnings."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_agent_source_boundary_rfc_inventories_import_coupling():
    """The #2453 source-boundary work must keep a concrete import inventory.

    The risk in #2453 is not just a Docker mount mode; it is that WebUI behavior
    still relies on Hermes Agent internals. Pinning these rows prevents the docs
    from degrading into a vague security note without the follow-up task list.
    """
    doc = REPO / "docs" / "rfcs" / "agent-source-boundary.md"
    assert doc.exists(), "#2453 needs a durable source/API boundary RFC"
    src = doc.read_text(encoding="utf-8")

    required_terms = [
        "run_agent.AIAgent",
        "hermes_cli.profiles",
        "hermes_cli.goals",
        "hermes_cli.commands",
        "hermes_cli.plugins",
        "hermes_cli.models",
        "hermes_cli.auth",
        "agent.credential_pool",
        "agent.redact.redact_sensitive_text",
        "state.db",
    ]
    for term in required_terms:
        assert term in src, f"agent source-boundary RFC must inventory {term}"

    for api_phrase in (
        "Run lifecycle API",
        "Profile management API",
        "Command/plugin capability discovery API",
        "Provider registry, model catalog, auth status",
        "Session listing/transcript/metadata API",
    ):
        assert api_phrase in src, f"RFC must name replacement contract: {api_phrase}"


def test_docker_startup_warns_when_agent_source_mount_is_writable():
    """Mutable WebUI-side agent source mounts should be visibly discouraged.

    The compose default is read-only, but custom bind mounts can still make the
    WebUI's agent source path writable. The entrypoint should warn instead of
    silently accepting a weakened boundary.
    """
    src = (REPO / "docker_init.bash").read_text(encoding="utf-8")

    assert "agent source mount is writable" in src
    assert "read-only mount" in src
    assert "$_agent_src" in src
    assert "-w \"$_agent_src\"" in src


def test_docker_docs_link_source_boundary_inventory():
    """Docker docs should link the #2453 inventory from the boundary section."""
    src = (REPO / "docs" / "docker.md").read_text(encoding="utf-8")

    assert "agent-source-boundary.md" in src
    assert "source/API boundary inventory" in src
    assert "#2453" in src
