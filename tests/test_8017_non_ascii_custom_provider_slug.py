"""
Tests for non-ASCII-only custom provider names (issue #8017).

A `custom_providers[]` entry whose `name` has no ASCII characters (e.g. a
pure-CJK name) used to slugify to "" in `_custom_provider_slug_from_name`,
so the model-catalog builder skipped the entry entirely: its models never
appeared in the picker and no diagnostic was recorded, while the CLI kept
working. The catalog now derives a stable fallback identity for such
entries — the record's `provider_key`, then an endpoint-derived slug
disambiguated by a short name hash, then a name-hash slug — and uses that
same identity through catalog construction and provider resolution.
"""
import pytest
import api.config as config

CJK_NAME = "晨光鑫遇专用"
CJK_SIBLING = "星野专用"
ENDPOINT = "http://127.0.0.1:8317/v1"


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


def _models_with_cfg(model_cfg=None, custom_providers=None):
    """Temporarily patch config.cfg, call get_available_models(), restore.

    Mirrors tests/test_custom_provider_display_name.py, including the
    _cfg_mtime pin so the reload guard does not discard the patch.
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    config.cfg.clear()
    if model_cfg:
        config.cfg["model"] = model_cfg
    if custom_providers is not None:
        config.cfg["custom_providers"] = custom_providers
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


def _group(result, provider_name):
    return next(
        (g for g in result.get("groups", []) if g["provider"] == provider_name),
        None,
    )


def _model_ids(group):
    """Model ids in a group, with any @provider: qualification stripped."""
    ids = []
    for m in group.get("models", []):
        s = str(m["id"] or "")
        if s.startswith("@") and ":" in s:
            s = s.rsplit(":", 1)[1]
        ids.append(s)
    return ids


# ── Slug identity ────────────────────────────────────────────────────────────


class TestFallbackSlug:
    def test_ascii_names_are_unchanged(self):
        assert config._custom_provider_slug_from_name("Agent37") == "custom:agent37"
        # The issue's A/B case: mixed CJK + ASCII keeps its ASCII identity.
        assert (
            config._custom_provider_slug_from_name("晨光鑫遇专用 (cgxy-cpa)")
            == "custom:cgxy-cpa"
        )
        entry = {"name": "Agent37", "base_url": "https://agent37.example.com/v1"}
        assert config._custom_provider_entry_slug(entry) == "custom:agent37"

    def test_non_ascii_only_name_slugifies_empty(self):
        # The root cause, pinned so the fallback below stays necessary.
        assert config._custom_provider_slug_from_name(CJK_NAME) == ""

    def test_provider_key_is_the_first_fallback(self):
        entry = {"name": CJK_NAME, "provider_key": "cgxy-cpa", "base_url": ENDPOINT}
        assert config._custom_provider_entry_slug(entry) == "custom:cgxy-cpa"

    def test_endpoint_fallback_is_stable_and_name_specific(self):
        first = config._custom_provider_entry_slug(
            {"name": CJK_NAME, "base_url": ENDPOINT}
        )
        again = config._custom_provider_entry_slug(
            {"name": CJK_NAME, "base_url": ENDPOINT}
        )
        sibling = config._custom_provider_entry_slug(
            {"name": CJK_SIBLING, "base_url": ENDPOINT}
        )
        assert first == again, "fallback identity must be stable across calls"
        assert first.startswith("custom:127.0.0.1-8317-")
        # Same endpoint, different record: the identities must not merge.
        assert sibling.startswith("custom:127.0.0.1-8317-")
        assert sibling != first

    def test_name_hash_fallback_without_endpoint_or_key(self):
        slug = config._custom_provider_entry_slug({"name": CJK_NAME})
        assert slug.startswith("custom:name-")
        assert slug == config._custom_provider_entry_slug({"name": CJK_NAME})
        assert slug != config._custom_provider_entry_slug({"name": CJK_SIBLING})

    def test_unnamed_entry_has_no_identity(self):
        assert config._custom_provider_entry_slug({"name": ""}) == ""
        assert config._custom_provider_entry_slug({}) == ""


# ── Catalog construction ─────────────────────────────────────────────────────


class TestCatalogIncludesNonAsciiProviders:
    def test_cjk_entry_with_provider_key_appears_in_catalog(self):
        result = _models_with_cfg(
            model_cfg={"provider": "custom", "base_url": ENDPOINT},
            custom_providers=[
                {
                    "name": CJK_NAME,
                    "provider_key": "cgxy-cpa",
                    "model": "DeepSeek-V4.1-Flash",
                    "base_url": ENDPOINT,
                }
            ],
        )
        group = _group(result, CJK_NAME)
        assert group is not None, (
            f"Expected a group for {CJK_NAME!r}, got "
            f"{[g['provider'] for g in result.get('groups', [])]}"
        )
        assert "DeepSeek-V4.1-Flash" in _model_ids(group)

    def test_cjk_entry_without_provider_key_appears_in_catalog(self):
        result = _models_with_cfg(
            model_cfg={"provider": "custom", "base_url": ENDPOINT},
            custom_providers=[
                {"name": CJK_NAME, "model": "local-llm", "base_url": ENDPOINT}
            ],
        )
        group = _group(result, CJK_NAME)
        assert group is not None
        assert "local-llm" in _model_ids(group)

    def test_same_endpoint_cjk_siblings_get_separate_groups(self):
        result = _models_with_cfg(
            model_cfg={"provider": "custom", "base_url": ENDPOINT},
            custom_providers=[
                {"name": CJK_NAME, "model": "model-a", "base_url": ENDPOINT},
                {"name": CJK_SIBLING, "model": "model-b", "base_url": ENDPOINT},
            ],
        )
        first = _group(result, CJK_NAME)
        second = _group(result, CJK_SIBLING)
        assert first is not None and second is not None
        assert "model-a" in _model_ids(first)
        assert "model-b" in _model_ids(second)
        assert "model-b" not in _model_ids(first)


# ── Selection round-tripping ─────────────────────────────────────────────────


class TestSelectionRoundTrip:
    def test_fallback_identity_resolves_back_to_the_entry(self):
        entries = [
            {"name": CJK_NAME, "provider_key": "cgxy-cpa", "base_url": ENDPOINT},
            {"name": CJK_SIBLING, "base_url": ENDPOINT},
        ]
        old_cfg = dict(config.cfg)
        config.cfg.clear()
        config.cfg["custom_providers"] = entries
        try:
            for entry in entries:
                slug = config._custom_provider_entry_slug(entry)
                assert slug
                assert config._named_custom_provider_slug_for_provider(slug) == slug
                assert (
                    config._named_custom_provider_slug_for_base_url(
                        entry["base_url"]
                    )
                    in {config._custom_provider_entry_slug(e) for e in entries}
                )
            named = config._named_custom_provider_slugs()
            assert {config._custom_provider_entry_slug(e) for e in entries} <= named
        finally:
            config.cfg.clear()
            config.cfg.update(old_cfg)
