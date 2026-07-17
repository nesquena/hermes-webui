"""
Tests for resolve_model_provider() model routing logic.
Verifies that model IDs are correctly resolved to (model, provider, base_url)
tuples for different provider configurations.
"""
import pytest
import api.config as config


def _resolve_with_config(model_id, provider=None, base_url=None, default=None, custom_providers=None,
                         explicitly_picked=False):
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
        return config.resolve_model_provider(model_id, explicitly_picked=explicitly_picked)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


def _resolve_with_catalog(model_id, advertised_ids, *, provider=None, base_url=None,
                          provider_id='custom', default=None, explicitly_picked=False):
    """Resolve with a seeded models-catalog snapshot (#5979 provenance).

    ``advertised_ids`` is the list of model ids the endpoint's own group
    advertised (what the user could have picked from the dropdown). Pass ``None``
    to simulate a COLD/unbuilt catalog. Seeds ``config._available_models_cache``
    (the snapshot ``_endpoint_advertised_model_ids`` reads) for the duration of
    the call, resets the derivation memo, and restores both afterwards so tests
    don't leak catalog state into each other.
    """
    old_cache = config._available_models_cache
    old_memo = config._advertised_model_ids_memo
    old_fp = config._available_models_cache_source_fingerprint
    old_prov = config._models_cache_provenance
    if advertised_ids is None:
        config._available_models_cache = None
    else:
        config._available_models_cache = {
            'groups': [{
                'provider_id': provider_id,
                'models': [{'id': mid, 'label': mid} for mid in advertised_ids],
            }]
        }
        # Stamp the source fingerprint exactly as the real publish sites do, so
        # the accessor's profile-isolation guard trusts this seeded snapshot.
        config._available_models_cache_source_fingerprint = config._models_cache_source_fingerprint()
    config._advertised_model_ids_memo = None  # force recompute against the seeded snapshot
    config._sync_models_cache_provenance()  # publish the atomic (snapshot, fingerprint) pair
    try:
        return _resolve_with_config(model_id, provider=provider, base_url=base_url, default=default,
                                    explicitly_picked=explicitly_picked)
    finally:
        config._available_models_cache = old_cache
        config._advertised_model_ids_memo = old_memo
        config._available_models_cache_source_fingerprint = old_fp
        config._models_cache_provenance = old_prov


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


# ── #3872: bare ``custom`` provider is a vendor-routing proxy — preserve the
#    full model id (the prefix is intrinsic). #433's redundant-prefix strip is
#    scoped to real first-party providers (provider=openai + proxy base_url),
#    which is covered by test_custom_endpoint_slash_model_routes_to_custom_not_openrouter.

def test_custom_remote_preserves_intrinsic_vendor_prefix_3872():
    """#3872: bedrock/opus-4-6 on a bare-custom remote proxy keeps its full id.

    A bare ``custom`` provider with a remote base_url is a vendor-routing proxy
    (LiteLLM, Bedrock gateway). ``bedrock/`` is an intrinsic routing segment the
    proxy needs whole; stripping it to ``opus-4-6`` makes the proxy return 403
    "model not allowed for your group". The proxy advertised the full id (the
    user picked it from the dropdown), so provenance preserves it.
    """
    model, provider, base_url = _resolve_with_catalog(
        'bedrock/opus-4-6',
        advertised_ids=['bedrock/opus-4-6'],
        provider='custom',
        base_url='https://router.example.com/v1',
    )
    assert model == 'bedrock/opus-4-6', f"intrinsic prefix must be preserved, got {model!r}"
    assert provider == 'custom'
    assert base_url == 'https://router.example.com/v1'


def test_custom_remote_strips_redundant_first_party_prefix_433():
    """#433: bare-custom remote proxy strips a prefix ONLY when the endpoint
    advertised just the BARE id (not the full ``vendor/model``).

    #433 is a verified real relay whose ``/v1/models`` returned bare ``gpt-5.4``
    and rejected ``openai/gpt-5.4`` (a stale cross-provider leftover). The strip
    is now justified by PROVENANCE — the catalog advertises exactly ``gpt-5.4``
    and NOT ``openai/gpt-5.4`` — instead of the old catalog-family guess that
    couldn't tell this stale-leftover case apart from #5979's advertised-full-id
    case. Behaviour is also pinned by
    test_sprint40_ui_polish.py::test_prefixed_model_stripped_for_custom_endpoint.
    """
    model, provider, base_url = _resolve_with_catalog(
        'openai/gpt-5.4',
        advertised_ids=['gpt-5.4'],  # relay advertises ONLY the bare id
        provider='custom',
        base_url='https://router.example.com/v1',
    )
    assert model == 'gpt-5.4', f"redundant first-party prefix must be stripped, got {model!r}"
    assert provider == 'custom'


def test_custom_remote_preserves_advertised_full_id_5979():
    """#5979 (the P0 regression): a custom proxy that advertises the FULL
    ``x-ai/grok-4.5`` must receive it whole — never the bare ``grok-4.5``.

    The old code stripped it because ``grok-4.5`` had graduated into the x-ai
    first-party catalog (agent commit 62ada5175), flipping ``_is_first_party_model``
    to True for a model the proxy routes on by its full ``x-ai/`` namespace. This
    is the exact HTTP 400 b3nw hit ("Invalid model format ... grok-4.5"). The
    provenance rule preserves it because the endpoint advertised the full id.
    """
    # Deterministically reproduce the data-driven trigger regardless of the
    # hermes-agent catalog version CI happens to run against: ensure grok-4.5 is
    # first-party of x-ai so _is_first_party_model('x-ai','grok-4.5') is True (the
    # condition under which the OLD code stripped). We stub it into the catalog
    # rather than asserting the live catalog already contains it.
    xai_catalog = list(config._PROVIDER_MODELS.get('x-ai') or [])
    had_grok = any(isinstance(m, dict) and m.get('id') == 'grok-4.5' for m in xai_catalog)
    old_xai = config._PROVIDER_MODELS.get('x-ai')
    if not had_grok:
        config._PROVIDER_MODELS['x-ai'] = xai_catalog + [{'id': 'grok-4.5', 'label': 'Grok 4.5'}]
    try:
        assert config._is_first_party_model('x-ai', 'grok-4.5'), (
            "precondition: grok-4.5 must be first-party of x-ai for this regression"
        )
        model, provider, base_url = _resolve_with_catalog(
            'x-ai/grok-4.5',
            advertised_ids=['x-ai/grok-4.5'],  # proxy advertises the FULL namespaced id
            provider='custom',
            base_url='https://proxy.example.com/v1',
        )
    finally:
        if not had_grok:
            if old_xai is None:
                config._PROVIDER_MODELS.pop('x-ai', None)
            else:
                config._PROVIDER_MODELS['x-ai'] = old_xai
    assert model == 'x-ai/grok-4.5', (
        f"advertised full id must be preserved for routing, got {model!r}"
    )
    assert provider == 'custom'
    assert base_url == 'https://proxy.example.com/v1'


def test_named_custom_slug_preserves_advertised_full_id_5979():
    """#5979 (named-custom variant): provider=custom:<slug> proxy advertising the
    full ``x-ai/grok-4.5`` also preserves it."""
    model, provider, base_url = _resolve_with_catalog(
        'x-ai/grok-4.5',
        advertised_ids=['x-ai/grok-4.5'],
        provider='custom:my-gateway',
        provider_id='custom:my-gateway',
        base_url='https://proxy.example.com/v1',
    )
    assert model == 'x-ai/grok-4.5', f"full id must be preserved for custom:slug, got {model!r}"


def test_custom_remote_cold_catalog_explicit_pick_preserves_5979():
    """#5979 (reopened): with a COLD catalog and no config declaration, an
    EXPLICITLY-PICKED custom-proxy id is preserved verbatim (the user chose it;
    the proxy routes on it), while an UNMARKED id gets the legacy strip.

    This is the persisted-explicit-pick resolution (Codex's mechanism, Nathan's
    call): the cold decision is no longer an unconditional preserve — it is
    gated on whether the user deliberately selected the model this session.
    """
    # explicitly picked → PRESERVE even though grok graduated into first-party x-ai
    picked, _, _ = _resolve_with_catalog(
        'openai/gpt-5.4', advertised_ids=None,
        provider='custom', base_url='https://router.example.com/v1',
        explicitly_picked=True,
    )
    assert picked == 'openai/gpt-5.4', f"explicit pick must preserve cold, got {picked!r}"
    # NOT picked (stale leftover) → legacy strip keeps #433 relay routing cold
    stale, _, _ = _resolve_with_catalog(
        'openai/gpt-5.4', advertised_ids=None,
        provider='custom', base_url='https://router.example.com/v1',
        explicitly_picked=False,
    )
    assert stale == 'gpt-5.4', f"unmarked stale id must strip cold (legacy #433), got {stale!r}"
    # intrinsic/unknown prefix → preserve regardless (no first-party family match)
    unknown, _, _ = _resolve_with_catalog(
        'zai-org/GLM-5.1', advertised_ids=None,
        provider='custom', base_url='https://api.deepinfra.com/v1/openai',
        explicitly_picked=False,
    )
    assert unknown == 'zai-org/GLM-5.1', f"cold unknown prefix must preserve, got {unknown!r}"


def test_custom_remote_config_declared_full_id_preserved_cold_5979():
    """#5979 cold-restart survival: even with a COLD catalog, a full vendor id
    the user DECLARED in config (model.default) is preserved — config is
    network-free provenance that outlives a process restart.
    """
    old_cache = config._available_models_cache
    old_memo = config._advertised_model_ids_memo
    old_fp = config._available_models_cache_source_fingerprint
    old_prov = config._models_cache_provenance
    old_cfg = dict(config.cfg)
    config._available_models_cache = None  # cold
    config._advertised_model_ids_memo = None
    config._sync_models_cache_provenance()  # publish the cold (None) provenance
    config.cfg['model'] = {
        'provider': 'custom',
        'default': 'x-ai/grok-4.5',  # user-declared full id
        'base_url': 'https://proxy.example.com/v1',
    }
    try:
        model, provider, _ = config.resolve_model_provider('x-ai/grok-4.5')
    finally:
        config._available_models_cache = old_cache
        config._advertised_model_ids_memo = old_memo
        config._available_models_cache_source_fingerprint = old_fp
        config._models_cache_provenance = old_prov
        config.cfg.clear()
        config.cfg.update(old_cfg)
    assert model == 'x-ai/grok-4.5', (
        f"config-declared full id must survive a cold catalog, got {model!r}"
    )
    assert provider == 'custom'


def test_custom_remote_prefers_full_id_when_both_advertised_5979():
    """When a proxy advertises BOTH the full ``x-ai/grok-4.5`` and a bare
    ``grok-4.5``, the exact full selection wins (preserve)."""
    model, _, _ = _resolve_with_catalog(
        'x-ai/grok-4.5',
        advertised_ids=['x-ai/grok-4.5', 'grok-4.5'],
        provider='custom',
        base_url='https://proxy.example.com/v1',
    )
    assert model == 'x-ai/grok-4.5', f"exact full selection must win, got {model!r}"


def test_custom_remote_extra_models_bucket_counts_as_advertised_5979():
    """Provenance must read BOTH catalog buckets. A relay's bare id sitting in
    ``extra_models`` (picker overflow) still counts as advertised, so the stale
    ``openai/gpt-5.4`` prefix is stripped (#433) even when ``models`` is full of
    OTHER ids and the bare id overflowed into ``extra_models``.
    """
    old_cache = config._available_models_cache
    old_memo = config._advertised_model_ids_memo
    old_fp = config._available_models_cache_source_fingerprint
    old_prov = config._models_cache_provenance
    config._available_models_cache = {
        'groups': [{
            'provider_id': 'custom',
            'models': [{'id': f'filler-{i}', 'label': f'filler-{i}'} for i in range(30)],
            'extra_models': [{'id': 'gpt-5.4', 'label': 'gpt-5.4'}],  # bare id overflowed here
        }]
    }
    config._available_models_cache_source_fingerprint = config._models_cache_source_fingerprint()
    config._advertised_model_ids_memo = None
    config._sync_models_cache_provenance()
    try:
        model, _, _ = _resolve_with_config(
            'openai/gpt-5.4', provider='custom', base_url='https://relay.example/v1',
        )
    finally:
        config._available_models_cache = old_cache
        config._advertised_model_ids_memo = old_memo
        config._available_models_cache_source_fingerprint = old_fp
        config._models_cache_provenance = old_prov
    assert model == 'gpt-5.4', f"bare id in extra_models must count as advertised, got {model!r}"


def test_custom_remote_foreign_profile_catalog_ignored_5979():
    """Profile-isolation fail-safe: when the catalog snapshot's source
    fingerprint does NOT match the current runtime (a concurrently-active
    foreign profile published it), that snapshot is NOT trusted for provenance —
    the id is preserved verbatim instead of stripped against another profile's
    catalog.

    Proof id: ``openai/gpt-5.4`` with the FOREIGN catalog advertising ONLY the
    bare ``gpt-5.4``, resolved as an EXPLICIT pick. If the foreign catalog were
    (wrongly) trusted, the bare-only-advertised rule (warm provenance, which
    wins over the pick flag) would strip → ``gpt-5.4``. Because the fingerprint
    mismatches, the snapshot is ignored and resolution takes the cold branch;
    with the explicit-pick flag set that preserves → ``openai/gpt-5.4``. The two
    outcomes genuinely diverge, so ``openai/gpt-5.4`` proves the foreign catalog
    was ignored.
    """
    old_cache = config._available_models_cache
    old_memo = config._advertised_model_ids_memo
    old_fp = config._available_models_cache_source_fingerprint
    old_prov = config._models_cache_provenance
    config._available_models_cache = {
        'groups': [{'provider_id': 'custom', 'models': [{'id': 'gpt-5.4', 'label': 'gpt-5.4'}]}]
    }
    config._available_models_cache_source_fingerprint = {'config_yaml': {'path': '/some/other/profile'}}
    config._advertised_model_ids_memo = None
    config._sync_models_cache_provenance()
    try:
        model, _, _ = _resolve_with_config(
            'openai/gpt-5.4', provider='custom', base_url='https://relay.example/v1',
            explicitly_picked=True,
        )
    finally:
        config._available_models_cache = old_cache
        config._advertised_model_ids_memo = old_memo
        config._available_models_cache_source_fingerprint = old_fp
        config._models_cache_provenance = old_prov
    assert model == 'openai/gpt-5.4', (
        f"foreign-profile catalog must be ignored (cold explicit-pick preserve), got {model!r}"
    )


def test_resolver_provenance_read_does_not_block_on_cache_lock_5979():
    """Regression: the resolver's per-send provenance read must be LOCK-FREE
    with respect to ``_available_models_cache_lock``.

    Codex found a deadlock in an earlier cut where the accessor acquired that
    lock: config-save (``_cfg_lock`` → cache lock) opposed catalog-refresh (cache
    lock → ``_cfg_lock``). The fix publishes an atomic ``(snapshot, fingerprint)``
    tuple the resolver reads with one lock-free load. Proof: hold the cache lock
    on one thread while another thread resolves — it must complete promptly, not
    block behind the held lock.
    """
    import threading
    import time as _time
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update({'model': {
        'provider': 'custom', 'default': 'x-ai/grok-4.5',
        'base_url': 'https://proxy.example/v1', 'models': {'x-ai/grok-4.5': {}},
    }})
    config.invalidate_models_cache()
    config.get_available_models()  # warm the catalog + publish provenance
    try:
        got = config._available_models_cache_lock.acquire(blocking=False)
        assert got, "precondition: could not take cache lock non-blocking"
        result = {}
        def _worker():
            t0 = _time.time()
            result['model'] = config.resolve_model_provider(
                config.model_with_provider_context('x-ai/grok-4.5', 'custom')
            )[0]
            result['elapsed'] = _time.time() - t0
        th = threading.Thread(target=_worker)
        th.start()
        th.join(timeout=5)
        blocked = th.is_alive()
        config._available_models_cache_lock.release()
        if blocked:
            th.join(timeout=5)
        assert not blocked, "DEADLOCK: resolver blocked on _available_models_cache_lock"
        assert result.get('model') == 'x-ai/grok-4.5', f"got {result.get('model')!r}"
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config.invalidate_models_cache()


def test_b3nw_cold_nondeclared_custom_slug_preserves_full_id_5979():
    """#5979 (reopened, b3nw's exact case): a NON-declared model actively
    selected on a ``custom:<slug>`` proxy is preserved on a COLD catalog.

    b3nw's config: ``provider: custom:llm-proxy``, ``model.default: x-ai/grok-4.5``,
    NO ``models[]`` allowlist. He picks ``x-ai/grok-composer-2.5-fast`` in the UI
    (not his default). On a cold send the old code stripped it to
    ``grok-composer-2.5-fast`` via the legacy family heuristic (the model had
    graduated into the x-ai first-party catalog), and his proxy 400'd. The flip
    to cold-preserve fixes it. grok-composer-2.5-fast is stubbed into
    ``_PROVIDER_MODELS['x-ai']`` (as test_custom_remote_preserves_advertised_full_id_5979
    does) so the first-party trigger is reproduced deterministically regardless
    of the agent catalog version.
    """
    mid = 'x-ai/grok-composer-2.5-fast'
    bare = 'grok-composer-2.5-fast'
    xai = list(config._PROVIDER_MODELS.get('x-ai') or [])
    had = any(isinstance(m, dict) and m.get('id') == bare for m in xai)
    old_xai = config._PROVIDER_MODELS.get('x-ai')
    old_cfg = dict(config.cfg)
    old_prov = config._models_cache_provenance
    old_cache = config._available_models_cache
    if not had:
        config._PROVIDER_MODELS['x-ai'] = xai + [{'id': bare, 'label': 'Grok Composer 2.5 Fast'}]
    config.cfg.clear()
    config.cfg.update({
        'model': {'default': 'x-ai/grok-4.5', 'provider': 'custom:llm-proxy',
                  'base_url': 'https://proxy.example/v1'},
        'custom_providers': [{'name': 'llm-proxy', 'base_url': 'https://proxy.example/v1',
                              'key_env': 'LLM_PROXY_API_KEY'}],
    })
    config._available_models_cache = None  # COLD
    config._advertised_model_ids_memo = None
    config._sync_models_cache_provenance()
    try:
        assert config._is_first_party_model('x-ai', bare), "precondition: stub failed"
        model, provider, _ = config.resolve_model_provider(mid, explicitly_picked=True)
    finally:
        if not had:
            if old_xai is None:
                config._PROVIDER_MODELS.pop('x-ai', None)
            else:
                config._PROVIDER_MODELS['x-ai'] = old_xai
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._models_cache_provenance = old_prov
        config._available_models_cache = old_cache
    assert model == mid, f"b3nw's non-declared cold custom-proxy pick must preserve, got {model!r}"
    assert provider == 'custom:llm-proxy'


def test_custom_slug_cold_stale_not_picked_still_strips_5979():
    """Companion to b3nw's case: the SAME cold custom:<slug> shape, but the id is
    NOT explicitly picked (a stale first-party leftover), still gets the legacy
    strip — so #433-style stale sessions keep routing when cold and only
    deliberate picks are preserved verbatim.
    """
    mid = 'x-ai/grok-composer-2.5-fast'
    bare = 'grok-composer-2.5-fast'
    xai = list(config._PROVIDER_MODELS.get('x-ai') or [])
    had = any(isinstance(m, dict) and m.get('id') == bare for m in xai)
    old_xai = config._PROVIDER_MODELS.get('x-ai')
    old_cfg = dict(config.cfg)
    old_prov = config._models_cache_provenance
    old_cache = config._available_models_cache
    if not had:
        config._PROVIDER_MODELS['x-ai'] = xai + [{'id': bare, 'label': 'Grok Composer 2.5 Fast'}]
    config.cfg.clear()
    config.cfg.update({
        'model': {'default': 'x-ai/grok-4.5', 'provider': 'custom:llm-proxy',
                  'base_url': 'https://proxy.example/v1'},
        'custom_providers': [{'name': 'llm-proxy', 'base_url': 'https://proxy.example/v1',
                              'key_env': 'LLM_PROXY_API_KEY'}],
    })
    config._available_models_cache = None  # COLD
    config._advertised_model_ids_memo = None
    config._sync_models_cache_provenance()
    try:
        model, _, _ = config.resolve_model_provider(mid, explicitly_picked=False)
    finally:
        if not had:
            if old_xai is None:
                config._PROVIDER_MODELS.pop('x-ai', None)
            else:
                config._PROVIDER_MODELS['x-ai'] = old_xai
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._models_cache_provenance = old_prov
        config._available_models_cache = old_cache
    assert model == bare, f"unmarked stale cold custom:slug id must strip (legacy), got {model!r}"


def test_warm_models_catalog_provenance_if_cold_publishes_from_disk_5979():
    """The send-path warm helper publishes provenance from a valid disk cache
    when memory is cold — restoring the endpoint-advertised signal so #433
    strips and #5979 preserves — WITHOUT a live rebuild.
    """
    old_cfg = dict(config.cfg)
    config.cfg.clear()
    config.cfg.update({
        'model': {'default': 'x-ai/grok-4.5', 'provider': 'custom:llm-proxy',
                  'base_url': 'https://proxy.example/v1'},
        'custom_providers': [{'name': 'llm-proxy', 'base_url': 'https://proxy.example/v1',
                              'key_env': 'LLM_PROXY_API_KEY',
                              'models': ['x-ai/grok-4.5', 'x-ai/grok-composer-2.5-fast']}],
    })
    try:
        # Build + persist to disk, then simulate a cold memory cache (restart).
        config.invalidate_models_cache()
        config.get_available_models()  # publishes to memory + disk
        assert config._models_cache_provenance is not None
        # Simulate cold memory but valid disk cache (do NOT delete disk).
        config._available_models_cache = None
        config._advertised_model_ids_memo = None
        config._sync_models_cache_provenance()
        assert config._models_cache_provenance is None, "precondition: memory cold"
        # The warm helper should republish provenance from disk (network-free).
        config.warm_models_catalog_provenance_if_cold()
        assert config._models_cache_provenance is not None, (
            "warm helper must publish provenance from the disk cache"
        )
        adv = config._endpoint_advertised_model_ids('custom:llm-proxy')
        assert adv and 'x-ai/grok-composer-2.5-fast' in adv, (
            f"warmed provenance must carry the endpoint-advertised ids, got {adv!r}"
        )
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config.invalidate_models_cache()


def test_warm_models_catalog_provenance_noop_when_already_warm_5979():
    """The warm helper is a cheap no-op when provenance for the CURRENT profile
    is already published — it must not reload or mutate a warm catalog.
    """
    # Use the CURRENT runtime fingerprint so the profile-match fast path is hit.
    current_fp = config._models_cache_source_fingerprint()
    sentinel = ({'groups': []}, current_fp)
    old_prov = config._models_cache_provenance
    config._models_cache_provenance = sentinel
    try:
        config.warm_models_catalog_provenance_if_cold()
        assert config._models_cache_provenance is sentinel, (
            "warm helper must not touch already-warm current-profile provenance"
        )
    finally:
        config._models_cache_provenance = old_prov


def test_warm_models_catalog_provenance_ignores_foreign_profile_resident_5979(monkeypatch):
    """Profile isolation (Codex finding): a FOREIGN profile's resident provenance
    must NOT satisfy the fast no-op. The helper falls through, loads THIS
    profile's own disk snapshot, and republishes provenance stamped with the
    current runtime fingerprint — so a concurrently-active profile's catalog can
    never block this profile from warming its own.
    """
    foreign = ({'groups': [{'provider_id': 'custom', 'models': [{'id': 'gpt-5.4'}]}]},
               {'config_yaml': {'path': '/some/other/profile'}})
    my_disk = {'groups': [{'provider_id': 'custom:llm-proxy',
                           'models': [{'id': 'x-ai/grok-composer-2.5-fast'}]}]}
    old_prov = config._models_cache_provenance
    old_cache = config._available_models_cache
    old_fp = config._available_models_cache_source_fingerprint
    old_memo = config._advertised_model_ids_memo
    config._models_cache_provenance = foreign
    monkeypatch.setattr(config, '_load_models_cache_from_disk', lambda: my_disk)
    try:
        config.warm_models_catalog_provenance_if_cold()
        prov = config._models_cache_provenance
        assert prov is not None and prov[0] is my_disk, (
            "warm must replace foreign provenance with THIS profile's disk snapshot"
        )
        assert prov[1] == config._models_cache_source_fingerprint(), (
            "republished provenance must carry the current profile's fingerprint"
        )
    finally:
        config._models_cache_provenance = old_prov
        config._available_models_cache = old_cache
        config._available_models_cache_source_fingerprint = old_fp
        config._advertised_model_ids_memo = old_memo


def test_warm_models_catalog_provenance_never_live_rebuilds_5979(monkeypatch):
    """The warm helper must NEVER call get_available_models (which can acquire
    the cache lock and block on / trigger a rebuild). It reads the disk cache
    directly, so a live provider probe is structurally impossible on the send
    path.
    """
    old_prov = config._models_cache_provenance
    config._models_cache_provenance = None
    called = {'get_available_models': False}
    def _boom(*a, **k):
        called['get_available_models'] = True
        raise AssertionError("warm must not call get_available_models")
    monkeypatch.setattr(config, 'get_available_models', _boom)
    try:
        config.warm_models_catalog_provenance_if_cold()  # must not raise / call the above
    finally:
        config._models_cache_provenance = old_prov
    assert called['get_available_models'] is False, (
        "warm helper must read disk directly, never via get_available_models"
    )


def test_warm_models_catalog_provenance_nonblocking_when_lock_held_5979():
    """The warm helper must NOT block on the models-cache lock. It tries the
    lock non-blocking and returns immediately when a concurrent build/publish
    holds it, so a send can never hang up to 60s behind a rebuild.
    """
    import threading
    import time as _time
    old_prov = config._models_cache_provenance
    config._models_cache_provenance = None
    got = config._available_models_cache_lock.acquire(blocking=False)
    assert got, "precondition: could not take cache lock"
    try:
        result = {}
        def _worker():
            t0 = _time.time()
            config.warm_models_catalog_provenance_if_cold()
            result['elapsed'] = _time.time() - t0
        th = threading.Thread(target=_worker)
        th.start()
        th.join(timeout=5)
        blocked = th.is_alive()
    finally:
        config._available_models_cache_lock.release()
        config._models_cache_provenance = old_prov
    if blocked:
        th.join(timeout=5)
    assert not blocked, "warm helper blocked on a held cache lock (must be non-blocking)"
    assert result.get('elapsed', 99) < 2.0, (
        f"warm helper should return promptly when lock held, took {result.get('elapsed')!r}s"
    )


def test_custom_remote_preserves_unknown_prefix_548():
    """#548: an unknown vendor prefix (zai-org/GLM-5.1) is always preserved.

    The proxy advertised the full id; ``zai-org`` isn't in _PROVIDER_MODELS so
    even the bare-advertised belt could never strip it.
    """
    model, provider, base_url = _resolve_with_catalog(
        'zai-org/GLM-5.1',
        advertised_ids=['zai-org/GLM-5.1'],
        provider='custom',
        base_url='https://api.deepinfra.com/v1/openai',
    )
    assert model == 'zai-org/GLM-5.1', f"unknown prefix must be preserved, got {model!r}"
    assert provider == 'custom'


def test_named_custom_slug_preserves_intrinsic_vendor_prefix_3872():
    """#3872 (named-custom variant): provider=custom:<slug> + remote base_url also
    preserves an intrinsic vendor prefix the endpoint advertised whole.
    """
    model, provider, base_url = _resolve_with_catalog(
        'bedrock/opus-4-6',
        advertised_ids=['bedrock/opus-4-6'],
        provider='custom:my-gateway',
        provider_id='custom:my-gateway',
        base_url='https://router.example.com/v1',
    )
    assert model == 'bedrock/opus-4-6', f"intrinsic prefix must be preserved for custom:slug, got {model!r}"


def test_first_party_provider_proxy_still_strips_prefix_433():
    """#433/dc2334c5: provider=openai + remote proxy still strips the prefix.

    This is the deliberate behaviour the #3872 fix must NOT regress: a real
    first-party provider pointed at an OpenAI-compatible proxy expects the bare
    id. (Mirrors the public-host branch of
    test_custom_endpoint_slash_model_routes_to_custom_not_openrouter.)
    """
    model, provider, base_url = _resolve_with_config(
        'openai/gpt-5.4',
        provider='openai',
        base_url='https://litellm.example.com/v1',
    )
    assert model == 'gpt-5.4', f"redundant first-party prefix must be stripped, got {model!r}"


def test_custom_provider_models_dict_routes_to_named_custom_provider():
    """Models listed only under custom_providers[].models still route to that endpoint."""
    model, provider, base_url = _resolve_with_config(
        'sensenova-6.7-flash-lite',
        provider='xiaomi',
        custom_providers=[{
            'name': 'LiteLLM Proxy',
            'base_url': 'http://127.0.0.1:8080/v1',
            'model': 'deepseek-v4-flash',
            'models': {
                'deepseek-v4-flash': {},
                'sensenova-6.7-flash-lite': {},
            },
        }],
    )
    assert model == 'sensenova-6.7-flash-lite'
    assert provider == 'custom:litellm-proxy'
    assert base_url == 'http://127.0.0.1:8080/v1'


def test_custom_provider_models_string_list_routes_to_named_custom_provider_6121():
    """#6121: picker-visible string-list models must route to their custom provider."""
    model, provider, base_url = _resolve_with_config(
        'qwen3.6:35b-a3b',
        provider='anthropic',
        default='claude-haiku-4-5',
        custom_providers=[{
            'name': 'storm-ollama',
            'base_url': 'http://ollama.test:11434/v1',
            'models': ['qwen3.6:35b-a3b', 'violet-lotus:latest'],
        }],
    )
    assert model == 'qwen3.6:35b-a3b'
    assert provider == 'custom:storm-ollama'
    assert base_url == 'http://ollama.test:11434/v1'


def test_custom_provider_models_object_list_routes_to_named_custom_provider_6121():
    """#6121: resolver accepts the same object-list ids as the model picker."""
    for model_entry in (
        {'id': 'picker-model-id'},
        {'model': 'picker-model-name'},
        {'name': 'picker-model-label'},
    ):
        requested_model = next(iter(model_entry.values()))
        model, provider, base_url = _resolve_with_config(
            requested_model,
            provider='anthropic',
            default='claude-haiku-4-5',
            custom_providers=[{
                'name': 'Picker Parity',
                'base_url': 'https://models.example.test/v1',
                'models': [model_entry],
            }],
        )
        assert model == requested_model
        assert provider == 'custom:picker-parity'
        assert base_url == 'https://models.example.test/v1'


# ── Issue #2047: parenthesized local provider names with ports ────────────

def test_custom_provider_name_with_parenthesized_port_uses_safe_slug():
    """Setup-generated names like 'Local (host:port)' must not leak ':' into slugs."""
    model, provider, base_url = _resolve_with_config(
        'deepseek-v4-flash',
        provider='custom',
        custom_providers=[{
            'name': 'Local (127.0.0.1:15721)',
            'base_url': 'http://127.0.0.1:15721/v1',
            'model': 'deepseek-v4-flash',
        }],
    )
    assert model == 'deepseek-v4-flash'
    assert provider == 'custom:local-127.0.0.1-15721'
    assert base_url == 'http://127.0.0.1:15721/v1'


def test_safe_custom_provider_hint_keeps_model_after_port_slug():
    """The safe slug emitted by the picker must parse back without corrupting the model."""
    model, provider, base_url = _resolve_with_config(
        '@custom:local-127.0.0.1-15721:deepseek-v4-flash',
        provider='custom',
    )
    assert model == 'deepseek-v4-flash'
    assert provider == 'custom:local-127.0.0.1-15721'
    assert base_url is None


# ── Issue #1922: default model shadowed by overlapping custom_providers[] ──

def test_default_model_not_shadowed_by_overlapping_custom_provider():
    r'''Regression test for #1922.

    When the active provider is an explicit non-custom provider (e.g. ai-gateway,
    openrouter, xiaomi) AND the requested model_id matches the configured default
    model, the active provider's base_url must take precedence over an overlapping
    custom_providers[] entry. Otherwise the WebUI routes to 'custom:<name>' with
    the wrong endpoint, causing 401 errors.

    This test mirrors the reported scenario:
      - provider: ai-gateway
      - base_url: https://api.ai-gateway.example/v1
      - default: gpt-5.4
      - An overlapping custom_providers[] entry with the same default model

    Expected: active provider (ai-gateway) wins over custom provider.
    '''
    model, provider, base_url = _resolve_with_config(
        'gpt-5.4',
        provider='ai-gateway',
        base_url='https://api.ai-gateway.example/v1',
        default='gpt-5.4',
        custom_providers=[{
            'name': 'My Custom Endpoint',
            'base_url': 'http://localhost:8080/v1',
            'model': 'gpt-5.4',
        }],
    )
    assert model == 'gpt-5.4', f'Expected model=gpt-5.4, got {model!r}'
    assert provider == 'ai-gateway', f'Expected provider=ai-gateway, got {provider!r}'
    assert base_url == 'https://api.ai-gateway.example/v1', f'Expected base_url from active provider, got {base_url!r}'


def test_default_model_shadowed_with_xiaomi_provider():
    r'''Same regression test with provider=xiaomi instead of ai-gateway.'''
    model, provider, base_url = _resolve_with_config(
        'deepseek-v4-flash',
        provider='xiaomi',
        default='deepseek-v4-flash',
        custom_providers=[{
            'name': 'LiteLLM Proxy',
            'base_url': 'http://127.0.0.1:8080/v1',
            'model': 'deepseek-v4-flash',
        }],
    )
    assert model == 'deepseek-v4-flash'
    assert provider == 'xiaomi'
    assert base_url is None  # xiaomi has no config base_url in this test


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


def test_default_provider_models_not_prefixed(monkeypatch):
    """The active provider's models remain bare (no @prefix added)."""
    import api.config as _cfg
    monkeypatch.setattr(_cfg, "_read_live_provider_model_ids", lambda pid: ["claude-sonnet-5.0"] if pid == "anthropic" else [])
    result = _available_models_with_provider('anthropic')
    groups = {g['provider']: g['models'] for g in result['groups']}
    if 'Anthropic' in groups:
        returned_ids = {m['id'] for m in groups['Anthropic']}
        assert "claude-sonnet-5.0" in returned_ids
        assert not any(mid.startswith('@anthropic:') for mid in returned_ids), returned_ids


def test_provider_config_object_list_catalog_uses_picker_supported_keys_6121(monkeypatch):
    """Provider allowlists in the live catalog accept id/model/name object keys."""
    import api.config as _cfg
    old_cfg = dict(_cfg.cfg)
    _cfg.cfg['model'] = {
        'provider': 'myprov',
        'default': 'by-model-key',
    }
    _cfg.cfg['providers'] = {
        'myprov': {
            'models': [
                {'model': 'by-model-key', 'label': 'By Model Key'},
                {'name': 'by-name-key'},
                {'id': 'by-id-key', 'label': 'By Id Key'},
            ],
        },
    }
    try:
        _cfg._cfg_mtime = _cfg.Path(_cfg._get_config_path()).stat().st_mtime
    except Exception:
        _cfg._cfg_mtime = 0.0
    monkeypatch.setattr(_cfg, "_read_live_provider_model_ids", lambda pid: [])
    try:
        result = _cfg.get_available_models()
    finally:
        _cfg.cfg.clear()
        _cfg.cfg.update(old_cfg)

    groups = {group['provider_id']: group['models'] for group in result['groups']}
    model_rows = groups.get('myprov') or []
    model_ids = [row['id'] for row in model_rows]
    assert 'by-model-key' in model_ids
    assert 'by-name-key' in model_ids
    assert 'by-id-key' in model_ids
    labels = {row['id']: row['label'] for row in model_rows}
    assert labels['by-model-key'] == 'By Model Key'
    assert labels['by-id-key'] == 'By Id Key'


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

    Regression guard for the bug where unknown providers ended up showing
    gpt-5.4-mini even though those providers do not serve it. Minimax-Cn is
    now known and should show its own catalog instead.
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
    assert 'MiniMax (China)' in groups, (
        f"Minimax-Cn should render its own static catalog: {groups}"
    )
    assert not any(
        norm(mid) == 'gpt-5.4-mini'
        for mid in groups.get('Alibaba', []) + groups.get('MiniMax (China)', [])
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
        url = getattr(req, 'full_url', '')
        if 'example.test' in url:
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
    # Model ID may be prefixed with @provider: due to cross-provider dedup (#1228)
    assert any('gpt-5.2' in m for m in groups['Custom']), f'gpt-5.2 not found in Custom: {groups}'


# -- Issue #230: custom provider with slash model name -----------------------

def test_custom_endpoint_slash_model_routes_to_custom_not_openrouter():
    """Regression test for #230, updated for #1625.

    When provider=custom (or any non-openrouter provider) and base_url is set,
    a model name containing a slash (e.g. google/gemma-4-26b-a4b) must NOT be
    rerouted to OpenRouter -- it should stay on the configured custom endpoint.

    #1625 layered an additional rule on top: a base_url pointing at a loopback
    or private-IP host is treated as a local model server (LM Studio, Ollama,
    llama.cpp, vLLM, TabbyAPI), which register models under their full
    HuggingFace path. On such hosts the prefix is now PRESERVED. The original
    #433 strip behaviour still applies on public hosts (real OpenAI-compatible
    proxies like LiteLLM at https://litellm.example.com/v1).
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
    # #1625 (supersedes the v0.50 #433 strip-on-custom rule for loopback hosts):
    # 127.0.0.1 base_url is almost certainly a local LM Studio / Ollama / etc.,
    # which keys models on the full HuggingFace path. Preserve the prefix.
    assert model == 'google/gemma-4-26b-a4b', (
        "Model name prefix must be PRESERVED on loopback base_url (#1625), got '{}'.".format(model)
    )

    # --- public-host openai-compatible proxy STILL strips per #433 ----------
    model2, provider2, base_url2 = _resolve_with_config(
        'google/gemma-4-26b-a4b',
        provider='openai',
        base_url='https://litellm.example.com/v1',
        default='google/gemma-4-26b-a4b',
    )
    assert model2 == 'gemma-4-26b-a4b', (
        "Public-host OpenAI-compat proxy must still strip prefix per #433, got '{}'.".format(model2)
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


# ── #4210: custom provider (no base_url) must not be hijacked to openrouter
#    when the model id has a known-provider prefix (sibling of #3872, which
#    only covered the base_url-set variant). Bug-report case 1.

def test_custom_provider_no_base_url_with_known_prefix_keeps_custom_and_full_id_4210():
    """#4210: provider=custom:llm-proxy (no base_url) + 'x-ai/grok-2' must NOT
    be redirected to openrouter. The prefix is intrinsic to the custom proxy's
    routing; the user did not pick anything from the OpenRouter dropdown."""
    model, provider, base_url = _resolve_with_config(
        'x-ai/grok-2',
        provider='custom:llm-proxy',
        default='x-ai/grok-2',
    )
    assert provider == 'custom:llm-proxy', (
        "Custom provider must not be hijacked to openrouter when no base_url is "
        "set; got provider={!r} model={!r}".format(provider, model)
    )
    assert model == 'x-ai/grok-2', (
        "Custom provider must preserve the full model id; got model={!r}".format(model)
    )
    assert base_url is None


def test_bare_custom_provider_no_base_url_with_known_prefix_keeps_custom_and_full_id_4210():
    """#4210 sibling: bare 'custom' (no 'custom:<slug>') with no base_url
    must also not be hijacked to openrouter for a known-prefix model id."""
    model, provider, base_url = _resolve_with_config(
        'google/gemma-2-9b',
        provider='custom',
        default='google/gemma-2-9b',
    )
    assert provider == 'custom', (
        "Bare 'custom' provider must not be hijacked to openrouter when no "
        "base_url is set; got provider={!r}".format(provider)
    )
    assert model == 'google/gemma-2-9b'
    assert base_url is None


# ── providers: (config.yaml user-defined provider) scan (#5511) ─────────────

def _resolve_with_providers(model_id, providers_cfg, *, provider=None, default=None):
    """Helper: temporarily set config.cfg['providers'] + model, call resolve, restore."""
    old_cfg = dict(config.cfg)
    model_cfg = {}
    if provider:
        model_cfg['provider'] = provider
    if default:
        model_cfg['default'] = default
    config.cfg['model'] = model_cfg
    config.cfg['providers'] = providers_cfg
    try:
        return config.resolve_model_provider(model_id)
    finally:
        config.cfg.clear()
        config.cfg.update(old_cfg)


def test_providers_scan_routes_user_defined_allowlist_5511():
    """A user-defined providers.<slug>.models allowlist routes a bare model id
    to that provider (the feature #5511 adds)."""
    model, provider, base_url = _resolve_with_providers(
        'my-model-1',
        {'myprov': {'base_url': 'https://my.example/v1', 'models': ['my-model-1', 'my-model-2']}},
        provider='openai',
        default='gpt-5',
    )
    assert provider == 'myprov', f"user-defined provider allowlist must route; got {provider!r}"
    assert model == 'my-model-1'
    assert base_url == 'https://my.example/v1'


def test_providers_scan_skips_copilot_settings_map_5511():
    """providers.copilot.models is a per-model SETTINGS map, NOT a routable
    allowlist — a Copilot per-model settings entry must NOT hijack routing away
    from the model's real provider (#5511 gate-cert CORE finding)."""
    model, provider, base_url = _resolve_with_providers(
        'gpt-5',
        {'copilot': {'models': {'gpt-5': {'reasoning_effort': 'high'}}}},
        provider='openai',
        default='gpt-5',
    )
    assert provider == 'openai', (
        "Copilot settings-map entry must NOT hijack routing; "
        f"gpt-5 must stay on openai, got {provider!r}"
    )
    assert model == 'gpt-5'


def test_providers_scan_copilot_list_shape_also_skipped_5511():
    """Defense in depth: even if providers.copilot.models is a list shape, the
    Copilot exclusion still prevents a routing hijack."""
    model, provider, base_url = _resolve_with_providers(
        'gpt-5',
        {'copilot': {'models': ['gpt-5', 'gpt-5-mini']}},
        provider='openai',
        default='gpt-5',
    )
    assert provider == 'openai', (
        f"Copilot (list shape) must not hijack routing; got {provider!r}"
    )
    assert model == 'gpt-5'


def test_providers_scan_honors_active_provider_ownership_5511():
    """When the active provider owns the model (it's the configured default),
    another provider's overlapping providers.<slug>.models entry must NOT hijack
    routing away from the active provider (#5511 gate finding — active ai-gateway
    + default gpt-5 was being pulled to providers.openai.models.gpt-5)."""
    model, provider, base_url = _resolve_with_providers(
        'gpt-5',
        {'openai': {'models': ['gpt-5']}},
        provider='ai-gateway',
        default='gpt-5',
    )
    assert provider == 'ai-gateway', (
        "active provider that owns the default model must keep routing; "
        f"gpt-5 must stay on ai-gateway, got {provider!r}"
    )
    assert model == 'gpt-5'


def test_providers_scan_active_provider_own_entry_still_matches_5511():
    """The ownership guard still lets the ACTIVE provider's own providers: entry
    match (e.g. active myprov + a model in providers.myprov.models resolves to
    myprov with its base_url)."""
    model, provider, base_url = _resolve_with_providers(
        'gpt-5',
        {'myprov': {'base_url': 'https://my.example/v1', 'models': ['gpt-5']}},
        provider='myprov',
        default='gpt-5',
    )
    assert provider == 'myprov', f"active provider's own entry must match; got {provider!r}"
    assert base_url == 'https://my.example/v1'


def test_providers_scan_ownership_guard_canonicalises_aliased_active_provider_5511():
    """An ALIASED active provider (e.g. 'z-ai' → canonical 'zai') must still be
    recognized as owning its catalog models, so another providers.<slug>.models
    entry can't hijack an active-owned model (#5511 latent-bug gate finding —
    _provider_models_set was built with the raw alias, missing the canonical
    _PROVIDER_MODELS key, so the ownership guard silently failed)."""
    import api.config as config
    # Pick a real catalog model id owned by the canonical 'zai' provider.
    zai_models = config._PROVIDER_MODELS.get('zai') or []
    zai_ids = [m.get('id') for m in zai_models if isinstance(m, dict) and m.get('id')]
    if not zai_ids:
        import pytest
        pytest.skip("no zai catalog models to exercise the alias ownership guard")
    owned = zai_ids[0]
    model, provider, base_url = _resolve_with_providers(
        owned,
        {'openai': {'models': [owned]}},
        provider='z-ai',        # aliased form the user may write in config
    )
    assert config._canonicalise_provider_id('z-ai') == 'zai'
    assert provider == 'z-ai', (
        "aliased active provider (z-ai→zai) that owns the model must not be "
        f"hijacked by providers.openai.models; got {provider!r}"
    )


def test_providers_scan_ownership_guard_canonicalises_gemini_alias_5511():
    """A Gemini-family alias (google-gemini → gemini) must canonicalise so the
    active provider is recognized as owning its catalog models (#5511 latent bug:
    `gemini` is in _PROVIDER_MODELS but not _PROVIDER_DISPLAY, so the alias was
    rejected and the ownership guard silently failed)."""
    import api.config as config
    assert config._canonicalise_provider_id('google-gemini') == 'gemini', (
        "google-gemini must canonicalise to gemini"
    )
    gem_models = config._PROVIDER_MODELS.get('gemini') or []
    gem_ids = [m.get('id') for m in gem_models if isinstance(m, dict) and m.get('id')]
    if not gem_ids:
        import pytest
        pytest.skip("no gemini catalog models to exercise the alias ownership guard")
    owned = gem_ids[0]
    model, provider, base_url = _resolve_with_providers(
        owned,
        {'openai': {'models': [owned]}},
        provider='google-gemini',
    )
    assert provider == 'google-gemini', (
        "aliased active Gemini provider that owns the model must not be hijacked "
        f"by providers.openai.models; got {provider!r}"
    )


def test_providers_scan_active_own_providers_entry_owns_over_other_slug_5511():
    """An active provider defined purely via config.yaml `providers:` (no static
    catalog entry) owns the models in its OWN providers.<active>.models allowlist,
    so another provider's entry listing the same bare id (even earlier in config
    order) must NOT hijack it (#5511 gate finding 5)."""
    # 'openai' entry lists gpt-x first, but the ACTIVE provider (myprov) also
    # declares gpt-x in its own providers.myprov.models — active must win.
    model, provider, base_url = _resolve_with_providers(
        'gpt-x',
        {
            'openai': {'models': ['gpt-x']},
            'myprov': {'base_url': 'https://my.example/v1', 'models': ['gpt-x']},
        },
        provider='myprov',
    )
    assert provider == 'myprov', (
        "active provider's own providers: allowlist must own its model over "
        f"another slug's overlapping entry; got {provider!r}"
    )
    assert base_url == 'https://my.example/v1'


# ── #5979: explicit-pick signature lifecycle (Codex-flagged session-update hole) ──

def test_explicit_pick_signature_matches_same_context_5979():
    """The signature matches when model+provider are unchanged (deliberate pick
    is honored across same-model follow-up sends)."""
    import api.models as models
    sig = models.model_explicit_pick_signature
    picked = sig('x-ai/grok-composer-2.5-fast', 'custom:llm-proxy')
    assert picked == sig('x-ai/grok-composer-2.5-fast', 'custom:llm-proxy')


def test_explicit_pick_signature_invalidated_on_model_change_5979():
    """A model change (e.g. via /api/session/update) yields a DIFFERENT signature,
    so a stale explicit-pick can't wrongly preserve a #433 leftover on cold."""
    import api.models as models
    sig = models.model_explicit_pick_signature
    picked = sig('x-ai/grok-composer-2.5-fast', 'custom:llm-proxy')
    # switched to a stale first-party id on the same proxy → signature differs
    assert picked != sig('openai/gpt-5.4', 'custom:llm-proxy')
    # provider switch also differs
    assert picked != sig('x-ai/grok-composer-2.5-fast', 'openai')


def test_explicit_pick_signature_persists_round_trip_5979():
    """Session.model_explicit_pick_signature survives save/reload (b3nw's cold
    restart scenario), and defaults to None when absent."""
    import api.models as models
    s = models.Session(session_id='sig5979', model='x-ai/grok-composer-2.5-fast',
                        model_provider='custom:llm-proxy')
    s.model_explicit_pick_signature = models.model_explicit_pick_signature(
        'x-ai/grok-composer-2.5-fast', 'custom:llm-proxy')
    data = {k: getattr(s, k, None) for k in
            ['session_id', 'title', 'workspace', 'model', 'model_provider',
             'model_explicit_pick_signature']}
    s2 = models.Session(**data)
    assert s2.model_explicit_pick_signature == s.model_explicit_pick_signature
    # absent → None (unmarked)
    s3 = models.Session(session_id='nopick', model='y')
    assert s3.model_explicit_pick_signature is None


def test_streaming_explicitly_picked_computation_5979():
    """End-to-end lifecycle (Fable fast-follow): reproduce the exact
    signature-comparison the streaming worker does at api/streaming.py, proving
    the persisted signature drives explicitly_picked correctly across the
    deliberate-pick, stale-after-update, and never-picked cases.

    Mirrors streaming.py:
        _picked_sig = s.model_explicit_pick_signature
        _current_sig = mk_sig(s.model or model, s.model_provider or provider_context)
        _explicitly_picked = bool(_picked_sig) and _picked_sig == _current_sig
    """
    import api.models as models
    mk = models.model_explicit_pick_signature

    def _explicitly_picked(session):
        picked = getattr(session, 'model_explicit_pick_signature', None)
        cur = mk(getattr(session, 'model', None), getattr(session, 'model_provider', None))
        return bool(picked) and picked == cur

    # 1) deliberate pick, unchanged context → honored
    s = models.Session(session_id='e2e1', model='x-ai/grok-composer-2.5-fast',
                        model_provider='custom:llm-proxy')
    s.model_explicit_pick_signature = mk('x-ai/grok-composer-2.5-fast', 'custom:llm-proxy')
    assert _explicitly_picked(s) is True

    # 2) session/update switches the model WITHOUT restamping → stale pick ignored
    s.model = 'openai/gpt-5.4'  # e.g. user switched; signature now stale
    assert _explicitly_picked(s) is False

    # 3) provider switch without restamp → stale pick ignored
    s2 = models.Session(session_id='e2e2', model='x-ai/grok-composer-2.5-fast',
                        model_provider='custom:llm-proxy')
    s2.model_explicit_pick_signature = mk('x-ai/grok-composer-2.5-fast', 'custom:llm-proxy')
    s2.model_provider = 'openai'
    assert _explicitly_picked(s2) is False

    # 4) never picked → unmarked
    s3 = models.Session(session_id='e2e3', model='x-ai/grok-composer-2.5-fast',
                        model_provider='custom:llm-proxy')
    assert _explicitly_picked(s3) is False
