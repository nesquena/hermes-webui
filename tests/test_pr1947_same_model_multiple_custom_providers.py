"""Regression tests for PR #1947 / issue: same model exposed by multiple named
custom providers should appear in the dropdown for each provider, not be
silently deduplicated by the global ``_seen_custom_ids`` bucket.

Pre-fix, ``get_available_models()`` initialized ``_seen_custom_ids`` with bare
model IDs and used a single global dedup set when iterating
``custom_providers``. If two named custom providers exposed the same raw model
ID (e.g. both ``baidu`` and ``huoshan`` offering ``glm-5.1``), the first
provider to be processed claimed the ID and later providers silently lost
their copy.

Post-fix, the dedup key is ``f"{slug}:{model_id}"`` per named provider, so each
provider's models are tracked independently. Per-provider dedup of duplicate
entries within the same provider still works.
"""

import pytest
import api.config as config


@pytest.fixture(autouse=True)
def _isolate_models_cache():
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
    """Patch config.cfg, call get_available_models(), restore.

    Mirrors the pattern in test_custom_provider_display_name.py — pins
    _cfg_mtime so get_available_models()'s reload guard doesn't overwrite
    the patch from on-disk config.yaml.
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


def _group_for_provider(result, slug):
    """Find the rendered ``groups`` entry for a given custom-provider slug.

    Named custom-provider groups have ``provider_id == f"custom:{slug}"``.
    """
    target = f"custom:{slug}"
    for grp in result.get("groups", []) or []:
        if grp.get("provider_id") == target:
            return grp
    return None


def _model_ids(group):
    return [m.get("id") for m in (group or {}).get("models", []) or []]


class TestPR1947SameModelMultipleProviders:
    """Same raw model ID exposed by multiple named custom providers should
    survive the named-custom-group assembly with provider-aware suffixing."""

    def test_two_providers_same_model_both_present(self):
        """Two named providers both expose ``glm-5.1`` — both must appear.

        Pre-fix: ``baidu`` (processed first) claimed ``glm-5.1`` in the global
        ``_seen_custom_ids`` bucket and ``huoshan``'s entry was silently
        dropped. Post-fix: the dedup key is ``slug:model_id`` so both survive.
        """
        result = _models_with_cfg(
            model_cfg={
                "provider": "custom",
                "base_url": "https://baidu.example.com/v1",
            },
            custom_providers=[
                {
                    "name": "baidu",
                    "model": "glm-5.1",
                    "base_url": "https://baidu.example.com/v1",
                },
                {
                    "name": "huoshan",
                    "model": "glm-5.1",
                    "base_url": "https://huoshan.example.com/v1",
                },
            ],
        )

        baidu = _group_for_provider(result, "baidu")
        huoshan = _group_for_provider(result, "huoshan")
        assert baidu is not None, (
            f"baidu group missing; groups="
            f"{[g.get('provider_id') for g in result.get('groups', [])]}"
        )
        assert huoshan is not None, (
            f"huoshan group missing — silent dedup regression; groups="
            f"{[g.get('provider_id') for g in result.get('groups', [])]}"
        )

        baidu_ids = _model_ids(baidu)
        huoshan_ids = _model_ids(huoshan)
        # baidu is the active provider, so its model lands as the bare id.
        # huoshan is a non-active named provider, so it lands as
        # ``@custom:huoshan:glm-5.1`` per the existing namespacing rules.
        assert any("glm-5.1" in (x or "") for x in baidu_ids), (
            f"baidu glm-5.1 missing; baidu ids: {baidu_ids}"
        )
        assert any("glm-5.1" in (x or "") for x in huoshan_ids), (
            f"huoshan glm-5.1 missing — silent dedup regression; huoshan ids: {huoshan_ids}"
        )

    def test_three_providers_same_model_all_present(self):
        """Three providers all expose ``gpt-5.4`` — none should be dropped."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom", "base_url": "https://a.example.com/v1"},
            custom_providers=[
                {
                    "name": "edith",
                    "model": "gpt-5.4",
                    "base_url": "https://a.example.com/v1",
                },
                {
                    "name": "super-javis",
                    "model": "gpt-5.4",
                    "base_url": "https://b.example.com/v1",
                },
                {
                    "name": "vision-prime",
                    "model": "gpt-5.4",
                    "base_url": "https://c.example.com/v1",
                },
            ],
        )

        # All three providers must surface their gpt-5.4 entry.
        for slug in ("edith", "super-javis", "vision-prime"):
            grp = _group_for_provider(result, slug)
            assert grp is not None, (
                f"group for {slug} missing — silent dedup regression; "
                f"groups={[g.get('provider_id') for g in result.get('groups', [])]}"
            )
            ids = _model_ids(grp)
            assert any("gpt-5.4" in (x or "") for x in ids), (
                f"{slug} gpt-5.4 missing; ids: {ids}"
            )

    def test_distinct_models_per_provider_still_grouped_correctly(self):
        """Different models per provider land in their own groups (sanity)."""
        result = _models_with_cfg(
            model_cfg={"provider": "custom", "base_url": "https://a.example.com/v1"},
            custom_providers=[
                {
                    "name": "alpha",
                    "model": "model-a",
                    "base_url": "https://a.example.com/v1",
                },
                {
                    "name": "beta",
                    "model": "model-b",
                    "base_url": "https://b.example.com/v1",
                },
            ],
        )
        alpha = _group_for_provider(result, "alpha")
        beta = _group_for_provider(result, "beta")
        assert alpha is not None and beta is not None
        assert any("model-a" in (x or "") for x in _model_ids(alpha))
        assert any("model-b" in (x or "") for x in _model_ids(beta))
