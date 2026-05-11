"""Regression tests for the HermesOS Cloud fork's provider auto-naming —
guards against UI labels leaking into the agent's runtime auth path.

Background
----------
The fork adds a built-in ``base_url`` → provider-slug table
(`_BUILTIN_BASE_URL_PROVIDERS`) so a user with a generic
``provider: custom + base_url: https://crof.ai/v1`` config sees the
friendly "CrofAI" label in the model dropdown instead of the bare
"Custom" group.

The intent was UI-cosmetic only. But the function that drives this
(`_named_custom_provider_slug_for_base_url`) was also being called from
two paths that DO affect runtime:

1. ``_resolve_configured_provider_id(..., resolve_alias=False)`` —
   feeds the agent's ``provider`` arg via ``resolve_model_provider``.

2. ``resolve_model_provider`` parses ``@<slug>:model_id`` prefixes
   added by the dropdown JS — the prefix carries the auto-detected slug
   even though the underlying config is ``provider: custom``.

Both paths were leaking the UI slug into the agent's auth resolution.
The agent's ``hermes_cli.auth.PROVIDER_REGISTRY`` doesn't know about
``crof``/``venice``/``bankr``/etc. (they're WebUI-fork-only display
names), so it raises ``"Provider 'crof' is set in config.yaml but no
API key was found. Set the CROF_API_KEY environment variable."`` even
when the user's ``.env`` correctly has ``OPENAI_API_KEY`` set.

Two-layer fix:

a. ``_named_custom_provider_slug_for_base_url`` gained an
   ``include_builtin_fallback`` kwarg. The runtime call site at
   ``_resolve_configured_provider_id(resolve_alias=False)`` passes
   ``False``, so the agent's provider arg stays ``"custom"``. UI callers
   continue to default to ``True``.

b. ``resolve_model_provider`` got an ``@``-prefix guard. When the prefix
   matches the built-in slug for the user's configured ``base_url`` AND
   the user's config is ``provider: custom``, the prefix is translated
   back to ``("model", "custom", base_url)``.

These tests are PARAMETERIZED OVER EVERY ENTRY in
``_BUILTIN_BASE_URL_PROVIDERS`` so adding a new aggregator to the table
automatically gets the same protection — and removing this regression
suite is a noisy diff.
"""

from pathlib import Path

import pytest

import api.config as cfg


# ── Source-of-truth: every (hostname_fragment, slug) pair we care about ──
#
# Pulled directly from the module under test so this suite stays in lock-step
# with the actual table — if anyone adds an entry to _BUILTIN_BASE_URL_PROVIDERS
# without thinking about the runtime auth path, every test below applies to
# the new entry too.
BUILTIN_PROVIDERS = tuple(cfg._BUILTIN_BASE_URL_PROVIDERS)


def _base_url_for_slug(slug: str) -> str:
    """Return a representative base_url that matches the slug's fragment."""
    for fragment, s in BUILTIN_PROVIDERS:
        if s == slug:
            return f"https://{fragment}/v1"
    raise AssertionError(f"slug not in _BUILTIN_BASE_URL_PROVIDERS: {slug!r}")


# ── 1. _builtin_provider_slug_for_base_url — the lookup itself ───────────


class TestBuiltinSlugLookup:
    """The slug-from-base-URL helper used by both UI and runtime callers."""

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_direct_match(self, fragment, slug):
        """Each entry resolves to its own slug given a canonical base_url."""
        url = f"https://{fragment}/v1"
        assert cfg._builtin_provider_slug_for_base_url(url) == slug

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_subdomain_match(self, fragment, slug):
        """Substring match — an ``api.<fragment>`` form should still resolve.

        The crof.ai bug surfaced via ``https://crof.ai/v1`` but the table is
        designed to handle the more common ``https://api.<host>/v1`` shape too.
        """
        if fragment.startswith("api."):
            return  # already api-subdomain — direct case covers it
        url = f"https://api.{fragment}/v1"
        assert cfg._builtin_provider_slug_for_base_url(url) == slug

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_uppercase_input(self, fragment, slug):
        """Case-insensitive — YAML may have an uppercase host."""
        url = f"https://{fragment.upper()}/v1"
        assert cfg._builtin_provider_slug_for_base_url(url) == slug

    def test_empty_input(self):
        assert cfg._builtin_provider_slug_for_base_url("") == ""
        assert cfg._builtin_provider_slug_for_base_url(None) == ""

    def test_unknown_host_returns_empty(self):
        """A self-hosted endpoint that isn't in the table must not match."""
        assert cfg._builtin_provider_slug_for_base_url("https://my.private.example.com/v1") == ""
        assert cfg._builtin_provider_slug_for_base_url("http://127.0.0.1:8000/v1") == ""


# ── 2. _named_custom_provider_slug_for_base_url — UI vs runtime gating ──


class TestNamedCustomProviderSlugGating:
    """The fork added an ``include_builtin_fallback`` kwarg so the runtime
    auth path can opt out of the built-in table while UI keeps using it."""

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_ui_path_includes_builtin_fallback(self, fragment, slug):
        """Default ``include_builtin_fallback=True`` returns the friendly slug."""
        url = f"https://{fragment}/v1"
        result = cfg._named_custom_provider_slug_for_base_url(url, {}, include_builtin_fallback=True)
        assert result == slug

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_runtime_path_skips_builtin_fallback(self, fragment, slug):
        """``include_builtin_fallback=False`` must NOT return the built-in slug.

        Without this gate the agent's auth resolution flips from
        ``OPENAI_API_KEY`` (which the user has) to ``<SLUG>_API_KEY`` (which
        they don't) and the request fails with "no API key was found".
        """
        url = f"https://{fragment}/v1"
        result = cfg._named_custom_provider_slug_for_base_url(url, {}, include_builtin_fallback=False)
        assert result == "", (
            f"Runtime path leaked the UI-only slug {slug!r} for base_url {url!r}. "
            f"This breaks every VM whose .env has only OPENAI_API_KEY (the "
            f"canonical generic-OpenAI-compat env var) — the agent would go "
            f"looking for {slug.upper()}_API_KEY and fail."
        )

    def test_user_defined_custom_providers_win_over_builtin(self):
        """A user-declared ``custom_providers`` entry must take precedence over
        the built-in fallback regardless of the ``include_builtin_fallback``
        kwarg — the explicit user choice is always authoritative."""
        config_obj = {
            "custom_providers": [
                {"name": "MyCrofProxy", "base_url": "https://crof.ai/v1"},
            ]
        }
        # UI path (default): user's name wins
        # User-declared entries are slugified as ``custom:<name>`` so they
        # never collide with first-class provider slugs.
        ui = cfg._named_custom_provider_slug_for_base_url(
            "https://crof.ai/v1", config_obj, include_builtin_fallback=True
        )
        assert ui == "custom:mycrofproxy"
        # Runtime path: user's name still wins (runtime opt-out only gates the
        # built-in fallback, not user-declared entries)
        rt = cfg._named_custom_provider_slug_for_base_url(
            "https://crof.ai/v1", config_obj, include_builtin_fallback=False
        )
        assert rt == "custom:mycrofproxy"


# ── 3. _resolve_configured_provider_id — UI vs runtime split ────────────


class TestResolveConfiguredProviderIdSplit:
    """``_resolve_configured_provider_id`` is the actual function called by
    every config-reader in the codebase. With ``resolve_alias=True`` (UI /
    badge surfaces) it should auto-name; with ``resolve_alias=False``
    (runtime / agent auth) it should stay literal."""

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_ui_path_auto_resolves_to_slug(self, fragment, slug):
        url = f"https://{fragment}/v1"
        result = cfg._resolve_configured_provider_id(
            "custom", {}, base_url=url, resolve_alias=True
        )
        assert result == slug

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_runtime_path_stays_custom(self, fragment, slug):
        """``provider: custom + base_url: <built-in>`` must stay ``"custom"``
        at runtime. This is the immediate cause of the auth failure."""
        url = f"https://{fragment}/v1"
        result = cfg._resolve_configured_provider_id(
            "custom", {}, base_url=url, resolve_alias=False
        )
        assert result == "custom", (
            f"Runtime resolution leaked {result!r} for base_url {url!r} — "
            f"agent will fail with '{slug.upper()}_API_KEY environment variable' "
            f"error. See HERMES_FORK_PATCHES.md → api/config.py:880."
        )

    def test_runtime_path_honours_explicit_known_provider(self):
        """If the user EXPLICITLY sets ``provider: openrouter`` (any non-custom
        provider), the base_url-based auto-detect must not override it."""
        result = cfg._resolve_configured_provider_id(
            "openrouter", {}, base_url="https://crof.ai/v1", resolve_alias=False
        )
        assert result == "openrouter"

    def test_runtime_path_unknown_base_url_stays_custom(self):
        """Self-hosted endpoint outside the built-in table → ``"custom"``."""
        result = cfg._resolve_configured_provider_id(
            "custom", {}, base_url="https://my.private.api/v1", resolve_alias=False
        )
        assert result == "custom"


# ── 4. resolve_model_provider — @-prefix smuggling guard (THE bug) ──────


class TestResolveModelProviderAtPrefixGuard:
    """The most user-visible regression: the dropdown JS adds an
    ``@<slug>:`` prefix to model IDs when they come from a non-default
    provider group. Because the fork's auto-detect names a "Custom" group
    after the matching built-in aggregator (e.g. "CrofAI"), every model in
    that group gets an ``@crof:`` prefix. ``resolve_model_provider`` parses
    that prefix as an EXPLICIT runtime provider override and forwards
    "crof" to the agent — which doesn't know what crof is and errors.

    The guard: when the @-prefix matches the built-in slug for the user's
    configured base_url AND their config is ``provider: custom``, the
    prefix is treated as cosmetic and the request resolves to
    ``("model", "custom", base_url)``.
    """

    def _write_custom_config(self, tmp_path, base_url: str) -> Path:
        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n"
            "  default: some-model\n"
            "  provider: custom\n"
            f"  base_url: {base_url}\n",
            encoding="utf-8",
        )
        return cfgfile

    @pytest.mark.parametrize("fragment,slug", BUILTIN_PROVIDERS)
    def test_at_prefix_collapses_to_custom_for_matching_base_url(
        self, fragment, slug, tmp_path, monkeypatch
    ):
        """``@<slug>:model`` + matching base_url + ``provider: custom`` →
        agent receives ``("model", "custom", base_url)``."""
        base_url = f"https://{fragment}/v1"
        cfgfile = self._write_custom_config(tmp_path, base_url)
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            model_id, provider, returned_base_url = cfg.resolve_model_provider(
                f"@{slug}:test-model"
            )
            assert provider == "custom", (
                f"@{slug}: prefix leaked into runtime provider for matching "
                f"base_url {base_url!r}. Got provider={provider!r}, expected "
                f"'custom'. The agent's auxiliary_client will raise "
                f"'Provider {slug!r} is set in config.yaml but no API key "
                f"was found' even though OPENAI_API_KEY is set."
            )
            assert returned_base_url == base_url
            assert model_id == "test-model"
        finally:
            cfg.reload_config()

    def test_at_prefix_honoured_when_config_provider_is_not_custom(
        self, tmp_path, monkeypatch
    ):
        """If the user's config is ``provider: openrouter`` (explicit
        non-custom) and they pick an ``@crof:model`` from the dropdown,
        the @-prefix is a deliberate runtime override — must NOT be
        collapsed back to openrouter."""
        cfgfile = tmp_path / "config.yaml"
        cfgfile.write_text(
            "model:\n"
            "  default: kimi-k2.6\n"
            "  provider: openrouter\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            _, provider, _ = cfg.resolve_model_provider("@crof:kimi-k2.6")
            assert provider == "crof", (
                "Explicit @-prefix must be honoured when config.provider is "
                "NOT 'custom' — the user is deliberately routing through "
                f"a different provider. Got provider={provider!r}."
            )
        finally:
            cfg.reload_config()

    def test_at_prefix_honoured_when_base_url_not_in_builtin_table(
        self, tmp_path, monkeypatch
    ):
        """``provider: custom`` + a non-built-in base_url + ``@crof:model``
        means the user explicitly picked crof — pass it through."""
        cfgfile = self._write_custom_config(tmp_path, "https://my.private.api/v1")
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            _, provider, _ = cfg.resolve_model_provider("@crof:kimi-k2.6")
            assert provider == "crof"
        finally:
            cfg.reload_config()

    def test_at_prefix_mismatched_slug_honoured(self, tmp_path, monkeypatch):
        """``provider: custom + base_url: crof.ai`` BUT ``@venice:model``
        selected — venice doesn't match crof.ai's auto-slug, so the user
        is explicitly cross-routing. Pass through."""
        cfgfile = self._write_custom_config(tmp_path, "https://crof.ai/v1")
        monkeypatch.setattr(cfg, "_get_config_path", lambda: cfgfile)
        cfg.reload_config()
        try:
            _, provider, _ = cfg.resolve_model_provider("@venice:dolphin-3.0")
            assert provider == "venice"
        finally:
            cfg.reload_config()


# ── 5. Source-code wiring guards ────────────────────────────────────────
#
# These assertions catch a future contributor who reintroduces the bug by
# silently removing the runtime guard — the marker block disappears and
# the test fails with a clear pointer to HERMES_FORK_PATCHES.md.


class TestSourceCodeGuardsWired:
    SRC = Path(cfg.__file__).read_text(encoding="utf-8")

    def test_include_builtin_fallback_param_exists(self):
        assert "include_builtin_fallback: bool = True" in self.SRC, (
            "`_named_custom_provider_slug_for_base_url` must keep the "
            "include_builtin_fallback kwarg — see HERMES_FORK_PATCHES.md "
            "and tests/test_hermes_fork_provider_resolution.py docstring."
        )

    def test_runtime_path_passes_false_to_builtin_fallback(self):
        """The ``resolve_alias=False`` branch must pass
        ``include_builtin_fallback=False`` so the runtime auth path doesn't
        flip provider to a fork-only slug."""
        assert "include_builtin_fallback=False" in self.SRC, (
            "The runtime auth path in `_resolve_configured_provider_id` "
            "must pass include_builtin_fallback=False to "
            "_named_custom_provider_slug_for_base_url. Without this, every "
            "VM running `provider: custom + base_url: <aggregator>` will "
            "fail with 'no API key was found'. See HERMES_FORK_PATCHES.md "
            "section api/config.py."
        )

    def test_resolve_model_provider_has_at_prefix_guard(self):
        """The ``@<built-in-slug>:model`` smuggling guard must remain in
        ``resolve_model_provider``."""
        assert "protect runtime from @<built-in-slug>: leak" in self.SRC, (
            "resolve_model_provider must keep the @<slug>: prefix guard. "
            "Without it, the model dropdown's friendly group label "
            "('CrofAI', 'Venice', etc.) leaks back into the runtime "
            "provider arg via the @-prefix on selected model IDs."
        )

    def test_marker_blocks_balanced(self):
        """Every ``>>> hermes-fork`` marker block in this file should have a
        matching ``<<< hermes-fork`` close."""
        opens = self.SRC.count(">>> hermes-fork")
        closes = self.SRC.count("<<< hermes-fork")
        assert opens == closes, (
            f"Fork marker blocks unbalanced in api/config.py: "
            f"{opens} opens, {closes} closes. A truncated edit may have "
            f"left dead patch code visible to upstream rebase resolution."
        )
