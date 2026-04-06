"""
Tests for resolve_model_provider() model routing logic.
Verifies that model IDs are correctly resolved to (model, provider, base_url)
tuples for different provider configurations.
"""
import api.config as config


def _resolve_with_config(model_id, provider=None, base_url=None, default=None):
    """Helper: temporarily set config.cfg model section, call resolve, restore."""
    old_cfg = dict(config.cfg)
    model_cfg = {}
    if provider:
        model_cfg['provider'] = provider
    if base_url:
        model_cfg['base_url'] = base_url
    if default:
        model_cfg['default'] = default
    config.cfg['model'] = model_cfg if model_cfg else {}
    try:
        return config.resolve_model_provider(model_id)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


# ── OpenRouter prefix handling ────────────────────────────────────────────

def test_openrouter_free_keeps_full_path():
    """openrouter/free must NOT be stripped to 'free' when provider is openrouter."""
    model, provider, base_url = _resolve_with_config(
        'openrouter/free', provider='openrouter',
        base_url='https://openrouter.ai/api/v1',
    )
    assert model == 'openrouter/free', f"Expected 'openrouter/free', got '{model}'"
    assert provider == 'openrouter'


def test_openrouter_model_with_provider_prefix():
    """anthropic/claude-sonnet-4.6 via openrouter keeps full path."""
    model, provider, base_url = _resolve_with_config(
        'anthropic/claude-sonnet-4.6', provider='openrouter',
        base_url='https://openrouter.ai/api/v1',
    )
    assert model == 'anthropic/claude-sonnet-4.6'
    assert provider == 'openrouter'


# ── Direct provider prefix stripping ─────────────────────────────────────

def test_anthropic_prefix_stripped_for_direct_api():
    """anthropic/claude-sonnet-4.6 strips prefix when provider is anthropic."""
    model, provider, base_url = _resolve_with_config(
        'anthropic/claude-sonnet-4.6', provider='anthropic',
    )
    assert model == 'claude-sonnet-4.6'
    assert provider == 'anthropic'


def test_openai_prefix_stripped_for_direct_api():
    """openai/gpt-5.4-mini strips prefix when provider is openai."""
    model, provider, base_url = _resolve_with_config(
        'openai/gpt-5.4-mini', provider='openai',
    )
    assert model == 'gpt-5.4-mini'
    assert provider == 'openai'


# ── Cross-provider routing ───────────────────────────────────────────────

def test_cross_provider_routes_through_openrouter():
    """Picking openai model when config is anthropic routes via openrouter."""
    model, provider, base_url = _resolve_with_config(
        'openai/gpt-5.4-mini', provider='anthropic',
    )
    assert model == 'openai/gpt-5.4-mini'
    assert provider == 'openrouter'
    assert base_url is None  # openrouter uses its own endpoint


# ── Bare model names ─────────────────────────────────────────────────────

def test_bare_model_uses_config_provider():
    """A model name without / uses the config provider and base_url."""
    model, provider, base_url = _resolve_with_config(
        'gemma-4-26B', provider='custom',
        base_url='http://192.168.1.160:4000',
    )
    assert model == 'gemma-4-26B'
    assert provider == 'custom'
    assert base_url == 'http://192.168.1.160:4000'


def test_empty_model_returns_config_defaults():
    """Empty model string returns config provider and base_url."""
    model, provider, base_url = _resolve_with_config(
        '', provider='anthropic',
    )
    assert model == ''
    assert provider == 'anthropic'


# ── @provider:model hint routing (Issue #138 v2) ────────────────────────

def test_provider_hint_routes_to_specific_provider():
    """@minimax:MiniMax-M2.7 routes to minimax provider directly."""
    model, provider, base_url = _resolve_with_config(
        '@minimax:MiniMax-M2.7', provider='anthropic',
    )
    assert model == 'MiniMax-M2.7'
    assert provider == 'minimax'
    assert base_url is None  # resolve_runtime_provider will fill this


def test_provider_hint_zai():
    """@zai:GLM-5 routes to zai provider directly."""
    model, provider, base_url = _resolve_with_config(
        '@zai:GLM-5', provider='openai',
    )
    assert model == 'GLM-5'
    assert provider == 'zai'


def test_provider_hint_deepseek():
    """@deepseek:deepseek-chat routes to deepseek provider."""
    model, provider, base_url = _resolve_with_config(
        '@deepseek:deepseek-chat', provider='anthropic',
    )
    assert model == 'deepseek-chat'
    assert provider == 'deepseek'


def test_slash_prefix_non_default_still_routes_openrouter():
    """minimax/MiniMax-M2.7 (old format) still routes through openrouter."""
    model, provider, base_url = _resolve_with_config(
        'minimax/MiniMax-M2.7', provider='anthropic',
    )
    assert model == 'minimax/MiniMax-M2.7'
    assert provider == 'openrouter'


# ── get_available_models() @provider: hint behaviour ──────────────────────

def _available_models_with_provider(provider):
    """Helper: temporarily set active_provider in config."""
    old_cfg = dict(config.cfg)
    config.cfg['model'] = {'provider': provider}
    try:
        return config.get_available_models()
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


def test_non_default_provider_models_use_hint_prefix():
    """With anthropic as default, minimax model IDs should use @minimax: prefix."""
    result = _available_models_with_provider('anthropic')
    groups = {g['provider']: g['models'] for g in result['groups']}
    if 'MiniMax' in groups:
        for m in groups['MiniMax']:
            assert m['id'].startswith('@minimax:'), (
                f"Expected @minimax: prefix, got: {m['id']!r}"
            )


def test_default_provider_models_not_prefixed():
    """The active provider's models remain bare (no @prefix added)."""
    import api.config as _cfg
    raw_anthropic_ids = {m['id'] for m in _cfg._PROVIDER_MODELS.get('anthropic', [])}
    result = _available_models_with_provider('anthropic')
    groups = {g['provider']: g['models'] for g in result['groups']}
    if 'Anthropic' in groups:
        returned_ids = {m['id'] for m in groups['Anthropic']}
        for bare_id in raw_anthropic_ids:
            assert bare_id in returned_ids, (
                f"_PROVIDER_MODELS entry '{bare_id}' is missing from the Anthropic group"
            )
