"""Regression coverage for atomic automated model/provider transitions (OPS-359)."""

import threading

from types import SimpleNamespace

import pytest

import api.config as config
import api.routes as routes


def _catalog(*groups):
    return {"groups": list(groups)}


def _group(provider_id, *models, error=None):
    group = {
        "provider_id": provider_id,
        "models": [{"id": model} for model in models],
    }
    if error:
        group["models_endpoint_error"] = error
    return group


@pytest.mark.parametrize("model", ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol"])
def test_explicit_pair_repairs_stale_xai_provider_from_unique_cached_owner(monkeypatch, model):
    calls = []
    monkeypatch.setattr(
        routes,
        "get_nonblocking_available_models_snapshot",
        lambda: (
            calls.append(True)
            or _catalog(
                _group("xai-oauth", "grok-4.3"),
                _group("openai-codex", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol"),
            )
        ),
    )

    assert routes._resolve_compatible_session_model_state(model, "xai-oauth") == (
        model,
        "openai-codex",
        True,
    )
    assert calls == [True]


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("gpt-5.6-terra", "openai-codex"),
        ("grok-4.3", "xai-oauth"),
    ],
)
def test_explicit_pair_preserves_correct_cached_owner(monkeypatch, model, provider):
    monkeypatch.setattr(
        routes,
        "get_nonblocking_available_models_snapshot",
        lambda: _catalog(
            _group("xai-oauth", "grok-4.3"),
            _group("openai-codex", "gpt-5.6-terra"),
        ),
    )

    assert routes._resolve_compatible_session_model_state(model, provider) == (
        model,
        provider,
        False,
    )


def test_explicit_pair_preserves_legitimate_shared_model_id(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_nonblocking_available_models_snapshot",
        lambda: _catalog(
            _group("xai-oauth", "shared-model"),
            _group("openai-codex", "shared-model"),
        ),
    )

    assert routes._resolve_compatible_session_model_state("shared-model", "xai-oauth") == (
        "shared-model",
        "xai-oauth",
        False,
    )


@pytest.mark.parametrize(
    "catalog",
    [
        _catalog(
            _group("xai-oauth", "grok-4.3"),
            _group("openai-codex", "gpt-5.6-terra"),
            _group("openrouter", "gpt-5.6-terra"),
        ),
        _catalog(
            _group("xai-oauth", "grok-4.3", error="catalog unavailable"),
            _group("openai-codex", "gpt-5.6-terra"),
        ),
        _catalog(_group("openai-codex", "gpt-5.6-terra")),
    ],
)
def test_explicit_pair_does_not_infer_without_complete_unique_ownership(monkeypatch, catalog):
    monkeypatch.setattr(routes, "get_nonblocking_available_models_snapshot", lambda: catalog)

    assert routes._resolve_compatible_session_model_state("gpt-5.6-terra", "xai-oauth") == (
        "gpt-5.6-terra",
        "xai-oauth",
        False,
    )


def test_explicit_pair_does_not_infer_when_cached_catalog_raises(monkeypatch):
    def unavailable():
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(routes, "get_nonblocking_available_models_snapshot", unavailable)

    assert routes._resolve_compatible_session_model_state("gpt-5.6-terra", "xai-oauth") == (
        "gpt-5.6-terra",
        "xai-oauth",
        False,
    )


def test_chat_start_persists_repaired_model_provider_pair(monkeypatch, tmp_path):
    saves = []
    session = SimpleNamespace(
        session_id="ops-359",
        workspace=str(tmp_path),
        model="gpt-5.6-terra",
        model_provider="xai-oauth",
        profile="default",
        messages=[],
        context_messages=[],
        pending_user_message=None,
        title="OPS-359",
        session_source=None,
        save=lambda *args, **kwargs: saves.append((args, kwargs)),
    )
    captured = {}

    def start_run(current, **kwargs):
        captured.update(kwargs)
        routes._prepare_chat_start_session_for_stream(
            current,
            msg=kwargs["msg"],
            attachments=kwargs["attachments"],
            workspace=kwargs["workspace"],
            model=kwargs["model"],
            model_provider=kwargs["model_provider"],
            stream_id="ops-359-stream",
        )
        return {
            "stream_id": "ops-359-stream",
            "effective_model_provider": kwargs["model_provider"],
        }

    monkeypatch.setattr(routes, "_get_or_materialize_session", lambda _sid, **_kwargs: session)
    monkeypatch.setattr(routes, "_resolve_chat_workspace_with_recovery", lambda _s, _w: str(tmp_path))
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda _s, _p: (None, None, {}))
    monkeypatch.setattr(
        routes,
        "get_nonblocking_available_models_snapshot",
        lambda: _catalog(
            _group("xai-oauth", "grok-4.3"),
            _group("openai-codex", "gpt-5.6-terra"),
        ),
    )
    monkeypatch.setattr(routes, "_start_run", start_run)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, status=200: payload)

    response = routes._handle_chat_start(
        None,
        {"session_id": session.session_id, "message": "resume automatically"},
    )

    assert captured["model"] == "gpt-5.6-terra"
    assert captured["model_provider"] == "openai-codex"
    assert session.model == "gpt-5.6-terra"
    assert session.model_provider == "openai-codex"
    assert response["effective_model_provider"] == "openai-codex"
    assert saves


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("gpt-5.6-terra", "xai-oauth"),
        ("@openrouter:anthropic/claude-opus-4.7", "openrouter"),
        ("openai/gpt-5.4-mini", "openai-codex"),
    ],
)
def test_explicit_pair_resolution_never_waits_for_models_cache_lock(monkeypatch, model, provider):
    """A catalog rebuild cannot delay any explicit-pair chat start."""
    lock_held = threading.Event()
    release_lock = threading.Event()
    builder_called = threading.Event()
    resolved = []
    finished = threading.Event()

    def hold_models_cache_lock():
        with config._available_models_cache_lock:
            lock_held.set()
            release_lock.wait(timeout=2.0)

    def blocking_catalog_builder(*, prefer_cache=False):
        builder_called.set()
        with config._available_models_cache_lock:
            return _catalog(_group("openai-codex", "gpt-5.6-terra"))

    def resolve_pair():
        try:
            resolved.append(
                routes._resolve_compatible_session_model_state(
                    model,
                    provider,
                )
            )
        finally:
            finished.set()

    monkeypatch.setattr(routes, "get_available_models", blocking_catalog_builder)
    monkeypatch.setattr(config, "_load_models_cache_from_disk", lambda: None)
    holder = threading.Thread(target=hold_models_cache_lock, daemon=True)
    worker = threading.Thread(target=resolve_pair, daemon=True)
    holder.start()
    assert lock_held.wait(timeout=1.0)
    worker.start()
    try:
        assert finished.wait(timeout=0.2), (
            "explicit-pair resolution waited for _available_models_cache_lock"
        )
    finally:
        release_lock.set()
        holder.join(timeout=1.0)
        worker.join(timeout=1.0)

    assert resolved == [(model, provider, False)]
    assert not builder_called.is_set()


def test_requested_provider_alias_matches_single_canonical_catalog_group(monkeypatch):
    """A unique canonical alias group can prove that the requested owner is stale."""
    monkeypatch.setattr(
        routes,
        "get_nonblocking_available_models_snapshot",
        lambda: _catalog(
            _group("anthropic", "claude-sonnet-4.6"),
            _group("openai-codex", "gpt-5.6-terra"),
        ),
    )

    assert routes._resolve_compatible_session_model_state(
        "gpt-5.6-terra",
        "claude-code",
    ) == ("gpt-5.6-terra", "openai-codex", True)


def test_requested_provider_canonical_group_ambiguity_preserves_explicit_pair(monkeypatch):
    """Several groups in one provider family are not ownership proof."""
    monkeypatch.setattr(
        routes,
        "get_nonblocking_available_models_snapshot",
        lambda: _catalog(
            _group("openai", "gpt-4.1"),
            _group("openai-codex", "gpt-5.6-terra"),
            _group("anthropic", "claude-sonnet-4.6"),
        ),
    )

    assert routes._resolve_compatible_session_model_state(
        "claude-sonnet-4.6",
        "gpt",
    ) == ("claude-sonnet-4.6", "gpt", False)
