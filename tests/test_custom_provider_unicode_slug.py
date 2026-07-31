"""
Tests for Unicode-aware custom provider slug generation (PR #6646).

The slug contract: ASCII-safe with a deterministic hash fallback for
names whose ASCII characters are stripped away (pure CJK, etc.).
All slug paths (config.py, routes.py fallback, _api_key_env_name)
must be consistent.
"""

import unicodedata

import api.config as config


# ── Unit tests for _ascii_slug_from_name ──────────────────────────────────────

class TestAsciiSlugFromName:
    """Direct unit tests for the extracted slug helper."""

    def test_ascii_name_preserved(self):
        """ASCII names produce the expected kebab slug."""
        assert config._ascii_slug_from_name("My Provider") == "my-provider"

    def test_ascii_with_punctuation(self):
        """Punctuation is replaced with hyphens, not removed."""
        slug = config._ascii_slug_from_name("Local (127.0.0.1:15721)")
        assert slug == "local-127.0.0.1-15721"

    def test_pure_cjk_name_produces_hash_fallback(self):
        """A pure Chinese name produces a deterministic hash-based slug."""
        slug = config._ascii_slug_from_name("我的提供商")
        assert slug.startswith("provider-"), f"Expected hash fallback, got {slug!r}"
        assert len(slug) == len("provider-") + 12, (
            f"Expected 'provider-' + 12 hex chars, got {slug!r}"
        )
        # Must be deterministic
        assert config._ascii_slug_from_name("我的提供商") == slug

    def test_cjk_with_ascii_mixed(self):
        """Mixed CJK + ASCII: ASCII part preserved, hash prevents collision."""
        slug = config._ascii_slug_from_name("我的Proxy服务器")
        assert slug.startswith("proxy-"), f"Expected 'proxy-<hash>', got {slug!r}"
        assert len(slug) == len("proxy-") + 12, (
            f"Expected 'proxy-' + 12 hex chars, got {slug!r}"
        )
        # Must be deterministic
        assert config._ascii_slug_from_name("我的Proxy服务器") == slug
        # Must not collide with pure ASCII "Proxy"
        assert config._ascii_slug_from_name("Proxy") != slug

    def test_empty_name_returns_empty(self):
        """Empty or whitespace-only name returns empty string."""
        assert config._ascii_slug_from_name("") == ""
        assert config._ascii_slug_from_name("   ") == ""

    def test_two_distinct_cjk_names_produce_different_slugs(self):
        """Two different pure-CJK names must not collide."""
        slug_a = config._ascii_slug_from_name("我的提供商")
        slug_b = config._ascii_slug_from_name("另一个名字")
        assert slug_a != slug_b, (
            f"Distinct CJK names must produce different slugs, got {slug_a!r} == {slug_b!r}"
        )

    def test_cjk_punctuation_handled(self):
        """CJK punctuation (full-width) is stripped like ASCII punctuation."""
        slug = config._ascii_slug_from_name("测试·提供商")
        assert slug.startswith("provider-"), (
            f"Expected hash fallback for CJK name with punctuation, got {slug!r}"
        )

    def test_canonically_equivalent_names_produce_same_slug(self):
        """NFC normalization ensures composed/decomposed forms collide."""
        # U+00E9 (é precomposed) vs U+0065 U+0301 (e + combining acute)
        composed = "caf\u00e9"
        decomposed = unicodedata.normalize("NFD", "caf\u00e9")
        assert composed != decomposed, "precondition: forms differ"
        assert config._ascii_slug_from_name(composed) == config._ascii_slug_from_name(decomposed), (
            "NFC-normalised names must produce the same slug"
        )

    def test_leading_trailing_hyphens_stripped(self):
        """Leading/trailing hyphens from punctuation replacement are stripped."""
        slug = config._ascii_slug_from_name("---hello---")
        assert slug == "hello", f"Expected 'hello', got {slug!r}"

    def test_consecutive_hyphens_collapsed(self):
        """Multiple consecutive hyphens are collapsed to one."""
        slug = config._ascii_slug_from_name("a   b")
        assert slug == "a-b", f"Expected 'a-b', got {slug!r}"

    def test_ascii_only_names_never_change(self):
        """Regression: pure-ASCII provider IDs must not change for legacy compat."""
        assert config._ascii_slug_from_name("Proxy") == "proxy"
        assert config._ascii_slug_from_name("My-Proxy") == "my-proxy"
        assert config._ascii_slug_from_name("Test123") == "test123"

    def test_mixed_script_no_collision_with_ascii_projection(self):
        """Names with CJK + ASCII must not collide with their ASCII projection."""
        slug_mixed = config._ascii_slug_from_name("我的Proxy服务器")
        slug_ascii = config._ascii_slug_from_name("Proxy")
        assert slug_mixed != slug_ascii, (
            f"Mixed-script name must not collide with ASCII projection: "
            f"{slug_mixed!r} == {slug_ascii!r}"
        )

    def test_two_cjk_names_with_different_ascii_projections(self):
        """Two CJK names with different ASCII projections must not collide.

        Example: '东京-1' and '大阪-1' both strip to '1' in ASCII-only mode;
        the hash suffix must distinguish them.
        """
        slug_a = config._ascii_slug_from_name("东京-1")
        slug_b = config._ascii_slug_from_name("大阪-1")
        assert slug_a != slug_b, (
            f"Distinct CJK names with same ASCII projection must differ: "
            f"{slug_a!r} == {slug_b!r}"
        )
        assert slug_a.startswith("1-"), f"Expected '1-<hash>', got {slug_a!r}"
        assert slug_b.startswith("1-"), f"Expected '1-<hash>', got {slug_b!r}"


# ── Integration: _custom_provider_slug_from_name ──────────────────────────────

class TestCustomProviderSlugFromName:
    """The full slug function that returns 'custom:<slug>'."""

    def test_ascii_name_full_slug(self):
        assert config._custom_provider_slug_from_name("My Provider") == "custom:my-provider"

    def test_pure_cjk_name_full_slug(self):
        slug = config._custom_provider_slug_from_name("我的提供商")
        assert slug.startswith("custom:provider-"), (
            f"Expected 'custom:provider-<hash>', got {slug!r}"
        )

    def test_already_prefixed_name_passthrough(self):
        assert config._custom_provider_slug_from_name("custom:existing") == "custom:existing"

    def test_empty_name_returns_empty(self):
        assert config._custom_provider_slug_from_name("") == ""

    def test_mixed_cjk_ascii_no_collision(self):
        """Mixed CJK+ASCII provider name must not collide with ASCII-only name."""
        slug_mixed = config._custom_provider_slug_from_name("我的Proxy服务器")
        slug_ascii = config._custom_provider_slug_from_name("Proxy")
        assert slug_mixed != slug_ascii, (
            f"Full slug collision: {slug_mixed!r} == {slug_ascii!r}"
        )

    def test_two_cjk_ascii_names_no_collision(self):
        """Two CJK names with same ASCII projection must produce distinct slugs."""
        slug_a = config._custom_provider_slug_from_name("东京-1")
        slug_b = config._custom_provider_slug_from_name("大阪-1")
        assert slug_a != slug_b, (
            f"Full slug collision: {slug_a!r} == {slug_b!r}"
        )


# ── API-key environment name consistency ──────────────────────────────────────

class TestApiKeyEnvName:
    """Distinct provider IDs must produce distinct API-key env names."""

    def test_distinct_cjk_providers_have_distinct_env_names(self):
        """Two different pure-CJK provider names → different env variable names."""
        pid_a = config._custom_provider_slug_from_name("提供商A")
        pid_b = config._custom_provider_slug_from_name("提供商B")
        assert pid_a != pid_b
        assert config._api_key_env_name(pid_a) != config._api_key_env_name(pid_b), (
            f"Distinct providers must have distinct env names: "
            f"{config._api_key_env_name(pid_a)} == {config._api_key_env_name(pid_b)}"
        )

    def test_env_name_starts_with_custom(self):
        pid = config._custom_provider_slug_from_name("我的提供商")
        env = config._api_key_env_name(pid)
        assert env.startswith("CUSTOM_"), f"Expected CUSTOM_ prefix, got {env!r}"
        assert env.endswith("_API_KEY"), f"Expected _API_KEY suffix, got {env!r}"

    def test_ascii_provider_env_name(self):
        pid = config._custom_provider_slug_from_name("My-Proxy")
        assert config._api_key_env_name(pid) == "CUSTOM_MY_PROXY_API_KEY"

    def test_mixed_cjk_ascii_distinct_env_names(self):
        """Mixed CJK+ASCII and ASCII-only provider → distinct env names."""
        pid_mixed = config._custom_provider_slug_from_name("我的Proxy服务器")
        pid_ascii = config._custom_provider_slug_from_name("Proxy")
        assert config._api_key_env_name(pid_mixed) != config._api_key_env_name(pid_ascii), (
            "Credential env names must differ for distinct providers"
        )

    def test_two_cjk_ascii_distinct_env_names(self):
        """Two CJK names with same ASCII projection → distinct env names."""
        pid_a = config._custom_provider_slug_from_name("东京-1")
        pid_b = config._custom_provider_slug_from_name("大阪-1")
        assert config._api_key_env_name(pid_a) != config._api_key_env_name(pid_b), (
            "Credential env names must differ for distinct providers"
        )


# ── Route fallback parity ─────────────────────────────────────────────────────

class TestRouteFallbackParity:
    """The routes.py fallback must produce the same slug as config.py."""

    def test_fallback_matches_config_for_cjk(self):
        from api.routes import _custom_provider_slug_for_context

        name = "我的提供商"
        config_slug = config._custom_provider_slug_from_name(name)
        fallback_slug = _custom_provider_slug_for_context(name)
        assert config_slug == fallback_slug, (
            f"Route fallback must match config: {config_slug!r} != {fallback_slug!r}"
        )

    def test_fallback_matches_config_for_ascii(self):
        from api.routes import _custom_provider_slug_for_context

        name = "My Proxy (v2)"
        config_slug = config._custom_provider_slug_from_name(name)
        fallback_slug = _custom_provider_slug_for_context(name)
        assert config_slug == fallback_slug

    def test_fallback_matches_config_for_empty(self):
        from api.routes import _custom_provider_slug_for_context

        assert config._custom_provider_slug_from_name("") == _custom_provider_slug_for_context("") == ""

    def test_fallback_no_collision_mixed_cjk_ascii(self):
        """Route fallback must not collide mixed CJK+ASCII with ASCII-only."""
        from api.routes import _custom_provider_slug_for_context

        slug_mixed = _custom_provider_slug_for_context("我的Proxy服务器")
        slug_ascii = _custom_provider_slug_for_context("Proxy")
        assert slug_mixed != slug_ascii, (
            f"Route fallback collision: {slug_mixed!r} == {slug_ascii!r}"
        )

    def test_fallback_no_collision_two_cjk_ascii(self):
        """Route fallback must not collide two CJK names with same ASCII projection."""
        from api.routes import _custom_provider_slug_for_context

        slug_a = _custom_provider_slug_for_context("东京-1")
        slug_b = _custom_provider_slug_for_context("大阪-1")
        assert slug_a != slug_b, (
            f"Route fallback collision: {slug_a!r} == {slug_b!r}"
        )