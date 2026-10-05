"""Regression tests: GLM-5.3 Flash catalog presence and reasoning tier.

GLM-5.3 Flash is Z.ai's fast/cheap sibling of GLM-5.3 (same generation,
effort-ladder tier via the >=5.2 version gate). These tests pin catalog
presence (opt-in selection), fallback entry, sibling ordering (base before
flash, mirroring the glm-4.5 / glm-4.5-flash pattern), and the full
reasoning-effort ladder — ensuring the model users see matches what's
declared in api/config.py. Like glm-5.3 itself, the onboarding default
deliberately REMAINS glm-5.1 (see test_glm_5_3_catalog.py).
"""
import unittest.mock as mock

import pytest

import api.config as cfg


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the TTL model cache before AND after every test.

    Mirrors the fixture in test_glm_5_3_catalog.py: ``get_available_models()``
    caches its result keyed on config.yaml mtime. Tests here repoint
    ``_get_config_path`` to a tmp_path, populate the cache there, then let
    monkeypatch restore the original path. Clearing the cache around each test
    keeps that stale data from poisoning neighboring test files.
    """
    import api.config as c
    import api.providers as p
    import api.profiles as profiles
    old_cfg = dict(c.cfg)
    old_mtime = c._cfg_mtime
    old_path = c._cfg_path
    old_fingerprint = c._cfg_fingerprint
    try:
        c.invalidate_models_cache()
        p.invalidate_providers_cache()
        profiles._invalidate_root_profile_cache()
        from api.plugin_providers import invalidate_plugin_model_provider_cache
        invalidate_plugin_model_provider_cache()
    except Exception:
        pass
    yield
    c.cfg.clear()
    c.cfg.update(old_cfg)
    c._cfg_mtime = old_mtime
    c._cfg_path = old_path
    c._cfg_fingerprint = old_fingerprint
    try:
        c.invalidate_models_cache()
        p.invalidate_providers_cache()
        profiles._invalidate_root_profile_cache()
        from api.plugin_providers import invalidate_plugin_model_provider_cache
        invalidate_plugin_model_provider_cache()
    except Exception:
        pass


def test_glm_5_3_flash_in_provider_models():
    """GLM-5.3 Flash must appear in the zai provider catalog with the correct label."""
    zai_models = cfg._PROVIDER_MODELS.get("zai", [])
    model_ids = [m["id"] for m in zai_models]
    assert "glm-5.3-flash" in model_ids, (
        f"glm-5.3-flash missing from zai provider models; got {model_ids}"
    )

    # Verify the exact label
    flash_entries = [m for m in zai_models if m["id"] == "glm-5.3-flash"]
    assert len(flash_entries) == 1
    assert flash_entries[0]["label"] == "GLM-5.3 Flash", (
        f'Expected label "GLM-5.3 Flash", got {flash_entries[0]["label"]!r}'
    )


def test_glm_5_3_flash_ordered_base_then_flash_then_glm_5_1():
    """Flash sibling must sit directly after its base glm-5.3, then glm-5.2 (newest-first).

    Mirrors the existing glm-4.5 -> glm-4.5-flash pattern: base first, then the
    flash variant of the same generation. The sibling-adjacency contract is
    pinned in BOTH catalogs: this test pins it for the zai provider catalog,
    and test_glm_5_3_flash_positioned_after_glm_5_3_in_zai_fallback_block pins
    the same adjacency for the Z.AI fallback block.
    """
    zai_models = cfg._PROVIDER_MODELS.get("zai", [])
    ids = [m["id"] for m in zai_models]
    indices = {}
    for i, model in enumerate(zai_models):
        if model["id"] in ("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1"):
            indices[model["id"]] = i
    assert "glm-5.3" in indices, "glm-5.3 not found in zai models"
    assert "glm-5.3-flash" in indices, "glm-5.3-flash not found in zai models"
    assert "glm-5.2" in indices, "glm-5.2 not found in zai models"
    assert "glm-5.1" in indices, "glm-5.1 not found in zai models"
    assert indices["glm-5.3-flash"] == indices["glm-5.3"] + 1, (
        f"Expected glm-5.3-flash immediately after glm-5.3 in the zai provider "
        f"catalog; glm-5.3 index {indices['glm-5.3']}, glm-5.3-flash index "
        f"{indices['glm-5.3-flash']}; got {ids}"
    )
    assert indices["glm-5.2"] == indices["glm-5.3-flash"] + 1, (
        f"Expected glm-5.2 immediately after glm-5.3-flash in the zai provider "
        f"catalog; glm-5.3-flash index {indices['glm-5.3-flash']}, glm-5.2 "
        f"index {indices['glm-5.2']}; got {ids}"
    )
    assert indices["glm-5.3-flash"] < indices["glm-5.1"], (
        f"glm-5.3-flash (index {indices['glm-5.3-flash']}) must appear before "
        f"glm-5.1 (index {indices['glm-5.1']})"
    )


def test_glm_5_3_flash_in_fallback_models():
    """GLM-5.3 Flash must appear in _FALLBACK_MODELS with correct provider and label."""
    fallback_entries = [
        m for m in cfg._FALLBACK_MODELS if m["id"] == "zai/glm-5.3-flash"
    ]
    assert len(fallback_entries) == 1, (
        f"Expected exactly one zai/glm-5.3-flash entry in _FALLBACK_MODELS; "
        f"found {len(fallback_entries)}"
    )

    entry = fallback_entries[0]
    assert entry["provider"] == "Z.AI", (
        f'Expected provider "Z.AI", got {entry["provider"]!r}'
    )
    assert entry["label"] == "GLM-5.3 Flash", (
        f'Expected label "GLM-5.3 Flash", got {entry["label"]!r}'
    )


def test_glm_5_3_flash_positioned_after_glm_5_3_in_zai_fallback_block():
    """zai/glm-5.3-flash must sit directly after zai/glm-5.3 among Z.AI entries."""
    zai_ids = [m["id"] for m in cfg._FALLBACK_MODELS if m["provider"] == "Z.AI"]
    assert "zai/glm-5.3" in zai_ids, "zai/glm-5.3 not found in _FALLBACK_MODELS"
    assert "zai/glm-5.3-flash" in zai_ids, (
        f"zai/glm-5.3-flash not found in _FALLBACK_MODELS; got {zai_ids}"
    )
    base_index = zai_ids.index("zai/glm-5.3")
    flash_index = zai_ids.index("zai/glm-5.3-flash")
    assert flash_index == base_index + 1, (
        f"Expected zai/glm-5.3-flash immediately after zai/glm-5.3 in the "
        f"Z.AI fallback block; got {zai_ids}"
    )


def test_glm_5_3_flash_reasoning_efforts():
    """GLM-5.3 Flash must support the full reasoning_effort ladder (GLM-5.2+ tier).

    No flash-specific gate is expected: _zai_glm_classification parses the
    version out of the id, so >=5.2 flash variants inherit the effort tier.
    """
    assert cfg._zai_glm_classification("glm-5.3-flash", "zai") == "effort", (
        'glm-5.3-flash must classify as "effort" tier via the >=5.2 version gate'
    )
    efforts = cfg.resolve_model_reasoning_efforts("glm-5.3-flash", provider_id="zai")
    assert set(efforts) == {"minimal", "low", "medium", "high", "xhigh", "max"}, (
        f"glm-5.3-flash must support the full reasoning_effort ladder; got {efforts!r}"
    )


def test_glm_5_3_flash_in_models_payload_for_zai_provider(tmp_path, monkeypatch):
    """GLM-5.3 Flash must appear in the /api/models payload when zai is active.

    This tests the actual observable behavior — what the WebUI model dropdown
    sees — not just module-level structures. It verifies that the catalog
    entries flow through get_available_models() to the frontend payload.

    The configured default is deliberately glm-5.2 (a different model), so
    presence of glm-5.3-flash in the zai group's models list proves catalog
    propagation from _PROVIDER_MODELS, not config echo.
    """
    import api.config as c

    cfgfile = tmp_path / "config.yaml"
    cfgfile.write_text(
        "model:\n  provider: zai\n  default: glm-5.2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(c, "_get_config_path", lambda: cfgfile)
    c.reload_config()

    # Mock list_available_providers to avoid real network calls
    fake_prov = mock.MagicMock()
    fake_prov.return_value = []
    try:
        import hermes_cli.models as hm
        monkeypatch.setattr(hm, "list_available_providers", fake_prov)
    except Exception:
        pass

    # Pin the agent-core catalog so the repo's static _PROVIDER_MODELS
    # fallback is exercised (see the matching stub rationale in
    # test_glm_5_3_catalog.py): this tests WebUI catalog propagation, not
    # the installed core version.
    try:
        import hermes_cli.models as hm
        monkeypatch.setattr(hm, "provider_model_ids", lambda _pid: [])
    except Exception:
        pass

    result = c.get_available_models()
    c.reload_config()

    # Find the zai group
    zai_group = None
    for group in result.get("groups", []):
        if group.get("provider_id") == "zai" or group.get("provider") == "zai":
            zai_group = group
            break

    assert zai_group is not None, (
        "No zai provider group found in get_available_models() output; "
        f"got providers: {[g.get('provider_id') or g.get('provider') for g in result.get('groups', [])]}"
    )

    # Check that glm-5.3-flash appears in the models list
    model_ids = [m["id"] for m in zai_group.get("models", [])]
    assert "glm-5.3-flash" in model_ids, (
        f"glm-5.3-flash missing from zai group models; got {model_ids}"
    )
