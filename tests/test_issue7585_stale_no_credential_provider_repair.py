"""Regression coverage for stale no-credential provider repair (#7585).

A session whose stored ``model_provider`` belongs to the catalog-backed class
(e.g. ``openrouter``) but whose catalog group is missing -- typically because
no credential is configured -- must not preserve that lane forever: every
agent construction for it re-routed auxiliary calls through the stale paid
provider. #5731's fail-safe (preserve when ownership evidence is ambiguous or
the stored provider may legitimately own unlisted models) must stay intact.
"""

from types import SimpleNamespace

import pytest

import api.routes as routes


def _catalog(*groups):
    return {"groups": list(groups)}


def _group(provider_id, *models):
    return {"provider_id": provider_id, "models": [{"id": model} for model in models]}


def _session(*, model="deepseek/deepseek-v4.1-flash", provider="openrouter"):
    return SimpleNamespace(model=model, model_provider=provider)


def _repair(session, *, profile_provider="nous", **kwargs):
    return routes._repair_foreign_session_model_provider(
        session,
        requested_model=session.model,
        requested_provider=session.model_provider,
        resolved_model=session.model,
        resolved_provider=session.model_provider,
        explicit_model_pick=False,
        profile_provider=profile_provider,
        **kwargs,
    )


def _patch_catalog(monkeypatch, catalog):
    monkeypatch.setattr(routes, "get_available_models", lambda *, prefer_cache=False: catalog)


@pytest.fixture()
def no_openrouter_credential(monkeypatch):
    monkeypatch.setattr(
        routes, "provider_has_usable_credential",
        lambda pid, **_kw: str(pid).strip().lower() != "openrouter",
        raising=False,
    )


@pytest.fixture()
def no_plugin_providers(monkeypatch):
    monkeypatch.setattr(routes, "is_plugin_model_provider", lambda _pid: False, raising=False)


@pytest.fixture()
def no_provider_credentials(monkeypatch):
    """No provider of any kind has a usable API credential.

    Unlike ``no_openrouter_credential`` (which keeps other lanes live), the
    keyless-local cases under test must have *every* lane credential-dead, so a
    lane is preserved only by the legitimately-keyless / base_url rules and not by
    a surviving credential fallback (PR #7594 review).
    """
    monkeypatch.setattr(
        routes, "provider_has_usable_credential",
        lambda _pid, **_kw: False,
        raising=False,
    )


def test_stale_no_credential_provider_cleared_to_single_owner(
    monkeypatch, no_openrouter_credential, no_plugin_providers
):
    """The #7585 repro: openrouter lane survives while the model is Nous-owned."""
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session()) == "nous"


def test_ambiguous_ownership_still_preserved(
    monkeypatch, no_openrouter_credential, no_plugin_providers
):
    _patch_catalog(monkeypatch, _catalog(
        _group("nous", "deepseek/deepseek-v4.1-flash"),
        _group("other", "deepseek/deepseek-v4.1-flash"),
    ))

    assert _repair(_session()) == "openrouter"


def test_live_stored_credential_still_preserved(
    monkeypatch, no_plugin_providers
):
    monkeypatch.setattr(routes, "provider_has_usable_credential", lambda _pid, **_kw: True, raising=False)
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session()) == "openrouter"


def test_incomplete_catalog_evidence_still_preserved(
    monkeypatch, no_openrouter_credential, no_plugin_providers
):
    errored = _group("thirdparty", "some-model")
    errored["models_endpoint_error"] = "upstream 502"
    _patch_catalog(monkeypatch, _catalog(
        _group("nous", "deepseek/deepseek-v4.1-flash"),
        errored,
    ))

    assert _repair(_session()) == "openrouter"


def test_self_hosted_stored_provider_keeps_5731_fail_safe(monkeypatch, no_plugin_providers):
    """ollama may legitimately own unlisted models: missing group stays preserved."""
    monkeypatch.setattr(routes, "provider_has_usable_credential", lambda _pid, **_kw: False, raising=False)
    _patch_catalog(monkeypatch, _catalog(_group("kilocode", "kilo/minimax/minimax-m3")))
    session = _session(model="kilo/minimax/minimax-m3", provider="ollama")

    assert _repair(session, profile_provider="kilocode") == "ollama"


def test_plugin_stored_provider_stays_preserved(monkeypatch, no_openrouter_credential):
    monkeypatch.setattr(routes, "is_plugin_model_provider", lambda _pid: True, raising=False)
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session()) == "openrouter"


def test_chat_start_no_longer_routes_agent_through_stale_paid_lane(
    monkeypatch, tmp_path, no_openrouter_credential, no_plugin_providers
):
    """End-to-end billing guard: the stale openrouter lane must not reach a run."""
    session = SimpleNamespace(
        session_id="issue-7585",
        workspace=str(tmp_path),
        model="deepseek/deepseek-v4.1-flash",
        model_provider="openrouter",
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
        save=lambda: None,
    )
    captured = {}

    def start_run(s, **kwargs):
        captured.update(kwargs)
        routes._prepare_chat_start_session_for_stream(
            s,
            msg=kwargs["msg"],
            attachments=kwargs["attachments"],
            workspace=kwargs["workspace"],
            model=kwargs["model"],
            model_provider=kwargs["model_provider"],
            stream_id="issue-7585-stream",
        )
        return {"stream_id": "issue-7585-stream"}

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid, **_kwargs: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {"model": {"provider": "nous"}}))
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))
    monkeypatch.setattr(routes, "_start_run", start_run)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    routes._handle_chat_start(None, {"session_id": session.session_id, "message": "continue"})

    assert captured["model_provider"] == "nous", captured["model_provider"]
    assert session.model == "deepseek/deepseek-v4.1-flash"
    assert session.model_provider == "nous"


def test_minimal_fallback_catalog_stays_preserved(
    monkeypatch, no_openrouter_credential, no_plugin_providers
):
    """A cold/emergency minimal catalog is not complete discovery evidence.

    The fallback lists only the active provider's default model; treating it
    as a full catalog could reassign the session to the wrong owner when
    other configured providers were merely omitted (PR #7594 review).
    """
    minimal = _catalog(_group("nous", "deepseek/deepseek-v4.1-flash"))
    minimal["catalog_minimal"] = True
    _patch_catalog(monkeypatch, minimal)

    assert _repair(_session()) == "openrouter"


def test_keyless_custom_endpoint_provider_stays_preserved(
    monkeypatch, no_openrouter_credential, no_plugin_providers
):
    """custom:<slug> lanes (vLLM, llama-server) need no API key.

    A missing catalog group proves nothing for them: absence of a key must
    not be read as proof of staleness (PR #7594 review).
    """
    monkeypatch.setattr(routes, "provider_has_usable_credential", lambda _pid, **_kw: False, raising=False)
    _patch_catalog(monkeypatch, _catalog(_group("nous", "vendor/local-model")))
    session = _session(model="vendor/local-model", provider="custom:vllm-local")

    assert _repair(session, profile_provider="nous") == "custom:vllm-local"


# ---------------------------------------------------------------------------
# PR #7594 review (CHANGES_REQUESTED): keyless LOCAL-server lanes beyond
# ollama/lmstudio and any configured ``providers.<id>.base_url`` OpenAI-compatible
# endpoint must be preserved even with no catalog group and no API key. The #7585
# repair must never silently reassign a working self-hosted session to the catalog
# owner, nor persist such a swap.
# ---------------------------------------------------------------------------


def test_raw_keyless_vllm_provider_preserved_without_catalog(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """A raw routable ``vllm`` lane (no catalog group, no key) must stay put.

    vllm is a local model server (`_is_local_server_provider`): its models never
    appear in any catalog, so the *absence* of a vllm group proves nothing.
    Clearing it here would silently reassign a working self-hosted session to the
    catalog owner (PR #7594 review, CORE).
    """
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session(provider="vllm")) == "vllm"


def test_raw_keyless_llamacpp_provider_preserved_without_catalog(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """A raw ``llamacpp`` lane is likewise self-hosted and must be preserved."""
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session(provider="llamacpp")) == "llamacpp"


def test_raw_keyless_tabby_provider_preserved_without_catalog(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """``tabby`` (TabbyAPI) is another local server name in _LOCAL_SERVER_PROVIDERS."""
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session(provider="tabby")) == "tabby"


def _run_chat_start_local_lane(monkeypatch, tmp_path, *, session_id, provider, profile_cfg):
    """Drive a real chat start for a session whose stored provider is ``provider``.

    Returns (captured_kwargs_passed_to__start_run, session_provider_after, provider_before).
    ``profile_cfg`` is the per-profile config returned by the (patched)
    ``_read_profile_model_config``; it must keep the ``nous`` catalog owning the
    stored model so a replacement would target it.
    """
    session = SimpleNamespace(
        session_id=session_id,
        workspace=str(tmp_path),
        model="deepseek/deepseek-v4.1-flash",
        model_provider=provider,
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
        save=lambda: None,
    )
    provider_before = session.model_provider
    captured = {}

    def start_run(s, **kwargs):
        captured.update(kwargs)
        routes._prepare_chat_start_session_for_stream(
            s,
            msg=kwargs["msg"],
            attachments=kwargs["attachments"],
            workspace=kwargs["workspace"],
            model=kwargs["model"],
            model_provider=kwargs["model_provider"],
            stream_id=session_id,
        )
        return {"stream_id": session_id}

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid, **_kwargs: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(
        routes,
        "_read_profile_model_config",
        lambda _s, _p: (None, None, profile_cfg or {"model": {"provider": "nous"}}),
    )
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))
    monkeypatch.setattr(routes, "_start_run", start_run)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    routes._handle_chat_start(None, {"session_id": session.session_id, "message": "continue"})

    return captured, session.model_provider, provider_before


def test_chat_start_keeps_keyless_local_vllm_lane_and_does_not_persist(
    monkeypatch, tmp_path, no_provider_credentials, no_plugin_providers
):
    """End-to-end: a keyless raw ``vllm`` lane passes through chat start untouched.

    The stored provider must not be silently swapped for the catalog owner and must
    not be persisted as a different provider. Provider is identical before/after.
    """
    captured, provider_after, provider_before = _run_chat_start_local_lane(
        monkeypatch, tmp_path, session_id="issue-7594-vllm", provider="vllm",
        profile_cfg=None,
    )

    assert captured["model_provider"] == "vllm", captured["model_provider"]
    assert provider_after == "vllm", provider_after
    assert provider_before == provider_after


def test_chat_start_keeps_configured_openai_compatible_base_url_lane(
    monkeypatch, tmp_path, no_provider_credentials, no_plugin_providers
):
    """Any profile-scoped ``providers.<id>.base_url`` OpenAI-compatible endpoint is
    preserved even with no catalog group and no key.

    ``llama-server`` here is an arbitrary OpenAI-compatible id declared via
    ``providers.llama-server.base_url``; its models are served without any catalog.
    Chat start must keep the lane identical (no replace, no persist).
    """
    profile_cfg = {
        "providers": {
            "llama-server": {"base_url": "http://127.0.0.1:8080/v1"},
        },
        "model": {"provider": "nous"},
    }
    captured, provider_after, provider_before = _run_chat_start_local_lane(
        monkeypatch, tmp_path, session_id="issue-7594-burl", provider="llama-server",
        profile_cfg=profile_cfg,
    )

    assert captured["model_provider"] == "llama-server", captured["model_provider"]
    assert provider_after == "llama-server", provider_after
    assert provider_before == provider_after


# ---------------------------------------------------------------------------
# PR #7594 review P1 (latest): a TOP-LEVEL ``model.base_url`` is ownership
# evidence ONLY for the provider it is configured under.
# ``model.base_url`` belongs to ``model.provider``. It must not make an
# UNRELATED stored provider look like a legitimate local lane: when the
# profile configures a loopback/private URL for provider X while the session
# stores a different, keyless, catalog-less provider Y, the stale lane Y must
# still be repaired to the sole catalog owner. When the stored provider IS the
# configured provider (the FAQ shape ``model: {provider: my-local-server,
# base_url: http://127.0.0.1:...}``), the same URL must keep preserving the
# lane. A public URL is not local evidence for anybody, and no URL without a
# configured provider owner is evidence at all. (Greptile P1, 2026-09-16.)
# ---------------------------------------------------------------------------


def _repair_with_top_level(session, profile_cfg, *, provider="nous"):
    return routes._repair_foreign_session_model_provider(
        session,
        requested_model=session.model,
        requested_provider=session.model_provider,
        resolved_model=session.model,
        resolved_provider=session.model_provider,
        explicit_model_pick=False,
        profile_provider=provider,
        profile_config=profile_cfg,
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8000/v1",
        "http://localhost:8000/v1",
        "http://192.168.1.50:1234/v1",
        "http://10.0.0.7:8080/v1",
    ],
)
def test_top_level_base_url_for_foreign_provider_does_not_preserve_stale_lane(
    monkeypatch, no_provider_credentials, no_plugin_providers, base_url
):
    """Repro (latest P1): ``model.base_url`` is evidence for ``model.provider``
    ONLY. A loopback/private URL configured for ``nous`` while the session
    stores an UNRELATED keyless, catalog-less ``my-local-server`` must NOT
    protect that stale lane: the repair still reassigns it to the sole catalog
    owner."""
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))
    profile_config = {"model": {"provider": "nous", "base_url": base_url}}
    session = _session(provider="my-local-server")

    result = _repair_with_top_level(session, profile_config)

    assert result == "nous", result


def test_top_level_base_url_evidence_requires_matching_configured_provider(
    no_plugin_providers,
):
    """Unit face of the same contract, via _stored_provider_can_legitimately_own_model.

    Match (case/whitespace-insensitive) -> True; configured provider differing
    from the stored provider -> False; a URL with NO configured provider at all
    is ownership evidence for nobody -> False (strictest defensible reading of
    "normalized provider matches the stored provider").
    """
    local = "http://127.0.0.1:8000/v1"
    own = routes._stored_provider_can_legitimately_own_model
    assert own(
        "my-local-server", {"model": {"provider": "my-local-server", "base_url": local}}
    ) is True
    assert own("my-local-server", {"model": {"provider": " My-Local-Server ", "base_url": local}}) is True
    assert own("my-local-server", {"model": {"provider": "nous", "base_url": local}}) is False
    assert own("my-local-server", {"model": {"base_url": local}}) is False


def test_top_level_model_base_url_arbitrary_provider_preserved_when_default_provider_matches(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """Same lane preserved when the profile default provider IS the arbitrary id.

    The top-level model.provider being the stored provider is the FAQ-documented
    setup; a top-level base_url still must not let the repair clear it.
    """
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))
    profile = {"model": {"provider": "my-local-server", "base_url": "http://127.0.0.1:8000/v1"}}
    session = _session(provider="my-local-server")

    result = _repair_with_top_level(session, profile, provider="my-local-server")

    assert result == "my-local-server", result


def test_chat_start_repairs_foreign_lane_behind_another_provider_top_level_base_url(
    monkeypatch, tmp_path, no_provider_credentials, no_plugin_providers
):
    """End-to-end (latest P1): a profile whose TOP-LEVEL model.base_url loopback
    belongs to ``nous`` must not keep an unrelated stored ``my-local-server``
    alive. Chat start must repair the lane to the sole catalog owner and
    persist that provider -- the wrong-lane session may not keep routing
    through a provider nobody configured a lane for."""
    profile_cfg = {"model": {"provider": "nous", "base_url": "http://127.0.0.1:8080/v1"}}
    captured, provider_after, provider_before = _run_chat_start_local_lane(
        monkeypatch, tmp_path, session_id="issue-7594-topburl", provider="my-local-server",
        profile_cfg=profile_cfg,
    )

    assert provider_before == "my-local-server"
    assert captured["model_provider"] == "nous", captured["model_provider"]
    assert provider_after == "nous", provider_after


def test_top_level_public_base_url_does_not_preserve_stale_lane(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """A NON-loopback/private top-level base_url is not a local-routing signal.

    The P1 contract pins loopback/private base_url URLs (127.0.0.1, localhost,
    RFC1918 private, 10.x) as local-host evidence. A public endpoint (here a
    vendor-relay-ish URL) with no key and no catalog group must still be
    treated as a stale catalog-backed lane and reassigned to the single catalog
    owner -- not blanket-preserved by any base_url value.
    """
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))
    profile_config = {
        "model": {"provider": "nous", "base_url": "https://relay.example.com/v1"}
    }
    session = _session(provider="my-local-server")

    result = _repair_with_top_level(session, profile_config)

    assert result == "nous", result


# ---------------------------------------------------------------------------
# Gate round 5 (CHANGES_REQUESTED @ 81c5c5d, CORE #1): the repair path
# compares provider IDs with raw string equality while the catalog
# canonicalises them via ``_canonicalise_provider_id``
# (api/config.py:1561 — underscore->hyphen fold + alias table, so
# ``my_local``/``My-Local`` -> ``my-local`` and ``z-ai``/``glm`` -> ``zai``).
# Canonically-equivalent forms must be treated as the SAME provider at every
# comparison site: nested ``providers.<id>.base_url`` key lookup, top-level
# ``model.provider`` vs stored provider, ``profile_provider`` vs stored
# provider, catalog ``provider_id`` vs stored provider, and owner-group
# provider_id matching. If canonicalisation itself raises, preserve the lane
# (maintainer's exact-fix comment: "keep the fail-safe").
# ---------------------------------------------------------------------------


def test_canonicalise_helper_equivalence_classes():
    """Pin the equivalence vocabulary the repair contract relies on."""
    import api.config as config

    canon = config._canonicalise_provider_id
    assert canon("my_local") == canon("my-local") == canon("My-Local") == "my-local"
    assert canon("z-ai") == canon("glm") == canon("zai") == "zai"
    # Unknown ids keep their normalised form (no alias target).
    assert canon("llama_cpp") == "llama-cpp"
    assert canon("tabby_api") == "tabby-api"


@pytest.mark.parametrize(
    "provider_key",
    ["my_local", "My_Local", " my_local ", "my-local", "My-Local"],
)
def test_nested_base_url_key_canonical_match_preserves_lane(
    monkeypatch, no_provider_credentials, no_plugin_providers, provider_key
):
    """Repro (gate CORE #1a): ``providers.my_local.base_url`` must protect a
    session stored under ``my-local``. Raw dict lookup misses the underscore
    key, the base_url evidence is ignored, and the lane is wrongly reassigned
    to the sole catalog owner."""
    _patch_catalog(monkeypatch, _catalog(_group("owner-x", "deepseek/deepseek-v4.1-flash")))
    profile_config = {"providers": {provider_key: {"base_url": "http://127.0.0.1:8080/v1"}}}

    assert _repair(_session(provider="my-local"), profile_provider="owner-x",
                   profile_config=profile_config) == "my-local"


def test_top_level_alias_provider_matches_stored_canonical(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """Repro (gate CORE #1b): profile top-level ``provider: z-ai`` + loopback
    URL, session stored as canonical ``zai`` — same provider, so the lane is
    preserved. Raw equality says z-ai != zai and clears the working lane."""
    _patch_catalog(monkeypatch, _catalog(_group("owner-x", "deepseek/deepseek-v4.1-flash")))
    profile_config = {"model": {"provider": "z-ai", "base_url": "http://127.0.0.1:8000/v1"}}

    assert _repair(_session(provider="zai"), profile_provider="owner-x",
                   profile_config=profile_config) == "zai"


def test_profile_provider_alias_equiv_still_short_circuits_repair(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """Repro (gate CORE #1c): profile ``model.provider: z-ai`` is the stored
    ``zai`` lane itself — repair must not even start. Raw equality lets the
    keyless ``zai`` lane be reassigned to the sole catalog owner."""
    _patch_catalog(monkeypatch, _catalog(_group("kilocode", "glm/glm-4.6")))
    session = _session(model="glm/glm-4.6", provider="zai")

    assert _repair(session, profile_provider="z-ai") == "zai"


def test_catalog_provider_id_underscore_form_matches_stored(
    monkeypatch, no_plugin_providers
):
    """Repro (gate CORE #1d): the stored ``my-local`` lane's OWN catalog group
    is published under the un-canonicalised id ``my_local``. The raw
    provider_id != stored comparison treats that group as a foreign sole owner
    and "repairs" the session to ``my_local`` — rewriting the provider to a
    canonically-identical string and bypassing the exact-ownership check."""
    monkeypatch.setattr(routes, "provider_has_usable_credential", lambda _pid, **_kw: False, raising=False)
    _patch_catalog(monkeypatch, _catalog(_group("my_local", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session(provider="my-local"), profile_provider="vendor-z") == "my-local"


def test_catalog_owner_group_canonicalised_to_single_owner(
    monkeypatch, no_openrouter_credential, no_plugin_providers
):
    """The sole owner may be declared under a canonically-equivalent id
    (``Owner_X`` for ``owner-x``); the repair still fires and hands over to a
    provider id that canonicalises to that group's owner."""
    import api.config as config

    _patch_catalog(monkeypatch, _catalog(_group("Owner_X", "deepseek/deepseek-v4.1-flash")))

    result = _repair(_session(provider="openrouter"), profile_provider="vendor-x")
    assert config._canonicalise_provider_id(result) == "owner-x", result


def test_public_url_with_alias_equiv_provider_still_repaired(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """Canonical matching must not widen the local-lane protection: alias-
    equivalent provider (config ``glm`` == stored ``zai``) with a PUBLIC
    base_url is still repaired to the sole catalog owner."""
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))
    profile_config = {"model": {"provider": "glm", "base_url": "https://relay.example.com/v1"}}

    assert _repair(_session(provider="zai"), profile_provider="nous",
                   profile_config=profile_config) == "nous"


def test_local_url_for_alias_other_provider_still_repaired(
    monkeypatch, no_provider_credentials, no_plugin_providers
):
    """Canonicalisation is not a blanket pass: config provider ``glm``
    canonicalises to ``zai`` which is NOT the stored ``my-local`` lane, so the
    loopback URL protects nobody and the stale lane is repaired."""
    _patch_catalog(monkeypatch, _catalog(_group("zai", "deepseek/deepseek-v4.1-flash")))
    profile_config = {"model": {"provider": "glm", "base_url": "http://127.0.0.1:8000/v1"}}

    assert _repair(_session(provider="my-local"), profile_provider="z-ai",
                   profile_config=profile_config) == "zai"


def test_canonicalisation_failure_preserves_lane_fail_safe(
    monkeypatch, no_openrouter_credential, no_plugin_providers
):
    """Maintainer exact-fix: "if canonicalization raises, preserve the lane
    (keep the fail-safe)". A raising ``_canonicalise_provider_id`` on the
    owner-matching path must never clear or swap the stored provider.
    Patched in BOTH namespaces so it intercepts whichever binding the repair
    uses (routes-level import or an in-function ``api.config`` import)."""
    import api.config as config

    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))
    _boom = lambda _name: (_ for _ in ()).throw(RuntimeError("boom"))
    monkeypatch.setattr(routes, "_canonicalise_provider_id", _boom, raising=False)
    monkeypatch.setattr(config, "_canonicalise_provider_id", _boom, raising=True)

    assert _repair(_session()) == "openrouter"


@pytest.mark.parametrize(
    "provider",
    ["llama-cpp", "tabbyapi", "koboldcpp", "textgen", "localai"],
)
def test_raw_keyless_local_server_variants_preserved_without_catalog(
    monkeypatch, no_provider_credentials, no_plugin_providers, provider
):
    """Complete the _LOCAL_SERVER_PROVIDERS matrix from the gate checklist:
    every local-server name (not just vllm/llamacpp/tabby) survives with no
    catalog group and no key."""
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session(provider=provider)) == provider


@pytest.mark.parametrize("provider", ["llama_cpp"])
def test_local_server_underscore_forms_preserved(
    monkeypatch, no_provider_credentials, no_plugin_providers, provider
):
    """``llama_cpp`` folds to the documented local-server name ``llama-cpp``."""
    _patch_catalog(monkeypatch, _catalog(_group("nous", "deepseek/deepseek-v4.1-flash")))

    assert _repair(_session(provider=provider)) == provider


def test_chat_start_keeps_canonically_declared_local_lane(
    monkeypatch, tmp_path, no_provider_credentials, no_plugin_providers
):
    """End-to-end: profile declares the top-level local lane as
    ``provider: my_local`` while the session stores ``my-local``. Chat start
    must pass the lane through unswapped and unpersisted."""
    profile_cfg = {"model": {"provider": "my_local", "base_url": "http://127.0.0.1:8000/v1"}}
    captured, provider_after, provider_before = _run_chat_start_local_lane(
        monkeypatch, tmp_path, session_id="issue-7594-canon-lane", provider="my-local",
        profile_cfg=profile_cfg,
    )

    assert provider_before == "my-local"
    assert captured["model_provider"] == "my-local", captured["model_provider"]
    assert provider_after == "my-local", provider_after


def test_chat_start_repairs_stale_lane_declared_with_underscore(
    monkeypatch, tmp_path, no_provider_credentials, no_plugin_providers
):
    """End-to-end converse: a stale ``my_local`` session with NO local-lane
    evidence behind it must still be repaired and persisted to the sole
    catalog owner — canonicalisation may not become a preserve-everything
    bypass."""
    captured, provider_after, provider_before = _run_chat_start_local_lane(
        monkeypatch, tmp_path, session_id="issue-7594-canon-repair", provider="my_local",
        profile_cfg={"model": {"provider": "nous"}},
    )

    assert provider_before == "my_local"
    assert captured["model_provider"] == "nous", captured["model_provider"]
    assert provider_after == "nous", provider_after


def _capture_provider_saves(monkeypatch):
    """Observe the provider handed to session.save by real chat preparation."""
    saved = []
    prepare = routes._prepare_chat_start_session_for_stream

    def capture_save(session, **kwargs):
        session.save = lambda: saved.append(session.model_provider)
        return prepare(session, **kwargs)

    monkeypatch.setattr(routes, "_prepare_chat_start_session_for_stream", capture_save)
    return saved


def test_chat_start_preserves_underscore_named_custom_provider_and_saves(
    monkeypatch, tmp_path, no_provider_credentials, no_plugin_providers
):
    """#7594 review: raw custom names survive absent credentials/catalog groups."""
    import api.config as config

    profile = {
        "model": {"provider": "nous"},
        "custom_providers": [
            {"name": "my_local", "base_url": "http://127.0.0.1:8080/v1"},
        ],
    }
    monkeypatch.setattr(config, "cfg", profile)
    saved = _capture_provider_saves(monkeypatch)
    captured, after, before = _run_chat_start_local_lane(
        monkeypatch, tmp_path, session_id="issue-7594-raw-custom",
        provider="my_local", profile_cfg=profile,
    )

    assert before == "my_local"
    assert (captured["model_provider"], after, saved) == (
        "my_local", "my_local", ["my_local"],
    )


def test_chat_start_preserves_underscore_plugin_provider_and_saves(
    monkeypatch, tmp_path, no_provider_credentials
):
    """#7594 review: a registered raw plugin id must not become a foreign lane."""
    import api.config as config
    import api.plugin_providers as plugins

    profile = {"model": {"provider": "nous"}, "custom_providers": []}
    monkeypatch.setattr(config, "cfg", profile)
    monkeypatch.setattr(
        plugins, "plugin_model_provider_profiles",
        lambda: {"plugin_x": SimpleNamespace(name="plugin_x")},
    )
    saved = _capture_provider_saves(monkeypatch)
    captured, after, before = _run_chat_start_local_lane(
        monkeypatch, tmp_path, session_id="issue-7594-raw-plugin",
        provider="plugin_x", profile_cfg=profile,
    )

    assert before == "plugin_x"
    assert (captured["model_provider"], after, saved) == (
        "plugin_x", "plugin_x", ["plugin_x"],
    )
