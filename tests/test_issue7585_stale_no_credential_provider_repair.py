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
