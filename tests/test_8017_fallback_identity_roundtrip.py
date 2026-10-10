"""
Round-trip regression tests for #8017 fallback provider identities.

The first #8017 fix minted fallback identities (provider_key, then
endpoint+name-hash, then name-hash) for custom_providers[] entries whose
name slugifies to "" (pure-CJK names), and the catalog handed those
identities out — but the owning-record consumers still compared only
name-derived slugs. Selecting a fallback catalog option therefore lost
its row: qualified resolution returned no endpoint, the runtime bundle
reported the route missing/unowned, the active-owner check matched by
name only, and collisions between a fallback identity and an ASCII name
silently resolved the wrong record.

These tests consume ACTUAL catalog model IDs (and catalog-minted slugs)
through resolve_model_provider() and resolve_custom_provider_bundle()
and assert the exact row — endpoint, key and API mode — comes back.
"""
import contextlib
import hashlib

import pytest

import api.config as config

CJK_NAME = "晨光鑫遇专用"
CJK_SIBLING = "星野专用"
URL_A = "http://127.0.0.1:8317/v1"
URL_B = "http://127.0.0.1:8318/v1"
SHARED = "shared-llm"


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the models TTL cache before and after every test in this file."""
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


@contextlib.contextmanager
def _cfg(model_cfg=None, custom_providers=None):
    """Patch config.cfg in place (1806-fixture style), restore afterwards."""
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    if model_cfg is not None:
        config.cfg["model"] = model_cfg
    if custom_providers is not None:
        config.cfg["custom_providers"] = custom_providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()
    config.invalidate_models_cache()
    try:
        yield
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()


def _entry(name, base_url, model=SHARED, **fields):
    entry = {"name": name, "base_url": base_url, "model": model}
    entry.update(fields)
    return entry


def _slug(entry):
    return config._custom_provider_entry_slug(entry)


def _catalog_option(result, slug, model):
    """The exact option id the catalog emitted for (slug, model)."""
    want = f"@{slug}:{model}"
    seen = []
    for group in result.get("groups", []):
        for m in group.get("models", []):
            seen.append(m.get("id"))
            if m.get("id") == want:
                return want
    raise AssertionError(f"catalog option {want!r} not emitted; saw {seen!r}")


def _assert_bundle(slug, *, name, base_url, api_key, api_mode):
    bundle = config.resolve_custom_provider_bundle(slug)
    assert bundle is not None
    assert bundle["status"] == config.CUSTOM_SELECTION_EXACT
    assert bundle["is_exact"] is True
    assert bundle["record"] is not None
    assert bundle["record"]["name"] == name
    assert bundle["base_url"] == base_url
    assert bundle["api_key"] == api_key
    assert bundle["owned"].get("api_mode") == api_mode


# ── Endpoint/hash fallback: different sibling endpoints ──────────────────────


class TestEndpointFallbackRoundTrip:
    def _entries(self):
        return [
            _entry(CJK_NAME, URL_A, api_key="key-a", api_mode="chat_completions"),
            _entry(CJK_SIBLING, URL_B, api_key="key-b", api_mode="anthropic_messages"),
        ]

    def test_catalog_ids_resolve_to_the_exact_rows(self):
        entries = self._entries()
        slug_a, slug_b = _slug(entries[0]), _slug(entries[1])
        assert slug_a != slug_b
        with _cfg({"provider": "custom"}, entries):
            result = config.get_available_models()
            option_a = _catalog_option(result, slug_a, SHARED)
            option_b = _catalog_option(result, slug_b, SHARED)

            # The maintainer's acceptance scenario: the qualified option for
            # one sibling must resolve THAT sibling's endpoint, not None and
            # not the other row.
            assert config.resolve_model_provider(option_a) == (SHARED, slug_a, URL_A)
            assert config.resolve_model_provider(option_b) == (SHARED, slug_b, URL_B)

            _assert_bundle(
                slug_a,
                name=CJK_NAME,
                base_url=URL_A,
                api_key="key-a",
                api_mode="chat_completions",
            )
            _assert_bundle(
                slug_b,
                name=CJK_SIBLING,
                base_url=URL_B,
                api_key="key-b",
                api_mode="anthropic_messages",
            )


# ── Provider-key fallback ────────────────────────────────────────────────────


class TestProviderKeyFallbackRoundTrip:
    def test_catalog_id_resolves_to_the_exact_row(self):
        entries = [
            _entry(
                CJK_NAME,
                URL_A,
                provider_key="cgxy-cpa",
                api_key="key-cpa",
                api_mode="chat_completions",
            ),
            _entry(CJK_SIBLING, URL_B, api_key="key-b", api_mode="anthropic_messages"),
        ]
        assert _slug(entries[0]) == "custom:cgxy-cpa"
        with _cfg({"provider": "custom"}, entries):
            result = config.get_available_models()
            option = _catalog_option(result, "custom:cgxy-cpa", SHARED)
            assert config.resolve_model_provider(option) == (
                SHARED,
                "custom:cgxy-cpa",
                URL_A,
            )
            _assert_bundle(
                "custom:cgxy-cpa",
                name=CJK_NAME,
                base_url=URL_A,
                api_key="key-cpa",
                api_mode="chat_completions",
            )


# ── Shared sibling endpoint: identities stay separate ────────────────────────


class TestSharedEndpointSiblings:
    def test_same_endpoint_rows_do_not_merge(self):
        entries = [
            _entry(CJK_NAME, URL_A, api_key="key-a", api_mode="chat_completions"),
            _entry(CJK_SIBLING, URL_A, api_key="key-b", api_mode="anthropic_messages"),
        ]
        slug_a, slug_b = _slug(entries[0]), _slug(entries[1])
        assert slug_a != slug_b, "same endpoint must not merge sibling identities"
        with _cfg({"provider": "custom"}, entries):
            result = config.get_available_models()
            option_b = _catalog_option(result, slug_b, SHARED)
            assert config.resolve_model_provider(option_b) == (SHARED, slug_b, URL_A)
            _assert_bundle(
                slug_a,
                name=CJK_NAME,
                base_url=URL_A,
                api_key="key-a",
                api_mode="chat_completions",
            )
            _assert_bundle(
                slug_b,
                name=CJK_SIBLING,
                base_url=URL_A,
                api_key="key-b",
                api_mode="anthropic_messages",
            )


# ── Active-owner precedence with a fallback identity ─────────────────────────


class TestActiveSiblingPrecedence:
    def test_active_fallback_provider_wins_over_config_order(self):
        entries = [
            _entry(CJK_NAME, URL_A, api_key="key-a"),
            _entry(CJK_SIBLING, URL_B, api_key="key-b"),
        ]
        slug_b = _slug(entries[1])
        # The ACTIVE provider is sibling B (second in config order); a bare
        # shared model must resolve through B, not through first-listed A.
        with _cfg({"provider": slug_b}, entries):
            assert config.resolve_model_provider(SHARED) == (SHARED, slug_b, URL_B)


# ── Fallback-key / name collisions fail closed ───────────────────────────────


class TestFallbackCollisionFailsClosed:
    def _entries(self):
        return [
            _entry(
                "review-collision",
                "http://127.0.0.1:7111/v1",
                model="ascii-model",
                api_key="key-ascii",
            ),
            _entry(
                CJK_NAME,
                "http://127.0.0.1:7222/v1",
                model="cjk-model",
                provider_key="review-collision",
                api_key="key-cjk",
            ),
        ]

    def test_both_rows_mint_the_same_identity(self):
        entries = self._entries()
        assert _slug(entries[0]) == "custom:review-collision"
        assert _slug(entries[1]) == "custom:review-collision"

    def test_qualified_resolution_raises_instead_of_picking_the_ascii_row(self):
        with _cfg({"provider": "custom"}, self._entries()):
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.resolve_model_provider("@custom:review-collision:cjk-model")

    def test_bare_resolution_raises(self):
        with _cfg({"provider": "custom"}, self._entries()):
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.resolve_model_provider("ascii-model")

    def test_bundle_selection_raises(self):
        with _cfg({"provider": "custom"}, self._entries()):
            with pytest.raises(config.AmbiguousCustomProviderError):
                config.resolve_custom_provider_bundle("custom:review-collision")


# ── IPv6 endpoints ───────────────────────────────────────────────────────────


class TestIPv6Endpoint:
    IPV6_URL = "http://[::1]:8317/v1"

    def test_ipv6_host_is_encoded_without_colons(self):
        digest = hashlib.sha256(CJK_NAME.encode("utf-8")).hexdigest()[:8]
        slug = _slug(_entry(CJK_NAME, self.IPV6_URL))
        assert slug == f"custom:--1-8317-{digest}"
        assert slug.count(":") == 1, "only the custom: prefix colon may remain"

    def test_ipv6_option_parses_and_round_trips(self):
        entry = _entry(
            CJK_NAME,
            self.IPV6_URL,
            api_key="key-v6",
            api_mode="chat_completions",
        )
        slug = _slug(entry)
        option = f"@{slug}:{SHARED}"
        assert config._parse_provider_qualified_model_id(option) == (SHARED, slug)
        with _cfg({"provider": "custom"}, [entry]):
            assert config.resolve_model_provider(option) == (
                SHARED,
                slug,
                self.IPV6_URL,
            )
            _assert_bundle(
                slug,
                name=CJK_NAME,
                base_url=self.IPV6_URL,
                api_key="key-v6",
                api_mode="chat_completions",
            )


# ── Colon-tagged model IDs ───────────────────────────────────────────────────


class TestColonTaggedModelId:
    def test_tagged_model_round_trips_through_a_fallback_slug(self):
        entry = _entry(CJK_NAME, URL_A, model="shared-llm:free", api_key="key-a")
        slug = _slug(entry)
        option = f"@{slug}:shared-llm:free"
        assert config._parse_provider_qualified_model_id(option) == (
            "shared-llm:free",
            slug,
        )
        with _cfg({"provider": "custom"}, [entry]):
            assert config.resolve_model_provider(option) == (
                "shared-llm:free",
                slug,
                URL_A,
            )


# ── Malformed ports degrade instead of aborting ──────────────────────────────


class TestMalformedPort:
    BAD_URL = "http://127.0.0.1:notaport/v1"

    def test_fallback_slug_survives_a_malformed_port(self):
        digest = hashlib.sha256(CJK_NAME.encode("utf-8")).hexdigest()[:8]
        slug = config._custom_provider_fallback_slug(CJK_NAME, None, self.BAD_URL)
        assert slug == f"custom:127.0.0.1-80-{digest}"

    def test_catalog_build_survives_a_malformed_port(self):
        entry = _entry(CJK_NAME, self.BAD_URL, model="port-llm")
        with _cfg({"provider": "custom"}, [entry]):
            result = config.get_available_models()
            _catalog_option(result, _slug(entry), "port-llm")


# ── Cold/static catalog uses the same identity ───────────────────────────────


class TestStaticCatalogIdentity:
    def test_static_catalog_groups_under_the_fallback_slug(self):
        entry = _entry(CJK_NAME, URL_A, model="static-llm")
        slug = _slug(entry)
        with _cfg({"provider": "custom"}, [entry]):
            result = config._static_models_catalog_without_live_probes()
        groups = {g["provider_id"]: g for g in result.get("groups", [])}
        assert slug in groups, (
            f"expected a static group {slug!r}, got {sorted(groups)}"
        )
        static_ids = [str(m.get("id") or "") for m in groups[slug].get("models", [])]
        assert any(mid == "static-llm" or mid.endswith(":static-llm") for mid in static_ids)
        generic = groups.get("custom")
        if generic is not None:
            generic_ids = [str(m.get("id") or "") for m in generic.get("models", [])]
            assert not any(
                mid == "static-llm" or mid.endswith(":static-llm")
                for mid in generic_ids
            ), "the CJK entry's models must not fall into the generic custom group"

    def test_static_catalog_keeps_underscore_provider_key_identity(self):
        # A provider_key with an underscore mints custom:cgxy_cpa. The
        # static catalog must not fold that identity to custom:cgxy-cpa
        # when canonicalising detected providers — the group is stored
        # under the minted slug, so folding orphans it and the provider
        # disappears from the cold picker.
        entry = _entry(CJK_NAME, URL_A, model="key-llm", provider_key="cgxy_cpa")
        slug = _slug(entry)
        assert slug == "custom:cgxy_cpa"
        with _cfg({"provider": "custom"}, [entry]):
            result = config._static_models_catalog_without_live_probes()
        groups = {g["provider_id"]: g for g in result.get("groups", [])}
        assert slug in groups, (
            f"expected a static group {slug!r}, got {sorted(groups)}"
        )
        static_ids = [str(m.get("id") or "") for m in groups[slug].get("models", [])]
        assert any(mid == "key-llm" or mid.endswith(":key-llm") for mid in static_ids)

    def test_static_catalog_keeps_underscore_ascii_name_identity(self):
        # Control: an ASCII name with an underscore already mints an
        # underscore slug in the live catalog; the cold catalog must use
        # the same identity, not the hyphen-folded form.
        entry = _entry("my_provider", URL_A, model="under-llm")
        slug = _slug(entry)
        assert slug == "custom:my_provider"
        with _cfg({"provider": "custom"}, [entry]):
            result = config._static_models_catalog_without_live_probes()
        groups = {g["provider_id"]: g for g in result.get("groups", [])}
        assert slug in groups, (
            f"expected a static group {slug!r}, got {sorted(groups)}"
        )


# ── Generic records claim fallback identities consistently ───────────────────


class TestRecordClaimsFallbackSlug:
    def test_record_with_non_ascii_name_claims_its_fallback_identity(self):
        record = {"name": CJK_NAME, "base_url": URL_A}
        bare_key = _slug(record).split(":", 1)[1]
        assert config._custom_record_claims_slug(record, bare_key) is True
        # A model: block's name is the model's name, never a provider claim.
        assert (
            config._custom_record_claims_slug(record, bare_key, allow_name=False)
            is False
        )


# ── ASCII control: name-derived identities are untouched ─────────────────────


class TestAsciiControlRoundTrip:
    def test_ascii_entry_round_trips_as_before(self):
        entry = _entry(
            "Review ASCII",
            "http://127.0.0.1:7331/v1",
            model="ascii-llm",
            api_key="key-ascii",
            api_mode="chat_completions",
        )
        assert _slug(entry) == "custom:review-ascii"
        with _cfg({"provider": "custom"}, [entry]):
            result = config.get_available_models()
            option = _catalog_option(result, "custom:review-ascii", "ascii-llm")
            assert config.resolve_model_provider(option) == (
                "ascii-llm",
                "custom:review-ascii",
                "http://127.0.0.1:7331/v1",
            )
            _assert_bundle(
                "custom:review-ascii",
                name="Review ASCII",
                base_url="http://127.0.0.1:7331/v1",
                api_key="key-ascii",
                api_mode="chat_completions",
            )
