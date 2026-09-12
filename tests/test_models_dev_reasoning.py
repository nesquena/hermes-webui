"""Regression coverage for Agent model-metadata reasoning capability lookup."""

import sys
import types

import pytest
from types import SimpleNamespace


def _install_fake_models_dev(monkeypatch, fake_fn):
    fake_agent = types.ModuleType("agent")
    fake_models_dev = types.ModuleType("agent.models_dev")
    setattr(fake_models_dev, "get_model_capabilities", fake_fn)
    setattr(fake_agent, "models_dev", fake_models_dev)
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.models_dev", fake_models_dev)


def test_models_dev_true_returns_full_efforts(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=True),
    )

    import api.config as cfg

    assert cfg._models_dev_reasoning_efforts("grok-4.3", "xai-oauth") == list(
        cfg.VALID_REASONING_EFFORTS
    )


def test_models_dev_false_returns_authoritative_empty(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=False),
    )

    import api.config as cfg

    assert cfg._models_dev_reasoning_efforts("grok-4.20-non-reasoning", "xai-oauth") == []


def test_models_dev_unknown_allows_compatibility_fallback(monkeypatch):
    _install_fake_models_dev(monkeypatch, lambda provider, model: None)

    import api.config as cfg

    assert cfg.resolve_model_reasoning_efforts(
        "x-ai/grok-4", provider_id="openrouter"
    ) == list(cfg.VALID_REASONING_EFFORTS)


def test_xai_oauth_grok_uses_agent_metadata(monkeypatch):
    seen = []

    def fake_capabilities(provider, model):
        seen.append((provider, model))
        return SimpleNamespace(supports_reasoning=True)

    _install_fake_models_dev(monkeypatch, fake_capabilities)

    import api.config as cfg

    assert cfg.resolve_model_reasoning_efforts(
        "@xai-oauth:grok-4.3", provider_id="xai-oauth"
    ) == list(cfg.VALID_REASONING_EFFORTS)
    assert seen == [("xai-oauth", "grok-4.3")]


def test_models_dev_false_suppresses_prefix_heuristic(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=False),
    )

    import api.config as cfg

    assert cfg.resolve_model_reasoning_efforts(
        "x-ai/grok-4-non-reasoning", provider_id="openrouter"
    ) == []


def test_codex_gpt55_uses_models_dev_excluding_unsupported_max(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=True),
    )

    import api.config as cfg

    result = cfg.resolve_model_reasoning_efforts(
        "gpt-5.5", provider_id="openai-codex"
    )
    assert "xhigh" in result
    assert "max" not in result


def test_codex_metadata_false_returns_empty(monkeypatch):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=False),
    )

    import api.config as cfg

    assert cfg.resolve_model_reasoning_efforts(
        "gpt-5.5", provider_id="openai-codex"
    ) == []


@pytest.fixture(autouse=True)
def _isolate_copilot_module_caches():
    """Reset the core's module-level Copilot caches around every test.

    `hermes_cli.models` memoises both the resolved catalog token and the
    catalog itself, deliberately and for the whole process. A test that
    monkeypatches the resolution seams therefore leaves its result behind:
    the patch is undone at teardown, but the cached VALUE survives and the
    next test silently reuses it.

    Concretely, the static-fallback test caches an empty token, and the
    live-catalog test that follows then resolves through the offline
    fallback instead of the catalog it just installed — passing alone and
    failing in file order. Reset before and after so neither direction of
    leakage is possible.
    """
    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        yield
        return

    import api.config as cfg  # noqa: F401  (import kept for fixture parity)

    names = (
        "_copilot_reasoning_token_cache",
        "_copilot_reasoning_token_cache_time",
        "_github_model_catalog_cache",
        "_github_model_catalog_cache_key",
        "_github_model_catalog_cache_time",
    )

    def _clear():
        for name in names:
            if hasattr(copilot_models, name):
                setattr(
                    copilot_models,
                    name,
                    0.0 if name.endswith("_time") else None,
                )

    _clear()
    yield
    _clear()


def test_copilot_gpt55_static_fallback_caps_at_high(monkeypatch):
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    # Force the "no catalog available" world this test is about. Without
    # pinning the capability flag, a catalog-aware core resolves the capability
    # probe as True and the lookup takes the catalog branch instead of the
    # static fallback — the test would then be asserting about a path it did
    # not mean to exercise.
    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: False
    )
    monkeypatch.setattr(copilot_models, "_resolve_copilot_catalog_api_key", lambda: "")
    monkeypatch.setattr(copilot_models, "fetch_github_model_catalog", lambda **_k: None)

    result = cfg.resolve_model_reasoning_efforts(
        "gpt-5.5", provider_id="copilot"
    )
    assert "medium" in result
    assert "high" in result
    assert "xhigh" not in result
    assert "max" not in result


def test_copilot_catalog_ladder_wins_through_public_resolver(monkeypatch):
    """The catalog ladder must survive all the way through the PUBLIC resolver.

    This deliberately asserts on `resolve_model_reasoning_efforts()` rather
    than on the private heuristic. An earlier revision passed a
    heuristic-level assertion while the public resolver still returned `[]` on
    a released core — the feature was inert in production and the suite was
    green, because the test never exercised the path the UI actually calls.

    Forcing the capability on (rather than skipping when the core lacks it)
    also means this cannot silently stop testing anything: the tri-state
    lookup has to classify a real catalog answer as an answer.
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    catalog = [
        {
            "id": "claude-opus-5",
            "capabilities": {
                "type": "chat",
                "supports": {
                    "reasoning_effort": ["low", "medium", "high", "xhigh", "max"]
                },
            },
        },
        {
            "id": "gemini-3.6-flash",
            "capabilities": {
                "type": "chat",
                "supports": {"reasoning_effort": ["minimal", "low", "medium", "high"]},
            },
        },
        {
            "id": "grok-4.6",
            "capabilities": {
                "type": "chat",
                "supports": {"reasoning_effort": ["low", "medium", "high", "xhigh"]},
            },
        },
        {
            "id": "claude-haiku-4.5",
            "capabilities": {"type": "chat", "supports": {}},
        },
    ]

    monkeypatch.setattr(
        copilot_models, "_resolve_copilot_catalog_api_key", lambda: "tok", raising=False
    )
    monkeypatch.setattr(
        copilot_models, "fetch_github_model_catalog", lambda **_k: catalog, raising=False
    )
    # The core is the authority here, so make it behave like one regardless of
    # which core is installed: answer from the injected catalog.
    def _from_catalog(model_id, **_kw):
        entry = next((e for e in catalog if e["id"] == model_id), None)
        if entry is None:
            return []
        return list(entry["capabilities"].get("supports", {}).get("reasoning_effort", []))

    monkeypatch.setattr(
        copilot_models, "github_model_reasoning_efforts", _from_catalog, raising=False
    )
    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: True
    )

    assert cfg.resolve_model_reasoning_efforts(
        "claude-opus-5", provider_id="copilot"
    ) == ["low", "medium", "high", "xhigh", "max"]
    assert cfg.resolve_model_reasoning_efforts(
        "gemini-3.6-flash", provider_id="copilot"
    ) == ["minimal", "low", "medium", "high"]
    assert cfg.resolve_model_reasoning_efforts(
        "grok-4.6", provider_id="copilot"
    ) == ["low", "medium", "high", "xhigh"]
    # An authoritative EMPTY entry must be honoured, not replaced by a guess.
    assert cfg.resolve_model_reasoning_efforts(
        "claude-haiku-4.5", provider_id="copilot"
    ) == []


def test_released_core_hides_control_it_cannot_actually_send(monkeypatch):
    """A core that ignores the ladder must get NO control, not a heuristic one.

    Earlier this test asserted the opposite — that a released core should still
    surface a heuristic ladder. That encoded the bug: the released core's wire
    path calls `github_model_reasoning_efforts()` bare, gets `[]` for
    Claude/Gemini/Grok/MAI, and sends no reasoning field at all. Showing a
    populated control on top of that is a visible-but-inert widget: the user
    picks a thinking level and nothing whatsoever changes on the wire.

    The honest contract is that the UI must not offer a reasoning value the
    installed runtime will not send. The ladder returns once the core resolves
    it on a bare call (covered by the sibling test below).
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    # Simulate the released core exactly: the reasoning lookup ignores the
    # catalog entirely and knows only GPT-5 / o-series.
    def _released_core(model_id, **_kw):
        bare = str(model_id or "").lower()
        if bare.startswith("gpt-5"):
            return ["minimal", "low", "medium", "high"]
        return []

    monkeypatch.setattr(
        copilot_models, "github_model_reasoning_efforts", _released_core, raising=False
    )
    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: False
    )

    for model_id in ("claude-opus-5", "gemini-3.6-flash", "grok-4.6", "mai-code-1.1-flash"):
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="copilot")
        assert efforts == [], (
            f"{model_id}: a control was offered but this core sends no reasoning "
            f"field for it — visible but inert (got {efforts})"
        )

    # The model this core DOES resolve keeps its ladder: the gate is about
    # runtime agreement, not about suppressing Copilot reasoning wholesale.
    gpt = cfg.resolve_model_reasoning_efforts("gpt-5.5", provider_id="copilot")
    assert {"low", "medium", "high"} <= set(gpt), gpt

    # Models that genuinely have no ladder must stay empty either way.
    for model_id in ("claude-haiku-4.5", "grok-4", "gemini-embedding-001"):
        assert cfg.resolve_model_reasoning_efforts(
            model_id, provider_id="copilot"
        ) == [], model_id


def test_catalog_aware_core_exposes_full_ladder(monkeypatch):
    """The counterpart: when the core DOES resolve bare calls, show the ladder.

    Pairs with the test above so the gate is proven in both directions — the
    control must appear exactly when the runtime will honour it.
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    ladders = {
        "claude-opus-5": ["low", "medium", "high", "xhigh", "max"],
        "gemini-3.6-flash": ["minimal", "low", "medium", "high"],
        "grok-4.6": ["low", "medium", "high", "xhigh"],
        "mai-code-1.1-flash": ["low", "medium", "high"],
    }

    def _catalog_core(model_id, **_kw):
        return list(ladders.get(str(model_id or "").lower(), []))

    # Stub the fetch too: without it the resolver takes the real catalog path
    # and this test silently measures the live catalog instead of the branch
    # it names. A stub must match production on every dimension the code under
    # test depends on, and only simulate the one being varied.
    catalog = [
        {
            "id": model_id,
            "capabilities": {
                "type": "chat",
                "supports": {"reasoning_effort": levels},
            },
        }
        for model_id, levels in ladders.items()
    ]
    monkeypatch.setattr(
        copilot_models, "fetch_github_model_catalog", lambda **_k: catalog, raising=False
    )
    monkeypatch.setattr(
        copilot_models, "_resolve_copilot_catalog_api_key", lambda: "probe", raising=False
    )
    monkeypatch.setattr(
        copilot_models, "github_model_reasoning_efforts", _catalog_core, raising=False
    )
    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: True
    )

    for model_id, expected in ladders.items():
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="copilot")
        assert efforts == expected, (model_id, efforts, expected)


def test_catalog_capable_core_with_failing_fetch_falls_back(monkeypatch):
    """A live fetch failure is "unavailable", not "authoritatively empty".

    The discriminator has to reflect THIS lookup's outcome, not a static
    capability of the installed core. A catalog-aware core whose real fetch
    fails right now still returns `[]`, and calling that authoritative drops
    the ladder for every model its own offline table does not know —
    `grok-3-mini` and `grok-4.3` are exactly that shape, so they lose the
    control whenever the network or the credential is down.
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    # Core CAN read catalogs (capability probe passes) but the real fetch fails.
    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: True
    )
    monkeypatch.setattr(
        copilot_models, "fetch_github_model_catalog", lambda **_k: None, raising=False
    )
    monkeypatch.setattr(
        copilot_models, "_resolve_copilot_catalog_api_key", lambda: "", raising=False
    )

    def _empty_for_non_gpt(model_id, **_kw):
        return [] if not str(model_id or "").lower().startswith("gpt-5") else ["low", "high"]

    monkeypatch.setattr(
        copilot_models,
        "github_model_reasoning_efforts",
        _empty_for_non_gpt,
        raising=False,
    )

    for model_id in ("grok-4.3", "grok-3-mini", "claude-opus-5"):
        efforts = cfg.resolve_model_reasoning_efforts(model_id, provider_id="copilot")
        assert {"low", "medium", "high"} <= set(efforts), (
            f"{model_id}: a failed catalog fetch was treated as an authoritative "
            f"empty answer — the ladder vanished (got {efforts})"
        )

    # Models with genuinely no ladder stay empty via the heuristic.
    for model_id in ("grok-4", "claude-haiku-4.5"):
        assert cfg.resolve_model_reasoning_efforts(
            model_id, provider_id="copilot"
        ) == [], model_id


def test_transient_fetch_failure_then_success_keeps_ladder(monkeypatch):
    """A `None -> catalog` fetch sequence must not drop the ladder.

    Classification and resolution used to come from two separate fetches. With
    a transient failure on the first and a success on the second, the pair read
    as "empty" AND "authoritative" — a positive deny — so `grok-4.3` lost its
    control during exactly the network/auth-recovery window it should survive.

    Asserts on the public resolver, and also pins that only ONE fetch happens:
    a single fetch is what makes the two answers unable to disagree, so the
    call count is part of the contract rather than an efficiency note.
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    catalog = [
        {
            "id": "claude-opus-5",
            "capabilities": {
                "type": "chat",
                "supports": {"reasoning_effort": ["low", "medium", "high"]},
            },
        },
        {
            "id": "claude-haiku-4.5",
            "capabilities": {"type": "chat", "supports": {}},
        },
    ]

    calls = {"n": 0}

    def _transient_fetch(**_kw):
        calls["n"] += 1
        return None if calls["n"] == 1 else catalog

    def _resolver(model_id, **kwargs):
        cat = kwargs.get("catalog")
        if cat is None:
            cat = copilot_models.fetch_github_model_catalog(api_key="tok")
        if not cat:
            return []
        entry = next((e for e in cat if e["id"] == model_id), None)
        if entry is None:
            return []
        return list(entry["capabilities"].get("supports", {}).get("reasoning_effort", []))

    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: True
    )
    monkeypatch.setattr(
        copilot_models, "fetch_github_model_catalog", _transient_fetch, raising=False
    )
    monkeypatch.setattr(
        copilot_models, "_resolve_copilot_catalog_api_key", lambda: "tok", raising=False
    )
    monkeypatch.setattr(
        copilot_models, "github_model_reasoning_efforts", _resolver, raising=False
    )

    efforts = cfg.resolve_model_reasoning_efforts("grok-4.3", provider_id="copilot")
    assert {"low", "medium", "high"} <= set(efforts), (
        f"grok-4.3 lost its ladder across a transient fetch failure (got {efforts})"
    )
    assert calls["n"] == 1, (
        f"expected exactly one catalog fetch, saw {calls['n']} — a second fetch "
        "reintroduces the check/use gap"
    )


def test_model_absent_from_catalog_is_not_an_authoritative_deny(monkeypatch):
    """A live catalog that simply does not list a model is not a deny.

    When the id is absent the core falls back to its own static table, and that
    table returning `[]` means "unknown to me", not "no ladder". Conflating the
    two would drop the control for any model missing from a partial catalog,
    while a model the catalog DOES list with no ladder must still stay empty.
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    # grok-4.3 is deliberately absent; claude-haiku-4.5 is listed with no ladder.
    catalog = [
        {
            "id": "claude-haiku-4.5",
            "capabilities": {"type": "chat", "supports": {}},
        },
    ]

    def _resolver(model_id, **kwargs):
        cat = kwargs.get("catalog") or catalog
        entry = next((e for e in cat if e["id"] == model_id), None)
        if entry is None:
            return []
        return list(entry["capabilities"].get("supports", {}).get("reasoning_effort", []))

    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: True
    )
    monkeypatch.setattr(
        copilot_models, "fetch_github_model_catalog", lambda **_k: catalog, raising=False
    )
    monkeypatch.setattr(
        copilot_models, "_resolve_copilot_catalog_api_key", lambda: "tok", raising=False
    )
    monkeypatch.setattr(
        copilot_models, "github_model_reasoning_efforts", _resolver, raising=False
    )

    absent = cfg.resolve_model_reasoning_efforts("grok-4.3", provider_id="copilot")
    assert {"low", "medium", "high"} <= set(absent), (
        f"grok-4.3 is absent from the catalog, not denied by it (got {absent})"
    )

    listed = cfg.resolve_model_reasoning_efforts(
        "claude-haiku-4.5", provider_id="copilot"
    )
    assert listed == [], (
        f"claude-haiku-4.5 IS listed with no ladder — that deny must be honoured "
        f"(got {listed})"
    )


def test_legacy_resolver_without_catalog_kwarg_still_resolves(monkeypatch):
    """A core resolver predating the `catalog=` keyword must still work.

    Decided from the signature rather than by catching TypeError, so it must
    be called bare — exactly once, with no failed probe call first.
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    catalog = [{
        "id": "claude-opus-5",
        "capabilities": {
            "type": "chat",
            "supports": {"reasoning_effort": ["low", "medium", "high", "xhigh", "max"]},
        },
    }]
    calls = []

    def _legacy(model_id):          # no catalog kwarg at all
        calls.append(model_id)
        return ["low", "medium", "high", "xhigh", "max"]

    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: True
    )
    monkeypatch.setattr(
        copilot_models, "fetch_github_model_catalog", lambda **_k: catalog, raising=False
    )
    monkeypatch.setattr(
        copilot_models, "_resolve_copilot_catalog_api_key", lambda: "tok", raising=False
    )
    monkeypatch.setattr(
        copilot_models, "github_model_reasoning_efforts", _legacy, raising=False
    )

    efforts = cfg.resolve_model_reasoning_efforts("claude-opus-5", provider_id="copilot")
    assert efforts == ["low", "medium", "high", "xhigh", "max"]
    assert len(calls) == 1, f"legacy resolver should be called once, saw {len(calls)}"


def test_internal_typeerror_is_not_mistaken_for_an_old_signature(monkeypatch):
    """A TypeError raised INSIDE a modern resolver must not trigger a retry.

    Catching TypeError at the call site cannot distinguish "this resolver has
    no catalog parameter" from "this resolver blew up internally". Retrying
    the second case without the catalog would resolve from a different
    observation than the one used to classify — the very gap the single-fetch
    collapse removed. The resolver must be called exactly once, and the
    failure must fall back rather than silently produce a second-observation
    answer.
    """
    import api.config as cfg

    try:
        import hermes_cli.models as copilot_models
    except ImportError:
        pytest.skip("hermes_cli not available")

    catalog = [{
        "id": "claude-opus-5",
        "capabilities": {
            "type": "chat",
            "supports": {"reasoning_effort": ["low", "medium", "high", "xhigh", "max"]},
        },
    }]
    seen = []

    def _modern_but_broken(model_id, **kwargs):
        seen.append("with_catalog" if "catalog" in kwargs else "bare")
        raise TypeError("unrelated internal failure")

    monkeypatch.setattr(
        cfg, "_copilot_core_reads_catalog_unprompted", lambda _m: True
    )
    monkeypatch.setattr(
        copilot_models, "fetch_github_model_catalog", lambda **_k: catalog, raising=False
    )
    monkeypatch.setattr(
        copilot_models, "_resolve_copilot_catalog_api_key", lambda: "tok", raising=False
    )
    monkeypatch.setattr(
        copilot_models, "github_model_reasoning_efforts", _modern_but_broken, raising=False
    )

    cfg.resolve_model_reasoning_efforts("claude-opus-5", provider_id="copilot")

    assert seen == ["with_catalog"], (
        f"expected a single catalog-passing call, saw {seen} — an internal "
        "TypeError was misread as an old signature and retried"
    )


def test_get_reasoning_status_uses_config_default_model(monkeypatch, tmp_path):
    _install_fake_models_dev(
        monkeypatch,
        lambda provider, model: SimpleNamespace(supports_reasoning=True)
        if (provider, model) == ("xai-oauth", "grok-4.3")
        else None,
    )

    import api.config as cfg

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
model:
  default: grok-4.3
  provider: xai-oauth
agent:
  reasoning_effort: medium
display:
  show_reasoning: true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(cfg, "_get_config_path", lambda: config_path)

    status = cfg.get_reasoning_status()

    assert status["reasoning_effort"] == "medium"
    assert status["supported_efforts"] == list(cfg.VALID_REASONING_EFFORTS)
    assert status["supports_reasoning_effort"] is True


@pytest.mark.parametrize(
    ("shape", "accepts_catalog"),
    [
        ("plain_modern", True),
        ("plain_legacy", False),
        ("var_keyword", True),
        ("wrapped_modern", True),
        ("wrapped_legacy", False),
        ("partial_modern", True),
        ("partial_legacy", False),
        ("callable_modern", True),
        ("callable_legacy", False),
        ("c_builtin", False),
        ("unreadable_signature", False),
        ("unreadable_signature_valueerror", False),
    ],
)
def test_signature_probe_handles_exotic_callables(shape, accepts_catalog):
    """Signature introspection must survive wrappers, partials and builtins.

    The dispatcher decides by signature rather than by calling and catching
    TypeError, so every callable shape a resolver can arrive in has to be
    classified correctly. A false positive passes `catalog=` to something that
    rejects it; a false negative silently drops the catalog and reintroduces
    the two-observation gap. Where the signature genuinely cannot be read
    (C-level builtins) the answer must be False -- fail closed to the bare
    call, which is the historical behaviour.
    """
    import functools

    import api.config as cfg

    def modern(model_id, *, catalog=None, api_key=None):
        return ["low", "medium", "high"]

    def legacy(model_id):
        return ["low", "medium", "high"]

    def var_kw(model_id, **kwargs):
        return ["low", "medium", "high"]

    def wrap(fn):
        @functools.wraps(fn)
        def inner(*args, **kwargs):
            return fn(*args, **kwargs)

        return inner

    class CallableModern:
        def __call__(self, model_id, *, catalog=None, api_key=None):
            return ["low", "medium", "high"]

    class CallableLegacy:
        def __call__(self, model_id):
            return ["low", "medium", "high"]

    resolvers = {
        "plain_modern": modern,
        "plain_legacy": legacy,
        "var_keyword": var_kw,
        "wrapped_modern": wrap(modern),
        "wrapped_legacy": wrap(legacy),
        "partial_modern": functools.partial(modern, api_key="k"),
        "partial_legacy": functools.partial(legacy),
        "callable_modern": CallableModern(),
        "callable_legacy": CallableLegacy(),
        "c_builtin": len,
        # inspect.signature() raises for these -- the fail-closed branch.
        # `len` is NOT such a case: its signature reads fine as (obj, /),
        # so it exercises the ordinary "no catalog param" path instead.
        "unreadable_signature": object(),
        "unreadable_signature_valueerror": type,
    }
    resolver = resolvers[shape]

    assert cfg._resolver_accepts_catalog_kwarg(resolver) is accepts_catalog

    # And the dispatch that uses the answer must not raise for any real shape.
    if shape not in {"c_builtin", "unreadable_signature",
                     "unreadable_signature_valueerror"}:
        assert cfg._call_copilot_resolver(
            resolver, "claude-opus-5", [{"id": "claude-opus-5"}]
        ) == ["low", "medium", "high"]
