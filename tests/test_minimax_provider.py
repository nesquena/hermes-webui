"""
Tests for MiniMax provider support in the model/provider discovery layer.

Covers:
  - MiniMax models appear in the fallback model list
  - MINIMAX_API_KEY env var is scanned and detected from os.environ
  - @minimax: provider hint routing works correctly
  - minimax/MiniMax-M2.7 (slash format) is routed via openrouter when active provider differs
"""
import os
import api.config as config


# ── Helper ────────────────────────────────────────────────────────────────────

def _resolve_with_config(model_id, provider=None, base_url=None):
    old_cfg = dict(config.cfg)
    model_cfg = {}
    if provider:
        model_cfg['provider'] = provider
    if base_url:
        model_cfg['base_url'] = base_url
    config.cfg['model'] = model_cfg if model_cfg else {}
    try:
        return config.resolve_model_provider(model_id)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


# ── Fallback model list ───────────────────────────────────────────────────────

def test_minimax_m2_7_in_fallback_models():
    """MiniMax-M2.7 must appear in the hardcoded fallback model list."""
    ids = [m['id'] for m in config._FALLBACK_MODELS]
    assert 'minimax/MiniMax-M2.7' in ids, (
        f"minimax/MiniMax-M2.7 missing from _FALLBACK_MODELS. Found: {ids}"
    )


def test_minimax_m2_7_highspeed_in_fallback_models():
    """MiniMax-M2.7-highspeed must appear in the hardcoded fallback model list."""
    ids = [m['id'] for m in config._FALLBACK_MODELS]
    assert 'minimax/MiniMax-M2.7-highspeed' in ids, (
        f"minimax/MiniMax-M2.7-highspeed missing from _FALLBACK_MODELS. Found: {ids}"
    )


def test_minimax_fallback_provider_label():
    """MiniMax fallback entries must use 'MiniMax' as the provider label."""
    minimax_entries = [m for m in config._FALLBACK_MODELS if 'minimax' in m['id'].lower()]
    assert minimax_entries, "No MiniMax entries found in _FALLBACK_MODELS"
    for entry in minimax_entries:
        assert entry['provider'] == 'MiniMax', (
            f"Expected provider='MiniMax', got '{entry['provider']}' for {entry['id']}"
        )


# ── _PROVIDER_MODELS ──────────────────────────────────────────────────────────

def test_minimax_provider_models_has_m2_7():
    """_PROVIDER_MODELS['minimax'] must include MiniMax-M2.7."""
    models = config._PROVIDER_MODELS.get('minimax', [])
    ids = [m['id'] for m in models]
    assert 'MiniMax-M2.7' in ids, (
        f"MiniMax-M2.7 missing from _PROVIDER_MODELS['minimax']. Found: {ids}"
    )


def test_minimax_provider_models_has_highspeed():
    """_PROVIDER_MODELS['minimax'] must include MiniMax-M2.7-highspeed."""
    models = config._PROVIDER_MODELS.get('minimax', [])
    ids = [m['id'] for m in models]
    assert 'MiniMax-M2.7-highspeed' in ids, (
        f"MiniMax-M2.7-highspeed missing from _PROVIDER_MODELS['minimax']. Found: {ids}"
    )


# ── MINIMAX_API_KEY env var detection ─────────────────────────────────────────

def test_minimax_api_key_in_env_scan_tuple():
    """MINIMAX_API_KEY must be included in the env var scan performed by
    get_available_models(), so users who export MINIMAX_API_KEY see the
    MiniMax provider in the dropdown without editing ~/.hermes/.env."""
    import inspect, ast, textwrap
    src = inspect.getsource(config.get_available_models)
    assert 'MINIMAX_API_KEY' in src, (
        "MINIMAX_API_KEY not found in get_available_models() source — "
        "it must be added to the env var scan tuple so os.environ is checked."
    )


def test_minimax_cn_api_key_in_env_scan_tuple():
    """MINIMAX_CN_API_KEY must also be scanned (mainland China API key variant)."""
    import inspect
    src = inspect.getsource(config.get_available_models)
    assert 'MINIMAX_CN_API_KEY' in src, (
        "MINIMAX_CN_API_KEY not found in get_available_models() source."
    )


def test_minimax_detected_from_os_environ(monkeypatch):
    """Setting MINIMAX_API_KEY in os.environ triggers minimax provider detection."""
    monkeypatch.setenv('MINIMAX_API_KEY', 'test-key-from-env')
    old_cfg = dict(config.cfg)
    # Clear model config so the env-var fallback path is exercised
    config.cfg['model'] = {}
    try:
        result = config.get_available_models()
        provider_names = [g['provider'] for g in result['groups']]
        assert 'MiniMax' in provider_names, (
            f"MiniMax not detected when MINIMAX_API_KEY is set in os.environ. "
            f"Active provider groups: {provider_names}"
        )
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


# ── Model routing ─────────────────────────────────────────────────────────────

def test_provider_hint_minimax_m2_7():
    """@minimax:MiniMax-M2.7 routes to minimax provider with bare model name."""
    model, provider, base_url = _resolve_with_config(
        '@minimax:MiniMax-M2.7', provider='anthropic',
    )
    assert model == 'MiniMax-M2.7'
    assert provider == 'minimax'
    assert base_url is None


def test_provider_hint_minimax_highspeed():
    """@minimax:MiniMax-M2.7-highspeed routes to minimax provider."""
    model, provider, base_url = _resolve_with_config(
        '@minimax:MiniMax-M2.7-highspeed', provider='openai',
    )
    assert model == 'MiniMax-M2.7-highspeed'
    assert provider == 'minimax'


def test_minimax_slash_format_routes_openrouter_when_not_active():
    """minimax/MiniMax-M2.7 (slash format) routes via openrouter when active
    provider is anthropic (cross-provider routing)."""
    model, provider, base_url = _resolve_with_config(
        'minimax/MiniMax-M2.7', provider='anthropic',
    )
    assert model == 'minimax/MiniMax-M2.7'
    assert provider == 'openrouter'
