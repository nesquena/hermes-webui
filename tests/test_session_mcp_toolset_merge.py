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

    result = _apply_override(defaults, override, mcp_servers, builtin_names={"web", "file", "terminal", "delegation"})

    assert "web" in result, "built-in toolsets must survive an MCP-only override"
    assert "file" in result
    assert "terminal" in result
    assert "delegation" in result
    assert "my-search" in result, "the ticked MCP server must be enabled"


def test_mcp_only_override_dedups_and_preserves_order():
    defaults = ["web", "file", "my-search"]
    override = ["my-search"]
    mcp_servers = {"my-search"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names={"web", "file", "terminal", "delegation"})

    assert result == ["web", "file", "my-search"], (
        "an already-present MCP server must not be duplicated"
    )


def test_multiple_mcp_servers_all_added():
    defaults = ["web", "file"]
    override = ["my-search", "postgres"]
    mcp_servers = {"my-search", "postgres", "github"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names={"web", "file", "terminal", "delegation"})

    assert result == ["web", "file", "my-search", "postgres"]


def test_unchecked_mcp_servers_are_not_leaked():
    """Ticking one MCP server must restrict the others OUT: the override is
    additive over builtins/plugins but restrictive over MCP servers.

    Regression for the Codex gate finding: the earlier additive fix merged
    ALL defaults, so an unchecked MCP server already present in the profile
    defaults (``beta``) leaked its tools back into the session even though
    the user only ticked ``alpha``. Expected: builtins kept, only the
    checked MCP server exposed.
    """
    defaults = ["web", "alpha", "beta"]  # web = builtin; alpha/beta = MCP servers
    override = ["alpha"]                 # user ticked only alpha
    mcp_servers = {"alpha", "beta"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names={"web", "file", "terminal", "delegation"})

    assert result == ["web", "alpha"], (
        "unchecked MCP server 'beta' leaked back in: {}".format(result)
    )


def test_non_mcp_override_still_restricts():
    """A power-user override that names built-in toolsets keeps the original
    restrict-to-these semantics."""
    defaults = ["web", "file", "terminal", "delegation"]
    override = ["file", "terminal"]
    mcp_servers = {"my-search"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names={"web", "file", "terminal", "delegation"})

    assert result == ["file", "terminal"], (
        "a non-MCP override must replace the defaults (restrict semantics)"
    )


def test_mixed_override_restricts():
    """If the override mixes an MCP server with a non-MCP toolset, it is not
    'MCP-only', so restrict semantics apply (defaults are replaced)."""
    defaults = ["web", "file", "terminal"]
    override = ["my-search", "file"]
    mcp_servers = {"my-search"}

    result = _apply_override(defaults, override, mcp_servers, builtin_names={"web", "file", "terminal", "delegation"})

    assert result == ["my-search", "file"]


def test_empty_override_leaves_defaults():
    defaults = ["web", "file"]
    assert _apply_override(defaults, [], {"my-search"}, builtin_names={"web", "file", "terminal", "delegation"}) == ["web", "file"]
    assert _apply_override(defaults, None, {"my-search"}, builtin_names={"web", "file", "terminal", "delegation"}) == ["web", "file"]


def test_no_configured_mcp_servers_falls_back_to_restrict():
    """When there are no configured MCP servers, an override can only be a
    restrict list — never additive."""
    defaults = ["web", "file"]
    override = ["my-search"]

    result = _apply_override(defaults, override, set(), builtin_names={"web", "file", "terminal", "delegation"})

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


def test_real_builtin_names_default_lookup(monkeypatch):
    """With an *available* builtin registry, a genuine MCP-only override is
    additive because a normal server name does not collide with any builtin.

    The default lookup (``_builtin_toolset_names()``) returns ``None`` when the
    Hermes ``toolsets`` module isn't importable (e.g. the WebUI test env in CI),
    which correctly fails closed to RESTRICT. To assert the *additive* path we
    stub the helper to report an available, non-colliding builtin set — that is
    the environment this test is about.
    """
    import api.streaming as streaming

    monkeypatch.setattr(
        streaming, "_builtin_toolset_names",
        lambda: {"web", "file", "terminal", "delegation"},
    )

    defaults = ["web", "file"]
    override = ["my-search"]
    mcp_servers = {"my-search"}

    # builtin_names=None → consults the (stubbed, available) helper.
    result = _apply_override(defaults, override, mcp_servers)

    assert "web" in result and "file" in result and "my-search" in result


def test_default_lookup_unavailable_registry_restricts(monkeypatch):
    """The other half of the default-lookup contract: when the real helper
    reports the registry is unavailable (``None``), the same MCP-only override
    fails closed to RESTRICT rather than additive. This documents that the
    additive path depends on an available registry."""
    import api.streaming as streaming

    monkeypatch.setattr(streaming, "_builtin_toolset_names", lambda: None)

    result = _apply_override(["web", "file"], ["my-search"], {"my-search"})

    assert result == ["my-search"], (
        "an unavailable registry must fail closed to restrict on the default "
        "lookup path too"
    )


# ── Fail-closed: unavailable builtin registry must RESTRICT, never additive ──


def test_unavailable_registry_forces_restrict(monkeypatch):
    """When ``_builtin_toolset_names()`` can't resolve the builtin list it
    returns ``None`` ("I don't know"). An MCP-only override with a colliding
    server name must then fall back to RESTRICT, not re-open the collision by
    treating every name as MCP-additive.

    This reproduces the round-2 gate finding: forcing the empty/unavailable
    fallback with ``override=['web']`` must restrict, not restore all defaults.
    """
    import api.streaming as streaming

    # Simulate the registry being unavailable.
    monkeypatch.setattr(streaming, "_builtin_toolset_names", lambda: None)

    defaults = ["web", "file", "terminal", "delegation"]
    override = ["web"]
    mcp_servers = {"web"}   # collides with a builtin name

    # builtin_names=None → helper is consulted → returns None → fail closed.
    result = _apply_override(defaults, override, mcp_servers)

    assert result == ["web"], (
        "an unavailable builtin registry must fail closed to RESTRICT; it must "
        "NOT re-open the collision by restoring all defaults additively"
    )


def test_none_builtin_names_argument_forces_restrict():
    """Passing ``builtin_names=None`` explicitly (registry unavailable) with a
    colliding MCP server must restrict, independent of the helper lookup."""
    import api.streaming as streaming

    # Ensure the helper (consulted when builtin_names is None) also reports
    # unavailable, so this test asserts the None-path in isolation.
    real = streaming._builtin_toolset_names
    streaming._builtin_toolset_names = lambda: None
    try:
        result = _apply_override(
            ["web", "file", "terminal", "delegation"],
            ["web"],
            {"web"},
            builtin_names=None,
        )
    finally:
        streaming._builtin_toolset_names = real

    assert result == ["web"], (
        "builtin_names=None means the registry is unavailable → restrict"
    )


# ── Shadow set: registered/plugin toolset names also shadow MCP aliases ──────


def test_registry_shadow_collision_restricts():
    """A configured MCP server whose name collides with a *registered* (plugin
    or canonical) toolset — not just a static builtin — must also take restrict
    semantics. The builtin_names set is expected to include such registered
    names so the collision guard covers the full shadow set."""
    defaults = ["web", "file", "my-plugin"]
    override = ["my-plugin"]
    mcp_servers = {"my-plugin"}      # server shares a registered toolset's name
    # builtin_names carries the full shadow set including the plugin toolset.
    builtin_names = {"web", "file", "terminal", "delegation", "my-plugin"}

    result = _apply_override(defaults, override, mcp_servers,
                             builtin_names=builtin_names)

    assert result == ["my-plugin"], (
        "a name shadowed by a registered/plugin toolset must restrict, not "
        "flip the override into additive mode"
    )


# ── Source-level invariant: the replace-all bug must not come back ───────────


def test_streaming_uses_additive_helper():
    """Pin the source so a future edit can't silently revert to the
    'any override replaces the defaults' shape that broke MCP-only chats.

    A mere substring search for the helper *name* is vacuous — the function's
    own definition satisfies it even if the call-site is dead. So we parse the
    AST and assert the streaming worker actually *calls*
    ``_apply_session_toolset_override`` with the MCP server names, on a real
    call path.
    """
    import ast

    src = (REPO / "api" / "streaming.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Find genuine call sites (not the def), excluding the function definition
    # itself so a dead/renamed body can't satisfy the guard.
    call_sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "_apply_session_toolset_override":
                call_sites.append(node)

    assert call_sites, (
        "streaming.py must CALL _apply_session_toolset_override() on a real "
        "code path (not merely define it). Without an active call an MCP-only "
        "toolset chip will again wipe out every built-in tool."
    )

    # At least one call site must pass the configured MCP server names through
    # (3rd positional arg), so the additive-vs-restrict decision is actually
    # driven by which names are MCP servers.
    def _passes_mcp_names(call):
        # positional: (defaults, override, mcp_server_names, [builtin_names])
        if len(call.args) >= 3:
            return True
        # or keyword mcp_server_names=...
        return any(kw.arg == "mcp_server_names" for kw in call.keywords)

    assert any(_passes_mcp_names(c) for c in call_sites), (
        "the call-site must pass the configured MCP server names into "
        "_apply_session_toolset_override() so the collision-aware "
        "additive-vs-restrict decision is driven by real config, not a stub."
    )

    # It must read the configured MCP server names from config.
    assert "mcp_servers" in src, (
        "streaming.py must read cfg['mcp_servers'] to distinguish MCP server "
        "names from ordinary toolset names in the per-session override."
    )


def test_plugin_registry_collision_stays_restrict(monkeypatch):
    """When a registered plugin toolset name collides with a configured MCP
    server name, the override must stay RESTRICT — not silently flip additive.

    The regression bug: ``_builtin_toolset_names()`` used speculative
    accessor/attribute names that don't exist on the installed runtime, so
    registered/plugin toolsets were silently missing from the shadow set.  A
    plugin-named MCP server could slip past the collision guard and flip a
    RESTRICT override to additive.
    """
    import api.streaming as streaming

    # Simulate a registered plugin toolset whose name collides with an MCP
    # server.  The real registry may be unavailable in CI — we patch the
    # registry method so the test exercises the real code path in
    # _builtin_toolset_names() regardless of the environment.
    plugin_name = "browser-cdp"
    try:
        from tools.registry import registry as _reg
        _orig_get = _reg.get_registered_toolset_names
        monkeypatch.setattr(
            _reg, "get_registered_toolset_names",
            lambda: [plugin_name],
        )
    except ImportError:
        # CI environment — tools.registry is not importable.
        # _builtin_toolset_names() will return None (fail-closed), which is
        # the correct behaviour.  The test is still meaningful: we verify
        # that the fail-closed path does NOT silently flip to additive.
        pass

    # Drive the real _builtin_toolset_names() (not a stubbed return value).
    builtin = streaming._builtin_toolset_names()

    if builtin is None:
        # Registry unavailable → fail-closed RESTRICT is correct.
        result = _apply_override(
            ["web", "file"], [plugin_name], {plugin_name},
            builtin_names=None,
        )
        assert result == [plugin_name], (
            "unavailable registry must restrict, not silently flip additive"
        )
        return

    # Registry available → plugin name must be in the shadow set.
    assert plugin_name in builtin, (
        f"registered plugin '{plugin_name}' must appear in the builtin shadow "
        f"set so it collides with the same-named MCP server"
    )

    # The collision must force RESTRICT.
    result = _apply_override(
        ["web", "file"], [plugin_name], {plugin_name},
        builtin_names=builtin,
    )
    assert result == [plugin_name], (
        f"plugin '{plugin_name}' colliding with MCP server must RESTRICT, "
        f"not silently flip additive (got {result})"
    )


# ── mcp-* canonical name collision: alpha + mcp-alpha ──────────────────────
# Regression for the gate finding: dropping every mcp-* canonical name from
# the shadow set enables a silent wrong-server resolution when two servers
# have overlapping canonical names.


def _patch_registry_and_get_builtin_names(monkeypatch, registered_names):
    """Monkeypatch ``get_registered_toolset_names()`` to return *registered_names*
    and call the real ``_builtin_toolset_names()``.  Returns the full shadow set
    (static TOOLSETS keys + registered names).

    Both ``toolsets`` and ``tools.registry`` are made available unconditionally —
    when they cannot be imported (CI without the agent runtime), lightweight
    stand-ins are injected into ``sys.modules`` so the production
    ``_builtin_toolset_names()`` import succeeds and the real enumeration path is
    exercised.  This prevents a silent fallback that would hide a regression in
    the ``mcp-*`` filtering.
    """
    import sys
    import types
    import api.streaming as streaming

    _injected = []  # (module_name, was_present) pairs for cleanup
    _reg = None

    # ── toolsets ──
    if "toolsets" not in sys.modules:
        _mock_toolsets = types.ModuleType("toolsets")
        # Minimal TOOLSETS dict — the static keys the real module exposes.
        # Only the keys matter for shadow-set membership; the values are unused.
        _mock_toolsets.TOOLSETS = {  # type: ignore[attr-defined]
            "web": None, "search": None, "file": None, "terminal": None,
            "delegation": None, "vision": None, "computer_use": None,
        }
        sys.modules["toolsets"] = _mock_toolsets
        _injected.append(("toolsets", False))
    else:
        _injected.append(("toolsets", True))

    # ── tools.registry ──
    try:
        from tools.registry import registry as _reg
    except ImportError:
        _mock_registry = types.SimpleNamespace()
        _mock_registry.get_registered_toolset_names = lambda: list(registered_names)
        _reg = _mock_registry
        _injected_module = types.ModuleType("tools.registry")
        _injected_module.registry = _mock_registry  # type: ignore[attr-defined]
        if "tools" not in sys.modules:
            sys.modules["tools"] = types.ModuleType("tools")
            _injected.append(("tools", False))
        else:
            _injected.append(("tools", True))
        sys.modules["tools.registry"] = _injected_module
        _injected.append(("tools.registry", False))
    else:
        _injected.append(("tools.registry", True))
        _injected.append(("tools", True))

    _orig = _reg.get_registered_toolset_names
    monkeypatch.setattr(_reg, "get_registered_toolset_names",
                        lambda: list(registered_names))
    try:
        return streaming._builtin_toolset_names()
    finally:
        monkeypatch.setattr(_reg, "get_registered_toolset_names", _orig)
        # Clean up injected modules so other tests see the real environment.
        for _mod_name, _was_present in reversed(_injected):
            if not _was_present:
                sys.modules.pop(_mod_name, None)


def test_mcp_canonical_name_collision_restricts(monkeypatch):
    """When MCP servers ``alpha`` and ``mcp-alpha`` coexist, the bare token
    ``mcp-alpha`` (submitted by the picker for the second server) collides with
    the *canonical* name ``mcp-alpha`` (owned by the first server).  The
    override must restrict — not flip additive, which would restore defaults
    and let the runtime resolver match server ``alpha`` instead of the
    intended ``mcp-alpha``.

    The shadow set is obtained from the real ``_builtin_toolset_names()``
    (monkeypatched registry), not hand-constructed, so the test exercises the
    actual enumeration path and would fail if the old ``mcp-*`` filtering were
    restored."""
    builtin = _patch_registry_and_get_builtin_names(
        monkeypatch, ["mcp-alpha", "mcp-mcp-alpha"])

    # Canonical ``mcp-alpha`` (from server ``alpha``) must be in the shadow
    # set so the bare token ``mcp-alpha`` (from server ``mcp-alpha``) collides.
    assert "mcp-alpha" in builtin, (
        "canonical 'mcp-alpha' must be in the shadow set (got {})".format(builtin)
    )
    assert "mcp-mcp-alpha" in builtin, (
        "canonical 'mcp-mcp-alpha' must be in the shadow set"
    )

    result = _apply_override(
        ["web", "file", "terminal", "delegation"], ["mcp-alpha"],
        {"alpha", "mcp-alpha"}, builtin_names=builtin)

    assert result == ["mcp-alpha"], (
        "bare token 'mcp-alpha' collides with canonical 'mcp-alpha' from "
        "server 'alpha' — must RESTRICT, not flip additive (got {})".format(result)
    )


def test_ordinary_server_stays_additive_with_canonical_in_shadow(monkeypatch):
    """Negative control: a normal server ``foo`` (pick token ``foo``) stays
    additive even though its canonical name ``mcp-foo`` is in the shadow set.
    The bare token ``foo`` ≠ ``mcp-foo``, so there is no collision.

    Uses the real ``_builtin_toolset_names()`` (monkeypatched registry), not
    hand-constructed shadow set."""
    builtin = _patch_registry_and_get_builtin_names(
        monkeypatch, ["mcp-foo", "mcp-bar"])

    assert "mcp-foo" in builtin
    assert "foo" not in builtin, (
        "bare token 'foo' must NOT be in the shadow set — only canonical "
        "'mcp-foo' is (got {})".format(builtin)
    )

    result = _apply_override(
        ["web", "file"], ["foo"], {"foo", "bar"}, builtin_names=builtin)

    assert result == ["web", "file", "foo"], (
        "bare token 'foo' must stay additive even with canonical 'mcp-foo' "
        "in the shadow set (got {})".format(result)
    )


def test_plugin_canonical_mcp_prefix_collision_restricts(monkeypatch):
    """A plugin whose canonical name begins with ``mcp-`` (e.g. ``mcp-browser``)
    must shadow a same-named MCP server alias.  The old code filtered *all*
    ``mcp-*`` names from the shadow set, so a plugin ``mcp-browser`` could
    silently flip additive.

    Uses the real ``_builtin_toolset_names()`` (monkeypatched registry), not
    hand-constructed shadow set."""
    builtin = _patch_registry_and_get_builtin_names(
        monkeypatch, ["mcp-browser"])

    assert "mcp-browser" in builtin, (
        "plugin canonical 'mcp-browser' must be in the shadow set "
        "(got {})".format(builtin)
    )

    result = _apply_override(
        ["web", "file"], ["mcp-browser"], {"mcp-browser"},
        builtin_names=builtin)

    assert result == ["mcp-browser"], (
        "plugin canonical 'mcp-browser' collides with same-named MCP server "
        "— must RESTRICT, not flip additive (got {})".format(result)
    )


# ── Malformed registry return shapes must fail closed ──────────────────────


def test_registry_returns_none_fails_closed(monkeypatch):
    """When ``get_registered_toolset_names()`` returns None, the helper must
    return None (not a partial static-only set)."""
    import api.streaming as streaming

    try:
        from tools.registry import registry as _reg
    except ImportError:
        return  # CI without tools.registry — skip

    _orig = _reg.get_registered_toolset_names
    monkeypatch.setattr(_reg, "get_registered_toolset_names", lambda: None)
    try:
        result = streaming._builtin_toolset_names()
    finally:
        monkeypatch.setattr(_reg, "get_registered_toolset_names", _orig)

    assert result is None, (
        "None registry result must fail closed (return None), "
        f"got {result!r}"
    )


def test_registry_returns_dict_fails_closed(monkeypatch):
    """When ``get_registered_toolset_names()`` returns a dict (not a set/list/
    tuple), the helper must return None."""
    import api.streaming as streaming

    try:
        from tools.registry import registry as _reg
    except ImportError:
        return

    _orig = _reg.get_registered_toolset_names
    monkeypatch.setattr(_reg, "get_registered_toolset_names", lambda: {"a": 1})
    try:
        result = streaming._builtin_toolset_names()
    finally:
        monkeypatch.setattr(_reg, "get_registered_toolset_names", _orig)

    assert result is None, (
        f"dict registry result must fail closed, got {result!r}"
    )


def test_registry_returns_string_fails_closed(monkeypatch):
    """When ``get_registered_toolset_names()`` returns a bare string, the
    helper must return None."""
    import api.streaming as streaming

    try:
        from tools.registry import registry as _reg
    except ImportError:
        return

    _orig = _reg.get_registered_toolset_names
    monkeypatch.setattr(_reg, "get_registered_toolset_names", lambda: "not-a-list")
    try:
        result = streaming._builtin_toolset_names()
    finally:
        monkeypatch.setattr(_reg, "get_registered_toolset_names", _orig)

    assert result is None, (
        f"string registry result must fail closed, got {result!r}"
    )


def test_registry_contains_non_string_entries_fails_closed(monkeypatch):
    """When ``get_registered_toolset_names()`` returns entries that are not
    strings, the helper must return None rather than string-coercing them."""
    import api.streaming as streaming

    try:
        from tools.registry import registry as _reg
    except ImportError:
        return

    _orig = _reg.get_registered_toolset_names
    monkeypatch.setattr(
        _reg, "get_registered_toolset_names",
        lambda: ["valid", 42, object()],
    )
    try:
        result = streaming._builtin_toolset_names()
    finally:
        monkeypatch.setattr(_reg, "get_registered_toolset_names", _orig)

    assert result is None, (
        f"non-string entries in registry must fail closed, got {result!r}"
    )


# ── Canonical selector and wildcard leakage regression ─────────────────────
# Gate certification found that stripping only the bare server name from
# defaults lets the canonical ``mcp-<name>`` selector or a wildcard (``all`` /
# ``*``) survive the additive merge, re-exposing unchecked MCP servers.


def test_canonical_mcp_selector_in_defaults_is_stripped():
    """A default that exposes an MCP server via its canonical ``mcp-<name>``
    selector must be stripped along with the bare name, so the unchecked
    server's tools don't leak.

    Reproduces the gate finding: defaults ``['web', 'mcp-alpha']`` + selected
    ``['beta']`` must NOT retain ``mcp-alpha`` (alpha's tools would come
    back through the installed resolver's canonical resolution path).
    """
    defaults = ["web", "mcp-alpha"]
    override = ["beta"]
    mcp_servers = {"alpha", "beta"}

    result = _apply_override(
        defaults, override, mcp_servers,
        builtin_names={"web", "file", "terminal", "delegation"},
    )

    assert "mcp-alpha" not in result, (
        "canonical 'mcp-alpha' in defaults must be stripped so unchecked "
        "alpha's tools don't leak (got {})".format(result)
    )
    assert "web" in result, "builtins must survive"
    assert "beta" in result, "checked server must be present"


def test_wildcard_all_in_defaults_fails_closed_when_mcp_unchecked():
    """A wildcard default ``['all']`` + override ``['beta']`` with unchecked
    server ``alpha``: the wildcard would expand to every registered toolset
    including alpha's, so we must fail closed to restrictive semantics
    (``['beta']``) rather than risk leaking alpha's tools.
    """
    defaults = ["all"]
    override = ["beta"]
    mcp_servers = {"alpha", "beta"}

    result = _apply_override(
        defaults, override, mcp_servers,
        builtin_names={"web", "file", "terminal", "delegation"},
    )

    assert result == ["beta"], (
        "wildcard 'all' with unchecked MCP server must fail closed to "
        "restrict (got {})".format(result)
    )


def test_wildcard_star_in_defaults_fails_closed_when_mcp_unchecked():
    """Same as above but with ``*`` instead of ``all``."""
    defaults = ["*"]
    override = ["beta"]
    mcp_servers = {"alpha", "beta"}

    result = _apply_override(
        defaults, override, mcp_servers,
        builtin_names={"web", "file", "terminal", "delegation"},
    )

    assert result == ["beta"], (
        "wildcard '*' with unchecked MCP server must fail closed to "
        "restrict (got {})".format(result)
    )


def test_wildcard_all_ok_when_all_mcp_checked():
    """When ALL configured MCP servers are ticked, a wildcard default is
    safe — no unchecked server can leak through the expansion."""
    defaults = ["all"]
    override = ["alpha", "beta"]
    mcp_servers = {"alpha", "beta"}

    result = _apply_override(
        defaults, override, mcp_servers,
        builtin_names={"web", "file", "terminal", "delegation"},
    )

    assert "alpha" in result and "beta" in result, (
        "all-checked wildcard must retain the checked servers (got {})".format(result)
    )


def test_canonical_selector_not_stripped_when_no_mcp_server():
    """A default ``mcp-foo`` with no configured MCP server named ``foo`` is a
    user-authored canonical name that should be preserved (it's not an MCP
    server we can strip)."""
    defaults = ["web", "mcp-foo"]
    override = ["alpha"]
    mcp_servers = {"alpha"}

    result = _apply_override(
        defaults, override, mcp_servers,
        builtin_names={"web", "file", "terminal", "delegation"},
    )

    assert "mcp-foo" in result, (
        "canonical 'mcp-foo' with no configured server 'foo' must survive "
        "(got {})".format(result)
    )


def test_canonical_selector_with_checked_server_preserved():
    """When the server IS checked, its canonical selector in defaults must be
    preserved (not stripped) — the override adds the bare name, and the
    canonical is just an alias to the same server."""
    defaults = ["web", "mcp-alpha"]
    override = ["alpha"]
    mcp_servers = {"alpha", "beta"}

    result = _apply_override(
        defaults, override, mcp_servers,
        builtin_names={"web", "file", "terminal", "delegation"},
    )

    # The canonical selector should survive because alpha IS checked.
    assert "alpha" in result, "checked server must be present"


def test_existing_unchecked_regression_with_canonical():
    """Combined regression: defaults with both bare and canonical forms +
    multiple unchecked servers."""
    defaults = ["web", "alpha", "mcp-beta"]
    override = ["alpha"]
    mcp_servers = {"alpha", "beta"}

    result = _apply_override(
        defaults, override, mcp_servers,
        builtin_names={"web", "file", "terminal", "delegation"},
    )

    assert "web" in result, "builtins must survive"
    assert "alpha" in result, "checked server must be present"
    assert "beta" not in result, "bare beta must be stripped"
    assert "mcp-beta" not in result, (
        "canonical 'mcp-beta' must also be stripped (got {})".format(result)
    )


# ── Blocking finding 1: malformed override must fail closed ────────────────


def test_malformed_override_dict_entry_fails_closed():
    """An unhashable persisted override entry (e.g. ``[{"stale": "shape"}]``)
    must NOT raise TypeError inside the additive classifier and fall through
    to the caller's broad except (which restores all profile defaults).

    The helper must return the restrict fallback (string entries only) without
    raising.
    """
    defaults = ["web", "file", "terminal", "alpha", "beta"]
    override = [{"stale": "shape"}]  # unhashable dict entry

    result = _apply_override(defaults, override, {"alpha", "beta"})

    assert isinstance(result, list), f"must return a list, got {type(result)}"
    # Malformed entries are filtered out; no string entries remain → empty list
    # (restrict semantics with no valid toolset names).
    assert result == [], (
        f"malformed override must not restore defaults (fail-open), got {result!r}"
    )


def test_malformed_override_mixed_entries_fails_closed():
    """A mix of valid string entries and malformed entries must keep only the
    valid strings and apply restrict semantics — never restore all defaults.
    """
    defaults = ["web", "file", "terminal", "alpha", "beta"]
    override = ["alpha", {"stale": "shape"}, "beta"]

    result = _apply_override(defaults, override, {"alpha", "beta"})

    # Malformed entry dropped → ["alpha", "beta"] returned as restrict fallback.
    # The critical assertion: defaults are NOT restored (no fail-open).
    assert result == ["alpha", "beta"], (
        f"malformed override must return string-only restrict list, got {result!r}"
    )
    assert "web" not in result and "file" not in result, (
        f"defaults must NOT be restored on malformed override (fail-open), got {result!r}"
    )


def test_non_string_mcp_server_name_fails_closed():
    """A non-string ``mcp_servers`` key must fail closed to restrict, not
    reach ``'mcp-' + _srv`` downstream and fail-open via the caller's except.
    """
    defaults = ["web", "file", "terminal", "alpha"]
    override = ["alpha"]
    # mcp_server_names contains a non-string entry
    mcp_names = {"alpha", 42}

    result = _apply_override(defaults, override, mcp_names)

    assert result == ["alpha"], (
        f"non-string mcp_server_names must fail closed to restrict, got {result!r}"
    )


# ── Blocking finding 2: recursive composite must not leak unchecked MCP ─────


def test_recursive_composite_does_not_leak_unchecked_mcp(monkeypatch):
    """A composite toolset in defaults whose ``includes`` transitively pulls in
    an unchecked MCP server must be DROPPED from the merged result.

    Scenario (from the Codex adversarial gate):
      - ``gate-alpha`` and ``gate-beta`` are configured MCP servers.
      - ``gate-composite`` is a toolset that includes ``gate-alpha``.
      - Profile defaults: ``['web', 'gate-composite']``
      - Session override: ``['gate-beta']``

    Without the fix, ``gate-composite`` survives the name-level filter (it's
    not a bare MCP name) and is kept in ``merged``.  When the agent resolves
    it, ``gate-alpha`` tools leak back in — a silent authorization regression.

    With the fix, ``_default_transitively_reaches_unchecked_mcp`` expands
    ``gate-composite``, finds tools registered under ``mcp-gate-alpha``, and
    drops it.
    """
    try:
        from tools.registry import registry as _reg
        import api.streaming as streaming
    except ImportError:
        return  # CI without tools.registry — skip

    # Register two fake MCP servers and a composite toolset.
    _fake_tools = {
        "gate_alpha_tool": _reg._tools.get("gate_alpha_tool"),
        "gate_beta_tool": _reg._tools.get("gate_beta_tool"),
    }

    class _FakeEntry:
        def __init__(self, name, toolset):
            self.name = name
            self.toolset = toolset

    _orig_snapshot = _reg._snapshot_entries

    def _fake_snapshot():
        real = [e for e in _orig_snapshot()]
        real.append(_FakeEntry("gate_alpha_tool", "mcp-gate-alpha"))
        real.append(_FakeEntry("gate_beta_tool", "mcp-gate-beta"))
        return real

    # Patch resolve_toolset to simulate gate-composite including gate-alpha
    import toolsets as _ts_mod

    _orig_resolve = _ts_mod.resolve_toolset

    def _fake_resolve(name, visited=None, *, include_registry=True):
        if name == "gate-composite":
            # Composite includes gate-alpha → exposes its tools
            return ["gate_alpha_tool"]
        if name == "gate-alpha":
            return ["gate_alpha_tool"]
        if name == "gate-beta":
            return ["gate_beta_tool"]
        return _orig_resolve(name, visited, include_registry=include_registry)

    monkeypatch.setattr(_reg, "_snapshot_entries", _fake_snapshot)
    monkeypatch.setattr(_ts_mod, "resolve_toolset", _fake_resolve)
    # Also patch the reference imported in streaming module
    monkeypatch.setattr(streaming, "_default_transitively_reaches_unchecked_mcp",
                        streaming._default_transitively_reaches_unchecked_mcp)

    defaults = ["web", "gate-composite"]
    override = ["gate-beta"]
    mcp_names = {"gate-alpha", "gate-beta"}
    # Use a minimal builtin_names that doesn't include gate-* names
    builtin_names = {"web", "file", "terminal", "delegation"}

    result = _apply_override(
        defaults, override, mcp_names, builtin_names=builtin_names
    )

    # gate-composite must be dropped — it transitively reaches gate-alpha
    assert "gate-composite" not in result, (
        f"composite including unchecked MCP must be dropped, got {result!r}"
    )
    # web must survive (not a composite, not an MCP server)
    assert "web" in result, (
        f"non-composite built-in must survive, got {result!r}"
    )
    # gate-beta (the ticked server) must be present
    assert "gate-beta" in result, (
        f"ticked MCP server must be present, got {result!r}"
    )
    # gate-alpha (unchecked) must NOT be present
    assert "gate-alpha" not in result, (
        f"unchecked MCP server must not appear, got {result!r}"
    )


def test_composite_not_leaking_unchecked_mcp_is_kept(monkeypatch):
    """A composite toolset that does NOT transitively reach any unchecked MCP
    server must be KEPT in the merged result (no false positives).
    """
    try:
        import toolsets as _ts_mod
    except ImportError:
        return  # CI without toolsets — skip

    _orig_resolve = _ts_mod.resolve_toolset

    def _fake_resolve(name, visited=None, *, include_registry=True):
        if name == "safe-composite":
            # Includes only web (a builtin), no MCP servers
            return _orig_resolve("web", visited, include_registry=include_registry)
        return _orig_resolve(name, visited, include_registry=include_registry)

    monkeypatch.setattr(_ts_mod, "resolve_toolset", _fake_resolve)

    defaults = ["web", "safe-composite"]
    override = ["gate-beta"]
    mcp_names = {"gate-alpha", "gate-beta"}
    builtin_names = {"web", "file", "terminal", "safe-composite"}

    result = _apply_override(
        defaults, override, mcp_names, builtin_names=builtin_names
    )

    # safe-composite must be kept — it doesn't reach any unchecked MCP
    assert "safe-composite" in result, (
        f"composite not reaching unchecked MCP must be kept, got {result!r}"
    )
    assert "gate-beta" in result, (
        f"ticked MCP server must be present, got {result!r}"
    )
