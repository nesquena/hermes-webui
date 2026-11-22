"""Regression tests: GLM-5.3 catalog and onboarding integration.

GLM-5.3 is Z.ai's current flagship model (successor to GLM-5.2). These tests
pin catalog presence (opt-in selection), fallback entry, reasoning ladder,
and payload propagation — ensuring the model users see matches what's declared
in api/config.py. The onboarding default deliberately REMAINS glm-5.1 until
Z.ai's direct API serves GLM-5.3 (Coding-Plan-only today; see #7017 review).
"""
import unittest.mock as mock

import pytest

import api.config as cfg
import api.onboarding as onboarding


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the TTL model cache before AND after every test.

    ``get_available_models()`` caches its result keyed on config.yaml mtime.
    Tests in this file repoint ``_get_config_path`` to a tmp_path, populate
    the cache there, then let monkeypatch restore the original path.  The
    cache, keyed on the tmp_path's mtime, then poisons downstream tests
    (e.g. test_model_resolver) which see stale data and never hit their
    mocks.  Clearing the cache around each test breaks that linkage.

    Also snapshots/restores ``c.cfg``, ``c._cfg_mtime``, ``c._cfg_path``,
    and ``c._cfg_fingerprint`` to prevent tests from leaking global config
    state to neighboring test files.
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


def test_glm_5_3_in_provider_models():
    """GLM-5.3 must appear in the zai provider catalog with the correct label."""
    zai_models = cfg._PROVIDER_MODELS.get("zai", [])
    model_ids = [m["id"] for m in zai_models]
    assert "glm-5.3" in model_ids, (
        f"glm-5.3 missing from zai provider models; got {model_ids}"
    )

    # Verify the exact label
    glm_5_3_entry = [m for m in zai_models if m["id"] == "glm-5.3"]
    assert len(glm_5_3_entry) == 1
    assert glm_5_3_entry[0]["label"] == "GLM-5.3", (
        f'Expected label "GLM-5.3", got {glm_5_3_entry[0]["label"]!r}'
    )


def test_glm_5_3_positioned_before_glm_5_2():
    """GLM-5.3 must appear BEFORE GLM-5.2 (lists are newest-first)."""
    zai_models = cfg._PROVIDER_MODELS.get("zai", [])
    glm_5_3_index = None
    glm_5_2_index = None

    for i, model in enumerate(zai_models):
        if model["id"] == "glm-5.3":
            glm_5_3_index = i
        elif model["id"] == "glm-5.2":
            glm_5_2_index = i
    assert glm_5_3_index is not None, "glm-5.3 not found in zai models"
    assert glm_5_2_index is not None, "glm-5.2 not found in zai models"
    assert glm_5_3_index < glm_5_2_index, (
        f"glm-5.3 (index {glm_5_3_index}) must appear before glm-5.2 (index {glm_5_2_index})"
    )


def test_glm_5_3_in_fallback_models():
    """GLM-5.3 must appear in _FALLBACK_MODELS with correct provider and label."""
    fallback_entries = [m for m in cfg._FALLBACK_MODELS if m["id"] == "zai/glm-5.3"]
    assert len(fallback_entries) == 1, (
        f"Expected exactly one zai/glm-5.3 entry in _FALLBACK_MODELS; "
        f"found {len(fallback_entries)}"
    )

    entry = fallback_entries[0]
    assert entry["provider"] == "Z.AI", (
        f'Expected provider "Z.AI", got {entry["provider"]!r}'
    )
    assert entry["label"] == "GLM-5.3", (
        f'Expected label "GLM-5.3", got {entry["label"]!r}'
    )


def test_glm_5_3_positioned_first_in_zai_fallback_block():
    """GLM-5.3 must appear as the FIRST Z.AI entry in _FALLBACK_MODELS."""
    zai_entries = [m for m in cfg._FALLBACK_MODELS if m["provider"] == "Z.AI"]
    assert len(zai_entries) > 0, "No Z.AI entries found in _FALLBACK_MODELS"

    first_zai_entry = zai_entries[0]
    assert first_zai_entry["id"] == "zai/glm-5.3", (
        f'Expected first Z.AI entry to be "zai/glm-5.3", got {first_zai_entry["id"]!r}'
    )


def test_zai_onboarding_default_stays_glm_5_1_until_direct_api_serves_glm_5_3():
    """Z.AI onboarding default must remain glm-5.1 until the direct endpoint serves GLM-5.3.

    The zai provider calls Z.ai's direct endpoint (api.z.ai/api/paas/v4), where the GLM-5.3
    API is not live yet. Z.ai's GLM-5.3 guide marks it "coming soon"; GLM-5.3 is
    Coding-Plan-only today. Defaulting direct-API users onto glm-5.3 would fail their first
    message with model-not-found, so the default stays glm-5.1 — bump it only after the
    direct endpoint serves GLM-5.3 (per the #7017 review gate).
    """
    zai_setup = onboarding._SUPPORTED_PROVIDER_SETUPS.get("zai", {})
    assert zai_setup, "zai setup not found in _SUPPORTED_PROVIDER_SETUPS"

    default_model = zai_setup.get("default_model")
    assert default_model == "glm-5.1", (
        f'Expected default_model "glm-5.1" (not glm-5.3), got {default_model!r}'
    )


def test_glm_5_3_in_zai_onboarding_models_list():
    """GLM-5.3 must appear in the zai onboarding setup's models list."""
    zai_setup = onboarding._SUPPORTED_PROVIDER_SETUPS.get("zai", {})
    assert zai_setup, "zai setup not found in _SUPPORTED_PROVIDER_SETUPS"

    models = zai_setup.get("models", [])
    model_ids = [m["id"] for m in models]
    assert "glm-5.3" in model_ids, (
        f"glm-5.3 missing from zai onboarding models list; got {model_ids}"
    )

    # Verify the exact label in the onboarding list
    glm_5_3_entry = [m for m in models if m["id"] == "glm-5.3"]
    assert len(glm_5_3_entry) == 1
    assert glm_5_3_entry[0]["label"] == "GLM-5.3", (
        f'Expected label "GLM-5.3" in onboarding, got {glm_5_3_entry[0]["label"]!r}'
    )


def test_glm_5_3_reasoning_efforts():
    """GLM-5.3 must support the full reasoning_effort ladder (GLM-5.2+ tier)."""
    efforts = cfg.resolve_model_reasoning_efforts("glm-5.3", provider_id="zai")
    assert set(efforts) == {"minimal", "low", "medium", "high", "xhigh", "max"}, (
        f"glm-5.3 must support the full reasoning_effort ladder; got {efforts!r}"
    )


def test_glm_5_3_in_models_payload_for_zai_provider(tmp_path, monkeypatch):
    """GLM-5.3 must appear in the /api/models payload when zai is active.

    This tests the actual observable behavior — what the WebUI model dropdown
    sees — not just module-level structures. It verifies that the catalog
    entries flow through get_available_models() to the frontend payload.

    The configured default is deliberately glm-5.2 (a different model), so
    presence of glm-5.3 in the zai group's models list proves catalog
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

    # Pin the agent-core catalog: get_available_models() sources the zai
    # model list from the INSTALLED hermes-cli core (via
    # _read_live_provider_model_ids -> provider_model_ids), not from the
    # repo's static _PROVIDER_MODELS. On a box whose installed core predates
    # glm-5.3 this test previously failed spuriously ("glm-5.3 missing from
    # zai group models"). Stub the core catalog to a deterministic empty
    # list so the repo's own _PROVIDER_MODELS fallback is exercised — this
    # tests WebUI catalog propagation, not the installed core version.
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

    # Check that glm-5.3 appears in the models list
    model_ids = [m["id"] for m in zai_group.get("models", [])]
    assert "glm-5.3" in model_ids, (
        f"glm-5.3 missing from zai group models; got {model_ids}"
    )

