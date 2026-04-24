"""
Tests for resolve_model_provider() model routing logic.
Verifies that model IDs are correctly resolved to (model, provider, base_url)
tuples for different provider configurations.
"""
import pytest
import api.config as config


def _resolve_with_config(model_id, provider=None, base_url=None, default=None, custom_providers=None):
    """Helper: temporarily set config.cfg model/custom provider sections, call resolve, restore."""
    old_cfg = dict(config.cfg)
    model_cfg = {}
    if provider:
        model_cfg['provider'] = provider
    if base_url:
        model_cfg['base_url'] = base_url
    if default:
        model_cfg['default'] = default
    config.cfg['model'] = model_cfg if model_cfg else {}
    if custom_providers is not None:
        config.cfg['custom_providers'] = custom_providers
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


def test_custom_provider_model_with_slash_routes_to_named_custom_provider():
    """Slash-containing custom endpoint model IDs must not be mistaken for OpenRouter models."""
    model, provider, base_url = _resolve_with_config(
        'google/gemma-4-26b-a4b',
        provider='openrouter',
        base_url='https://openrouter.ai/api/v1',
        custom_providers=[{
            'name': 'Local LM Studio',
            'base_url': 'http://lmstudio.local:1234/v1',
            'model': 'google/gemma-4-26b-a4b',
        }],
    )
    assert model == 'google/gemma-4-26b-a4b'
    assert provider == 'custom:local-lm-studio'
    assert base_url == 'http://lmstudio.local:1234/v1'


# ── get_available_models() @provider: hint behaviour ──────────────────────


@pytest.fixture(autouse=True)
def _isolate_models_cache():
    """Invalidate the models TTL cache before and after every test in this file.

    Several helpers here mutate ``config.cfg`` in-memory and call
    ``get_available_models()``.  Without this guard, a prior test that called
    ``get_available_models()`` leaves a 60-second TTL cache entry; the next
    test that mutates cfg and calls the function gets a cache hit instead of
    running the function body, causing silently wrong results (e.g. the
    ``test_custom_endpoint_uses_model_config_api_key_for_model_discovery``
    ``KeyError: 'auth'`` on CI where ``urlopen`` is never reached).
    """
    try:
        config.invalidate_models_cache()
    except Exception:
        pass
    yield
    try:
        config.invalidate_models_cache()
    except Exception:
        pass


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


def test_no_duplicate_when_default_model_is_prefixed():
    """Issue #147 Bug 2: 'anthropic/claude-opus-4.6' as default_model must not
    inject a duplicate alongside the existing bare 'claude-opus-4.6' entry in
    the same provider group."""
    import api.config as _cfg
    old_cfg = dict(_cfg.cfg)
    _cfg.cfg['model'] = {
        'provider': 'anthropic',
        'default': 'anthropic/claude-opus-4.6',
    }
    try:
        result = _cfg.get_available_models()
        norm = lambda mid: mid.split('/', 1)[-1] if '/' in mid else mid
        # Check each group individually: no group should have two entries that
        # normalize to the same bare model name
        for g in result['groups']:
            bare_ids = [norm(m['id']) for m in g['models']]
            duplicates = [mid for mid in set(bare_ids) if bare_ids.count(mid) > 1]
            assert not duplicates, (
                f"Provider group '{g['provider']}' has duplicate models after normalization: "
                f"{duplicates}\nFull group: {[m['id'] for m in g['models']]}"
            )
    finally:
        _cfg.cfg.clear()
        _cfg.cfg.update(old_cfg)


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


# ── get_available_models(): phantom "Custom" group regression ─────────────
#
# When the user has model.provider set to a real provider (e.g. openai-codex)
# AND a model.base_url set, hermes_cli reports the 'custom' pseudo-provider as
# authenticated. The WebUI picker must NOT build a separate "Custom" group in
# that case — the base_url belongs to the active provider.

def _available_models_with_full_cfg(provider, default, base_url):
    """Helper: set model.provider, model.default, model.base_url at once.

    Clears model-override env vars (HERMES_MODEL, OPENAI_MODEL, LLM_MODEL)
    during the call so the real hermes profile environment doesn't leak into
    the test and override the fixture's default model.
    """
    import os
    import api.config as _cfg
    old_cfg = dict(_cfg.cfg)
    _cfg.cfg['model'] = {
        'provider': provider,
        'default': default,
        'base_url': base_url,
    }
    try:
        _cfg._cfg_mtime = _cfg.Path(_cfg._get_config_path()).stat().st_mtime
    except Exception:
        # No config.yaml on this machine (e.g. CI); pin to 0.0 so the mtime check
        # inside get_available_models() sees 0.0 == 0.0 and doesn't call reload_config(),
        # which would overwrite the in-memory cfg we just set up.
        _cfg._cfg_mtime = 0.0
    # Clear model-override env vars to prevent the real profile from leaking in
    _model_env_keys = ('HERMES_MODEL', 'OPENAI_MODEL', 'LLM_MODEL')
    _saved_env = {k: os.environ.pop(k, None) for k in _model_env_keys}
    try:
        return _cfg.get_available_models()
    finally:
        _cfg.cfg.clear()
        _cfg.cfg.update(old_cfg)
        for k, v in _saved_env.items():
            if v is not None:
                os.environ[k] = v


def test_no_phantom_custom_group_when_active_provider_is_set(monkeypatch):
    """Issue: with provider=openai-codex + base_url set, gpt-5.4 was landing
    under a phantom "Custom" group instead of the "OpenAI Codex" group."""
    import sys, types

    # Force hermes_cli to report both the real provider and the phantom
    # 'custom' as authenticated, simulating what list_available_providers()
    # returns when base_url is configured.
    fake_mod = types.ModuleType('hermes_cli.models')
    fake_mod.list_available_providers = lambda: [
        {'id': 'openai-codex', 'authenticated': True},
        {'id': 'custom',       'authenticated': True},
    ]
    fake_auth = types.ModuleType('hermes_cli.auth')
    fake_auth.get_auth_status = lambda pid: {'key_source': 'env'}
    monkeypatch.setitem(sys.modules, 'hermes_cli.models', fake_mod)
    monkeypatch.setitem(sys.modules, 'hermes_cli.auth', fake_auth)

    result = _available_models_with_full_cfg(
        provider='openai-codex',
        default='gpt-5.4',
        base_url='https://chatgpt.com/backend-api/codex',
    )
    group_names = [g['provider'] for g in result['groups']]
    assert 'Custom' not in group_names, (
        f"Phantom 'Custom' group present; full groups: {group_names}"
    )


def test_default_model_lands_under_active_provider_group(monkeypatch):
    """The configured default_model must appear under the active provider's
    display group, even when the model isn't in _PROVIDER_MODELS[provider]
    AND the active provider isn't the alphabetical first detected provider.

    Regression guard for a hyphen-vs-space bug in the "ensure default_model
    appears" post-pass: the substring check `active_provider.lower() in
    g.get('provider', '').lower()` was failing for 'openai-codex' vs
    display name 'OpenAI Codex' (hyphen vs. space), silently falling back
    to groups[0] — which, when another provider sorted earlier
    alphabetically (e.g. 'anthropic'), placed gpt-5.4 in the WRONG group.
    """
    import sys, types
    fake_mod = types.ModuleType('hermes_cli.models')
    fake_mod.list_available_providers = lambda: [
        {'id': 'anthropic',    'authenticated': True},  # sorts before openai-codex
        {'id': 'openai-codex', 'authenticated': True},
        {'id': 'custom',       'authenticated': True},
    ]
    fake_auth = types.ModuleType('hermes_cli.auth')
    fake_auth.get_auth_status = lambda pid: {'key_source': 'env'}
    monkeypatch.setitem(sys.modules, 'hermes_cli.models', fake_mod)
    monkeypatch.setitem(sys.modules, 'hermes_cli.auth', fake_auth)

    result = _available_models_with_full_cfg(
        provider='openai-codex',
        default='gpt-5.4',
        base_url='https://chatgpt.com/backend-api/codex',
    )
    groups = {g['provider']: [m['id'] for m in g['models']] for g in result['groups']}
    assert 'OpenAI Codex' in groups, f"OpenAI Codex group missing: {list(groups)}"
    norm = lambda mid: mid.split('/', 1)[-1].split(':', 1)[-1]
    assert 'gpt-5.4' in {norm(mid) for mid in groups['OpenAI Codex']}, (
        f"gpt-5.4 not in OpenAI Codex group; contents: {groups['OpenAI Codex']}"
    )
    # And crucially, it must NOT have landed in the alphabetically-first
    # group (Anthropic) via the fallback path.
    assert 'gpt-5.4' not in {norm(mid) for mid in groups.get('Anthropic', [])}, (
        f"gpt-5.4 leaked into Anthropic group via fallback: {groups.get('Anthropic')}"
    )


def test_unknown_providers_do_not_inherit_default_model(monkeypatch):
    """Detected providers without their own model catalog must not be filled
    with the global default_model placeholder.

    Regression guard for the bug where Alibaba / Minimax-Cn ended up showing
    gpt-5.4-mini even though those providers do not serve it.
    """
    import sys, types

    fake_mod = types.ModuleType('hermes_cli.models')
    fake_mod.list_available_providers = lambda: [
        {'id': 'openai-codex', 'authenticated': True},
        {'id': 'alibaba',      'authenticated': True},
        {'id': 'minimax-cn',   'authenticated': True},
    ]
    fake_auth = types.ModuleType('hermes_cli.auth')
    fake_auth.get_auth_status = lambda pid: {'key_source': 'env'}
    monkeypatch.setitem(sys.modules, 'hermes_cli.models', fake_mod)
    monkeypatch.setitem(sys.modules, 'hermes_cli.auth', fake_auth)

    result = _available_models_with_full_cfg(
        provider='openai-codex',
        default='gpt-5.4-mini',
        base_url='',
    )
    groups = {g['provider']: [m['id'] for m in g['models']] for g in result['groups']}
    norm = lambda mid: mid.split('/', 1)[-1].split(':', 1)[-1]

    assert 'Alibaba' not in groups, (
        f"Alibaba should not inherit the default model placeholder: {groups}"
    )
    assert 'Minimax-Cn' not in groups, (
        f"Minimax-Cn should not inherit the default model placeholder: {groups}"
    )
    assert not any(
        norm(mid) == 'gpt-5.4-mini'
        for mid in groups.get('Alibaba', []) + groups.get('Minimax-Cn', [])
    ), (
        f"Unknown provider groups still inherited the default model: {groups}"
    )


def test_custom_endpoint_uses_model_config_api_key_for_model_discovery(monkeypatch):
    """Custom endpoint model discovery must use model.api_key from config.yaml,
    not only environment variables, otherwise the dropdown collapses to the
    default model when /v1/models requires auth."""
    import json as _json
    import api.config as _cfg

    old_cfg = dict(_cfg.cfg)
    _cfg.cfg['model'] = {
        'provider': 'custom',
        'default': 'gpt-5.4',
        'base_url': 'https://example.test/v1',
        'api_key': 'sk-test-model-key',
    }
    try:
        _cfg._cfg_mtime = _cfg.Path(_cfg._get_config_path()).stat().st_mtime
    except Exception:
        # No config.yaml on this machine (e.g. CI); pin to 0.0 so the mtime check
        # inside get_available_models() sees 0.0 == 0.0 and skips reload_config().
        _cfg._cfg_mtime = 0.0
    _cfg.cfg.pop('providers', None)

    captured = {}

    class _Resp:
        def read(self):
            return _json.dumps({'data': [{'id': 'gpt-5.2', 'name': 'GPT-5.2'}]}).encode('utf-8')
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False

    def _fake_urlopen(req, timeout=10):
        captured['auth'] = req.get_header('Authorization')
        captured['ua'] = req.get_header('User-agent')
        return _Resp()

    monkeypatch.setattr('urllib.request.urlopen', _fake_urlopen)
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: [])
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('HERMES_API_KEY', raising=False)
    monkeypatch.delenv('HERMES_OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('LOCAL_API_KEY', raising=False)
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.delenv('API_KEY', raising=False)
    try:
        result = _cfg.get_available_models()
    finally:
        _cfg.cfg.clear()
        _cfg.cfg.update(old_cfg)

    assert captured['auth'] == 'Bearer sk-test-model-key'
    assert captured['ua'] == 'OpenAI/Python 1.0'
    groups = {g['provider']: [m['id'] for m in g['models']] for g in result['groups']}
    assert 'Custom' in groups
    assert 'gpt-5.2' in groups['Custom']


# -- Issue #230: custom provider with slash model name -----------------------

def test_custom_endpoint_slash_model_routes_to_custom_not_openrouter():
    """Regression test for #230.

    When provider=custom (or any non-openrouter provider) and base_url is set,
    a model name containing a slash (e.g. google/gemma-4-26b-a4b) must NOT be
    rerouted to OpenRouter -- it should stay on the configured custom endpoint.
    """
    # --- custom provider with slash model name should NOT go to openrouter ---
    model, provider, base_url = _resolve_with_config(
        'google/gemma-4-26b-a4b',
        provider='custom',
        base_url='http://127.0.0.1:1234/v1',
        default='google/gemma-4-26b-a4b',
    )
    assert provider.startswith('custom'), (
        "Expected provider starting with 'custom', got '{}'. "
        "Slash in model name should NOT trigger OpenRouter rerouting when base_url is set.".format(provider)
    )
    assert base_url == 'http://127.0.0.1:1234/v1', (
        "Expected base_url 'http://127.0.0.1:1234/v1', got '{}'.".format(base_url)
    )
    # Fix #433: provider prefix is now stripped for custom endpoints so stale
    # prefixed model IDs from previous sessions do not break custom endpoint routing.
    assert model == 'gemma-4-26b-a4b', (
        "Model name prefix should be stripped for custom base_url endpoint, got '{}'.".format(model)
    )

    # --- openrouter with slash model name MUST still route to openrouter -----
    model_or, provider_or, _ = _resolve_with_config(
        'google/gemma-4-26b-a4b',
        provider='openrouter',
        base_url='https://openrouter.ai/api/v1',
        default='google/gemma-4-26b-a4b',
    )
    assert provider_or == 'openrouter', (
        "Expected provider 'openrouter', got '{}'. "
        "Slash model via openrouter provider must still resolve to openrouter.".format(provider_or)
    )
    assert model_or == 'google/gemma-4-26b-a4b', (
        "Model name should be preserved for openrouter, got '{}'.".format(model_or)
    )
