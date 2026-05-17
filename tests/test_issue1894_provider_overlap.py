# Copyright 2025 the Hermes WebUI contributors
# SPDX-License-Identifier: MIT

# noqa: N801

# Regression tests for GitHub issue #1894.
#
# Symptom: when the WebUI's configured provider (e.g. `opencode-go`) and a
# `custom_providers[]` entry both expose the same bare model id (e.g.
# `deepseek-v4-pro`), the resolver was routing to `custom:<name>` instead of
# the configured `opencode-go` endpoint.
#
# Root cause: `resolve_model_provider()` in `api/config.py` guarded the custom-
# provider skip only when `model_id == model.default`.  If `model.default`
# was a different model (e.g. `glm-5.1`), the overlap was not detected and
# `deepseek-v4-pro` was matched against `custom_providers[]` first, routing
# the WebUI to the wrong endpoint.
#
# Fix: widen the guard so an explicit non-custom provider wins for any model
# it owns in `_PROVIDER_MODELS[config_provider]`.

from api.config import resolve_model_provider, model_with_provider_context


def _apply_config_overrides(cfg_module, overrides):
    old_model = cfg_module.cfg.get('model')
    cfg_module.cfg['model'] = {
        'provider': 'opencode-go',
        'default': 'glm-5.1',          # intentionally != the overlapping model
        **overrides,
    }
    old_custom = cfg_module.cfg.get('custom_providers')
    return old_model, old_custom


def _restore_config(cfg_module, old_model, old_custom):
    if old_model is None:
        cfg_module.cfg.pop('model', None)
    else:
        cfg_module.cfg['model'] = old_model
    if old_custom is None:
        cfg_module.cfg.pop('custom_providers', None)
    else:
        cfg_module.cfg['custom_providers'] = old_custom


# ---------------------------------------------------------------------------
# Case 1 — overlap: selected non-custom provider should win
# ---------------------------------------------------------------------------

def test_selected_opencode_go_wins_over_custom_provider_overlap():
    # opencode-go and a custom DeepSeek-compatible endpoint both serve
    # deepseek-v4-pro.  With opencode-go configured as the active provider,
    # selection of deepseek-v4-pro must route to opencode-go, not to the
    # custom endpoint.
    import api.config as cfg_mod
    old_model, old_custom = _apply_config_overrides(cfg_mod, {
        'base_url': 'https://api.opencode.ai/go/v1',
    })
    cfg_mod.cfg['custom_providers'] = [{
        'name': 'ds2api',
        'base_url': 'http://ds2api:5001/v1/',
        'models': {'deepseek-v4-pro': {}},
    }]
    try:
        # model_with_provider_context strips the prefix when config_provider
        # equals the selected provider — deepseek-v4-pro is passed bare.
        wrapped = model_with_provider_context('deepseek-v4-pro', 'opencode-go')
        model, provider, base_url = resolve_model_provider(wrapped)
        assert provider == 'opencode-go', (
            f'Expected provider=opencode-go, got provider={provider!r}. '
            f'WebUI was routed to custom provider instead.'
        )
        assert base_url == 'https://api.opencode.ai/go/v1', (
            f'Expected base_url from opencode-go config, got {base_url!r}'
        )
        assert model == 'deepseek-v4-pro'
    finally:
        _restore_config(cfg_mod, old_model, old_custom)


def test_selected_opencode_go_wins_direct_resolve():
    # Same scenario but bypassing model_with_provider_context to test the
    # resolver path directly with a bare model id.
    import api.config as cfg_mod
    old_model, old_custom = _apply_config_overrides(cfg_mod, {
        'base_url': 'https://api.opencode.ai/go/v1',
    })
    cfg_mod.cfg['custom_providers'] = [{
        'name': 'ds2api',
        'base_url': 'http://ds2api:5001/v1/',
        'models': {'deepseek-v4-pro': {}},
    }]
    try:
        model, provider, base_url = resolve_model_provider('deepseek-v4-pro')
        assert provider == 'opencode-go', (
            f'Expected provider=opencode-go, got provider={provider!r}'
        )
        assert base_url == 'https://api.opencode.ai/go/v1'
    finally:
        _restore_config(cfg_mod, old_model, old_custom)


# ---------------------------------------------------------------------------
# Case 2 — custom-only model: custom provider routing must stay intact
# ---------------------------------------------------------------------------

def test_custom_only_model_still_routes_to_custom_provider():
    # A model that exists only in a custom provider must still be routed
    # correctly when no explicit provider prefix is given.
    import api.config as cfg_mod
    old_model, old_custom = _apply_config_overrides(cfg_mod, {
        'base_url': 'https://api.opencode.ai/go/v1',
    })
    cfg_mod.cfg['custom_providers'] = [{
        'name': 'ds2api',
        'base_url': 'http://ds2api:5001/v1/',
        'models': {'my-private-model': {}},
    }]
    try:
        model, provider, base_url = resolve_model_provider('my-private-model')
        assert provider == 'custom:ds2api', (
            f'Expected provider=custom:ds2api, got provider={provider!r}'
        )
        assert base_url == 'http://ds2api:5001/v1/'
    finally:
        _restore_config(cfg_mod, old_model, old_custom)


# ---------------------------------------------------------------------------
# Case 3 — explicit custom provider selection still works
# ---------------------------------------------------------------------------

def test_explicit_custom_provider_selection_intact():
    # @custom:<name>:<model> syntax must not be swallowed by the new guard.
    model, provider, base_url = resolve_model_provider('@custom:ds2api:deepseek-v4-pro')
    assert provider == 'custom:ds2api', f'Expected provider=custom:ds2api, got {provider!r}'
    assert model == 'deepseek-v4-pro'


# ---------------------------------------------------------------------------
# Case 4 — existing suffix syntax is preserved
# ---------------------------------------------------------------------------

def test_openrouter_suffix_still_works():
    import api.config as cfg_mod
    old_model, old_custom = _apply_config_overrides(cfg_mod, {
        'provider': 'anthropic',          # non-openrouter so prefix is needed
        'default': 'claude-sonnet-4.6',
    })
    try:
        wrapped = model_with_provider_context('tencent/hy3-preview:free', 'openrouter')
        model, provider, _ = resolve_model_provider(wrapped)
        assert provider == 'openrouter'
        assert model == 'tencent/hy3-preview:free'
    finally:
        _restore_config(cfg_mod, old_model, old_custom)