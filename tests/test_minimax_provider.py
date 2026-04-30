"""
Tests for MiniMax provider support in the model/provider discovery layer.

Covers:
  - MiniMax models appear in the fallback model list
  - MINIMAX_API_KEY / MINIMAX_CN_API_KEY env vars are scanned and detected
  - @minimax: provider hint routing works correctly
  - minimax/MiniMax-M2.7 (slash format) is routed via openrouter when active provider differs
"""
import os
import pytest
import api.config as config


def _force_env_fallback(monkeypatch):
    """Force get_available_models() down the explicit env-var fallback path."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("hermes_cli.models", "hermes_cli.auth"):
            raise ImportError(name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _run_available_models_with_cfg(monkeypatch, tmp_path, cfg):
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    monkeypatch.setattr(config, "_get_config_path", lambda: tmp_path / "missing-config.yaml")
    config.cfg.clear()
    config.cfg.update(cfg)
    config._cfg_mtime = 0.0
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime


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


def test_minimax_cn_provider_models_match_hermes_agent_catalog():
    """minimax-cn must have its own static catalog so an empty config provider still shows models."""
    models = config._PROVIDER_MODELS.get('minimax-cn', [])
    ids = [m['id'] for m in models]
    assert ids == [
        'MiniMax-M2.7',
        'MiniMax-M2.5',
        'MiniMax-M2.1',
        'MiniMax-M2',
    ]
    assert config._PROVIDER_DISPLAY.get('minimax-cn') == 'MiniMax (China)'


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


def test_minimax_cn_detected_from_os_environ(monkeypatch, tmp_path):
    """MINIMAX_CN_API_KEY should show MiniMax (China), not the global MiniMax provider."""
    _force_env_fallback(monkeypatch)
    monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
    monkeypatch.setenv('MINIMAX_CN_API_KEY', 'test-cn-key-from-env')

    result = _run_available_models_with_cfg(monkeypatch, tmp_path, {'model': {}})
    groups = {g['provider_id']: g for g in result['groups']}

    assert 'minimax-cn' in groups, f"minimax-cn group missing: {groups.keys()}"
    assert groups['minimax-cn']['provider'] == 'MiniMax (China)'
    assert {m['id'] for m in groups['minimax-cn']['models']} == {
        'MiniMax-M2.7',
        'MiniMax-M2.5',
        'MiniMax-M2.1',
        'MiniMax-M2',
    }
    assert 'minimax' not in groups, (
        "MINIMAX_CN_API_KEY must not be collapsed into the global minimax provider"
    )


def test_minimax_cn_empty_config_provider_gets_static_models(monkeypatch, tmp_path):
    """providers.minimax-cn: {} should still render a populated model group."""
    _force_env_fallback(monkeypatch)
    monkeypatch.delenv('MINIMAX_API_KEY', raising=False)
    monkeypatch.delenv('MINIMAX_CN_API_KEY', raising=False)

    result = _run_available_models_with_cfg(
        monkeypatch,
        tmp_path,
        {
            'model': {'provider': 'minimax-cn', 'default': 'MiniMax-M2.7'},
            'providers': {'minimax-cn': {}},
        },
    )
    groups = {g['provider_id']: g for g in result['groups']}

    assert 'minimax-cn' in groups, f"minimax-cn group missing: {groups.keys()}"
    assert groups['minimax-cn']['models'], "minimax-cn group must not be empty"


def test_minimax_cn_key_can_be_managed_from_provider_settings():
    """Provider settings should use the Hermes Agent env var for minimax-cn."""
    from api.providers import _PROVIDER_ENV_VAR

    assert _PROVIDER_ENV_VAR.get('minimax-cn') == 'MINIMAX_CN_API_KEY'


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
