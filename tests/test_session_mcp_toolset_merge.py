"""Per-session MCP toolset override must be ADDITIVE, not replace-all.

Regression guard for the "ticking one MCP server in the per-session toolset
picker breaks the whole chat" bug.

The per-session toolset picker (composer chip) lets a user tick configured MCP
servers for the current chat. Those checkboxes emit the bare MCP server name
(e.g. "my-search"). The override was applied with a wholesale
``_toolsets = _override``, so ticking a single MCP server dropped every built-in
toolset (web, file, terminal, delegation, …) and left the model with an empty
tool list — every tool call failed with ``Tool '...' does not exist. Available
tools:`` (empty).

Fix: an override composed *only* of configured MCP servers is merged on top of
the profile defaults; an override that names any non-MCP toolset keeps the
original restrict-to-these semantics (the power-user free-text use case). A name
that is *both* an MCP server and a builtin toolset (collision, e.g. a server
named ``web``) is excluded from the MCP-only test so it can't silently flip the
override into additive mode and get shadowed by the builtin.

These tests exercise the REAL helper used by the streaming worker
(``api.streaming._apply_session_toolset_override``), not a copied reference, so
the merge-vs-restrict decision is covered on the actual code path.
"""

from pathlib import Path

from api.streaming import _apply_session_toolset_override as _apply_override

REPO = Path(__file__).resolve().parents[1]


# ── Behavioural tests for the merge-vs-restrict decision ─────────────────────


def test_mcp_only_override_is_additive():
    """Ticking a configured MCP server keeps the built-in toolsets and adds
    the MCP server on top."""
    defaults = ["web", "file", "terminal", "delegation"]
    override = ["my-search"]
    mcp_servers = {"my-search"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names=set())

    assert "web" in result, "built-in toolsets must survive an MCP-only override"
    assert "file" in result
    assert "terminal" in result
    assert "delegation" in result
    assert "my-search" in result, "the ticked MCP server must be enabled"


def test_mcp_only_override_dedups_and_preserves_order():
    defaults = ["web", "file", "my-search"]
    override = ["my-search"]
    mcp_servers = {"my-search"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names=set())

    assert result == ["web", "file", "my-search"], (
        "an already-present MCP server must not be duplicated"
    )


def test_multiple_mcp_servers_all_added():
    defaults = ["web", "file"]
    override = ["my-search", "postgres"]
    mcp_servers = {"my-search", "postgres", "github"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names=set())

    assert result == ["web", "file", "my-search", "postgres"]


def test_non_mcp_override_still_restricts():
    """A power-user override that names built-in toolsets keeps the original
    restrict-to-these semantics."""
    defaults = ["web", "file", "terminal", "delegation"]
    override = ["file", "terminal"]
    mcp_servers = {"my-search"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names=set())

    assert result == ["file", "terminal"], (
        "a non-MCP override must replace the defaults (restrict semantics)"
    )


def test_mixed_override_restricts():
    """If the override mixes an MCP server with a non-MCP toolset, it is not
    'MCP-only', so restrict semantics apply (defaults are replaced)."""
    defaults = ["web", "file", "terminal"]
    override = ["my-search", "file"]
    mcp_servers = {"my-search"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names=set())

    assert result == ["my-search", "file"]


def test_empty_override_leaves_defaults():
    defaults = ["web", "file"]
    assert _apply_override(defaults, [], {"my-search"}, builtin_names=set()) == ["web", "file"]
    assert _apply_override(defaults, None, {"my-search"}, builtin_names=set()) == ["web", "file"]


def test_no_configured_mcp_servers_falls_back_to_restrict():
    """When there are no configured MCP servers, an override can only be a
    restrict list — never additive."""
    defaults = ["web", "file"]
    override = ["my-search"]

    result = _apply_override(defaults, override, set(), builtin_names=set())

    assert result == ["my-search"]


# ── Collision case: MCP server name shadowed by a builtin toolset ────────────


def test_builtin_collision_is_restrict_not_additive():
    """A configured MCP server whose name collides with a builtin toolset
    (e.g. a server literally named ``web``) must NOT flip the override into
    additive mode. The builtin shadows the MCP alias at resolution time, so
    treating ``["web"]`` as additive would both mis-resolve the override and
    leave the MCP tools unavailable. Restrict semantics apply instead."""
    defaults = ["web", "file", "terminal", "delegation"]
    override = ["web"]
    mcp_servers = {"web"}          # a server that shares a builtin's name
    builtin_names = {"web", "file", "terminal", "delegation"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names=builtin_names)

    assert result == ["web"], (
        "a name that is both an MCP server and a builtin must take restrict "
        "semantics, not additive — otherwise it mis-resolves and the MCP "
        "tools stay unavailable"
    )


def test_mcp_only_additive_when_some_servers_collide():
    """A pure-MCP name (no builtin collision) stays additive even when *other*
    configured servers happen to collide with builtins."""
    defaults = ["web", "file", "terminal"]
    override = ["my-search"]
    mcp_servers = {"my-search", "web"}   # "web" collides, "my-search" does not
    builtin_names = {"web", "file", "terminal"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names=builtin_names)

    assert result == ["web", "file", "terminal", "my-search"], (
        "a non-colliding MCP-only tick must still be additive"
    )


def test_real_builtin_names_default_lookup():
    """With the default (real) builtin lookup, a genuine MCP-only override is
    additive because a normal server name does not collide with any builtin."""
    defaults = ["web", "file"]
    override = ["my-search"]
    mcp_servers = {"my-search"}

    # builtin_names=None → uses api.streaming._builtin_toolset_names()
    result = _apply_override(defaults, override, mcp_servers)

    assert "web" in result and "file" in result and "my-search" in result


# ── Source-level invariant: the replace-all bug must not come back ───────────


def test_streaming_uses_additive_helper():
    """Pin the source so a future edit can't silently revert to the
    'any override replaces the defaults' shape that broke MCP-only chats."""
    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")

    # The streaming worker must route the per-session override through the
    # collision-aware additive helper.
    assert "_apply_session_toolset_override(" in src, (
        "streaming.py must apply the per-session override through "
        "_apply_session_toolset_override() (additive-vs-restrict, "
        "collision-aware). Without it an MCP-only toolset chip will again "
        "wipe out every built-in tool."
    )

    # It must read the configured MCP server names from config.
    assert "mcp_servers" in src, (
        "streaming.py must read cfg['mcp_servers'] to distinguish MCP server "
        "names from ordinary toolset names in the per-session override."
    )
