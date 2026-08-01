"""
Tests for Unicode-aware custom provider slug generation (PR #6646).

The slug contract: preserve the provider display name as-is, only
replacing ``:`` and whitespace with ``-``.  This matches the Hermes
agent's ``_normalize_custom_pool_name`` convention
(``name.strip().lower().replace(" ", "-")``).
All slug paths (config.py, routes.py fallback) must be consistent.
"""

import unicodedata

import api.config as config


# ── Unit tests for _ascii_slug_from_name ──────────────────────────────────────

class TestAsciiSlugFromName:
    """Direct unit tests for _ascii_slug_from_name."""

    def test_ascii_name_preserved(self):
        """ASCII names produce the expected kebab slug."""
        assert config._ascii_slug_from_name("My Provider") == "my-provider"

    def test_ascii_with_punctuation(self):
        """Colons and whitespace are replaced with hyphens."""
        slug = config._ascii_slug_from_name("Local (127.0.0.1:15721)")
        assert slug == "local-(127.0.0.1-15721)"

    def test_pure_cjk_name_preserved(self):
        """A pure Chinese name is preserved as-is (matches Hermes convention)."""
        slug = config._ascii_slug_from_name("我的提供商")
        assert slug == "我的提供商", f"Expected preserved CJK, got {slug!r}"
        # Must be deterministic
        assert config._ascii_slug_from_name("我的提供商") == slug

    def test_cjk_with_ascii_mixed(self):
        """Mixed CJK + ASCII: all characters preserved, only :/space replaced."""
        slug = config._ascii_slug_from_name("我的Proxy服务器")
        assert slug == "我的proxy服务器", f"Expected preserved mixed, got {slug!r}"
        assert config._ascii_slug_from_name("我的Proxy服务器") == slug

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
        """CJK punctuation (full-width) is preserved."""
        slug = config._ascii_slug_from_name("测试·提供商")
        assert slug == "测试·提供商", (
            f"Expected preserved CJK with punctuation, got {slug!r}"
        )

    def test_canonically_equivalent_names_produce_same_slug(self):
        """NFC normalization ensures composed/decomposed forms collide."""
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

    def test_ascii_only_names_preserved(self):
        """Pure-ASCII provider IDs preserved as before."""
        assert config._ascii_slug_from_name("Proxy") == "proxy"
        assert config._ascii_slug_from_name("My-Proxy") == "my-proxy"
        assert config._ascii_slug_from_name("Test123") == "test123"

    def test_colon_replaced_with_hyphen(self):
        """Colons are replaced with hyphens."""
        assert config._ascii_slug_from_name("a:b") == "a-b"
        assert config._ascii_slug_from_name("my:provider") == "my-provider"

    def test_real_world_cjk_name(self):
        """Real-world CJK provider name '基元律动' preserved."""
        slug = config._ascii_slug_from_name("基元律动")
        assert slug == "基元律动", f"Expected '基元律动', got {slug!r}"


# ── Integration: _custom_provider_slug_from_name ──────────────────────────────

class TestCustomProviderSlugFromName:
    """The full slug function that returns 'custom:<slug>'."""

    def test_ascii_name_full_slug(self):
        assert config._custom_provider_slug_from_name("My Provider") == "custom:my-provider"

    def test_pure_cjk_name_full_slug(self):
        slug = config._custom_provider_slug_from_name("我的提供商")
        assert slug == "custom:我的提供商", (
            f"Expected 'custom:我的提供商', got {slug!r}"
        )

    def test_already_prefixed_name_passthrough(self):
        assert config._custom_provider_slug_from_name("custom:existing") == "custom:existing"

    def test_empty_name_returns_empty(self):
        assert config._custom_provider_slug_from_name("") == ""

    def test_mixed_cjk_ascii_full_slug(self):
        slug = config._custom_provider_slug_from_name("我的Proxy服务器")
        assert slug == "custom:我的proxy服务器", (
            f"Expected 'custom:我的proxy服务器', got {slug!r}"
        )

    def test_real_world_cjk_name(self):
        """Real-world CJK provider name '基元律动'."""
        slug = config._custom_provider_slug_from_name("基元律动")
        assert slug == "custom:基元律动", f"Expected 'custom:基元律动', got {slug!r}"


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


# ── Integration: resolve_custom_provider_connection ───────────────────────────

class TestResolveCustomProviderConnection:
    """resolve_custom_provider_connection must resolve CJK provider names."""

    def test_resolve_cjk_provider_by_canonical_slug(self, monkeypatch):
        """A CJK provider ID generated by _custom_provider_slug_from_name
        must resolve to its own endpoint and key."""
        pid = config._custom_provider_slug_from_name("我的提供商")
        assert pid == "custom:我的提供商", f"Expected 'custom:我的提供商', got {pid!r}"

        monkeypatch.setattr(
            config,
            "get_config",
            lambda: {
                "custom_providers": [
                    {"name": "我的提供商", "api_key": "key-a", "base_url": "https://a.example.com"},
                ],
            },
        )
        api_key, base_url = config.resolve_custom_provider_connection(pid)
        assert api_key == "key-a", f"Expected key-a, got {api_key!r}"
        assert base_url == "https://a.example.com"

    def test_resolve_two_cjk_providers_to_distinct_endpoints(self, monkeypatch):
        """Two different CJK providers must resolve to distinct endpoints."""
        pid_a = config._custom_provider_slug_from_name("提供商A")
        pid_b = config._custom_provider_slug_from_name("提供商B")
        assert pid_a != pid_b

        monkeypatch.setattr(
            config,
            "get_config",
            lambda: {
                "custom_providers": [
                    {"name": "提供商A", "api_key": "key-a", "base_url": "https://a.example.com"},
                    {"name": "提供商B", "api_key": "key-b", "base_url": "https://b.example.com"},
                ],
            },
        )
        key_a, url_a = config.resolve_custom_provider_connection(pid_a)
        key_b, url_b = config.resolve_custom_provider_connection(pid_b)
        assert key_a == "key-a", f"Provider A key mismatch: {key_a!r}"
        assert url_a == "https://a.example.com"
        assert key_b == "key-b", f"Provider B key mismatch: {key_b!r}"
        assert url_b == "https://b.example.com"

    def test_ascii_provider_still_resolves(self, monkeypatch):
        """ASCII provider IDs still resolve correctly."""
        monkeypatch.setattr(
            config,
            "get_config",
            lambda: {
                "custom_providers": [
                    {"name": "Proxy", "api_key": "key-p", "base_url": "https://proxy.example.com"},
                ],
            },
        )
        api_key, base_url = config.resolve_custom_provider_connection("custom:proxy")
        assert api_key == "key-p"
        assert base_url == "https://proxy.example.com"

    def test_mixed_cjk_ascii_name_resolves(self, monkeypatch):
        """'我的Proxy服务器' must resolve to its own endpoint."""
        pid_mixed = config._custom_provider_slug_from_name("我的Proxy服务器")
        assert pid_mixed == "custom:我的proxy服务器"

        monkeypatch.setattr(
            config,
            "get_config",
            lambda: {
                "custom_providers": [
                    {"name": "我的Proxy服务器", "api_key": "key-mixed", "base_url": "https://mixed.example.com"},
                    {"name": "Proxy", "api_key": "key-ascii", "base_url": "https://ascii.example.com"},
                ],
            },
        )
        key_mixed, url_mixed = config.resolve_custom_provider_connection(pid_mixed)
        assert key_mixed == "key-mixed"
        assert url_mixed == "https://mixed.example.com"

    def test_env_var_api_key_resolved_for_cjk_provider(self, monkeypatch):
        """${ENV_VAR} api_key syntax must work for CJK providers."""
        pid = config._custom_provider_slug_from_name("我的提供商")
        monkeypatch.setattr(
            config,
            "get_config",
            lambda: {
                "custom_providers": [
                    {"name": "我的提供商", "api_key": "${MY_CJK_KEY}", "base_url": "https://cj.example.com"},
                ],
            },
        )
        monkeypatch.setenv("MY_CJK_KEY", "secret-env-key")
        api_key, base_url = config.resolve_custom_provider_connection(pid)
        assert api_key == "secret-env-key"
        assert base_url == "https://cj.example.com"

    def test_real_world_tokenrhythm(self, monkeypatch):
        """Real-world TokenRhythm provider '基元律动' must resolve."""
        pid = config._custom_provider_slug_from_name("基元律动")
        assert pid == "custom:基元律动"

        monkeypatch.setattr(
            config,
            "get_config",
            lambda: {
                "custom_providers": [
                    {"name": "基元律动", "api_key": "sk-tr-key", "base_url": "https://tokenrhythm.studio/v1"},
                ],
            },
        )
        api_key, base_url = config.resolve_custom_provider_connection(pid)
        assert api_key == "sk-tr-key"
        assert base_url == "https://tokenrhythm.studio/v1"