"""Regression coverage for catalog-backed session provider repair (#5731)."""

from types import SimpleNamespace

import pytest

import api.routes as routes


def _catalog(*groups):
    return {"groups": list(groups)}


def _group(provider_id, *models):
    return {"provider_id": provider_id, "models": [{"id": model} for model in models]}


def _session(*, model="kilo/minimax/minimax-m3", provider="ollama"):
    return SimpleNamespace(model=model, model_provider=provider)


def _repair(
    session,
    catalog,
    *,
    requested_model=None,
    requested_provider=None,
    resolved_model=None,
    profile_provider="kilocode",
    explicit_model_pick=False,
):
    return routes._repair_foreign_session_model_provider(
        session,
        requested_model=requested_model if requested_model is not None else session.model,
        requested_provider=requested_provider if requested_provider is not None else session.model_provider,
        resolved_model=resolved_model if resolved_model is not None else session.model,
        resolved_provider=session.model_provider,
        explicit_model_pick=explicit_model_pick,
        profile_provider=profile_provider,
    )


def test_poisoned_pair_repairs_at_chat_start(monkeypatch, tmp_path):
    """A plain send repairs the documented Kilo model before normal persistence."""
    session = SimpleNamespace(
        session_id="issue-5731",
        workspace=str(tmp_path),
        model="kilo/minimax/minimax-m3",
        model_provider="ollama",
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
        save=lambda: None,
    )
    captured = {}
    catalog_calls = []

    def start_run(s, **kwargs):
        captured.update(kwargs)
        routes._prepare_chat_start_session_for_stream(
            s,
            msg=kwargs["msg"],
            attachments=kwargs["attachments"],
            workspace=kwargs["workspace"],
            model=kwargs["model"],
            model_provider=kwargs["model_provider"],
            stream_id="issue-5731-stream",
        )
        return {"stream_id": "issue-5731-stream"}

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid, **_kwargs: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {"model": {"provider": "kilocode"}}))
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *, prefer_cache=False: (
            catalog_calls.append(prefer_cache)
            or _catalog(
                _group("ollama", "llama3.2"),
                _group("kilocode", "@kilocode:kilo/minimax/minimax-m3"),
            )
        ),
    )
    monkeypatch.setattr(routes, "_start_run", start_run)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    routes._handle_chat_start(None, {"session_id": session.session_id, "message": "continue"})

    assert captured["model_provider"] == "kilocode", captured["model_provider"]
    assert session.model == "kilo/minimax/minimax-m3"
    assert session.model_provider == "kilocode"
    assert catalog_calls == [True]


def test_catalog_equivalent_owner_repairs_poisoned_pair(monkeypatch):
    session = _session(model="gpt-4o-mini", provider="ollama")
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *, prefer_cache=False: _catalog(
            _group("ollama", "llama3.2"),
            _group("kilocode", "GPT.4O.MINI"),
        ),
    )

    assert _repair(session, None) == "kilocode"


def test_equivalent_request_model_repairs_poisoned_pair(monkeypatch):
    session = _session(model="GPT.4O.MINI", provider="ollama")
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *, prefer_cache=False: _catalog(
            _group("ollama", "llama3.2"),
            _group("kilocode", "gpt-4o-mini"),
        ),
    )

    assert _repair(
        session,
        None,
        requested_model="gpt-4o-mini",
        resolved_model="gpt-4o-mini",
    ) == "kilocode"


@pytest.mark.parametrize("provider", ["ollama", "lmstudio"])
def test_self_hosted_exact_owner_is_preserved(monkeypatch, provider):
    session = _session(model="vendor/model/with/slashes", provider=provider)
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *, prefer_cache=False: _catalog(
            _group(provider, "vendor/model/with/slashes"),
            _group("kilocode", "other-model"),
        ),
    )

    assert _repair(session, None) == provider


def test_stored_owner_in_extra_models_is_preserved(monkeypatch):
    session = _session()
    stored_group = _group("ollama", "llama3.2")
    stored_group["extra_models"] = [{"id": "kilo/minimax/minimax-m3"}]
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *, prefer_cache=False: _catalog(
            stored_group,
            _group("kilocode", "kilo/minimax/minimax-m3"),
        ),
    )

    assert _repair(session, None) == "ollama"


def test_stored_provider_discovery_failure_is_preserved(monkeypatch):
    session = _session()
    stored_group = _group("ollama", "llama3.2")
    stored_group["models_endpoint_error"] = "catalog unavailable"
    monkeypatch.setattr(
        routes,
        "get_available_models",
        lambda *, prefer_cache=False: _catalog(
            stored_group,
            _group("kilocode", "kilo/minimax/minimax-m3"),
        ),
    )

    assert _repair(session, None) == "ollama"


@pytest.mark.parametrize(
    ("requested_model", "explicit_model_pick"),
    [
        ("kilo/minimax/minimax-m3", True),
        ("@ollama:kilo/minimax/minimax-m3", False),
    ],
)
def test_explicit_or_qualified_model_selection_is_preserved(monkeypatch, requested_model, explicit_model_pick):
    session = _session()
    monkeypatch.setattr(routes, "get_available_models", lambda **_kwargs: pytest.fail("catalog must not be read"))

    assert _repair(
        session,
        None,
        requested_model=requested_model,
        explicit_model_pick=explicit_model_pick,
    ) == "ollama"


@pytest.mark.parametrize(
    "catalog",
    [
        _catalog(_group("kilocode", "kilo/minimax/minimax-m3")),
        _catalog(_group("ollama", "llama3.2")),
        _catalog(
            _group("ollama", "llama3.2"),
            _group("kilocode", "kilo/minimax/minimax-m3"),
            _group("other", "kilo/minimax/minimax-m3"),
        ),
    ],
)
def test_missing_or_ambiguous_catalog_evidence_is_preserved(monkeypatch, catalog):
    session = _session()
    monkeypatch.setattr(routes, "get_available_models", lambda *, prefer_cache=False: catalog)

    assert _repair(session, catalog) == "ollama"


def test_matching_profile_provider_skips_catalog(monkeypatch):
    session = _session()
    monkeypatch.setattr(routes, "get_available_models", lambda **_kwargs: pytest.fail("catalog must not be read"))

    assert _repair(session, None, profile_provider="ollama") == "ollama"


@pytest.mark.parametrize(
    ("body", "catalog"),
    [
        ({}, _catalog(_group("ollama", "kilo/minimax/minimax-m3"))),
        ({"explicit_model_pick": True}, _catalog(_group("kilocode", "kilo/minimax/minimax-m3"))),
        ({"model": "@ollama:kilo/minimax/minimax-m3"}, _catalog(_group("ollama", "kilo/minimax/minimax-m3"))),
        (
            {},
            _catalog(
                _group("ollama", "llama3.2"),
                _group("kilocode", "kilo/minimax/minimax-m3"),
                _group("other", "kilo/minimax/minimax-m3"),
            ),
        ),
        ({}, RuntimeError("catalog unavailable")),
    ],
)
def test_preservation_cases_still_reach_chat_start(monkeypatch, tmp_path, body, catalog):
    session = SimpleNamespace(
        session_id="issue-5731-preserve",
        workspace=str(tmp_path),
        model="kilo/minimax/minimax-m3",
        model_provider="ollama",
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
    )
    captured = {}

    def get_catalog(*, prefer_cache=False):
        if isinstance(catalog, Exception):
            raise catalog
        return catalog

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid, **_kwargs: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {"model": {"provider": "kilocode"}}))
    monkeypatch.setattr(routes, "get_available_models", get_catalog)
    monkeypatch.setattr(routes, "_start_run", lambda _s, **kwargs: captured.update(kwargs) or {"ok": True})
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    routes._handle_chat_start(
        None,
        {"session_id": session.session_id, "message": "continue", **body},
    )

    assert captured["model_provider"] == "ollama"
