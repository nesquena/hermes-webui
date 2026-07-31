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