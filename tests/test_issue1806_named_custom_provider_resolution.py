"""Regression tests for #1806 named custom provider routing.

The WebUI must treat ``model.provider: <custom_providers[].name>`` as the
same provider slug the picker emits: ``custom:<name>``.  Otherwise a stale
agent-side base-url slug such as ``custom:local-(127.0.0.1:11434)`` can win
model selection and send runtime auth down an impossible env-var path.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sys
import types

import pytest

import api.config as config


@pytest.fixture(autouse=True)
def _isolate_models_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_models_cache_path", tmp_path / "models_cache.json")
    config.invalidate_models_cache()
    yield
    config.invalidate_models_cache()


def _with_ollama_local_config():
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "carnice-9b:latest",
                "provider": "ollama-local",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": "ollama",
            },
            "custom_providers": [
                {
                    "name": "ollama-local",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key": "ollama",
                    "model": "carnice-9b:latest",
                }
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()

    def restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    return restore


def test_model_provider_name_resolves_to_named_custom_slug():
    restore = _with_ollama_local_config()
    try:
        model, provider, base_url = config.resolve_model_provider("carnice-9b:latest")
    finally:
        restore()

    assert model == "carnice-9b:latest"
    assert provider == "custom:ollama-local"
    assert base_url == "http://127.0.0.1:11434/v1"


def test_available_models_drops_base_url_derived_custom_slug(monkeypatch):
    """A stale agent catalog slug must not create a second local custom group."""
    fake_models = types.ModuleType("hermes_cli.models")
    fake_models.list_available_providers = lambda: [
        {"id": "custom:local-(127.0.0.1:11434)", "authenticated": True},
    ]
    fake_auth = types.ModuleType("hermes_cli.auth")
    fake_auth.get_auth_status = lambda _pid: {"key_source": "config_yaml"}
    monkeypatch.setitem(sys.modules, "hermes_cli.models", fake_models)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", fake_auth)
    monkeypatch.setattr(config, "_get_auth_store_path", lambda: config.Path("/tmp/does-not-exist-auth.json"))
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [])

    class _Resp:
        def read(self):
            return json.dumps(
                {"data": [{"id": "carnice-9b:latest", "name": "carnice-9b:latest"}]}
            ).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())

    restore = _with_ollama_local_config()
    try:
        result = config.get_available_models()
    finally:
        restore()

    assert result["active_provider"] == "custom:ollama-local"
    groups_by_id = {g["provider_id"]: g for g in result["groups"]}
    assert "custom:ollama-local" in groups_by_id
    assert "custom:local-(127.0.0.1:11434)" not in groups_by_id
    assert "ollama-local" not in groups_by_id

    named_models = [m["id"] for m in groups_by_id["custom:ollama-local"]["models"]]
    assert "carnice-9b:latest" in named_models


def _with_multi_custom_provider_config():
    """Active custom provider PLUS a second, non-active named custom provider.

    Mirrors the config.yaml shape that has no ``providers:`` map at all: every
    endpoint lives in ``custom_providers:``, and only one of them is the active
    ``model.provider``.
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "active/model",
                "provider": "custom:active",
                "base_url": "https://active.example/v1",
                "api_key": "active-key",
            },
            "custom_providers": [
                {
                    "name": "active",
                    "base_url": "https://active.example/v1",
                    "api_key": "active-key",
                },
                {
                    "name": "omni",
                    "base_url": "https://omni.example/v1",
                    "api_key": "omni-key",
                },
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()

    def restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    return restore


def test_provider_qualified_id_uses_nonactive_custom_provider_base_url():
    """``@custom:omni:<model>`` must route to omni's base_url, not the default.

    ``_get_provider_base_url`` only reads ``providers:`` and the ACTIVE
    ``model.base_url``, so a named custom provider that lives solely in
    ``custom_providers:`` used to resolve to base_url=None. The WebUI then sent
    the bare model to the active endpoint and got back HTTP 400 "Invalid model
    format or no credentials for provider: <bare-model>".
    """
    restore = _with_multi_custom_provider_config()
    try:
        model, provider, base_url = config.resolve_model_provider(
            "@custom:omni:antigravity/gemini-3.7-flash-tiered"
        )
    finally:
        restore()

    assert model == "antigravity/gemini-3.7-flash-tiered"
    assert provider == "custom:omni"
    assert base_url == "https://omni.example/v1"


def test_provider_qualified_unknown_custom_slug_keeps_base_url_none():
    """An UNKNOWN ``custom:`` slug must stay base_url=None -- never a guess.

    Slugs derived from a base-url authority (``custom:local-(127.0.0.1:11434)``)
    or from a provider that is simply not in ``custom_providers:`` have no
    endpoint of their own. Guessing one (e.g. "there is only one custom provider,
    use it" or "fall back to the active ``model.base_url``") would persist a
    stale endpoint for that slug, which is the #4728 regression. Preserve the
    prior behaviour: no unique matching entry -> no base_url.
    """
    restore = _with_multi_custom_provider_config()
    try:
        model, provider, base_url = config.resolve_model_provider(
            "@custom:not-configured:qwen/qwen-1.5b"
        )
    finally:
        restore()

    assert model == "qwen/qwen-1.5b"
    assert provider == "custom:not-configured"
    assert base_url is None


def test_provider_qualified_active_custom_slug_still_resolves():
    """The ACTIVE custom provider keeps resolving to its own endpoint."""
    restore = _with_multi_custom_provider_config()
    try:
        model, provider, base_url = config.resolve_model_provider(
            "@custom:active:antigravity/gemini-3.7-flash-tiered"
        )
    finally:
        restore()

    assert model == "antigravity/gemini-3.7-flash-tiered"
    assert provider == "custom:active"
    assert base_url == "https://active.example/v1"


def test_provider_qualified_non_custom_provider_is_unaffected():
    """A non-``custom:`` @provider hint must not pick up a custom endpoint.

    The custom_providers lookup is gated on the ``custom:`` prefix, so an
    @openrouter route still resolves through _get_provider_base_url() -- None
    here, since openrouter is neither the active provider nor in ``providers:``.
    """
    restore = _with_multi_custom_provider_config()
    try:
        model, provider, base_url = config.resolve_model_provider(
            "@openrouter:anthropic/claude-sonnet-4.6"
        )
    finally:
        restore()

    assert model == "anthropic/claude-sonnet-4.6"
    assert provider == "openrouter"
    assert base_url is None


def _with_keyed_and_list_provider_config():
    """Same slug present BOTH as a ``providers:`` key and a ``custom_providers`` entry.

    Deployments that started on the legacy ``custom_providers:`` list and later
    gained a keyed ``providers:`` map can carry two records for one slug, each
    with its own ``base_url``. The other fixtures in this file deliberately model
    the list-only shape, so this one pins which record wins.
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "active/model",
                "provider": "custom:active",
                "base_url": "https://active.example/v1",
                "api_key": "active-key",
            },
            "providers": {
                "custom:omni": {
                    "base_url": "https://omni-keyed.example/v1",
                    "api_key": "omni-keyed-key",
                },
            },
            "custom_providers": [
                {
                    "name": "active",
                    "base_url": "https://active.example/v1",
                    "api_key": "active-key",
                },
                {
                    "name": "omni",
                    "base_url": "https://omni-list.example/v1",
                    "api_key": "omni-list-key",
                },
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()

    def restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    return restore


def test_list_entry_wins_over_keyed_providers_entry_for_same_slug():
    """``custom_providers[]`` outranks a same-slug ``providers:`` key.

    The named entry is the record the picker's ``custom:<slug>`` id is MINTED
    from (``_custom_provider_slug_from_name`` reads ``custom_providers[].name``),
    and it is the one credential resolution scans, so the endpoint must come from
    that same entry -- otherwise a stale keyed ``providers:`` leftover could pair
    entry A's URL with entry B's API key. Guards the precedence of the
    ``custom_base_url if custom_base_url is not None else _get_provider_base_url()``
    ordering: both lookups return a URL here, so a flipped order would silently
    route to ``omni-keyed`` instead.
    """
    restore = _with_keyed_and_list_provider_config()
    try:
        # Sanity: the keyed entry really is resolvable, so this test would fail
        # (not merely pass vacuously on a None fallback) if precedence flipped.
        keyed_base_url = config._get_provider_base_url("custom:omni")
        model, provider, base_url = config.resolve_model_provider(
            "@custom:omni:antigravity/gemini-3.7-flash-tiered"
        )
        conn_api_key, conn_base_url = config.resolve_custom_provider_connection("custom:omni")
    finally:
        restore()

    assert keyed_base_url == "https://omni-keyed.example/v1"
    assert model == "antigravity/gemini-3.7-flash-tiered"
    assert provider == "custom:omni"
    assert base_url == "https://omni-list.example/v1"
    assert conn_api_key == "omni-list-key"
    assert conn_base_url == "https://omni-list.example/v1"
    assert (base_url, conn_api_key) == ("https://omni-list.example/v1", "omni-list-key")


def _with_keyed_and_blank_list_provider_config():
    """Same slug present in ``providers:`` and as a ``custom_providers`` entry with blank base_url.

    The list entry exists and has a distinct API key, but its ``base_url`` is
    empty. The keyed ``providers:`` entry has both a valid ``base_url`` and its
    own distinct API key.
    """
    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(
        {
            "model": {
                "default": "active/model",
                "provider": "custom:active",
                "base_url": "https://active.example/v1",
                "api_key": "active-key",
            },
            "providers": {
                "custom:omni": {
                    "base_url": "https://omni-keyed.example/v1",
                    "api_key": "omni-keyed-key",
                },
            },
            "custom_providers": [
                {
                    "name": "active",
                    "base_url": "https://active.example/v1",
                    "api_key": "active-key",
                },
                {
                    "name": "omni",
                    "base_url": "",
                    "api_key": "omni-list-key",
                },
            ],
        }
    )
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()

    def restore():
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    return restore


def test_blank_list_entry_does_not_fall_through_to_keyed_providers_endpoint():
    """A blank ``custom_providers[].base_url`` must not fall through to a keyed endpoint.

    When a slug exists both in ``custom_providers[]`` (with a blank or empty
    base_url and API key K_list) and in ``providers:`` (with a populated base_url
    and API key K_keyed), resolution must treat the list entry as authoritative.
    Falling through to ``_get_provider_base_url()`` on empty base_url would pair
    the keyed record's endpoint with the list record's API key, violating the
    same-entry invariant between resolve_model_provider() and
    resolve_custom_provider_connection().
    """
    restore = _with_keyed_and_blank_list_provider_config()
    try:
        keyed_base_url = config._get_provider_base_url("custom:omni")
        model, provider, base_url = config.resolve_model_provider(
            "@custom:omni:antigravity/gemini-3.7-flash-tiered"
        )
        conn_api_key, conn_base_url = config.resolve_custom_provider_connection("custom:omni")
    finally:
        restore()

    assert keyed_base_url == "https://omni-keyed.example/v1"
    assert model == "antigravity/gemini-3.7-flash-tiered"
    assert provider == "custom:omni"
    assert base_url is None
    assert conn_base_url is None
    assert conn_api_key == "omni-list-key"
    assert (base_url, conn_api_key) != ("https://omni-keyed.example/v1", "omni-list-key")


def _setup_production_composed_runtime(
    monkeypatch,
    cfg_dict,
    runtime_dict,
    session_id="test-session-1806",
    fail_first=None,
    heal_mutate=None,
):
    """Compose the production streaming send path around a capturing agent.

    ``fail_first`` drives the two 401 self-heal retry paths that rebuild the
    runtime bundle a second time:

    * ``"returned_error"`` -- the first ``run_conversation`` RETURNS an auth
      error without raising (``api/streaming.py`` returned-error retry).
    * ``"raised"`` -- the first ``run_conversation`` RAISES it
      (``api/streaming.py`` raised-exception retry).

    Both make ``_attempt_credential_self_heal`` hand back the same ambient
    runtime dict production would re-resolve, so the retry sees the identical
    truthy side-field sentinels the initial send did.

    ``heal_mutate`` is the hook for the state a retry guard actually defends
    against: it runs INSIDE the stubbed self-heal, i.e. after the first agent
    has already been constructed and has already failed with a 401, but BEFORE
    the retry re-resolves its bundle. Mutating ``config.cfg`` there reproduces
    the row being edited, unnamed or drained mid-turn -- the only way a route
    that was routable at first resolution becomes terminal at retry
    construction.
    """
    from unittest import mock
    import queue
    import api.streaming as streaming
    import api.oauth

    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()

    old_cfg = dict(config.cfg)
    old_mtime = config._cfg_mtime
    old_path = getattr(config, "_cfg_path", None)
    config.cfg.clear()
    config.cfg.update(cfg_dict)
    try:
        config._cfg_mtime = config.Path(config._get_config_path()).stat().st_mtime
    except Exception:
        config._cfg_mtime = 0.0
    config._cfg_path = config._get_config_path()

    class FakeSession:
        def __init__(self):
            self.session_id = session_id
            self.title = "Test"
            self.workspace = "/tmp"
            self.model = "test-model"
            self.messages = []
            self.personality = None
            self.input_tokens = 0
            self.output_tokens = 0
            self.estimated_cost = None
            self.tool_calls = []
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.pending_user_source = None
            self.profile = None

        def save(self, touch_updated_at=True, skip_index=False):
            self._saved = touch_updated_at

        def compact(self):
            return {"session_id": self.session_id, "messages": self.messages}

    captured = {}

    class CapturingAgent:
        # Every constructor-routing field must be a NAMED parameter. The
        # streaming path gates each optional kwarg on
        # ``inspect.signature(AIAgent.__init__)``, so a double that only accepts
        # model/provider/base_url/api_key silently filters the runtime-owned
        # fields out — hiding exactly the stale-authority defect these tests pin.
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            api_mode=None,
            acp_command=None,
            acp_args=None,
            credential_pool=None,
            **kwargs,
        ):
            captured["init_kwargs"] = {
                "model": model,
                "provider": provider,
                "base_url": base_url,
                "api_key": api_key,
                "api_mode": api_mode,
                "acp_command": acp_command,
                "acp_args": acp_args,
                "credential_pool": credential_pool,
                **kwargs,
            }
            captured.setdefault("init_kwargs_history", []).append(
                dict(captured["init_kwargs"])
            )
            captured.setdefault("instances", []).append(self)
            # Mirror ``agent/agent_init.py:_init_openai_client()``:
            #
            #     if api_key and base_url:
            #         client_kwargs = _explicit_client_kwargs(...)
            #     else:
            #         client_kwargs = _routed_client_kwargs(...)
            #
            # An incomplete connection pair is NOT a refusal at this boundary —
            # it is the signal to resolve a provider all over again, through the
            # centralized router and then the init-time fallback chain. Recording
            # the branch here is what lets a test assert the real defect ("this
            # send would have been re-routed") instead of the weaker proxy
            # ("base_url came back None"), which an unroutable bundle satisfies
            # while still reaching a provider the user never chose.
            if api_key and base_url:
                captured.setdefault("explicit_client_kwargs_calls", []).append(
                    {"api_key": api_key, "base_url": base_url}
                )
            else:
                captured.setdefault("routed_client_kwargs_calls", []).append(
                    {"provider": provider, "api_key": api_key, "base_url": base_url}
                )
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            captured["run_kwargs"] = kwargs
            captured["run_calls"] = captured.get("run_calls", 0) + 1
            if fail_first is not None and captured["run_calls"] == 1:
                if fail_first == "raised":
                    raise RuntimeError("401 Unauthorized")
                return {"messages": [], "error": "401 Unauthorized"}
            return {
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ]
            }

        def interrupt(self, _message):
            captured["interrupted"] = _message

    fake_session = FakeSession()
    fake_stream_id = f"stream-{session_id}"
    # Both the returned-error retry branch AND the outer error emission live
    # behind the stale-writeback guard, which only lets the OWNING worker
    # persist and emit. /api/chat/start stamps ``active_stream_id`` before
    # dispatching the worker, so model that here unconditionally — otherwise the
    # guard returns early and a terminal route verdict reaches the queue as
    # nothing at all, which a refusal test would read as "no controlled failure".
    fake_session.active_stream_id = fake_stream_id
    fake_queue = queue.Queue()

    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(return_value=dict(runtime_dict))
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.runtime_provider = fake_runtime_module
    fake_hermes_state = types.ModuleType("hermes_state")
    fake_hermes_state.SessionDB = mock.Mock(return_value=object())

    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setitem(sys.modules, "hermes_state", fake_hermes_state)

    monkeypatch.setattr(
        api.oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )
    monkeypatch.setattr(streaming, "get_session", lambda _session_id: fake_session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: CapturingAgent)
    monkeypatch.setattr("api.config.get_config", lambda: dict(config.cfg))
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_args, **_kwargs: [])
    if fail_first is not None:
        # Production re-resolves the ambient runtime provider on a 401 heal, so
        # the retry must see the same truthy side-field sentinels — that is
        # exactly the state in which a partial rebuild leaks them through.
        def _fake_self_heal(*_args, **_kwargs):
            # Production re-reads provider state during the heal. Running the
            # mutation here -- not before the send -- is what makes the FIRST
            # resolution routable and only the RETRY resolution terminal.
            if heal_mutate is not None:
                heal_mutate()
            return dict(runtime_dict)

        monkeypatch.setattr(streaming, "_attempt_credential_self_heal", _fake_self_heal)

    def restore():
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.clear()
        config.cfg.clear()
        config.cfg.update(old_cfg)
        config._cfg_mtime = old_mtime
        config._cfg_path = old_path
        config.invalidate_models_cache()

    return fake_stream_id, fake_queue, captured, restore


def test_production_composed_nonblank_exact_list_row_yields_list_url_and_list_key(monkeypatch):
    """(a) Nonblank exact list row plus distinct same-slug keyed row yields list URL/list key.

    In production, resolve_runtime_provider() scans providers: first and returns
    the keyed URL and key. When an exact custom_providers[] entry matches, the
    runtime send path must atomically replace both fields so that final AIAgent
    construction receives list endpoint + list key, never keyed key.
    """
    import api.streaming as streaming

    cfg_dict = {
        "model": {"default": "active/model", "provider": "custom:active"},
        "providers": {
            "custom:omni": {
                "base_url": "https://keyed-url-sentinel.example/v1",
                "api_key": "keyed-key-sentinel-abc",
            },
        },
        "custom_providers": [
            {
                "name": "omni",
                "base_url": "https://list-url-sentinel.example/v1",
                "api_key": "list-key-sentinel-xyz",
            },
        ],
    }
    runtime_dict = {
        "provider": "custom:omni",
        "base_url": "https://keyed-url-sentinel.example/v1",
        "api_key": "keyed-key-sentinel-abc",
    }
    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch, cfg_dict, runtime_dict, session_id="session-1806-nonblank"
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id="session-1806-nonblank",
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    init_kwargs = captured["init_kwargs"]
    assert init_kwargs["base_url"] == "https://list-url-sentinel.example/v1"
    assert init_kwargs["api_key"] == "list-key-sentinel-xyz"
    assert init_kwargs["provider"] == "custom"
    assert init_kwargs["base_url"] != "https://keyed-url-sentinel.example/v1"
    assert init_kwargs["api_key"] != "keyed-key-sentinel-abc"


def test_production_composed_blank_exact_list_row_cannot_repopulate_from_keyed_row(monkeypatch):
    """(b) A blank exact list row is terminal — it cannot repopulate from the keyed row.

    When an exact ``custom_providers[]`` entry exists with an empty base_url,
    that row is authoritative and the route has no endpoint. Handing AIAgent
    ``base_url=None`` with the list key would NOT refuse: ``_init_openai_client()``
    honours an explicit pair only when both fields are truthy, so it would call
    ``_routed_client_kwargs()`` and re-resolve a provider — reaching the keyed
    row's endpoint by another door. The send must stop before construction.
    """
    cfg_dict = {
        "model": {"default": "active/model", "provider": "custom:active"},
        "providers": {
            "custom:omni": {
                "base_url": "https://keyed-url-sentinel.example/v1",
                "api_key": "keyed-key-sentinel-abc",
            },
        },
        "custom_providers": [
            {
                "name": "omni",
                "base_url": "",
                "api_key": "list-key-sentinel-xyz",
            },
        ],
    }
    runtime_dict = {
        "provider": "custom:omni",
        "base_url": "https://keyed-url-sentinel.example/v1",
        "api_key": "keyed-key-sentinel-abc",
    }

    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        cfg_dict,
        runtime_dict,
        "session-1806-blank",
        model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
    )

    payload = apperrors[-1]
    assert "resolved no endpoint" in payload["message"], payload
    assert "base_url" in payload["hint"], (
        f"the refusal must name the endpoint setting to fix: {payload}"
    )
    # The refusal must not hand the user the keyed row it declined to fall
    # through to — naming it would read as "this endpoint was used".
    assert "keyed-url-sentinel" not in str(payload), payload
    assert not captured.get("explicit_client_kwargs_calls"), (
        "a client was configured for a route with no endpoint"
    )


def test_production_composed_keyed_only_still_yields_keyed_url_and_key(monkeypatch):
    """(c) Keyed-only/no-list still yields the keyed URL/key.

    When no matching row exists in custom_providers[], the keyed providers:
    record is authoritative and supplies both base_url and api_key.
    """
    import api.streaming as streaming

    cfg_dict = {
        "model": {"default": "active/model", "provider": "custom:active"},
        "providers": {
            "custom:omni": {
                "base_url": "https://keyed-url-sentinel.example/v1",
                "api_key": "keyed-key-sentinel-abc",
            },
        },
        "custom_providers": [
            {
                "name": "active",
                "base_url": "https://active.example/v1",
                "api_key": "active-key",
            },
        ],
    }
    runtime_dict = {
        "provider": "custom:omni",
        "base_url": "https://keyed-url-sentinel.example/v1",
        "api_key": "keyed-key-sentinel-abc",
    }
    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch, cfg_dict, runtime_dict, session_id="session-1806-keyed"
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id="session-1806-keyed",
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    init_kwargs = captured["init_kwargs"]
    assert init_kwargs["base_url"] == "https://keyed-url-sentinel.example/v1"
    assert init_kwargs["api_key"] == "keyed-key-sentinel-abc"
    assert init_kwargs["provider"] == "custom"


# ─────────────────────────────────────────────────────────────────────────────
# Full-constructor bundle authority (streaming initial send + both retry paths)
#
# The connection fields are only half the constructor contract. AIAgent also
# takes ``credential_pool``, ``api_mode``, ``acp_command`` and ``acp_args`` from
# the resolved runtime provider, and the ambient provider legitimately reports
# all four (it is what the process is otherwise authenticated as). A send that
# replaces provider/base_url/api_key from an exact ``custom_providers[]`` row but
# keeps those four builds an agent whose credential source, wire protocol and
# transport still point at the previous authority — the custom HTTP endpoint gets
# Anthropic credential pooling and a Claude ACP subprocess command.
#
# ``_AMBIENT_SIDE_FIELD_RUNTIME`` seeds every one of them truthy so a
# pass-through is a hard assertion failure rather than a vacuous ``None == None``.
# ─────────────────────────────────────────────────────────────────────────────

_KEYED_VS_LIST_CFG = {
    "model": {"default": "active/model", "provider": "custom:active"},
    "providers": {
        "custom:omni": {
            "base_url": "https://keyed-url-sentinel.example/v1",
            "api_key": "keyed-key-sentinel-abc",
        },
    },
    "custom_providers": [
        {
            "name": "omni",
            "base_url": "https://list-url-sentinel.example/v1",
            "api_key": "list-key-sentinel-xyz",
        },
    ],
}

_AMBIENT_SIDE_FIELD_RUNTIME = {
    "provider": "custom:omni",
    "base_url": "https://keyed-url-sentinel.example/v1",
    "api_key": "keyed-key-sentinel-abc",
    # Runtime-owned constructor fields belonging to the AMBIENT provider.
    "credential_pool": ["ambient-pool-sentinel-1", "ambient-pool-sentinel-2"],
    "api_mode": "anthropic_messages",
    "command": "claude-code-acp-sentinel",
    "args": ["--acp-arg-sentinel"],
}

_LIST_ROW_URL = "https://list-url-sentinel.example/v1"
_LIST_ROW_KEY = "list-key-sentinel-xyz"


# Case (d): every side field in ``_AMBIENT_SIDE_FIELD_RUNTIME`` is genuinely
# FOREIGN to ``_KEYED_VS_LIST_CFG`` -- the list row owns a different endpoint and
# declares none of these for itself, so provenance proves they belong to the
# ambient provider and every one must be cleared. This is an ownership verdict,
# not a blanket "custom routes never carry side fields" rule: the cases below
# pin records that DO own them, and there the same merge must keep them.
_FOREIGN_AMBIENT_SIDE_FIELDS = {
    "api_mode": None,
    "acp_command": None,
    "acp_args": None,
    "credential_pool": None,
}


def _assert_side_fields(init_kwargs, expected, label):
    """Assert each constructor side field equals the authority that OWNS it."""
    for field, value in expected.items():
        actual = init_kwargs[field]
        if callable(value):
            assert actual is value, f"{label}: {field} lost its owner's value"
        else:
            assert actual == value, f"{label}: {field} is {actual!r}, expected {value!r}"


def _assert_exact_list_row_bundle(init_kwargs, label, side_fields=None):
    """Assert the whole constructor bundle is target-owned, not a mixed one.

    ``side_fields`` names what the exact list row's authority resolves each side
    field to; it defaults to case (d), the all-foreign ambient runtime.
    """
    assert init_kwargs["base_url"] == _LIST_ROW_URL, label
    assert init_kwargs["api_key"] == _LIST_ROW_KEY, label
    assert init_kwargs["provider"] == "custom", label
    # None of the keyed/ambient authority may survive anywhere in the bundle.
    assert init_kwargs["base_url"] != "https://keyed-url-sentinel.example/v1", label
    assert init_kwargs["api_key"] != "keyed-key-sentinel-abc", label
    _assert_side_fields(
        init_kwargs,
        _FOREIGN_AMBIENT_SIDE_FIELDS if side_fields is None else side_fields,
        label,
    )


def test_capturing_agent_exposes_every_runtime_constructor_field(monkeypatch):
    """Guard the guard: the double must not signature-filter the side fields.

    ``_run_agent_streaming`` gates each optional kwarg on
    ``inspect.signature(AIAgent.__init__).parameters``. A double that swallowed
    ``credential_pool`` / ``api_mode`` / ``acp_command`` / ``acp_args`` into
    ``**kwargs`` would make every assertion below pass vacuously, because the
    stale values would never be passed at all.
    """
    import inspect

    import api.streaming as streaming

    _stream_id, _q, _captured, restore = _setup_production_composed_runtime(
        monkeypatch, dict(_KEYED_VS_LIST_CFG), {}
    )
    try:
        params = set(inspect.signature(streaming._get_ai_agent().__init__).parameters)
    finally:
        restore()

    for field in ("api_mode", "acp_command", "acp_args", "credential_pool"):
        assert field in params, f"{field} would be signature-filtered out"


def test_production_composed_initial_send_replaces_full_runtime_bundle(monkeypatch):
    """Initial send: exact list row owns the WHOLE constructor bundle."""
    import api.streaming as streaming

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        dict(_KEYED_VS_LIST_CFG),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        session_id="session-1806-bundle-initial",
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id="session-1806-bundle-initial",
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    _assert_exact_list_row_bundle(captured["init_kwargs"], "initial send")


def test_production_composed_returned_error_retry_replaces_full_runtime_bundle(monkeypatch):
    """Returned-error 401 retry: the rebuilt bundle is target-owned too.

    The retry path rebuilds agent kwargs from the self-healed runtime dict. When
    it only refreshed provider/key/base_url (plus ``credential_pool`` from the
    heal result), the retry agent was constructed with the ambient provider's
    pool/api_mode/ACP fields even though the endpoint is the custom list row.
    """
    import api.streaming as streaming

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        dict(_KEYED_VS_LIST_CFG),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        session_id="session-1806-bundle-returned",
        fail_first="returned_error",
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id="session-1806-bundle-returned",
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    history = captured["init_kwargs_history"]
    assert len(history) >= 2, "returned-error retry did not construct a second agent"
    for index, init_kwargs in enumerate(history):
        _assert_exact_list_row_bundle(init_kwargs, f"returned-error construction #{index}")


def test_production_composed_raised_exception_retry_replaces_full_runtime_bundle(monkeypatch):
    """Raised-exception 401 retry: same complete-bundle contract."""
    import api.streaming as streaming

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        dict(_KEYED_VS_LIST_CFG),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        session_id="session-1806-bundle-raised",
        fail_first="raised",
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id="session-1806-bundle-raised",
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    history = captured["init_kwargs_history"]
    assert len(history) >= 2, "raised-exception retry did not construct a second agent"
    for index, init_kwargs in enumerate(history):
        _assert_exact_list_row_bundle(init_kwargs, f"raised-exception construction #{index}")


def test_agent_cache_signature_tracks_the_resolved_bundle(monkeypatch):
    """The cached-agent signature must be derived from the FINAL bundle.

    If ``_sig_blob`` still read ``api_mode`` / ACP / pool off the raw runtime
    provider, two sends whose resolved bundles differ only in a cleared side
    field would hash identically — so the second send would reuse an agent built
    on the previous authority instead of minting a new one.
    """
    import api.streaming as streaming

    def _run(runtime_dict, session_id):
        stream_id, q, captured, restore = _setup_production_composed_runtime(
            monkeypatch, dict(_KEYED_VS_LIST_CFG), runtime_dict, session_id=session_id
        )
        try:
            streaming.STREAMS[stream_id] = q
            streaming._run_agent_streaming(
                session_id=session_id,
                msg_text="hello",
                model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
                workspace="/tmp",
                stream_id=stream_id,
            )
            with config.SESSION_AGENT_CACHE_LOCK:
                return config.SESSION_AGENT_CACHE[session_id][1]
        finally:
            streaming.STREAMS.pop(stream_id, None)
            streaming.AGENT_INSTANCES.pop(stream_id, None)
            restore()

    # Same resolved bundle, wildly different ambient side fields: because the
    # custom row clears them all, the signature must be identical.
    plain_runtime = {
        "provider": "custom:omni",
        "base_url": "https://keyed-url-sentinel.example/v1",
        "api_key": "keyed-key-sentinel-abc",
    }
    sig_plain = _run(plain_runtime, "session-1806-sig-plain")
    sig_ambient = _run(dict(_AMBIENT_SIDE_FIELD_RUNTIME), "session-1806-sig-ambient")
    assert sig_plain == sig_ambient, (
        "signature still varies with runtime fields the bundle cleared"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ownership-aware side fields
#
# "Clear the runtime-owned side fields whenever a custom record supplies the
# connection" over-corrects. ``api_mode``, ``credential_pool``, ``acp_command``
# and ``acp_args`` are not intrinsically ambient: a ``custom_providers[]`` row is
# free to declare ``api_mode: anthropic_messages``, and a keyed
# ``providers['custom:<slug>']`` record is free to declare a pool and an ACP
# transport. Blanket-clearing them downgrades an Anthropic-protocol row to
# chat-completions and drops a keyed record's own pool/transport.
#
# The rule these tests pin is provenance, not field name:
#
#   * the selected record declares the field  -> the record's value wins;
#   * the runtime resolved the SAME endpoint  -> its value is same-authority, keep;
#   * the runtime resolved a DIFFERENT one    -> proven foreign, clear.
#
# The same distinction gates ``dummy-key``: it is a statement that the endpoint
# is UNAUTHENTICATED, so it may only be substituted once the record's whole
# credential ladder (pool, api_key, key_env, CUSTOM_<SLUG>_API_KEY, key_cmd,
# host-gated env) has come up empty. Substituting it over a ``key_cmd`` or a
# pooled credential turns a working endpoint into a 401.
# ─────────────────────────────────────────────────────────────────────────────


def _run_composed_send(monkeypatch, cfg_dict, runtime_dict, session_id, before_send=None):
    """Drive ONE production-composed streaming send; return the constructor kwargs.

    ``before_send`` runs after the fake ``hermes_cli.runtime_provider`` module is
    installed, so a test can hang extra runtime helpers (the credential-pool
    lookup) off it before resolution happens.
    """
    import api.streaming as streaming

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch, cfg_dict, runtime_dict, session_id=session_id
    )
    try:
        if before_send is not None:
            before_send()
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()
    return captured["init_kwargs"]


# ─────────────────────────────────────────────────────────────────────────────
# Terminal route verdicts: the send must STOP, not fail closed and continue
#
# "Fail closed" was the wrong shape for an unresolvable named route. Clearing
# ``base_url``/``api_key`` looks terminal in a bundle assertion, but at the
# constructor it is the opposite: ``_init_openai_client()`` honours an explicit
# pair only when BOTH fields are truthy and otherwise calls
# ``_routed_client_kwargs()``, which re-resolves a provider through the
# centralized router and the init-time fallback chain. So the very bundles the
# earlier tests asserted were "safe" are the ones that route the user's prompt —
# and whatever credential init finds — to a provider they never picked.
#
# The helpers below assert the real property: for both terminal shapes (a slug
# nothing owns, and an owned row whose declared credential resolved to nothing)
# NO agent is constructed at all, so ``_routed_client_kwargs()`` is never
# reached, the agent cache is never written, and the turn ends on a controlled
# ``provider_unroutable`` apperror instead.
# ─────────────────────────────────────────────────────────────────────────────


def _drain_apperrors(fake_queue):
    """Return every ``apperror`` payload the worker queued."""
    import queue as _queue

    payloads = []
    while True:
        try:
            item = fake_queue.get_nowait()
        except _queue.Empty:
            break
        if item and item[0] == "apperror":
            payloads.append(item[1])
    return payloads


def _assert_route_refused(captured, apperrors, label, *, expected_reason=None):
    """Assert the send stopped at the route verdict, before any provider routing."""
    routed = captured.get("routed_client_kwargs_calls", [])
    assert not routed, (
        f"{label}: AIAgent was constructed with an incomplete connection pair "
        f"{routed}, so _init_openai_client() fell through to "
        f"_routed_client_kwargs() and re-resolved a provider"
    )
    assert not captured.get("init_kwargs_history"), (
        f"{label}: an agent was constructed for an unroutable route"
    )
    assert not captured.get("run_calls"), f"{label}: the turn was actually sent"

    assert apperrors, f"{label}: no controlled failure was emitted"
    payload = apperrors[-1]
    assert payload["type"] == "provider_unroutable", (
        f"{label}: emitted {payload['type']!r} instead of a provider-route failure"
    )
    assert payload.get("hint"), f"{label}: the failure named no fix"
    if expected_reason is not None:
        assert expected_reason in payload.get("message", "") or expected_reason in payload.get(
            "hint", ""
        ), f"{label}: the failure did not name {expected_reason!r}: {payload}"

    with config.SESSION_AGENT_CACHE_LOCK:
        assert not config.SESSION_AGENT_CACHE, (
            f"{label}: the agent cache was poisoned with an unroutable bundle, so "
            f"every later turn in this session would reuse it"
        )
    return payload


def _run_composed_send_expecting_refusal(
    monkeypatch, cfg_dict, runtime_dict, session_id, *, model
):
    """Drive one composed send whose route is terminal at the FIRST resolution.

    Returns ``(captured, apperrors)``. Deliberately takes no ``fail_first``: the
    verdict lands before any agent exists, so there is no 401 for the self-heal
    retries to act on and threading one through would only produce cases that
    re-run identical code. The retry regions have their own harness,
    :func:`_run_composed_retry_expecting_abandoned_heal`.
    """
    import api.streaming as streaming

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch, cfg_dict, runtime_dict, session_id=session_id
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model=model,
            workspace="/tmp",
            stream_id=stream_id,
        )
        apperrors = _drain_apperrors(q)
        # Read the cache BEFORE restore() clears it.
        _assert_route_refused(captured, apperrors, session_id)
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()
    return captured, apperrors


def _exact_list_row_cfg(*, drop=(), **row_fields):
    """``_KEYED_VS_LIST_CFG`` with the exact ``custom_providers[]`` row extended."""
    cfg_dict = copy.deepcopy(_KEYED_VS_LIST_CFG)
    for field in drop:
        cfg_dict["custom_providers"][0].pop(field, None)
    cfg_dict["custom_providers"][0].update(row_fields)
    return cfg_dict


def _ambient_runtime(**overrides):
    runtime = copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME)
    runtime.update(overrides)
    return runtime


# ── (a) exact-list-owned Anthropic mode ──────────────────────────────────────


@pytest.mark.parametrize(
    "row_fields,session_suffix",
    [
        ({"api_mode": "anthropic_messages"}, "api-mode"),
        # ``transport:`` is the v12-migration spelling and ``anthropic`` an
        # accepted alias; a hand-edited config using either still owns the mode.
        ({"transport": "anthropic"}, "transport-alias"),
    ],
)
def test_exact_list_row_keeps_its_own_anthropic_api_mode(
    monkeypatch, row_fields, session_suffix
):
    """(a) The row declares its wire protocol, so neither clearing nor the ambient wins.

    The ambient runtime reports ``chat_completions`` for a DIFFERENT endpoint.
    Both failure modes are visible here: passing the ambient value through gives
    ``chat_completions``, blanket-clearing gives ``None``, and only reading the
    row's own declaration gives ``anthropic_messages`` — which is what decides
    whether the send speaks /v1/messages or /v1/chat/completions.
    """
    init_kwargs = _run_composed_send(
        monkeypatch,
        _exact_list_row_cfg(**row_fields),
        _ambient_runtime(api_mode="chat_completions"),
        f"session-1806-owned-{session_suffix}",
    )

    _assert_exact_list_row_bundle(
        init_kwargs,
        f"exact list row owns api_mode ({session_suffix})",
        side_fields={**_FOREIGN_AMBIENT_SIDE_FIELDS, "api_mode": "anthropic_messages"},
    )


# ── (b) exact-list key_cmd / pool credential ─────────────────────────────────


def _fake_command_token_source(monkeypatch, build):
    fake_module = types.ModuleType("agent.command_token_source")
    fake_module.build_command_token_provider = build
    monkeypatch.setitem(sys.modules, "agent.command_token_source", fake_module)


def test_exact_list_row_key_cmd_is_not_replaced_by_dummy_key(monkeypatch):
    """(b) A ``key_cmd`` row mints a real per-request bearer, so it is NOT keyless.

    ``key_cmd`` names a command that prints a short-lived bearer; both wire
    clients accept a callable api_key and mint one per request. Handing the
    endpoint ``dummy-key`` instead — because the row carries no literal
    ``api_key`` — is a guaranteed 401 against an endpoint that does want auth.
    """
    built = {}

    def _token_provider():
        return "minted-bearer-sentinel"

    def _build(key_cmd, name):
        built["key_cmd"] = key_cmd
        built["name"] = name
        return _token_provider

    _fake_command_token_source(monkeypatch, _build)

    init_kwargs = _run_composed_send(
        monkeypatch,
        _exact_list_row_cfg(drop=("api_key",), key_cmd="print-omni-bearer"),
        _ambient_runtime(),
        "session-1806-owned-key-cmd",
    )

    assert built["key_cmd"] == "print-omni-bearer", "key_cmd was never consulted"
    assert built["name"] == "omni"
    assert init_kwargs["api_key"] is _token_provider, "row's key_cmd token source lost"
    assert init_kwargs["api_key"]() == "minted-bearer-sentinel"
    assert init_kwargs["api_key"] != config.KEYLESS_CUSTOM_API_KEY
    assert init_kwargs["api_key"] != "keyed-key-sentinel-abc"
    assert init_kwargs["base_url"] == _LIST_ROW_URL
    assert init_kwargs["provider"] == "custom"
    _assert_side_fields(init_kwargs, _FOREIGN_AMBIENT_SIDE_FIELDS, "exact list row key_cmd")


def test_exact_list_row_unbuildable_key_cmd_still_refuses_dummy_key(monkeypatch):
    """(b) ``dummy-key`` asserts "this endpoint is unauthenticated" — never a guess.

    When the token provider cannot be built (older agent build, broken command
    spec) the endpoint is still an authenticated one whose credential is missing.
    The placeholder would report that as an opaque 401, and sending with NO key
    is no safer: an agent built with ``api_key=None`` never reaches an explicit
    client, so ``_routed_client_kwargs()`` re-resolves a provider and the turn
    leaves for whatever credential init finds next. The route is terminal.
    """

    def _build(_key_cmd, _name):
        raise RuntimeError("command token source unavailable")

    _fake_command_token_source(monkeypatch, _build)

    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        _exact_list_row_cfg(drop=("api_key",), key_cmd="print-omni-bearer"),
        _ambient_runtime(),
        "session-1806-owned-key-cmd-broken",
        model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
    )

    payload = apperrors[-1]
    assert "produced no API key" in payload["message"], payload
    assert "key_cmd" in payload["hint"], (
        f"the refusal must name the credential setting to fix: {payload}"
    )
    # Neither the placeholder nor the ambient keyed credential may appear
    # anywhere on the refusal path.
    assert config.KEYLESS_CUSTOM_API_KEY not in str(payload), payload
    assert "keyed-key-sentinel-abc" not in str(payload), payload
    assert not captured.get("explicit_client_kwargs_calls"), (
        "a client was configured for a route whose declared credential failed"
    )


def test_exact_list_row_pool_credential_and_pool_object_both_survive(monkeypatch):
    """(b) A pooled row keeps the pool's key AND the pool object — from ITS endpoint.

    The credential and the ``credential_pool`` the agent rotates it with come
    from one lookup keyed on the ROW's base_url. Clearing the pool (while keeping
    its key) leaves the agent unable to rotate; keeping the ambient pool points
    rotation at the previous authority; falling back to ``dummy-key`` drops the
    credential entirely.
    """
    pool_sentinel = ["list-row-pool-sentinel"]
    seen = {}

    def _before_send():
        runtime_module = sys.modules["hermes_cli.runtime_provider"]

        def _try_resolve_from_custom_pool(
            base_url, provider_label, api_mode_override=None, provider_name=None
        ):
            seen["base_url"] = base_url
            seen["provider_name"] = provider_name
            if base_url != _LIST_ROW_URL:
                return None
            return {"api_key": "pool-key-sentinel", "credential_pool": pool_sentinel}

        runtime_module._try_resolve_from_custom_pool = _try_resolve_from_custom_pool

    init_kwargs = _run_composed_send(
        monkeypatch,
        _exact_list_row_cfg(drop=("api_key",)),
        _ambient_runtime(),
        "session-1806-owned-pool",
        before_send=_before_send,
    )

    # The pool was looked up for the ROW's endpoint, not the ambient one.
    assert seen["base_url"] == _LIST_ROW_URL
    assert seen["provider_name"] == "omni"
    assert init_kwargs["api_key"] == "pool-key-sentinel"
    assert init_kwargs["api_key"] != config.KEYLESS_CUSTOM_API_KEY
    assert init_kwargs["api_key"] != "keyed-key-sentinel-abc"
    _assert_side_fields(
        init_kwargs,
        {**_FOREIGN_AMBIENT_SIDE_FIELDS, "credential_pool": pool_sentinel},
        "exact list row pool",
    )
    assert init_kwargs["credential_pool"] is pool_sentinel
    assert init_kwargs["credential_pool"] != _AMBIENT_SIDE_FIELD_RUNTIME["credential_pool"]


# ── (c) keyed-only record's own side fields ──────────────────────────────────


_KEYED_ONLY_OWNED_CFG = {
    "model": {"default": "active/model", "provider": "custom:active"},
    "providers": {
        "custom:omni": {
            "base_url": "https://keyed-url-sentinel.example/v1",
            "api_key": "keyed-key-sentinel-abc",
            # Side fields the KEYED record declares for itself.
            "api_mode": "anthropic_messages",
            "credential_pool": ["keyed-pool-sentinel"],
            "acp_command": "keyed-acp-sentinel",
            "acp_args": ["--keyed-arg-sentinel"],
        },
    },
    "custom_providers": [
        {
            "name": "active",
            "base_url": "https://active.example/v1",
            "api_key": "active-key",
        },
    ],
}


def test_keyed_only_record_keeps_the_side_fields_it_owns(monkeypatch):
    """(c) No list row: the keyed record is the authority for ITS side fields too.

    ``providers['custom:omni']`` supplies the endpoint and the credential, so it
    also owns the pool, wire protocol and ACP transport it declares. Clearing
    them because the route is ``custom:<slug>`` throws away the record's own
    configuration; taking the ambient values (all four differ here) routes the
    send through the previous authority.
    """
    init_kwargs = _run_composed_send(
        monkeypatch,
        copy.deepcopy(_KEYED_ONLY_OWNED_CFG),
        _ambient_runtime(
            api_mode="chat_completions",
            command="ambient-acp-sentinel",
            args=["--ambient-arg-sentinel"],
            credential_pool=["ambient-pool-sentinel"],
        ),
        "session-1806-keyed-owned",
    )

    assert init_kwargs["base_url"] == "https://keyed-url-sentinel.example/v1"
    assert init_kwargs["api_key"] == "keyed-key-sentinel-abc"
    assert init_kwargs["provider"] == "custom"
    _assert_side_fields(
        init_kwargs,
        {
            "api_mode": "anthropic_messages",
            "credential_pool": ["keyed-pool-sentinel"],
            "acp_command": "keyed-acp-sentinel",
            "acp_args": ["--keyed-arg-sentinel"],
        },
        "keyed-only owned side fields",
    )


def test_keyed_only_record_without_side_fields_keeps_same_endpoint_runtime(monkeypatch):
    """(c) The clear is provenance-driven, not slug-driven.

    Here the keyed record declares no side fields and the runtime resolved the
    SAME endpoint the record owns, so the runtime's values are same-authority.
    Clearing them would strip a legitimately-pooled Anthropic-protocol endpoint
    of its pool and protocol just because the route is spelled ``custom:<slug>``.
    """
    cfg_dict = copy.deepcopy(_KEYED_ONLY_OWNED_CFG)
    for field in ("api_mode", "credential_pool", "acp_command", "acp_args"):
        cfg_dict["providers"]["custom:omni"].pop(field)

    init_kwargs = _run_composed_send(
        monkeypatch,
        cfg_dict,
        _ambient_runtime(),
        "session-1806-keyed-same-authority",
    )

    assert init_kwargs["base_url"] == "https://keyed-url-sentinel.example/v1"
    _assert_side_fields(
        init_kwargs,
        {
            "api_mode": _AMBIENT_SIDE_FIELD_RUNTIME["api_mode"],
            "credential_pool": _AMBIENT_SIDE_FIELD_RUNTIME["credential_pool"],
            "acp_command": _AMBIENT_SIDE_FIELD_RUNTIME["command"],
            "acp_args": _AMBIENT_SIDE_FIELD_RUNTIME["args"],
        },
        "keyed-only same-authority runtime",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Non-streaming route consumers
#
# Four WebUI consumers build their own AIAgent outside the streaming path.
# Each resolved the endpoint deterministically (``resolve_model_provider`` now
# returns the exact ``custom_providers[]`` row's URL) and then applied the
# custom-provider connection with a FILL-ONLY pattern:
#
#     if not api_key and _cp_key: api_key = _cp_key
#
# Because ``resolve_runtime_provider`` had already supplied a truthy key from the
# same-slug keyed ``providers:`` record, the guard never fired — so the final
# constructor received the LIST row's URL paired with the KEYED row's API key.
# Each test below asserts on the FINAL constructor kwargs, so it fails on that
# mixed bundle and passes only once the row is applied atomically.
# ─────────────────────────────────────────────────────────────────────────────


class _RouteAgentCaptured(Exception):
    """Sentinel raised from the double's __init__ once the bundle is captured.

    All four consumers construct the agent and immediately use it (compress,
    run_conversation, text completion). Stopping at construction keeps each test
    scoped to the routing contract instead of to downstream persistence.
    """


# The runtime dict the route consumers see by default: production shape, where
# ``resolve_runtime_provider`` scans ``providers:`` first and so hands back the
# KEYED endpoint and the KEYED key for this slug.
_ROUTE_KEYED_RUNTIME = {
    "provider": "custom:omni",
    "base_url": "https://keyed-url-sentinel.example/v1",
    "api_key": "keyed-key-sentinel-abc",
}


def _setup_route_consumer_runtime(
    monkeypatch, session_messages=None, cfg_dict=None, runtime_dict=None
):
    """Compose the keyed-vs-list config around a capturing route agent.

    Returns ``(captured, fake_session)``. ``captured["init_kwargs"]`` holds the
    FINAL constructor bundle the consumer under test built.

    ``cfg_dict``/``runtime_dict`` override the defaults so a test can hand the
    consumers an exact row that OWNS side fields (``api_mode``,
    ``credential_pool``, ACP transport) and an ambient runtime that reports
    different ones -- the probe for whether the complete bundle, or just its
    three connection fields, reaches the constructor.
    """
    import types as _types
    from unittest import mock

    import api.config as _config
    import api.oauth
    import api.routes as routes

    cfg_dict = copy.deepcopy(_KEYED_VS_LIST_CFG if cfg_dict is None else cfg_dict)
    monkeypatch.setattr(_config, "cfg", dict(cfg_dict), raising=False)
    monkeypatch.setattr(_config, "get_config", lambda: copy.deepcopy(cfg_dict))

    runtime_dict = copy.deepcopy(
        _ROUTE_KEYED_RUNTIME if runtime_dict is None else runtime_dict
    )
    fake_runtime_module = _types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(
        return_value=runtime_dict
    )
    fake_hermes_cli = _types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setattr(
        api.oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )

    captured = {}

    class CapturingRouteAgent:
        # Every constructor-routing field must be a NAMED parameter, for the
        # same reason as the streaming double: the route consumers gate each
        # optional kwarg on ``inspect.signature(AIAgent.__init__)``, so a double
        # that swallowed them into ``**kwargs`` would filter out exactly the
        # fields these tests pin and pass vacuously.
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key=None,
            api_mode=None,
            acp_command=None,
            acp_args=None,
            credential_pool=None,
            **kwargs,
        ):
            captured["init_kwargs"] = {
                "model": model,
                "provider": provider,
                "base_url": base_url,
                "api_key": api_key,
                "api_mode": api_mode,
                "acp_command": acp_command,
                "acp_args": acp_args,
                "credential_pool": credential_pool,
                **kwargs,
            }
            raise _RouteAgentCaptured("captured")

    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: CapturingRouteAgent)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda *_a, **_k: [])
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda *_a, **_k: False)

    class _FakeSession:
        def __init__(self):
            self.session_id = "session-1806-route"
            self.title = "Test"
            self.workspace = "/tmp"
            self.model = "antigravity/gemini-3.7-flash-tiered"
            self.model_provider = "custom:omni"
            self.messages = list(session_messages or [])
            self.context_messages = list(session_messages or [])
            self.active_stream_id = None
            self.pending_user_message = None
            self.pending_attachments = []
            self.pending_started_at = None
            self.pending_user_source = None
            self.profile = None

        def save(self, *_a, **_k):
            return None

    return captured, _FakeSession()


def _assert_list_row_not_keyed(init_kwargs, label):
    """The endpoint AND the credential must both come from the list row."""
    assert init_kwargs["base_url"] == _LIST_ROW_URL, label
    assert init_kwargs["api_key"] == _LIST_ROW_KEY, label
    assert init_kwargs["base_url"] != "https://keyed-url-sentinel.example/v1", label
    assert init_kwargs["api_key"] != "keyed-key-sentinel-abc", label
    assert init_kwargs["provider"] == "custom", label


def _four_route_messages():
    return [
        {"role": "user", "content": "one", "timestamp": 1.0, "_ts": 1.0},
        {"role": "assistant", "content": "two", "timestamp": 2.0, "_ts": 2.0},
        {"role": "user", "content": "three", "timestamp": 3.0, "_ts": 3.0},
        {"role": "assistant", "content": "four", "timestamp": 4.0, "_ts": 4.0},
    ]


# Each driver composes the harness the way its consumer needs it and returns the
# FINAL constructor kwargs. Sharing them keeps the two contracts below --
# "the endpoint and credential are one record's" and "the record's side fields
# reach the constructor too" -- asserted over the SAME five consumers, so a new
# consumer cannot be added to one list and forgotten in the other.


def _drive_sync_chat_route(monkeypatch, cfg_dict=None, runtime_dict=None):
    """Consumer 1/5: POST /api/chat."""
    import api.routes as routes

    captured, fake_session = _setup_route_consumer_runtime(
        monkeypatch,
        session_messages=_four_route_messages(),
        cfg_dict=cfg_dict,
        runtime_dict=runtime_dict,
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws: "/tmp")
    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda _sid: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, None)
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_k: (model, provider),
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_k: payload)
    monkeypatch.setattr(routes, "bad", lambda _handler, message, *_a, **_k: {"error": message})

    with pytest.raises(_RouteAgentCaptured):
        routes._handle_chat_sync(
            object(), {"session_id": "session-1806-route", "message": "hi"}
        )
    return captured["init_kwargs"]


def _drive_manual_compression_route(monkeypatch, cfg_dict=None, runtime_dict=None):
    """Consumer 2/5: POST /api/session/{sid}/compress."""
    import api.config as _config
    import api.routes as routes

    captured, fake_session = _setup_route_consumer_runtime(
        monkeypatch,
        session_messages=_four_route_messages(),
        cfg_dict=cfg_dict,
        runtime_dict=runtime_dict,
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(
        _config, "_get_session_agent_lock", lambda _sid: contextlib.nullcontext()
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_k: payload)
    monkeypatch.setattr(routes, "bad", lambda _handler, message, *_a, **_k: {"error": message})

    routes._handle_session_compress(object(), {"session_id": "session-1806-route"})
    return captured["init_kwargs"]


def _decline_auxiliary_client(monkeypatch, recorded_main_runtime=None):
    """Force the auxiliary-model shortcut to decline.

    The consumers that have one would otherwise return before constructing the
    main-model agent -- and that constructor is the bundle under test.
    """
    import types as _types

    fake_aux = _types.ModuleType("agent.auxiliary_client")

    def _get_text_auxiliary_client(_task, main_runtime=None):
        if recorded_main_runtime is not None:
            recorded_main_runtime.update(main_runtime or {})
        return (None, None)

    fake_aux.get_text_auxiliary_client = _get_text_auxiliary_client
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", fake_aux)


def _drive_commit_message_route(
    monkeypatch, cfg_dict=None, runtime_dict=None, recorded_main_runtime=None
):
    """Consumer 3/5: LLM git commit-message generation."""
    import api.routes as routes

    captured, fake_session = _setup_route_consumer_runtime(
        monkeypatch, cfg_dict=cfg_dict, runtime_dict=runtime_dict
    )
    _decline_auxiliary_client(monkeypatch, recorded_main_runtime)

    with pytest.raises(_RouteAgentCaptured):
        routes._llm_git_commit_message("sys", "user", session=fake_session)
    return captured["init_kwargs"]


def _drive_handoff_summary_route(monkeypatch, cfg_dict=None, runtime_dict=None):
    """Consumer 4/5: on-demand handoff summary."""
    import api.models as models
    import api.routes as routes

    captured, fake_session = _setup_route_consumer_runtime(
        monkeypatch,
        session_messages=_four_route_messages(),
        cfg_dict=cfg_dict,
        runtime_dict=runtime_dict,
    )
    monkeypatch.setattr(models, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(
        models,
        "count_conversation_rounds",
        lambda _sid, since=None: models.CONVERSATION_ROUND_THRESHOLD + 1,
    )
    monkeypatch.setattr(
        models, "get_cli_session_messages", lambda _sid: _four_route_messages()
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_k: payload)
    monkeypatch.setattr(routes, "bad", lambda _handler, message, *_a, **_k: {"error": message})

    routes._handle_handoff_summary(object(), {"session_id": "session-1806-route"})
    return captured["init_kwargs"]


def _drive_update_summary_route(
    monkeypatch, cfg_dict=None, runtime_dict=None, recorded_main_runtime=None
):
    """Consumer 5/5: update summary (``_llm_update_summary``).

    This one resolves ``get_effective_default_model()`` rather than the session's
    model, so the default has to name the slug under test.
    """
    import api.routes as routes

    cfg_dict = copy.deepcopy(_KEYED_VS_LIST_CFG if cfg_dict is None else cfg_dict)
    cfg_dict["model"] = {
        "default": "@custom:omni:antigravity/gemini-3.7-flash-tiered",
        "provider": "custom:omni",
    }
    captured, _fake_session = _setup_route_consumer_runtime(
        monkeypatch, cfg_dict=cfg_dict, runtime_dict=runtime_dict
    )
    _decline_auxiliary_client(monkeypatch, recorded_main_runtime)

    with pytest.raises(_RouteAgentCaptured):
        routes._llm_update_summary("sys", "user", active_profile=None)
    return captured["init_kwargs"]


_ROUTE_CONSUMER_DRIVERS = [
    (_drive_sync_chat_route, "sync chat (/api/chat)"),
    (_drive_manual_compression_route, "manual compression (/compress)"),
    (_drive_commit_message_route, "git commit message"),
    (_drive_handoff_summary_route, "handoff summary"),
    (_drive_update_summary_route, "update summary"),
]


@pytest.mark.parametrize(
    "driver,label", _ROUTE_CONSUMER_DRIVERS, ids=[d[1] for d in _ROUTE_CONSUMER_DRIVERS]
)
def test_route_consumers_apply_exact_list_row_atomically(monkeypatch, driver, label):
    """Every non-streaming consumer applies the exact row's URL *and* its key."""
    _assert_list_row_not_keyed(driver(monkeypatch), label)


# The two consumers whose auxiliary-client shortcut can answer the request
# outright -- for them ``main_runtime`` is the only carrier of the resolved
# authority, because AIAgent is never built.
_AUXILIARY_ROUTE_DRIVERS = [
    (_drive_commit_message_route, "git commit message"),
    (_drive_update_summary_route, "update summary"),
]


@pytest.mark.parametrize(
    "driver,label",
    _AUXILIARY_ROUTE_DRIVERS,
    ids=[d[1] for d in _AUXILIARY_ROUTE_DRIVERS],
)
def test_auxiliary_routes_hand_the_row_connection_to_the_auxiliary_client(
    monkeypatch, driver, label
):
    """The aux-client shortcut must see the row's connection, not the keyed one.

    It runs BEFORE the main-model constructor, so a consumer that resolved the
    bundle only on the fallback path would still send this request to the keyed
    endpoint.
    """
    recorded_main_runtime = {}
    driver(monkeypatch, recorded_main_runtime=recorded_main_runtime)

    assert recorded_main_runtime.get("base_url") == _LIST_ROW_URL, label
    assert recorded_main_runtime.get("api_key") == _LIST_ROW_KEY, label


# ── The complete bundle, not just its three connection fields ────────────────
#
# ``apply_custom_provider_connection_authority`` returns ``(provider, api_key,
# base_url)``. Every consumer above used to apply exactly that and construct the
# agent from it, so an exact row's ``api_mode`` and ``credential_pool`` -- the
# wire protocol the send speaks and the credential source it rotates -- were
# truncated away before AIAgent ever saw them, while the streaming path carried
# them through. The row below OWNS both, and the ambient runtime reports
# different values for all four side fields, so a truncating consumer fails on
# ``None`` and a pass-through consumer fails on the ambient sentinel.


_ROUTE_ROW_POOL_SENTINEL = ["list-row-pool-sentinel"]

_ROUTE_OWNED_SIDE_FIELD_CFG = _exact_list_row_cfg(
    api_mode="anthropic_messages",
    credential_pool=_ROUTE_ROW_POOL_SENTINEL,
)

# The ambient runtime disagrees on every side field it reports.
_ROUTE_AMBIENT_RUNTIME = {
    **_ROUTE_KEYED_RUNTIME,
    "api_mode": "chat_completions",
    "credential_pool": ["ambient-pool-sentinel"],
    "command": "ambient-acp-sentinel",
    "args": ["--ambient-arg-sentinel"],
}


@pytest.mark.parametrize(
    "driver,label", _ROUTE_CONSUMER_DRIVERS, ids=[d[1] for d in _ROUTE_CONSUMER_DRIVERS]
)
def test_route_consumers_pass_the_exact_rows_side_fields_to_the_constructor(
    monkeypatch, driver, label
):
    """The exact row's ``api_mode``/``credential_pool`` reach the FINAL constructor."""
    init_kwargs = driver(
        monkeypatch,
        cfg_dict=copy.deepcopy(_ROUTE_OWNED_SIDE_FIELD_CFG),
        runtime_dict=copy.deepcopy(_ROUTE_AMBIENT_RUNTIME),
    )

    _assert_list_row_not_keyed(init_kwargs, label)
    _assert_side_fields(
        init_kwargs,
        {
            "api_mode": "anthropic_messages",
            "credential_pool": _ROUTE_ROW_POOL_SENTINEL,
            # The row declares no ACP transport and owns a different endpoint
            # than the runtime, so the ambient subprocess is provably foreign.
            "acp_command": None,
            "acp_args": None,
        },
        f"{label}: exact row side fields",
    )
    assert init_kwargs["api_mode"] != _ROUTE_AMBIENT_RUNTIME["api_mode"], (
        f"{label}: the ambient wire protocol reached the constructor"
    )
    assert init_kwargs["credential_pool"] != _ROUTE_AMBIENT_RUNTIME["credential_pool"], (
        f"{label}: the ambient credential pool reached the constructor"
    )


@pytest.mark.parametrize(
    "driver,label", _ROUTE_CONSUMER_DRIVERS, ids=[d[1] for d in _ROUTE_CONSUMER_DRIVERS]
)
def test_route_consumers_clear_foreign_ambient_side_fields(monkeypatch, driver, label):
    """A row that owns NO side fields still strips the ambient provider's.

    Complement of the test above: "carry the complete bundle" must not degrade
    into "pass the runtime's side fields through". The row here declares none of
    them and owns a different endpoint, so all four are provably foreign.
    """
    init_kwargs = driver(
        monkeypatch, runtime_dict=copy.deepcopy(_ROUTE_AMBIENT_RUNTIME)
    )

    _assert_list_row_not_keyed(init_kwargs, label)
    _assert_side_fields(init_kwargs, _FOREIGN_AMBIENT_SIDE_FIELDS, f"{label}: foreign ambient")


@pytest.mark.parametrize(
    "driver,label",
    _AUXILIARY_ROUTE_DRIVERS,
    ids=[d[1] for d in _AUXILIARY_ROUTE_DRIVERS],
)
def test_auxiliary_routes_hand_the_complete_bundle_to_the_auxiliary_client(
    monkeypatch, driver, label
):
    """``main_runtime`` carries the WHOLE bundle, not its three connection fields.

    When the auxiliary client answers, AIAgent is bypassed entirely, so the
    side fields that never entered ``main_runtime`` were simply lost: the exact
    row's ``api_mode: anthropic_messages`` silently degraded to the aux client's
    default wire protocol, and the credential pool/ACP transport the row owns
    never reached the send. The aux dict must therefore agree with the fallback
    constructor field for field -- same authority, whichever path answers.
    """
    import api.routes as routes

    recorded_main_runtime = {}
    init_kwargs = driver(
        monkeypatch,
        cfg_dict=copy.deepcopy(_ROUTE_OWNED_SIDE_FIELD_CFG),
        runtime_dict=copy.deepcopy(_ROUTE_AMBIENT_RUNTIME),
        recorded_main_runtime=recorded_main_runtime,
    )

    _assert_list_row_not_keyed(recorded_main_runtime, f"{label}: aux main_runtime")
    _assert_side_fields(
        recorded_main_runtime,
        {
            "api_mode": "anthropic_messages",
            "credential_pool": _ROUTE_ROW_POOL_SENTINEL,
            # The row declares no ACP transport and owns a different endpoint
            # than the runtime, so the ambient subprocess is provably foreign.
            "acp_command": None,
            "acp_args": None,
        },
        f"{label}: aux main_runtime side fields",
    )
    assert recorded_main_runtime["api_mode"] != _ROUTE_AMBIENT_RUNTIME["api_mode"], (
        f"{label}: the ambient wire protocol reached the auxiliary client"
    )
    assert (
        recorded_main_runtime["credential_pool"]
        != _ROUTE_AMBIENT_RUNTIME["credential_pool"]
    ), f"{label}: the ambient credential pool reached the auxiliary client"

    for field in ("provider", "model", "base_url", "api_key") + tuple(
        routes._AGENT_BUNDLE_SIDE_FIELDS
    ):
        assert recorded_main_runtime.get(field) == init_kwargs[field], (
            f"{label}: aux {field} disagrees with the fallback constructor, so "
            "which path answers decides the authority"
        )
    assert recorded_main_runtime["model"], f"{label}: aux main_runtime lost the model"


@pytest.mark.parametrize(
    "driver,label",
    _AUXILIARY_ROUTE_DRIVERS,
    ids=[d[1] for d in _AUXILIARY_ROUTE_DRIVERS],
)
def test_auxiliary_routes_clear_foreign_ambient_side_fields(monkeypatch, driver, label):
    """A row owning no side fields strips the ambient provider's from the aux dict too.

    Complement of the test above: "carry the complete bundle into
    ``main_runtime``" must not degrade into "pass the runtime's side fields
    through" on the path where nothing downstream re-resolves them.
    """
    recorded_main_runtime = {}
    driver(
        monkeypatch,
        runtime_dict=copy.deepcopy(_ROUTE_AMBIENT_RUNTIME),
        recorded_main_runtime=recorded_main_runtime,
    )

    _assert_list_row_not_keyed(recorded_main_runtime, f"{label}: aux main_runtime")
    _assert_side_fields(
        recorded_main_runtime,
        _FOREIGN_AMBIENT_SIDE_FIELDS,
        f"{label}: aux foreign ambient",
    )


def test_capturing_route_agent_exposes_every_runtime_constructor_field(monkeypatch):
    """Guard the guard, route edition.

    The consumers gate each optional kwarg on
    ``inspect.signature(AIAgent.__init__).parameters``. A double that swallowed
    ``api_mode`` / ``credential_pool`` / ``acp_command`` / ``acp_args`` into
    ``**kwargs`` would make every assertion above pass vacuously, because the
    fields would never be passed at all.
    """
    import inspect

    import api.routes as routes

    _captured, _fake_session = _setup_route_consumer_runtime(monkeypatch)
    agent_cls = routes.require_ai_agent_class()
    params = set(inspect.signature(agent_cls.__init__).parameters)

    for field in routes._AGENT_BUNDLE_SIDE_FIELDS:
        assert field in params, (
            f"the route double hides {field} behind **kwargs, so the "
            "signature gate would filter it out and the assertions would be vacuous"
        )


def test_production_composed_retry_caches_agent_under_recomputed_signature(monkeypatch):
    """Retry agents are cached under the healed bundle's signature, not the stale initial one."""
    import api.streaming as streaming

    computed_signatures = []
    real_compute_sig = streaming._compute_agent_cache_signature

    def tracking_compute_sig(*args, **kwargs):
        sig = real_compute_sig(*args, **kwargs)
        computed_signatures.append(sig)
        return sig

    monkeypatch.setattr(streaming, "_compute_agent_cache_signature", tracking_compute_sig)

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        dict(_KEYED_VS_LIST_CFG),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        session_id="session-1806-retry-cache-sig",
        fail_first="returned_error",
    )
    # Give self-heal a distinct provider/endpoint so initial and healed signatures differ
    healed_rt = dict(_AMBIENT_SIDE_FIELD_RUNTIME)
    healed_rt["base_url"] = "https://healed-endpoint.example/v1"
    monkeypatch.setattr(
        streaming,
        "_attempt_credential_self_heal",
        lambda *_args, **_kwargs: dict(healed_rt),
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id="session-1806-retry-cache-sig",
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
        assert len(computed_signatures) >= 2, "signature was not recomputed on retry"
        with config.SESSION_AGENT_CACHE_LOCK:
            cached = config.SESSION_AGENT_CACHE.get("session-1806-retry-cache-sig")
            assert cached is not None, "retry agent was not cached"
            cached_agent, cached_sig = cached
            assert cached_agent is not None
            assert cached_sig == computed_signatures[-1]
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()


# ─────────────────────────────────────────────────────────────────────────────
# Identity-owned selection, and the two ways a route can be "keyless"
#
# Selection for a named ``custom:<slug>`` must be owned by that identity: an
# exact normalized ``custom_providers[]`` row, an exact keyed record, or an
# explicitly matching ``model:``/bare-``custom`` authority. "The list happens to
# hold exactly one row, so use it" is NOT ownership — it resolved ``custom:ghost``
# to the endpoint AND credential of a sole unrelated row named ``omni``, sending
# the prompt and that credential to a provider the user never named.
#
# The second half of the same contract is ``dummy-key``. It asserts "this
# endpoint is UNAUTHENTICATED", so it may only appear when the record declares no
# credential source at all. A DECLARED credential that failed to resolve (unset
# ``${ENV}``, ``key_env`` naming a missing variable, a pool that yielded nothing)
# is a misconfiguration to surface, not an unauthenticated endpoint: substituting
# the placeholder there reports it as an opaque 401 from the endpoint instead.
# ─────────────────────────────────────────────────────────────────────────────


_SOLE_UNRELATED_ROW_CFG = {
    "model": {"default": "active/model", "provider": "custom:omni"},
    "custom_providers": [
        {
            "name": "omni",
            "base_url": "https://omni.example/v1",
            "api_key": "omni-key",
        },
    ],
}


def _with_direct_config(monkeypatch, cfg_dict):
    """Point both ``config.cfg`` and ``get_config()`` at ``cfg_dict``."""
    monkeypatch.setattr(config, "get_config", lambda: copy.deepcopy(cfg_dict))
    monkeypatch.setitem(config.cfg, "custom_providers", cfg_dict.get("custom_providers", []))
    monkeypatch.setitem(config.cfg, "model", cfg_dict.get("model", {}))
    monkeypatch.setitem(config.cfg, "providers", cfg_dict.get("providers", {}))


def test_unknown_named_slug_does_not_select_a_sole_unrelated_list_row(monkeypatch):
    """The reviewer's probe: ``custom:ghost`` must not inherit sole row ``omni``.

    ``_select_custom_provider_record`` used to append ``custom_providers[0]``
    whenever the list held exactly one row, then accept it merely because it had
    a key or a URL. Neither test is ownership: ``ghost`` and ``omni`` are
    different identities, so the pair belongs to ``omni`` alone.
    """
    _with_direct_config(monkeypatch, _SOLE_UNRELATED_ROW_CFG)

    assert config.resolve_custom_provider_connection("custom:ghost") == (None, None)
    # The row is still authoritative for its OWN slug.
    assert config.resolve_custom_provider_connection("custom:omni") == (
        "omni-key",
        "https://omni.example/v1",
    )


def test_unknown_named_slug_reports_an_explicit_missing_selection(monkeypatch):
    """The bundle carries the missing verdict instead of an ambiguous ``None``.

    ``keyless`` must be False on it: "nothing owns this route" is not a claim
    that the route is unauthenticated, and it is ``keyless`` alone that gates
    ``dummy-key``.
    """
    _with_direct_config(monkeypatch, _SOLE_UNRELATED_ROW_CFG)

    ghost = config.resolve_custom_provider_bundle("custom:ghost")
    assert ghost is not None, "a named custom route must report its selection outcome"
    assert ghost["status"] == config.CUSTOM_SELECTION_MISSING
    assert ghost["record"] is None
    assert ghost["base_url"] is None
    assert ghost["api_key"] is None
    assert ghost["keyless"] is False, "a missing route must not be reported keyless"
    assert ghost["owned"] == {}

    owned = config.resolve_custom_provider_bundle("custom:omni")
    assert owned["status"] == config.CUSTOM_SELECTION_EXACT
    assert owned["base_url"] == "https://omni.example/v1"


def test_unknown_named_slug_keeps_no_ambient_connection(monkeypatch):
    """The merge refuses the ambient provider's URL/key/pool for an unowned slug."""
    _with_direct_config(monkeypatch, _SOLE_UNRELATED_ROW_CFG)

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:ghost",
        "ambient-key-sentinel",
        "https://ambient.example/v1",
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        lookup_provider="custom:ghost",
    )

    assert bundle["base_url"] is None
    assert bundle["api_key"] is None
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY
    # The provider must NOT be rewritten to generic ``custom``: that would
    # present an unresolvable route as a resolved one.
    assert bundle["provider"] == "custom:ghost"
    _assert_side_fields(bundle, _FOREIGN_AMBIENT_SIDE_FIELDS, "unowned named slug")


# A provider LABEL is not proof of ownership. These two cases are how an unowned
# slug could still walk away with the ambient connection:
#
#   1. the runtime dict names ITSELF ``custom:ghost`` -- a self-assigned string
#      on a connection the process authenticated for some other reason; and
#   2. no runtime dict at all, the non-streaming consumers' shape, where
#      ``_connection_identity`` falls back to ``resolved_provider`` -- the very
#      slug being looked up -- so the route would match itself.
#
# Both must fail closed exactly as the differently-labelled ambient runtime does:
# ownership is decided by config records, and here there are none.


_SELF_LABELLED_GHOST_RUNTIME = {
    **_AMBIENT_SIDE_FIELD_RUNTIME,
    "provider": "custom:ghost",
    "base_url": "https://ambient.example/v1",
    "api_key": "ambient-key-sentinel",
}


def _assert_ghost_bundle_failed_closed(bundle, label):
    """No endpoint, no credential, no placeholder, no ambient side fields."""
    assert bundle["base_url"] is None, f"{label}: an unowned slug kept an endpoint"
    assert bundle["api_key"] is None, f"{label}: an unowned slug kept a credential"
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY, (
        f"{label}: an unresolvable route was handed the keyless placeholder"
    )
    # Still the NAMED slug: rewriting it to generic ``custom`` would present an
    # unresolvable route as a resolved one.
    assert bundle["provider"] == "custom:ghost", label
    _assert_side_fields(bundle, _FOREIGN_AMBIENT_SIDE_FIELDS, label)


@pytest.mark.parametrize(
    "runtime_dict,label",
    [
        (_SELF_LABELLED_GHOST_RUNTIME, "runtime labels itself custom:ghost"),
        (None, "no runtime dict (non-streaming caller shape)"),
    ],
    ids=["self-labelled-runtime", "no-runtime-dict"],
)
def test_unknown_named_slug_fails_closed_even_when_the_runtime_claims_the_slug(
    monkeypatch, runtime_dict, label
):
    """A matching provider label must not resurrect the ambient connection.

    Nothing in config owns ``custom:ghost``: not an exact ``custom_providers[]``
    row, not a keyed ``providers:`` record, not a ``model:`` authority. The only
    thing pointing at the slug is a string the caller supplied, so keeping the
    URL, key, wire protocol and credential pool that came with it would send the
    user's prompt and that credential to a provider they never configured.
    """
    _with_direct_config(monkeypatch, _SOLE_UNRELATED_ROW_CFG)

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:ghost",
        "ambient-key-sentinel",
        "https://ambient.example/v1",
        copy.deepcopy(runtime_dict) if runtime_dict else runtime_dict,
        lookup_provider="custom:ghost",
    )

    _assert_ghost_bundle_failed_closed(bundle, label)
    assert bundle["base_url"] != "https://ambient.example/v1", label
    assert bundle["api_key"] != "ambient-key-sentinel", label


def test_unknown_named_slug_connection_view_also_fails_closed(monkeypatch):
    """The three-field view reports the same verdict, and ``custom_owned`` False.

    Callers that genuinely construct nothing else still take this view, so the
    self-labelled runtime must not reach them with a connection either.
    """
    _with_direct_config(monkeypatch, _SOLE_UNRELATED_ROW_CFG)

    provider, api_key, base_url, custom_owned = (
        config.apply_custom_provider_connection_authority(
            "custom:ghost",
            "ambient-key-sentinel",
            "https://ambient.example/v1",
            lookup_provider="custom:ghost",
            runtime_provider=copy.deepcopy(_SELF_LABELLED_GHOST_RUNTIME),
        )
    )

    assert base_url is None
    assert api_key is None
    assert provider == "custom:ghost"
    assert custom_owned is False, "nothing owns the slug, so the record cannot be reported as owning it"


def test_unowned_named_slug_fails_closed_at_the_first_streaming_resolution(monkeypatch):
    """The initial resolution refuses the unrelated row — terminally.

    The ambient runtime seeds a truthy URL, key and pool (plus api_mode and an
    ACP transport), and config holds exactly ONE custom row — named ``omni``,
    with a truthy URL and key of its own. The send asks for ``custom:ghost``,
    which nothing owns.

    This covers the FIRST of the three streaming regions that build an agent,
    and only that one: the verdict is terminal here, so no agent is constructed,
    no turn is sent, and no 401 exists for the two self-heal retries to act on.
    Parametrizing ``fail_first`` over this case would re-run identical code and
    prove nothing about those retry guards — they are exercised directly by
    ``test_retry_abandons_the_heal_*`` below, which start from a ROUTABLE route
    and break it mid-turn.
    """
    label = "initial send"
    session_id = "session-1806-ghost-initial"

    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        copy.deepcopy(_SOLE_UNRELATED_ROW_CFG),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        session_id,
        model="@custom:ghost:antigravity/gemini-3.7-flash-tiered",
    )

    payload = apperrors[-1]
    assert "custom:ghost" in payload["message"], f"{label}: {payload}"
    assert "is not configured" in payload["message"], f"{label}: {payload}"

    # Nothing that could have been routed to may appear on the refusal path:
    # not the unrelated sole row's pair, not the ambient provider's, and never
    # the keyless placeholder.
    blob = str(payload) + str(captured)
    for leaked in (
        "https://omni.example/v1",
        "omni-key",
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        config.KEYLESS_CUSTOM_API_KEY,
    ):
        assert leaked not in blob, f"{label}: {leaked!r} leaked into the refused route"

    assert not captured.get("explicit_client_kwargs_calls"), (
        f"{label}: a client was configured for a slug nothing owns"
    )


# ── declared-but-unresolved credentials are NOT keyless ──────────────────────


_MISSING_ENV_VAR = "HERMES_TEST_1806_UNSET_CREDENTIAL"


def _clear_credential_env(monkeypatch):
    """Remove every env var the credential ladder could still mint a key from."""
    monkeypatch.delenv(_MISSING_ENV_VAR, raising=False)
    monkeypatch.delenv("CUSTOM_OMNI_API_KEY", raising=False)


_UNRESOLVED_CREDENTIAL_ROWS = [
    # A declared ``${ENV}`` reference whose variable is unset.
    ({"api_key": "${" + _MISSING_ENV_VAR + "}"}, "env-reference"),
    # A declared ``key_env`` naming a variable that does not exist.
    ({"key_env": _MISSING_ENV_VAR}, "key-env"),
    # A configured credential pool that yields nothing for this endpoint.
    ({"credential_pool": ["configured-pool-sentinel"]}, "unavailable-pool"),
]


@pytest.mark.parametrize("row_fields,label", _UNRESOLVED_CREDENTIAL_ROWS)
def test_declared_but_unresolved_credential_is_not_keyless(monkeypatch, row_fields, label):
    """``keyless`` means "declares no credential", not "resolved no credential".

    Each row here DECLARES a credential source that produces nothing. Reporting
    that as keyless is what let ``dummy-key`` be substituted for a genuinely
    missing credential, turning a fixable misconfiguration into an opaque 401.
    """
    _clear_credential_env(monkeypatch)
    cfg_dict = _exact_list_row_cfg(drop=("api_key",), **row_fields)
    _with_direct_config(monkeypatch, cfg_dict)

    bundle = config.resolve_custom_provider_bundle("custom:omni")

    assert bundle["api_key"] is None, f"{label}: the credential must not resolve here"
    assert bundle["keyless"] is False, (
        f"{label}: a declared credential source was reported as keyless"
    )


@pytest.mark.parametrize("row_fields,label", _UNRESOLVED_CREDENTIAL_ROWS)
def test_declared_but_unresolved_credential_never_gets_the_dummy_key(
    monkeypatch, row_fields, label
):
    """End to end: the send STOPS — neither the placeholder nor a keyless send.

    The endpoint resolves (the row owns it), but the declared credential source
    produced nothing. Sending anyway with ``api_key=None`` is not the safe
    middle ground it looks like: with only one of the pair truthy, AIAgent falls
    through to ``_routed_client_kwargs()`` and re-resolves a provider, so the
    turn — and whatever credential init then finds — leaves for an endpoint the
    user never chose. The turn ends on the actionable cause instead.
    """
    _clear_credential_env(monkeypatch)

    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        _exact_list_row_cfg(drop=("api_key",), **row_fields),
        _ambient_runtime(),
        f"session-1806-unresolved-{label}",
        model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
    )

    payload = apperrors[-1]
    assert "produced no API key" in payload["message"], f"{label}: {payload}"
    assert "custom:omni" in payload["message"], (
        f"{label}: the refusal did not name the provider that failed: {payload}"
    )
    assert config.KEYLESS_CUSTOM_API_KEY not in str(payload), (
        f"{label}: keyless placeholder masked a declared-but-missing credential"
    )
    assert "keyed-key-sentinel-abc" not in str(payload), (
        f"{label}: the ambient keyed record leaked into the refusal"
    )
    # The row owns its endpoint, so nothing may have been dialled with it.
    assert not captured.get("explicit_client_kwargs_calls"), f"{label}: a client was configured"


def test_row_declaring_no_credential_is_still_keyless(monkeypatch):
    """The negative control: a genuinely unauthenticated endpoint keeps ``dummy-key``.

    Local OpenAI-compatible servers routinely run without auth, and tightening
    the keyless rule must not take that away — otherwise every keyless setup
    regresses into an agent built with no credential at all.
    """
    _clear_credential_env(monkeypatch)

    init_kwargs = _run_composed_send(
        monkeypatch,
        _exact_list_row_cfg(drop=("api_key",)),
        _ambient_runtime(),
        "session-1806-genuinely-keyless",
    )

    assert init_kwargs["api_key"] == config.KEYLESS_CUSTOM_API_KEY
    assert init_kwargs["base_url"] == _LIST_ROW_URL
    assert init_kwargs["provider"] == "custom"


# ── the exact row's credential is never the same-endpoint keyed row's ───────
#
# Every unresolved-credential case above puts the keyed record on a DIFFERENT
# endpoint, so the merge's provenance test already proves the ambient credential
# foreign. That leaves the collision untested: a keyed
# ``providers["custom:<slug>"]`` record may declare the SAME ``base_url`` as the
# exact ``custom_providers[]`` row, and then endpoint equality alone cannot tell
# the row's own resolution from the keyed row's. Inferring "same authority" from
# the URL there hands the keyed row's key to the exact row — the row's endpoint
# married to somebody else's credential, which is the split-authority merge the
# whole module exists to prevent.
#
# An exact row is a COMPLETE record: its credential comes from its own ladder or
# it does not come at all.


_SHARED_ENDPOINT_URL = "https://shared-url-sentinel.example/v1"
_SHARED_ENDPOINT_KEYED_KEY = "shared-endpoint-keyed-sentinel-def"


def _shared_endpoint_cfg(**row_fields):
    """Exact list row and same-slug keyed record on the IDENTICAL ``base_url``."""
    return {
        "model": {"default": "active/model", "provider": "custom:active"},
        "providers": {
            "custom:omni": {
                "base_url": _SHARED_ENDPOINT_URL,
                "api_key": _SHARED_ENDPOINT_KEYED_KEY,
            },
        },
        "custom_providers": [
            {"name": "omni", "base_url": _SHARED_ENDPOINT_URL, **row_fields},
        ],
    }


def _shared_endpoint_runtime():
    """The ambient runtime that resolution of the KEYED record produces.

    Same endpoint as the exact row, carrying the keyed record's credential — the
    state in which URL equality stops being evidence of provenance.
    """
    return _ambient_runtime(
        base_url=_SHARED_ENDPOINT_URL, api_key=_SHARED_ENDPOINT_KEYED_KEY
    )


@pytest.mark.parametrize("row_fields,label", _UNRESOLVED_CREDENTIAL_ROWS)
def test_exact_row_unresolved_credential_ignores_same_endpoint_keyed_key(
    monkeypatch, row_fields, label
):
    """The row DECLARED a credential that produced nothing: the route is terminal.

    The keyed record sharing the endpoint changes nothing about that. If the
    merge accepts ``_rt["api_key"]`` because the URLs match, the bundle silently
    authenticates the row's endpoint with the keyed row's secret and reports
    itself routable, so the user never learns their ``key_env`` is unset.
    """
    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _shared_endpoint_cfg(**row_fields))

    resolved = config.resolve_custom_provider_bundle("custom:omni")
    assert resolved["is_exact"] is True, f"{label}: the exact row was not selected"
    assert resolved["keyless"] is False, (
        f"{label}: a declared credential source was reported as keyless"
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _SHARED_ENDPOINT_KEYED_KEY,
        _SHARED_ENDPOINT_URL,
        _shared_endpoint_runtime(),
        lookup_provider="custom:omni",
    )

    assert bundle["base_url"] == _SHARED_ENDPOINT_URL, f"{label}: the row lost its endpoint"
    assert bundle["api_key"] is None, (
        f"{label}: the exact row borrowed the same-endpoint keyed credential: {bundle!r}"
    )
    assert bundle["api_key"] != _SHARED_ENDPOINT_KEYED_KEY, f"{label}: {bundle!r}"
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY, (
        f"{label}: the placeholder masked a declared-but-missing credential"
    )
    verdict = config.custom_provider_route_error(bundle)
    assert verdict, f"{label}: an unroutable bundle reported itself routable: {bundle!r}"
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL, f"{label}: {verdict}"


@pytest.mark.parametrize("row_fields,label", _UNRESOLVED_CREDENTIAL_ROWS)
def test_composed_send_refuses_exact_row_sharing_the_keyed_endpoint(
    monkeypatch, row_fields, label
):
    """End to end: the turn stops instead of dialling with the keyed row's key.

    The production-composed path is where the borrowed credential would actually
    be spent — the endpoint resolves, so nothing downstream would question a
    truthy ``api_key``.
    """
    _clear_credential_env(monkeypatch)

    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        _shared_endpoint_cfg(**row_fields),
        _shared_endpoint_runtime(),
        f"session-1806-shared-endpoint-{label}",
        model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
    )

    payload = apperrors[-1]
    assert "produced no API key" in payload["message"], f"{label}: {payload}"
    assert "custom:omni" in payload["message"], f"{label}: {payload}"
    assert _SHARED_ENDPOINT_KEYED_KEY not in str(payload), (
        f"{label}: the same-endpoint keyed credential leaked into the refusal"
    )
    assert config.KEYLESS_CUSTOM_API_KEY not in str(payload), f"{label}: {payload}"
    assert not captured.get("explicit_client_kwargs_calls"), (
        f"{label}: a client was configured for a route whose credential failed"
    )


def test_exact_row_declaring_no_credential_ignores_the_shared_endpoint_key(monkeypatch):
    """The other half of the rule: a keyless row does not borrow the key either.

    The row declares NO credential, which is its own statement that the endpoint
    is unauthenticated. ``dummy-key`` follows from the row, not from the keyed
    record that happens to point at the same URL — so the route stays routable
    and the keyed secret still never leaves the keyed record.
    """
    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _shared_endpoint_cfg())

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _SHARED_ENDPOINT_KEYED_KEY,
        _SHARED_ENDPOINT_URL,
        _shared_endpoint_runtime(),
        lookup_provider="custom:omni",
    )

    assert bundle["base_url"] == _SHARED_ENDPOINT_URL
    assert bundle["api_key"] == config.KEYLESS_CUSTOM_API_KEY, (
        f"the keyless row did not keep its own credential verdict: {bundle!r}"
    )
    assert bundle["api_key"] != _SHARED_ENDPOINT_KEYED_KEY, bundle
    assert config.custom_provider_route_error(bundle) is None, bundle


# The exact row's ENDPOINT obeys the same positive-tie rule as every other
# record branch. "Complete record" decides whose endpoint and whose credential
# the route gets; it does not decide the SPELLING of an endpoint both sides
# already agree on.
_ROW_OWN_KEY = "row-own-key-sentinel-ghi"
_FOREIGN_AMBIENT_URL = "https://ambient-sentinel.example/v1"


@pytest.mark.parametrize(
    "runtime_base_url,label",
    [
        (_SHARED_ENDPOINT_URL, "same normalized URL from distinct keyed record"),
        (_FOREIGN_AMBIENT_URL, "foreign ambient endpoint"),
    ],
    ids=["same-normalized-url", "foreign-endpoint"],
)
def test_exact_row_preserves_own_endpoint_beside_distinct_keyed_runtime(
    monkeypatch, runtime_base_url, label
):
    """An exact row preserves its own declared endpoint spelling beside a distinct record.

    Endpoint equality alone is NOT selected-record provenance. When the runtime
    carries credentials from a same-slug keyed record (_SHARED_ENDPOINT_KEYED_KEY)
    while the exact row declares _ROW_OWN_KEY, matching normalized URL spelling
    does not tie the runtime to this row. The selected exact row's own endpoint
    spelling stands in both cases.
    """
    _clear_credential_env(monkeypatch)
    _with_direct_config(
        monkeypatch,
        _shared_endpoint_cfg(base_url=_SHARED_ENDPOINT_URL + "/", api_key=_ROW_OWN_KEY),
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _SHARED_ENDPOINT_KEYED_KEY,
        runtime_base_url,
        _ambient_runtime(base_url=runtime_base_url, api_key=_SHARED_ENDPOINT_KEYED_KEY),
        lookup_provider="custom:omni",
    )

    assert bundle["base_url"] == _SHARED_ENDPOINT_URL + "/", f"{label}: {bundle!r}"
    assert bundle["base_url"] != _FOREIGN_AMBIENT_URL, (
        f"{label}: the row was pointed at the ambient provider's endpoint"
    )
    assert bundle["api_key"] == _ROW_OWN_KEY, (
        f"{label}: the exact row did not supply its own credential: {bundle!r}"
    )
    assert bundle["api_key"] != _SHARED_ENDPOINT_KEYED_KEY, f"{label}: {bundle!r}"
    assert config.custom_provider_route_error(bundle) is None, f"{label}: {bundle!r}"


# ─────────────────────────────────────────────────────────────────────────────
# The two 401 self-heal RETRIES are route boundaries too
#
# Everything above stops a terminal route at the FIRST resolution, where no agent
# exists yet. The self-heal retries are a different boundary, and a strictly
# worse one: the route WAS routable when the turn started, an agent was built and
# written to ``SESSION_AGENT_CACHE`` under a valid bundle signature, the provider
# answered 401, and only THEN does the re-resolve run. Whatever that second
# resolution returns is what the retry agent gets constructed from.
#
# That window is real state, not a hypothetical: between the first send and the
# heal the owning ``custom_providers[]`` row can be edited, renamed or deleted in
# Settings, and its ``key_env`` / pool can stop yielding a credential. The
# refreshed bundle is then terminal, and building the retry agent from it hands
# ``_init_openai_client()`` an incomplete pair — so the retry, the send that
# actually carries the user's prompt, is the one that re-enters
# ``_routed_client_kwargs()``. It also overwrites a GOOD cache entry with the
# poisoned agent, so every later turn in the session reuses it.
#
# ``heal_mutate`` runs inside the stubbed ``_attempt_credential_self_heal`` —
# after the first agent has already failed with its 401, before the retry
# re-resolves its bundle — which is the only point at which that transition can
# be staged. Each test below asserts the first send really happened (one agent,
# one turn, one cache write), so a regression that makes the route terminal up
# front cannot pass these by the back door.
# ─────────────────────────────────────────────────────────────────────────────


# No ``providers:`` block and a ``model.provider`` naming an UNRELATED slug: the
# single list row is the ONLY authority for ``custom:omni``, so removing or
# renaming it below leaves the slug owned by nothing at all. A same-slug keyed
# record or a ``model:`` block naming ``custom:omni`` would each still be an
# authority and would make the "unowned" cases test something weaker.
_RETRY_OWNED_CFG = {
    "model": {"default": "active/model", "provider": "custom:active"},
    "custom_providers": [
        {
            "name": "omni",
            "base_url": _LIST_ROW_URL,
            "api_key": _LIST_ROW_KEY,
        },
    ],
}

_RETRY_MODEL = "@custom:omni:antigravity/gemini-3.7-flash-tiered"


def _retry_cfg_rows(**row_fields):
    """The owning row with ``row_fields`` applied; ``None`` values drop the field."""
    row = dict(_RETRY_OWNED_CFG["custom_providers"][0])
    for field, value in row_fields.items():
        if value is None:
            row.pop(field, None)
        else:
            row[field] = value
    return [row]


def _drop_the_owning_row():
    """The row is deleted mid-turn — nothing owns ``custom:omni`` any more."""
    config.cfg["custom_providers"] = []


def _rename_the_owning_row():
    """The row is renamed mid-turn, so it now owns a DIFFERENT slug.

    The pair is still sitting in config, which is exactly the shape that made
    the sole-unrelated-row fallback look harmless: ``custom:omni`` must not
    inherit it just because it is the only row left.
    """
    config.cfg["custom_providers"] = _retry_cfg_rows(name="omni-renamed")


def _malform_the_owning_row():
    """The row loses its name, so it names no slug and therefore owns none.

    ``CUSTOM_SELECTION_MALFORMED`` proper describes a bare ``custom:`` lookup
    with no slug behind it, which cannot arise mid-turn — the lookup is fixed at
    the first resolve. A row that stops naming any identity is the reachable
    malformed-record analogue, and it must fail closed the same way.
    """
    config.cfg["custom_providers"] = _retry_cfg_rows(name=None)


_RETRY_UNOWNED_MUTATIONS = [
    (_drop_the_owning_row, "deleted", "row deleted"),
    (_rename_the_owning_row, "renamed", "row renamed to another slug"),
    (_malform_the_owning_row, "unnamed", "row lost its name"),
]


def _retry_endpoint_mutation():
    """Return a heal mutation that removes only the owning row's endpoint."""

    def _mutate():
        # Keep the credential so this is the exact credential-only record shape:
        # the row still owns the named route, but cannot supply an endpoint.
        config.cfg["custom_providers"] = _retry_cfg_rows(base_url=None)

    return _mutate


def _retry_credential_mutation(row_fields):
    """Return a heal mutation that keeps the endpoint but breaks the credential."""

    def _mutate():
        # ``api_key`` first so a row that DECLARES one (the ``${ENV}`` shape)
        # overrides the drop, while ``key_env`` / ``credential_pool`` rows keep
        # it dropped. Either way the row still owns its endpoint.
        fields = {"api_key": None, **row_fields}
        config.cfg["custom_providers"] = _retry_cfg_rows(**fields)

    return _mutate


def _run_composed_retry_expecting_abandoned_heal(
    monkeypatch, cfg_dict, runtime_dict, session_id, *, fail_first, heal_mutate
):
    """Drive a send that starts routable and turns terminal at the 401 retry.

    Returns ``(captured, apperrors, cache_at_heal, cache_after)``. Both cache
    views are read while the worker's entries are still live — ``restore()``
    clears ``SESSION_AGENT_CACHE`` — so the caller can compare what the initial
    send cached against what survived the abandoned heal.
    """
    import api.streaming as streaming

    cache_at_heal = {}

    def _stage_the_terminal_retry():
        # Snapshot the entry the FIRST (routable) agent was cached under, taken
        # before the route is broken. The abandoned retry must leave it exactly
        # as it is: replacing it is how one mid-turn config edit would re-route
        # every remaining turn in the session.
        with config.SESSION_AGENT_CACHE_LOCK:
            cache_at_heal.update(config.SESSION_AGENT_CACHE)
        heal_mutate()

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        copy.deepcopy(cfg_dict),
        runtime_dict,
        session_id=session_id,
        fail_first=fail_first,
        heal_mutate=_stage_the_terminal_retry,
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model=_RETRY_MODEL,
            workspace="/tmp",
            stream_id=stream_id,
        )
        apperrors = _drain_apperrors(q)
        with config.SESSION_AGENT_CACHE_LOCK:
            cache_after = dict(config.SESSION_AGENT_CACHE)
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()
    return captured, apperrors, cache_at_heal, cache_after


def _assert_retry_abandoned(
    captured,
    apperrors,
    cache_at_heal,
    cache_after,
    session_id,
    label,
    *,
    expected_cause,
    expected_initial_url=_LIST_ROW_URL,
    expected_initial_key=_LIST_ROW_KEY,
):
    """Assert the retry stopped at the refreshed route verdict, not at the 401."""
    # (0) The case is only meaningful if the FIRST send was routable and really
    # ran. Without this, a regression that made the route terminal at initial
    # resolution would satisfy every assertion below for the wrong reason.
    history = captured.get("init_kwargs_history", [])
    assert history, f"{label}: the initial send never constructed an agent"
    assert history[0]["base_url"] == expected_initial_url, (
        f"{label}: the initial send did not resolve the owning row, so no 401 "
        f"retry path was ever reached: {history[0]}"
    )
    assert history[0]["api_key"] == expected_initial_key, f"{label}: {history[0]}"
    assert cache_at_heal.get(session_id), (
        f"{label}: the initial agent was never cached, so this case cannot show "
        f"that the abandoned retry left a good cache entry alone"
    )

    # (1) No second agent — the retry construction is what the guard prevents.
    assert len(history) == 1, (
        f"{label}: the retry constructed a second agent on a route that had "
        f"become terminal ({len(history)} constructions: {history})"
    )
    assert len(captured.get("instances", [])) == 1, label

    # (2) _routed_client_kwargs() is never reached. The one explicit call is the
    # initial, complete pair; anything else means an incomplete pair reached
    # _init_openai_client() and re-entered provider routing.
    routed = captured.get("routed_client_kwargs_calls", [])
    assert not routed, (
        f"{label}: the retry agent was built with an incomplete connection pair "
        f"{routed}, so _init_openai_client() fell through to "
        f"_routed_client_kwargs() and re-resolved a provider"
    )
    explicit = captured.get("explicit_client_kwargs_calls", [])
    assert explicit == [{"api_key": expected_initial_key, "base_url": expected_initial_url}], (
        f"{label}: expected only the initial send's explicit pair, got {explicit}"
    )

    # (3) The retry turn was never sent.
    assert captured.get("run_calls") == 1, (
        f"{label}: run_conversation ran {captured.get('run_calls')!r} times — the "
        f"retry turn was sent on a route that no longer resolves"
    )

    # (4) The cache still holds the FIRST agent under the FIRST bundle
    # signature. A retry write here would be the durable half of the defect:
    # later turns reuse the cached agent without re-resolving at all.
    assert list(cache_after) == [session_id], (
        f"{label}: unexpected agent-cache contents {list(cache_after)}"
    )
    assert cache_after[session_id][0] is cache_at_heal[session_id][0], (
        f"{label}: the agent cache was rewritten with a retry agent built on an "
        f"unroutable bundle"
    )
    assert cache_after[session_id][1] == cache_at_heal[session_id][1], (
        f"{label}: a cache entry was written under the invalid bundle's signature"
    )
    assert cache_after[session_id][0] is captured["instances"][0], label

    # (5) The client is told the real cause, not the 401 that triggered the heal.
    assert apperrors, f"{label}: no controlled failure was emitted"
    payload = apperrors[-1]
    assert payload["type"] == "provider_unroutable", (
        f"{label}: emitted {payload['type']!r} instead of a provider-route "
        f"failure — the 401 is the symptom, the unroutable route is the cause"
    )
    assert expected_cause in payload.get("message", ""), (
        f"{label}: the failure did not name {expected_cause!r}: {payload}"
    )
    assert "custom:omni" in payload.get("message", ""), (
        f"{label}: the failure did not name the provider that failed: {payload}"
    )
    assert payload.get("hint"), f"{label}: the failure named no fix"

    # Nothing the abandoned retry could have been re-routed to may appear on the
    # refusal path, and the keyless placeholder may never stand in for it.
    blob = str(payload)
    for leaked in (
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        config.KEYLESS_CUSTOM_API_KEY,
    ):
        assert leaked not in blob, f"{label}: {leaked!r} leaked into the refusal"
    return payload


@pytest.mark.parametrize("fail_first", ["returned_error", "raised"])
@pytest.mark.parametrize(
    "heal_mutate,mutation_slug,mutation_label",
    _RETRY_UNOWNED_MUTATIONS,
    ids=[slug for _fn, slug, _label in _RETRY_UNOWNED_MUTATIONS],
)
def test_retry_abandons_the_heal_when_the_slug_stops_being_owned(
    monkeypatch, fail_first, heal_mutate, mutation_slug, mutation_label
):
    """Case A — the refreshed route is unowned, so the retry must not be built.

    The turn starts on a row that owns ``custom:omni`` outright, so an agent is
    constructed with the row's exact pair and the turn is sent. The provider
    answers 401. Before the heal re-resolves, the row stops owning the slug.

    Building the retry from that bundle is the whole defect: its key and URL are
    empty, which ``_init_openai_client()`` reads as "resolve a provider yourself"
    — and the ambient runtime dict the heal returns still carries a truthy
    endpoint, credential and pool for a provider the user never named.
    """
    label = f"{mutation_label} / {fail_first}"
    session_id = f"session-1806-retry-unowned-{fail_first}-{mutation_slug}"

    captured, apperrors, cache_at_heal, cache_after = (
        _run_composed_retry_expecting_abandoned_heal(
            monkeypatch,
            _RETRY_OWNED_CFG,
            _ambient_runtime(),
            session_id,
            fail_first=fail_first,
            heal_mutate=heal_mutate,
        )
    )

    _assert_retry_abandoned(
        captured,
        apperrors,
        cache_at_heal,
        cache_after,
        session_id,
        label,
        expected_cause="is not configured",
    )


@pytest.mark.parametrize("fail_first", ["returned_error", "raised"])
@pytest.mark.parametrize("row_fields,credential_label", _UNRESOLVED_CREDENTIAL_ROWS)
def test_retry_abandons_the_heal_when_the_refreshed_credential_resolves_to_nothing(
    monkeypatch, fail_first, row_fields, credential_label
):
    """Case B — the row still owns the route, but its credential now yields nothing.

    This is the shape a 401 self-heal is most likely to meet in the wild: the
    401 happened BECAUSE the credential went away, so the re-resolve finds the
    same row with a declared-but-unresolved source. ``keyless`` is False there,
    so no ``dummy-key`` is substituted and the pair stays incomplete — and an
    incomplete pair at the constructor is a re-route, not a refusal.

    The retry therefore has to stop, and the turn has to end naming the
    credential setting rather than the 401 it produced.
    """
    _clear_credential_env(monkeypatch)
    label = f"{credential_label} / {fail_first}"
    session_id = f"session-1806-retry-nocred-{fail_first}-{credential_label}"

    captured, apperrors, cache_at_heal, cache_after = (
        _run_composed_retry_expecting_abandoned_heal(
            monkeypatch,
            _RETRY_OWNED_CFG,
            _ambient_runtime(),
            session_id,
            fail_first=fail_first,
            heal_mutate=_retry_credential_mutation(row_fields),
        )
    )

    _assert_retry_abandoned(
        captured,
        apperrors,
        cache_at_heal,
        cache_after,
        session_id,
        label,
        expected_cause="produced no API key",
    )


def test_retry_still_succeeds_when_the_refreshed_route_is_still_routable(monkeypatch):
    """The negative control: a heal that re-resolves a GOOD route still retries.

    Without this, every assertion above would also pass if the guards simply
    abandoned all self-heals — which would take the #1401 credential-refresh
    retry away entirely. Here the mutation swaps the row's key for a NEW one
    (the refresh a heal exists to pick up), so the retry must be constructed
    with the new pair and the turn must be sent a second time.
    """
    session_id = "session-1806-retry-still-routable"

    def _rotate_the_credential():
        config.cfg["custom_providers"] = _retry_cfg_rows(api_key="rotated-key-sentinel")

    import api.streaming as streaming

    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        copy.deepcopy(_RETRY_OWNED_CFG),
        _ambient_runtime(),
        session_id=session_id,
        fail_first="returned_error",
        heal_mutate=_rotate_the_credential,
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model=_RETRY_MODEL,
            workspace="/tmp",
            stream_id=stream_id,
        )
        apperrors = _drain_apperrors(q)
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    history = captured["init_kwargs_history"]
    assert len(history) == 2, f"the routable heal did not retry: {history}"
    assert history[1]["api_key"] == "rotated-key-sentinel"
    assert history[1]["base_url"] == _LIST_ROW_URL
    assert captured["run_calls"] == 2, "the refreshed credential never sent a turn"
    assert not apperrors, f"a routable retry emitted a failure: {apperrors}"


# ─────────────────────────────────────────────────────────────────────────────
# The generic bare-``custom`` authority owns NO named slug
#
# ``providers['custom']`` and a ``model:`` block whose provider is the bare
# string ``custom`` name no slug at all. While they were still eligible
# candidates for every ``custom:<slug>`` lookup, ``custom:ghost`` selected one of
# them and inherited its endpoint, credential, credential pool, ``api_mode`` and
# ACP transport — the same wrong-authority pairing the sole-unrelated-row rule
# above exists to prevent, only sourced from ``providers:``/``model:`` instead of
# ``custom_providers[]``. A named route is identity-owned: exact slug match, or
# nothing.
# ─────────────────────────────────────────────────────────────────────────────


_BARE_CUSTOM_PROVIDERS_RECORD = {
    "base_url": "https://bare-providers-sentinel.example/v1",
    "api_key": "bare-providers-key-sentinel",
    "api_mode": "anthropic_messages",
    "credential_pool": ["bare-pool-sentinel"],
    "acp_command": "bare-acp-sentinel",
    "acp_args": ["--bare-arg-sentinel"],
}

_BARE_CUSTOM_MODEL_BLOCK = {
    "provider": "custom",
    "base_url": "https://bare-model-sentinel.example/v1",
    "api_key": "bare-model-key-sentinel",
}


def _bare_custom_cfg(*, providers_record=True, model_block=True):
    """Config carrying the generic bare-``custom`` authority, but no ``ghost``."""
    cfg_dict = {
        "model": {"default": "ghostly/model"},
        "providers": {},
        "custom_providers": [
            {
                "name": "omni",
                "base_url": "https://omni.example/v1",
                "api_key": "omni-key",
            },
        ],
    }
    if providers_record:
        cfg_dict["providers"]["custom"] = copy.deepcopy(_BARE_CUSTOM_PROVIDERS_RECORD)
    if model_block:
        cfg_dict["model"].update(copy.deepcopy(_BARE_CUSTOM_MODEL_BLOCK))
    return cfg_dict


def _clear_ghost_credential_env(monkeypatch):
    """Keep the ``CUSTOM_<SLUG>_API_KEY`` convention out of these verdicts."""
    monkeypatch.delenv("CUSTOM_GHOST_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_OMNI_API_KEY", raising=False)


_BARE_CUSTOM_SHAPES = [
    (True, False, "providers['custom'] only"),
    (False, True, "model.provider: custom only"),
    (True, True, "both bare-custom authorities"),
]


@pytest.mark.parametrize(
    "providers_record,model_block,label",
    _BARE_CUSTOM_SHAPES,
    ids=["providers-custom", "model-provider-custom", "both"],
)
def test_unknown_named_slug_never_claims_the_bare_custom_authority(
    monkeypatch, providers_record, model_block, label
):
    """``custom:ghost`` must fail closed even with a generic ``custom`` record present.

    Neither authority names ``ghost``, so neither owns the route. Selecting one
    would pair the user's prompt with an endpoint and a credential they never
    pointed this slug at.
    """
    _clear_ghost_credential_env(monkeypatch)
    _with_direct_config(
        monkeypatch, _bare_custom_cfg(providers_record=providers_record, model_block=model_block)
    )

    assert config.resolve_custom_provider_connection("custom:ghost") == (None, None), label

    ghost = config.resolve_custom_provider_bundle("custom:ghost")
    assert ghost["status"] == config.CUSTOM_SELECTION_MISSING, label
    assert ghost["source"] == "", label
    assert ghost["record"] is None, f"{label}: a bare-custom record was selected for a named slug"
    assert ghost["base_url"] is None, label
    assert ghost["api_key"] is None, label
    assert ghost["keyless"] is False, f"{label}: an unowned route was reported keyless"
    assert ghost["owned"] == {}, f"{label}: an unowned route claimed side fields"

    # The named row is still authoritative for its OWN slug.
    owned = config.resolve_custom_provider_bundle("custom:omni")
    assert owned["status"] == config.CUSTOM_SELECTION_EXACT, label
    assert owned["base_url"] == "https://omni.example/v1", label


@pytest.mark.parametrize(
    "providers_record,model_block,label",
    _BARE_CUSTOM_SHAPES,
    ids=["providers-custom", "model-provider-custom", "both"],
)
def test_unknown_named_slug_inherits_no_bare_custom_connection_or_side_fields(
    monkeypatch, providers_record, model_block, label
):
    """The merge carries nothing from the bare-``custom`` record onto ``ghost``.

    Not the endpoint, not the credential, and — because a bare record's
    ``api_mode``/pool/ACP transport are as unowned as its URL — none of the side
    fields either.
    """
    _clear_ghost_credential_env(monkeypatch)
    _with_direct_config(
        monkeypatch, _bare_custom_cfg(providers_record=providers_record, model_block=model_block)
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:ghost",
        "ambient-key-sentinel",
        "https://ambient.example/v1",
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        lookup_provider="custom:ghost",
    )

    _assert_ghost_bundle_failed_closed(bundle, label)
    assert bundle["base_url"] != _BARE_CUSTOM_PROVIDERS_RECORD["base_url"], label
    assert bundle["base_url"] != _BARE_CUSTOM_MODEL_BLOCK["base_url"], label
    assert bundle["api_key"] != _BARE_CUSTOM_PROVIDERS_RECORD["api_key"], label
    assert bundle["api_key"] != _BARE_CUSTOM_MODEL_BLOCK["api_key"], label


def test_unknown_named_slug_connection_view_ignores_the_bare_custom_record(monkeypatch):
    """The three-field view reaches the same verdict, and reports ``custom_owned`` False."""
    _clear_ghost_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _bare_custom_cfg())

    provider, api_key, base_url, custom_owned = (
        config.apply_custom_provider_connection_authority(
            "custom:ghost",
            "ambient-key-sentinel",
            "https://ambient.example/v1",
            lookup_provider="custom:ghost",
            runtime_provider=copy.deepcopy(_SELF_LABELLED_GHOST_RUNTIME),
        )
    )

    assert base_url is None
    assert api_key is None
    assert provider == "custom:ghost"
    assert custom_owned is False


def test_bare_custom_route_still_uses_the_bare_custom_authority(monkeypatch):
    """The carve-out: closing the generic path for NAMED slugs, not for bare ``custom``.

    ``providers['custom']`` is the bare route's own identity record, so it stays
    eligible there — the tightening above is about a named slug borrowing an
    authority that never named it.
    """
    _clear_ghost_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _bare_custom_cfg(model_block=False))

    bare = config.resolve_custom_provider_bundle("custom:custom")
    assert bare["status"] == config.CUSTOM_SELECTION_KEYED
    assert bare["source"] == "providers"
    assert bare["base_url"] == _BARE_CUSTOM_PROVIDERS_RECORD["base_url"]
    assert bare["api_key"] == _BARE_CUSTOM_PROVIDERS_RECORD["api_key"]
    assert bare["owned"]["api_mode"] == "anthropic_messages"
    assert bare["owned"]["credential_pool"] == ["bare-pool-sentinel"]
    assert bare["owned"]["acp_command"] == "bare-acp-sentinel"


def test_model_block_naming_the_slug_outright_still_owns_it(monkeypatch):
    """Only the GENERIC ``custom`` spelling is refused, not an explicit slug.

    A ``model:`` block whose provider is ``custom:ghost`` names this identity, so
    it remains the route's authority.
    """
    _clear_ghost_credential_env(monkeypatch)
    cfg_dict = _bare_custom_cfg(model_block=False)
    cfg_dict["model"].update(
        {
            "provider": "custom:ghost",
            "base_url": "https://named-model-sentinel.example/v1",
            "api_key": "named-model-key-sentinel",
        }
    )
    _with_direct_config(monkeypatch, cfg_dict)

    ghost = config.resolve_custom_provider_bundle("custom:ghost")
    assert ghost["status"] == config.CUSTOM_SELECTION_KEYED
    assert ghost["source"] == "model"
    assert ghost["base_url"] == "https://named-model-sentinel.example/v1"
    assert ghost["api_key"] == "named-model-key-sentinel"
    # And it beats the bare-custom record that is still sitting in providers:.
    assert ghost["base_url"] != _BARE_CUSTOM_PROVIDERS_RECORD["base_url"]


# ─────────────────────────────────────────────────────────────────────────────
# An identity-keyed record owning ONLY side fields or a dynamic credential
#
# ``providers['custom:<slug>']`` names this provider outright, so whatever it
# declares, it owns. Judging it by "did a STATIC api_key resolve, or is there a
# base_url?" classified a perfectly valid ``key_cmd``-only / ``api_mode``-only /
# pool-only / ACP-only record as ``missing`` — and the merge then CLEARED the
# very fields that record exists to supply, reverting them to the ambient
# runtime's.
# ─────────────────────────────────────────────────────────────────────────────


def _keyed_side_field_cfg(record):
    """Config whose only authority for ``omni`` is the keyed record ``record``."""
    return {
        "model": {"default": "active/model", "provider": "custom:active"},
        "providers": {"custom:omni": copy.deepcopy(record)},
        "custom_providers": [
            {
                "name": "active",
                "base_url": "https://active.example/v1",
                "api_key": "active-key",
            },
        ],
    }


_KEYED_SIDE_FIELD_ONLY_RECORDS = [
    (
        {"api_mode": "anthropic_messages"},
        {"api_mode": "anthropic_messages"},
        True,
        "api_mode only",
    ),
    (
        {"transport": "anthropic"},
        {"api_mode": "anthropic_messages"},
        True,
        "transport alias only",
    ),
    (
        {"credential_pool": ["keyed-pool-sentinel"]},
        {"credential_pool": ["keyed-pool-sentinel"]},
        # A configured pool is a declared credential source, so NOT keyless.
        False,
        "credential_pool only",
    ),
    (
        {"acp_command": "keyed-acp-sentinel", "acp_args": ["--keyed-arg-sentinel"]},
        {"acp_command": "keyed-acp-sentinel", "acp_args": ["--keyed-arg-sentinel"]},
        True,
        "ACP transport only",
    ),
]


@pytest.mark.parametrize(
    "record,expected_owned,expected_keyless,label",
    _KEYED_SIDE_FIELD_ONLY_RECORDS,
    ids=["api-mode", "transport-alias", "credential-pool", "acp"],
)
def test_keyed_record_owning_only_side_fields_is_not_missing(
    monkeypatch, record, expected_owned, expected_keyless, label
):
    """A keyed record with no static URL/key still OWNS what it declares."""
    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _keyed_side_field_cfg(record))

    bundle = config.resolve_custom_provider_bundle("custom:omni")
    assert bundle["status"] == config.CUSTOM_SELECTION_KEYED, (
        f"{label}: a keyed record that owns side fields was classified as missing"
    )
    assert bundle["source"] == "providers", label
    assert bundle["record"] is not None, label
    assert bundle["owned"] == expected_owned, label
    assert bundle["is_exact"] is False, label
    # It declares no endpoint of its own; that is unowned, not invalid.
    assert bundle["base_url"] is None, label
    assert bundle["keyless"] is expected_keyless, label


def test_keyed_record_owning_only_key_cmd_is_not_missing(monkeypatch):
    """``key_cmd`` is a real credential source, so a ``key_cmd``-only record owns the route.

    It resolves no STATIC key by design — the command mints a short-lived bearer
    per request — so a static-key test reported the record as missing and the
    route fell back to the ambient authority.
    """
    _clear_credential_env(monkeypatch)

    def _token_provider():
        return "minted-bearer-sentinel"

    _fake_command_token_source(monkeypatch, lambda _key_cmd, _name: _token_provider)
    _with_direct_config(monkeypatch, _keyed_side_field_cfg({"key_cmd": "print-omni-bearer"}))

    bundle = config.resolve_custom_provider_bundle("custom:omni")
    assert bundle["status"] == config.CUSTOM_SELECTION_KEYED
    assert bundle["source"] == "providers"
    assert bundle["api_key"] is _token_provider, "the keyed record's key_cmd token source was lost"
    assert bundle["keyless"] is False, "a declared key_cmd must never be reported keyless"


def test_keyed_record_owning_only_unresolved_env_credential_is_not_missing(monkeypatch):
    """A declared-but-unset ``key_env`` still names this slug's credential source.

    Treating it as missing sends the route to the ambient endpoint instead of
    surfacing the misconfiguration.
    """
    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _keyed_side_field_cfg({"key_env": _MISSING_ENV_VAR}))

    bundle = config.resolve_custom_provider_bundle("custom:omni")
    assert bundle["status"] == config.CUSTOM_SELECTION_KEYED
    assert bundle["api_key"] is None
    assert bundle["keyless"] is False, "a declared-but-unresolved credential is not keyless"


_KEYED_SIDE_FIELDS_ONLY_CFG = {
    "model": {"default": "active/model", "provider": "custom:active"},
    "providers": {
        "custom:omni": {
            # No base_url, no api_key: side fields are ALL this record declares.
            "api_mode": "anthropic_messages",
            "credential_pool": ["keyed-pool-sentinel"],
            "acp_command": "keyed-acp-sentinel",
            "acp_args": ["--keyed-arg-sentinel"],
        },
    },
    "custom_providers": [
        {
            "name": "active",
            "base_url": "https://active.example/v1",
            "api_key": "active-key",
        },
    ],
}


def test_keyed_record_side_fields_survive_the_merge_without_a_static_pair(monkeypatch):
    """The merge applies the record's owned fields instead of clearing them.

    The route itself is terminal — this record declares no endpoint, so it
    borrows none — but "fail the route closed" must not degrade into "blank the
    whole bundle". The fields the record declares for ITSELF are still its own
    statement about itself, and only the ambient authority's values go with the
    endpoint that was refused.
    """
    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, copy.deepcopy(_KEYED_SIDE_FIELDS_ONLY_CFG))

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        "ambient-key-sentinel",
        "https://ambient.example/v1",
        _ambient_runtime(
            api_mode="chat_completions",
            command="ambient-acp-sentinel",
            args=["--ambient-arg-sentinel"],
            credential_pool=["ambient-pool-sentinel"],
        ),
        lookup_provider="custom:omni",
    )

    _assert_side_fields(
        bundle,
        {
            "api_mode": "anthropic_messages",
            "credential_pool": ["keyed-pool-sentinel"],
            "acp_command": "keyed-acp-sentinel",
            "acp_args": ["--keyed-arg-sentinel"],
        },
        "keyed record owning only side fields",
    )
    # The record owns no endpoint, so the route is terminal and keeps neither the
    # ambient URL nor a credential to pair with it.
    verdict = bundle[config.CUSTOM_ROUTE_ERROR_FIELD]
    assert verdict is not None and verdict["reason"] == config.CUSTOM_ROUTE_NO_ENDPOINT
    assert bundle["base_url"] is None
    assert bundle["api_key"] is None


def test_keyed_record_side_fields_survive_the_runtime_bundle(monkeypatch):
    """End to end: the production-composed send REFUSES, keeping no ambient pair.

    This record owns side fields and nothing else — no endpoint of its own. It
    used to reach the constructor on the ambient provider's URL under the "keyed
    fill-only" rule, which is exactly the defect: the record's own authority
    decided the wire protocol, pool and transport while the ACTIVE provider
    decided where the prompt went. Owning ``api_mode`` is not owning an endpoint.

    So the send stops at the terminal verdict instead, and the merge-level test
    above keeps pinning that the record's owned fields still ride on the bundle
    it hands back. ``custom:omni``'s own side fields therefore never arrive at a
    constructor here, because no constructor runs at all.
    """
    _clear_credential_env(monkeypatch)

    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        copy.deepcopy(_KEYED_SIDE_FIELDS_ONLY_CFG),
        _ambient_runtime(
            api_mode="chat_completions",
            command="ambient-acp-sentinel",
            args=["--ambient-arg-sentinel"],
            credential_pool=["ambient-pool-sentinel"],
        ),
        "session-1806-keyed-side-fields-only",
        model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
    )

    payload = apperrors[-1]
    assert config.CUSTOM_ROUTE_NO_ENDPOINT in str(payload) or "resolved no endpoint" in (
        payload.get("message", "")
    ), payload
    blob = str(payload) + str(captured)
    for leaked in (
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        "ambient-pool-sentinel",
        "ambient-acp-sentinel",
    ):
        assert leaked not in blob, (
            f"the ambient provider's {leaked!r} survived a route with no endpoint"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Non-streaming consumers: the verdict is terminal off the streaming path too
#
# ``api/streaming.py`` is not the only thing that builds an AIAgent from a
# merged bundle. POST /api/chat and the four auxiliary consumers all go through
# ``routes._resolve_agent_connection_bundle()``, which is the single chokepoint
# that must RAISE rather than hand back a bundle with a hole in it: an
# incomplete ``(api_key, base_url)`` pair is not a refusal at the constructor,
# it is the signal to re-resolve a provider through ``_routed_client_kwargs()``.
# These pin the raise, the 400 POST /api/chat turns it into, and the negative
# control that a routable bundle still comes back untouched.
# ─────────────────────────────────────────────────────────────────────────────


def _terminal_route_cfg(kind):
    """Config producing each of the three terminal verdicts for one slug."""
    if kind == "unowned":
        # Exactly one row, named something else: nothing owns ``custom:ghost``.
        return copy.deepcopy(_SOLE_UNRELATED_ROW_CFG)
    if kind == "no_endpoint":
        # The exact row owns the slug and blanks the endpoint, so the keyed
        # row's URL must not be reachable by falling through.
        return _exact_list_row_cfg(base_url="")
    # The exact row declares a credential source that resolves to nothing.
    return _exact_list_row_cfg(drop=("api_key",), key_env=_MISSING_ENV_VAR)


_TERMINAL_ROUTE_CASES = [
    ("unowned", "custom:ghost", config.CUSTOM_ROUTE_UNOWNED, "is not configured", "Settings"),
    (
        "no_endpoint",
        "custom:omni",
        config.CUSTOM_ROUTE_NO_ENDPOINT,
        "resolved no endpoint",
        "base_url",
    ),
    (
        "no_credential",
        "custom:omni",
        config.CUSTOM_ROUTE_NO_CREDENTIAL,
        "produced no API key",
        "key_env",
    ),
]


@pytest.mark.parametrize(
    "kind,slug,expected_reason,message_fragment,hint_fragment", _TERMINAL_ROUTE_CASES
)
def test_agent_connection_bundle_raises_on_terminal_route(
    monkeypatch, kind, slug, expected_reason, message_fragment, hint_fragment
):
    """The non-streaming chokepoint refuses instead of returning a holed bundle.

    Returning ``{"base_url": None, ...}`` here would look terminal to the caller
    and be the opposite at the constructor, so all five consumers that share
    this helper would each have had to remember to check. Raising makes the
    refusal structural — and the exception subclasses ``ValueError``, which is
    what the existing handlers at those call sites already catch.
    """
    import api.routes as routes

    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _terminal_route_cfg(kind))

    # Mirror POST /api/chat's own sequence: resolve the model's provider, then
    # let the ambient runtime fill the endpoint/credential it did not resolve.
    _model, provider, base_url = config.resolve_model_provider(
        f"@{slug}:antigravity/gemini-3.7-flash-tiered"
    )
    runtime = copy.deepcopy(_ROUTE_KEYED_RUNTIME)
    api_key = runtime["api_key"]
    if not base_url:
        base_url = runtime["base_url"]

    with pytest.raises(config.CustomProviderRouteError) as excinfo:
        routes._resolve_agent_connection_bundle(provider, api_key, base_url, runtime)

    err = excinfo.value
    assert isinstance(err, ValueError), (
        "the existing except-ValueError handlers at the five call sites must keep catching it"
    )
    assert err.reason == expected_reason
    assert err.provider == slug
    assert message_fragment in err.message, err.message
    assert hint_fragment in err.hint, err.hint


def test_agent_connection_bundle_returns_routable_bundle_unchanged(monkeypatch):
    """The negative control: a routable named route still comes back, not raised.

    Without this, the three cases above are equally satisfied by a helper that
    refuses everything — which would take every working custom provider down.
    """
    import api.routes as routes

    _with_direct_config(monkeypatch, copy.deepcopy(_KEYED_VS_LIST_CFG))

    bundle = routes._resolve_agent_connection_bundle(
        "custom:omni",
        "keyed-key-sentinel-abc",
        "https://keyed-url-sentinel.example/v1",
        copy.deepcopy(_ROUTE_KEYED_RUNTIME),
    )

    assert bundle["base_url"] == _LIST_ROW_URL
    assert bundle["api_key"] == _LIST_ROW_KEY
    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None


def _drive_sync_chat_route_expecting_refusal(monkeypatch, cfg_dict, slug):
    """Drive POST /api/chat to its refusal; return ``(payload, status, captured)``.

    The session carries the explicit ``@custom:<slug>:<model>`` form the picker
    emits, which ``model_with_provider_context()`` passes through untouched.
    That matters for the unowned case: a BARE model plus a stale
    ``model_provider`` never mints an ``@custom:ghost:`` route in the first
    place (the #7356 guard drops the hint), so the explicit form is the shape in
    which an unowned slug actually reaches this route.
    """
    import api.routes as routes

    captured, fake_session = _setup_route_consumer_runtime(
        monkeypatch,
        session_messages=_four_route_messages(),
        cfg_dict=cfg_dict,
        runtime_dict=copy.deepcopy(_ROUTE_KEYED_RUNTIME),
    )
    fake_session.model = f"@{slug}:antigravity/gemini-3.7-flash-tiered"
    fake_session.model_provider = slug

    responses = []
    monkeypatch.setattr(routes, "get_session", lambda _sid: fake_session)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws: "/tmp")
    monkeypatch.setattr(
        routes, "_get_session_agent_lock", lambda _sid: contextlib.nullcontext()
    )
    monkeypatch.setattr(
        routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, None)
    )
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_k: (model, provider),
    )
    # Capture the status alongside the payload -- "which HTTP code" is half the
    # contract here (400 user-fixable, not a 500 traceback).
    def _capture_j(_handler, payload, **kwargs):
        responses.append((payload, kwargs.get("status", 200)))
        return payload

    monkeypatch.setattr(routes, "j", _capture_j)
    monkeypatch.setattr(routes, "bad", lambda _handler, message, *_a, **_k: {"error": message})

    routes._handle_chat_sync(
        object(), {"session_id": "session-1806-route", "message": "hi"}
    )

    assert responses, "POST /api/chat returned no response at all"
    payload, status = responses[-1]
    return payload, status, captured


@pytest.mark.parametrize(
    "kind,slug,expected_reason,message_fragment,hint_fragment", _TERMINAL_ROUTE_CASES
)
def test_sync_chat_route_answers_400_on_unroutable_custom_provider(
    monkeypatch, kind, slug, expected_reason, message_fragment, hint_fragment
):
    """POST /api/chat answers the actionable cause, and never builds the agent.

    400, not 500: an unroutable ``custom:<slug>`` is a user-fixable provider
    misconfiguration, exactly like the ambiguous-slug collision. The constructor
    must not be reached at all — the capturing double raises on ``__init__``, so
    a bundle that got that far would surface as a leaked
    ``_RouteAgentCaptured`` rather than a quiet pass.
    """
    _clear_credential_env(monkeypatch)

    payload, status, captured = _drive_sync_chat_route_expecting_refusal(
        monkeypatch, _terminal_route_cfg(kind), slug
    )

    assert status == 400, payload
    assert payload["type"] == "custom_provider_unroutable", payload
    assert payload["reason"] == expected_reason, payload
    assert message_fragment in payload["error"], payload
    assert slug in payload["error"], payload
    assert hint_fragment in payload["hint"], payload

    assert "init_kwargs" not in captured, (
        "an agent was constructed for an unroutable route on the non-streaming path"
    )
    # The refusal must not hand back the endpoint or credential it declined to
    # route through.
    assert "keyed-url-sentinel" not in str(payload), payload
    assert "keyed-key-sentinel-abc" not in str(payload), payload


# ─────────────────────────────────────────────────────────────────────────────
# The standard Hermes v12 config shape: a RAW ``providers:<key>`` record
#
# Everything above this point describes a config that spells a named custom
# provider one of the two ways the WebUI itself writes: a ``custom_providers[]``
# row, or a ``providers['custom:<slug>']`` key. Hermes v12 writes neither. Its
# config is
#
#     model:
#       provider: custom:omni
#     providers:
#       omni:
#         base_url: https://omni.example/v1
#         key_env: OMNI_GATE_KEY
#
# and the installed Agent routes it fine: ``_match_new_style_provider()`` scans
# the ENABLED entries of the raw ``providers:`` mapping and takes the first whose
# alias set — minted from BOTH the display ``name`` and the config KEY — holds
# the requested identity. The WebUI looked only at the ``custom:``-prefixed
# spellings, so every send on a stock v12 config failed closed as
# ``unowned_custom_provider`` while the CLI on the same config sent happily.
#
# These regressions use ONLY that shape: no ``custom_providers[]`` list entry and
# no ``providers['custom:omni']`` key. :func:`_v12_raw_cfg` asserts the absence,
# so a future edit that quietly re-adds either spelling turns the whole section
# vacuous loudly rather than silently.
# ─────────────────────────────────────────────────────────────────────────────


_V12_URL = "https://v12-raw-url-sentinel.example/v1"
_V12_KEY = "v12-raw-key-sentinel"
_V12_KEY_ENV = "HERMES_TEST_1806_V12_OMNI_KEY"


def _v12_raw_cfg(record, *, key="omni", model_provider="custom:omni"):
    """``providers: {<key>: record}`` and a ``model:`` block — nothing else.

    The ``model:`` block deliberately declares NO connection field of its own.
    ``model.provider: custom:omni`` makes it a candidate in
    ``_select_custom_provider_record``, and a base_url there would let it own
    the route — which would prove nothing about the raw record under test.
    """
    cfg_dict = {
        "model": {
            "default": "antigravity/gemini-3.7-flash-tiered",
            "provider": model_provider,
        },
        "providers": {key: copy.deepcopy(record)},
    }
    assert "custom_providers" not in cfg_dict, "the raw shape carries no list"
    assert not any(
        str(k).lower().startswith("custom") for k in cfg_dict["providers"]
    ), "the raw shape carries no ``custom:``-prefixed key"
    assert not (set(cfg_dict["model"]) & {"base_url", "api_key", "key_env"}), (
        "the model: block must declare no connection of its own"
    )
    return cfg_dict


def _v12_env(monkeypatch):
    """Seed the ``key_env``/``api_key_env`` credential and clear the ladder below it."""
    monkeypatch.setenv(_V12_KEY_ENV, _V12_KEY)
    # ``CUSTOM_<SLUG>_API_KEY`` is the next rung of the record's own credential
    # ladder; leaving a stray one set would mask a record whose declared env var
    # was never read at all.
    monkeypatch.delenv("CUSTOM_OMNI_API_KEY", raising=False)
    monkeypatch.delenv("CUSTOM_MY_OMNI_API_KEY", raising=False)


# Each variant is ONE raw record that must resolve to the same (URL, key) pair
# through a different pair of field spellings. ``api``/``url`` are the Agent's
# ``_entry_url`` precedence and ``api_key_env`` its ``key_env`` synonym; a config
# hand-written or migrated either way names the same endpoint.
_V12_RECORD_VARIANTS = [
    ({"base_url": _V12_URL, "key_env": _V12_KEY_ENV}, "base_url+key_env"),
    ({"api": _V12_URL, "api_key_env": _V12_KEY_ENV}, "api+api_key_env"),
    ({"url": _V12_URL, "key_env": _V12_KEY_ENV}, "url+key_env"),
    ({"base_url": _V12_URL, "api_key": _V12_KEY}, "base_url+static_api_key"),
    # Display name AND config key disagree: the key alone still names the slug.
    (
        {"name": "My Omni", "base_url": _V12_URL, "key_env": _V12_KEY_ENV},
        "aliased-display-name",
    ),
]

_V12_VARIANT_IDS = [label for _record, label in _V12_RECORD_VARIANTS]


@pytest.mark.parametrize("record,label", _V12_RECORD_VARIANTS, ids=_V12_VARIANT_IDS)
def test_v12_raw_provider_record_owns_its_named_slug(monkeypatch, record, label):
    """``providers: {omni: ...}`` is the authority for ``custom:omni``.

    The bug: none of these resolved at all. ``custom:omni`` matched no
    ``custom_providers[]`` row and no ``providers['custom:omni']`` key, so the
    route was reported unowned and the send refused.
    """
    _v12_env(monkeypatch)
    _with_direct_config(monkeypatch, _v12_raw_cfg(record))

    api_key, base_url = config.resolve_custom_provider_connection("custom:omni")
    assert base_url == _V12_URL, label
    assert api_key == _V12_KEY, label

    bundle = config.resolve_custom_provider_bundle("custom:omni")
    assert bundle["status"] == config.CUSTOM_SELECTION_KEYED, label
    assert bundle["record"] is not None, label
    assert bundle["base_url"] == _V12_URL, label
    assert bundle["api_key"] == _V12_KEY, label
    # A record that resolved a credential is not keyless, so the route can never
    # be handed the placeholder that claims the endpoint needs no auth.
    assert bundle["keyless"] is False, label
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY, label


def test_v12_raw_record_is_reached_by_its_display_name_too(monkeypatch):
    """One record, keyed ``omni`` and named ``My Omni``, answers to BOTH identities.

    The Agent mints its alias set from the display name and the config key
    together, so ``custom:omni`` and ``custom:my-omni`` are the same provider to
    it. A WebUI that honoured only one of them would refuse half the sends the
    picker can emit for a single configured endpoint.
    """
    _v12_env(monkeypatch)
    record = {"name": "My Omni", "base_url": _V12_URL, "key_env": _V12_KEY_ENV}

    for slug in ("custom:omni", "custom:my-omni"):
        _with_direct_config(monkeypatch, _v12_raw_cfg(record, model_provider=slug))
        assert config.resolve_custom_provider_connection(slug) == (_V12_KEY, _V12_URL), slug

    # …and an identity the record does NOT name is still refused, so the alias
    # set widens the vocabulary without widening ownership.
    _with_direct_config(monkeypatch, _v12_raw_cfg(record, model_provider="custom:omni"))
    assert config.resolve_custom_provider_connection("custom:ghost") == (None, None)
    ghost = config.resolve_custom_provider_bundle("custom:ghost")
    assert ghost["status"] == config.CUSTOM_SELECTION_MISSING


# ── the non-active raw record: its endpoint, not the active provider's ───────


def test_v12_raw_record_supplies_its_own_endpoint_beside_its_own_key(monkeypatch):
    """A NON-ACTIVE raw record must not pair its key with the ambient endpoint.

    This is the #1806 split-authority failure mirrored onto the raw shape. The
    ambient runtime has resolved the ACTIVE provider, so it seeds a truthy
    ``base_url`` of its own. The merge then takes the credential from the raw
    record (the runtime is a different authority, so its key is not this
    provider's) — and, filling only what the runtime had left empty, kept the
    ACTIVE provider's URL. The send went to the active endpoint carrying the
    non-active provider's key: a guaranteed 401 at best, and the user's prompt
    delivered to a provider they did not pick at worst.

    The record declares the endpoint, so the record's endpoint wins.
    """
    _v12_env(monkeypatch)
    _with_direct_config(
        monkeypatch, _v12_raw_cfg({"base_url": _V12_URL, "key_env": _V12_KEY_ENV})
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME),
        lookup_provider="custom:omni",
    )

    assert bundle["base_url"] == _V12_URL
    assert bundle["api_key"] == _V12_KEY
    assert bundle["base_url"] != _AMBIENT_SIDE_FIELD_RUNTIME["base_url"], (
        "the record's key was paired with the ambient provider's endpoint"
    )
    assert bundle["api_key"] != _AMBIENT_SIDE_FIELD_RUNTIME["api_key"]
    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None
    # The runtime is a proven-foreign authority, so none of its side fields ride
    # along to the custom HTTP endpoint either.
    _assert_side_fields(bundle, _FOREIGN_AMBIENT_SIDE_FIELDS, "non-active raw record")


def test_v12_raw_record_keeps_the_runtimes_spelling_of_the_same_endpoint(monkeypatch):
    """Negative control: same authority, so the runtime's normalized URL stands.

    Without this, the test above is equally satisfied by "always overwrite the
    runtime's endpoint with the config spelling" — which would discard the
    normalized form the runtime actually resolved (trailing-``/v1``
    de-duplication and friends) for an endpoint that is provably the same one.
    """
    _v12_env(monkeypatch)
    _with_direct_config(
        monkeypatch, _v12_raw_cfg({"base_url": _V12_URL + "/", "key_env": _V12_KEY_ENV})
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        None,
        _V12_URL,
        {"provider": "custom:omni", "base_url": _V12_URL, "api_key": _V12_KEY},
        lookup_provider="custom:omni",
    )

    assert bundle["base_url"] == _V12_URL, "a same-authority runtime spelling was discarded"
    assert bundle["api_key"] == _V12_KEY
    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None


def test_matching_identity_metadata_with_conflicting_credential_rejects_tie(monkeypatch):
    """Even when identity metadata matches, conflicting credentials strictly reject a tie."""
    _v12_env(monkeypatch)
    _with_direct_config(
        monkeypatch, _v12_raw_cfg({"base_url": _V12_URL + "/", "key_env": _V12_KEY_ENV})
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        None,
        _V12_URL,
        {
            "provider": "custom:omni",
            "base_url": _V12_URL,
            "record_id": "omni",
            "api_key": "conflicting-foreign-key",
        },
        lookup_provider="custom:omni",
    )

    assert bundle["base_url"] == _V12_URL + "/"
    assert bundle["api_key"] == _V12_KEY


_CALLER_RESOLVED_URL = "https://caller-resolved-sentinel.example/v1"


def test_v12_raw_record_supplies_the_endpoint_nothing_is_positively_tied_to(monkeypatch):
    """With NO runtime dict there is no TIE, so the record's own endpoint wins.

    The endpoint the caller arrived with was resolved before this slug was ever
    looked up, so it says nothing about the record selected for it. Keeping it
    only because no runtime dict arrived to contradict it reads silence as
    provenance — and pairs the record's own credential with whatever endpoint
    happened to be in flight, which is the split-authority send this module
    exists to stop.

    So the rule is positive: an endpoint survives the merge only when the
    SELECTED record supplied it, or when a runtime dict is positively tied to
    that same record (:func:`_custom_runtime_endpoint_is_record_owned`, pinned by
    the same-authority control above). An absent runtime dict is neither, so the
    record's ``base_url`` displaces the caller's. The #2271 fill-only shape lives
    on the injected-``connection_resolver`` path, where there IS no record to
    take provenance from — see the control below.
    """
    _v12_env(monkeypatch)
    _with_direct_config(
        monkeypatch, _v12_raw_cfg({"base_url": _V12_URL, "key_env": _V12_KEY_ENV})
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        None,
        _CALLER_RESOLVED_URL,
        None,
        lookup_provider="custom:omni",
    )

    assert bundle["base_url"] == _V12_URL, (
        "the record's credential was left beside an endpoint it never declared"
    )
    assert bundle["base_url"] != _CALLER_RESOLVED_URL
    assert bundle["api_key"] == _V12_KEY
    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None


def test_injected_connection_resolver_still_keeps_the_callers_base_url(monkeypatch):
    """Negative control for #2271: no record, so the fill-only shape is unchanged.

    An injected ``connection_resolver`` reports a bare (key, URL) pair and IS the
    whole authority on that path — there is no config record whose endpoint
    provenance could displace anything. Applying the record rule here would
    silently re-point every such caller at the resolver's spelling, which is
    #2271's ``test_named_custom_provider_keeps_existing_runtime_base_url``.
    """
    _v12_env(monkeypatch)
    _with_direct_config(
        monkeypatch, _v12_raw_cfg({"base_url": _V12_URL, "key_env": _V12_KEY_ENV})
    )

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        None,
        _CALLER_RESOLVED_URL,
        None,
        lookup_provider="custom:omni",
        connection_resolver=lambda _pid, **_kw: (
            "resolver-key-sentinel",
            "https://resolver-config-sentinel.example/v1",
        ),
    )

    assert bundle["base_url"] == _CALLER_RESOLVED_URL, (
        "the resolver path lost #2271's fill-only base_url handling"
    )
    assert bundle["api_key"] == "resolver-key-sentinel"
    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None


# ── disabled rows are invisible to the Agent, so they own nothing here ───────


_V12_DISABLED_FLAGS = [False, "false", "no", "off", "0"]


@pytest.mark.parametrize("flag", _V12_DISABLED_FLAGS, ids=[str(f) for f in _V12_DISABLED_FLAGS])
def test_v12_disabled_raw_record_fails_closed_as_unowned(monkeypatch, flag):
    """A switched-off row must not quietly keep routing.

    ``is_provider_enabled()`` hides a falsey ``enabled:`` entry from the Agent's
    resolver, so honouring it here would make the WebUI send through an endpoint
    the user disabled — and send it through the AMBIENT connection the moment the
    row itself resolved nothing. Both halves are pinned: the verdict is
    ``unowned_custom_provider``, and the bundle keeps no endpoint, credential,
    placeholder or side field from anywhere.
    """
    _v12_env(monkeypatch)
    record = {"enabled": flag, "base_url": _V12_URL, "key_env": _V12_KEY_ENV}
    _with_direct_config(monkeypatch, _v12_raw_cfg(record))

    assert config.resolve_custom_provider_connection("custom:omni") == (None, None)
    selection = config.resolve_custom_provider_bundle("custom:omni")
    assert selection["status"] == config.CUSTOM_SELECTION_MISSING
    assert selection["record"] is None

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME),
        lookup_provider="custom:omni",
    )
    verdict = bundle[config.CUSTOM_ROUTE_ERROR_FIELD]
    assert verdict is not None, "a disabled row left the route looking routable"
    assert verdict["reason"] == config.CUSTOM_ROUTE_UNOWNED
    assert verdict["provider"] == "custom:omni"
    assert bundle["base_url"] is None
    assert bundle["api_key"] is None
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY
    assert bundle["provider"] == "custom:omni"
    _assert_side_fields(bundle, _FOREIGN_AMBIENT_SIDE_FIELDS, f"enabled: {flag!r}")
    # The disabled row's own endpoint and credential must not leak either.
    assert _V12_URL not in str(bundle)
    assert _V12_KEY not in str(bundle)


@pytest.mark.parametrize("flag", [True, "true", "yes", "on", "1"], ids=lambda f: str(f))
def test_v12_explicitly_enabled_raw_record_still_owns_its_slug(monkeypatch, flag):
    """Negative control: only the FALSEY words hide a row.

    A mirror that read ``enabled`` as "present means off", or that treated any
    string as truthy-by-presence, would take every explicitly-enabled v12
    provider offline — the same outage as the bug, from the opposite direction.
    """
    _v12_env(monkeypatch)
    record = {"enabled": flag, "base_url": _V12_URL, "key_env": _V12_KEY_ENV}
    _with_direct_config(monkeypatch, _v12_raw_cfg(record))

    assert config.resolve_custom_provider_connection("custom:omni") == (_V12_KEY, _V12_URL)


# ── two raw records claiming one identity split the authority ────────────────


_V12_AMBIGUOUS_CASES = [
    (
        # Two config KEYS whose alias sets overlap: ``Omni`` normalizes onto the
        # other record's key.
        {
            "omni": {"base_url": "https://omni-a.example/v1", "api_key": "omni-a-key"},
            "omni-two": {
                "name": "Omni",
                "base_url": "https://omni-b.example/v1",
                "api_key": "omni-b-key",
            },
        },
        "key-vs-display-name",
    ),
    (
        # Two display NAMES that normalize to the same slug: ``Omni`` and
        # ``omni`` are one identity to the Agent's alias minting.
        {
            "gate-a": {
                "name": "Omni",
                "base_url": "https://omni-a.example/v1",
                "key_env": _V12_KEY_ENV,
            },
            "gate-b": {
                "name": "omni",
                "base_url": "https://omni-b.example/v1",
                "api_key": "omni-b-key",
            },
        },
        "display-name-vs-display-name",
    ),
    (
        # The legacy ``custom:``-carrying spelling of a display name is the same
        # identity as the bare config key.
        {
            "omni": {"base_url": "https://omni-a.example/v1", "api_key": "omni-a-key"},
            "gate-b": {
                "name": "custom:omni",
                "base_url": "https://omni-b.example/v1",
                "api_key": "omni-b-key",
            },
        },
        "bare-key-vs-custom-prefixed-name",
    ),
]


@pytest.mark.parametrize(
    "providers_map,label",
    [(m, lbl) for m, lbl in _V12_AMBIGUOUS_CASES],
    ids=[lbl for _m, lbl in _V12_AMBIGUOUS_CASES],
)
def test_v12_ambiguous_raw_records_fail_closed(monkeypatch, providers_map, label):
    """Two raw records naming one slug is a refusal, not a first-match race.

    Config order would decide which endpoint and which credential the send gets,
    and nothing forces those two to come from the SAME record — the split
    authority ``_unique_custom_provider_entry`` already refuses for
    ``custom_providers[]``. Every entry point into the route must raise, because
    each of them is the first thing some consumer calls.
    """
    # The first case's ``omni`` key is ambiguous only because the OTHER record
    # exists, so build the config by hand rather than via ``_v12_raw_cfg``.
    cfg_dict = {
        "model": {"default": "antigravity/gemini-3.7-flash-tiered", "provider": "custom:omni"},
        "providers": copy.deepcopy(providers_map),
    }
    _v12_env(monkeypatch)
    _with_direct_config(monkeypatch, cfg_dict)

    import api.routes as routes

    entry_points = {
        "resolve_custom_provider_connection": lambda: config.resolve_custom_provider_connection(
            "custom:omni"
        ),
        "resolve_custom_provider_bundle": lambda: config.resolve_custom_provider_bundle(
            "custom:omni"
        ),
        "merge_custom_provider_runtime_bundle": lambda: (
            config.merge_custom_provider_runtime_bundle(
                "custom:omni",
                _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
                _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
                copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME),
                lookup_provider="custom:omni",
            )
        ),
        "_resolve_agent_connection_bundle": lambda: routes._resolve_agent_connection_bundle(
            "custom:omni",
            _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
            _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
            copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME),
        ),
    }

    for name, call in entry_points.items():
        with pytest.raises(config.AmbiguousCustomProviderError) as excinfo:
            call()
        # The refusal has to be actionable: it must name the colliding records
        # and subclass ValueError, which is what the existing handlers at the
        # consumer call sites already catch.
        assert isinstance(excinfo.value, ValueError), f"{label}/{name}"
        message = str(excinfo.value)
        for key in providers_map:
            assert key in message, f"{label}/{name}: {message}"

    # An identity only ONE of them names is unaffected: a collision on ``omni``
    # must not take an unrelated provider down with it.
    unique_cfg = {
        "model": cfg_dict["model"],
        "providers": {
            "solo": {"base_url": _V12_URL, "key_env": _V12_KEY_ENV},
            **copy.deepcopy(providers_map),
        },
    }
    _with_direct_config(monkeypatch, unique_cfg)
    assert config.resolve_custom_provider_connection("custom:solo") == (_V12_KEY, _V12_URL)


def test_v12_record_naming_the_slug_without_a_connection_is_not_a_collision(monkeypatch):
    """A record that declares nothing is not a competing authority.

    Only a record that could actually supply an endpoint or a credential can
    split the authority, so a same-slug row holding e.g. a models allowlist and
    nothing else must neither win the route nor block it. Refusing here would
    fail closed on configs that are not ambiguous at all.
    """
    _v12_env(monkeypatch)
    cfg_dict = {
        "model": {"default": "antigravity/gemini-3.7-flash-tiered", "provider": "custom:omni"},
        "providers": {
            "omni": {"base_url": _V12_URL, "key_env": _V12_KEY_ENV},
            "omni-notes": {"name": "Omni", "models": ["antigravity/gemini-3.7-flash-tiered"]},
        },
    }
    _with_direct_config(monkeypatch, cfg_dict)

    assert config.resolve_custom_provider_connection("custom:omni") == (_V12_KEY, _V12_URL)


# ── the three streaming regions that build an agent ──────────────────────────


def _assert_v12_bundle(init_kwargs, label, *, expected_key=_V12_KEY):
    """The constructor bundle is wholly owned by the raw v12 record."""
    assert init_kwargs["base_url"] == _V12_URL, label
    assert init_kwargs["api_key"] == expected_key, label
    # ``custom``, not ``custom:omni``: the named provider has supplied a concrete
    # endpoint, so Agent init must not synthesize ``CUSTOM:OMNI_API_KEY`` hints.
    assert init_kwargs["provider"] == "custom", label
    assert init_kwargs["base_url"] != _AMBIENT_SIDE_FIELD_RUNTIME["base_url"], label
    assert init_kwargs["api_key"] != _AMBIENT_SIDE_FIELD_RUNTIME["api_key"], label
    assert init_kwargs["api_key"] != config.KEYLESS_CUSTOM_API_KEY, label
    _assert_side_fields(init_kwargs, _FOREIGN_AMBIENT_SIDE_FIELDS, label)


def _assert_explicit_client_only(captured, label):
    """``_init_openai_client()`` took the explicit branch on EVERY construction.

    The weaker ``base_url is not None`` proxy is satisfied by a bundle that still
    sends the turn somewhere else: an incomplete pair makes Agent init call
    ``_routed_client_kwargs()`` and re-resolve a provider through the centralized
    router. Asserting the branch is what pins "this send reached the endpoint the
    user picked".
    """
    routed = captured.get("routed_client_kwargs_calls", [])
    assert not routed, f"{label}: _routed_client_kwargs() was reached: {routed}"
    explicit = captured.get("explicit_client_kwargs_calls", [])
    assert explicit, f"{label}: no client was configured at all"
    for call in explicit:
        assert call["base_url"] == _V12_URL, label
        assert call["api_key"] == _V12_KEY, label


@pytest.mark.parametrize("record,label", _V12_RECORD_VARIANTS, ids=_V12_VARIANT_IDS)
def test_v12_raw_record_initial_streaming_send_builds_the_agent(monkeypatch, record, label):
    """Initial send: the stock v12 config routes, and routes to its OWN endpoint.

    Before the fix this send did not merely mis-route — it never happened. The
    route was refused as ``unowned_custom_provider`` on a config the CLI sends on
    every day.
    """
    import api.streaming as streaming

    _v12_env(monkeypatch)
    session_id = f"session-1806-v12-initial-{label}"
    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        _v12_raw_cfg(record),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        session_id=session_id,
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
        apperrors = _drain_apperrors(q)
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    assert not apperrors, f"{label}: the send failed closed: {apperrors}"
    assert captured.get("run_calls"), f"{label}: the turn was never sent"
    _assert_v12_bundle(captured["init_kwargs"], f"{label}/initial send")
    _assert_explicit_client_only(captured, f"{label}/initial send")


@pytest.mark.parametrize(
    "fail_first,label",
    [("returned_error", "returned-error heal"), ("raised", "raised-exception heal")],
    ids=["returned-error", "raised-exception"],
)
def test_v12_raw_record_survives_both_credential_heal_paths(monkeypatch, fail_first, label):
    """Both 401 self-heal retries rebuild the SAME raw-record bundle.

    The retry paths re-resolve the runtime provider from scratch, so they are
    where a partial rebuild leaks the ambient authority back in: the heal hands
    back the ambient dict, and a retry that refreshed only provider/key/base_url
    would reconstruct the agent with the ambient pool, wire protocol and ACP
    transport beside the custom endpoint. Every construction in the history is
    checked, not just the last.
    """
    import api.streaming as streaming

    _v12_env(monkeypatch)
    session_id = f"session-1806-v12-{fail_first}"
    stream_id, q, captured, restore = _setup_production_composed_runtime(
        monkeypatch,
        _v12_raw_cfg({"base_url": _V12_URL, "key_env": _V12_KEY_ENV}),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        session_id=session_id,
        fail_first=fail_first,
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()

    history = captured["init_kwargs_history"]
    assert len(history) >= 2, f"{label}: the retry constructed no second agent"
    for index, init_kwargs in enumerate(history):
        _assert_v12_bundle(init_kwargs, f"{label} construction #{index}")
    _assert_explicit_client_only(captured, label)


def test_v12_disabled_record_refuses_the_streaming_send(monkeypatch):
    """A disabled v12 row stops the turn instead of routing it anywhere.

    The complement of the two tests above at the same boundary: no agent is
    constructed, so ``_routed_client_kwargs()`` is never reached, the agent cache
    is never poisoned, and the turn ends on a controlled ``provider_unroutable``
    apperror naming the provider and a fix.
    """
    _v12_env(monkeypatch)
    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        _v12_raw_cfg({"enabled": False, "base_url": _V12_URL, "key_env": _V12_KEY_ENV}),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        "session-1806-v12-disabled",
        model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
    )

    payload = apperrors[-1]
    assert "custom:omni" in payload["message"], payload
    assert "is not configured" in payload["message"], payload

    blob = str(payload) + str(captured)
    for leaked in (
        _V12_URL,
        _V12_KEY,
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        config.KEYLESS_CUSTOM_API_KEY,
    ):
        assert leaked not in blob, f"{leaked!r} leaked into the refused route"
    assert not captured.get("explicit_client_kwargs_calls")


# ── the shared non-streaming constructor boundary ────────────────────────────


@pytest.mark.parametrize("record,label", _V12_RECORD_VARIANTS, ids=_V12_VARIANT_IDS)
def test_v12_raw_record_resolves_at_the_non_streaming_chokepoint(monkeypatch, record, label):
    """``_resolve_agent_connection_bundle()`` returns the record's bundle, unraised.

    POST /api/chat and the four auxiliary consumers all build their agent from
    here, so a v12 config that only worked on the streaming path would still take
    compression, commit messages, handoffs and summaries down. The helper RAISES
    on a terminal verdict, so a clean return is itself half the assertion.
    """
    import api.routes as routes

    _v12_env(monkeypatch)
    _with_direct_config(monkeypatch, _v12_raw_cfg(record))

    # Mirror POST /api/chat's own sequence: resolve the model's provider, then
    # let the ambient runtime fill the endpoint it did not resolve.
    _model, provider, base_url = config.resolve_model_provider(
        "@custom:omni:antigravity/gemini-3.7-flash-tiered"
    )
    assert provider == "custom:omni", label
    runtime = copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME)
    bundle = routes._resolve_agent_connection_bundle(
        provider,
        runtime["api_key"],
        base_url or runtime["base_url"],
        runtime,
    )

    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None, label
    _assert_v12_bundle(bundle, f"{label}/non-streaming chokepoint")


def test_v12_disabled_record_raises_at_the_non_streaming_chokepoint(monkeypatch):
    """The same boundary refuses a disabled row, with the actionable reason."""
    import api.routes as routes

    _v12_env(monkeypatch)
    _with_direct_config(
        monkeypatch,
        _v12_raw_cfg({"enabled": "off", "base_url": _V12_URL, "key_env": _V12_KEY_ENV}),
    )

    with pytest.raises(config.CustomProviderRouteError) as excinfo:
        routes._resolve_agent_connection_bundle(
            "custom:omni",
            _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
            _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
            copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME),
        )

    err = excinfo.value
    assert isinstance(err, ValueError)
    assert err.reason == config.CUSTOM_ROUTE_UNOWNED
    assert err.provider == "custom:omni"
    assert "is not configured" in err.message, err.message
    assert err.hint, "the refusal named no fix"


# ── the agent cache signature is derived from the resolved bundle ────────────


def _v12_send_signature(monkeypatch, cfg_dict, runtime_dict, session_id):
    """Drive one composed send and return the signature its agent was cached under."""
    import api.streaming as streaming

    stream_id, q, _captured, restore = _setup_production_composed_runtime(
        monkeypatch, cfg_dict, runtime_dict, session_id=session_id
    )
    try:
        streaming.STREAMS[stream_id] = q
        streaming._run_agent_streaming(
            session_id=session_id,
            msg_text="hello",
            model="@custom:omni:antigravity/gemini-3.7-flash-tiered",
            workspace="/tmp",
            stream_id=stream_id,
        )
        with config.SESSION_AGENT_CACHE_LOCK:
            assert session_id in config.SESSION_AGENT_CACHE, "the send cached no agent"
            return config.SESSION_AGENT_CACHE[session_id][1]
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        restore()


def test_v12_cache_signature_is_bundle_derived_not_runtime_derived(monkeypatch):
    """Two sends whose RESOLVED bundle is identical hash identically.

    The raw record clears every ambient side field, so a signature still taken
    off the raw runtime provider would differ between these two sends even though
    the agents they build are the same — churning a new agent per send.
    """
    _v12_env(monkeypatch)
    record = {"base_url": _V12_URL, "key_env": _V12_KEY_ENV}

    sig_plain = _v12_send_signature(
        monkeypatch,
        _v12_raw_cfg(record),
        {"provider": "custom:omni", "base_url": "https://ambient.example/v1", "api_key": "amb"},
        "session-1806-v12-sig-plain",
    )
    sig_ambient = _v12_send_signature(
        monkeypatch,
        _v12_raw_cfg(record),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        "session-1806-v12-sig-ambient",
    )

    assert sig_plain == sig_ambient, (
        "the signature still varies with runtime fields the bundle cleared"
    )


_V12_SIGNATURE_MUTATIONS = [
    ({"base_url": _V12_URL + "-moved", "key_env": _V12_KEY_ENV}, "endpoint"),
    ({"base_url": _V12_URL, "api_key": _V12_KEY + "-rotated"}, "credential"),
    (
        {"base_url": _V12_URL, "key_env": _V12_KEY_ENV, "api_mode": "anthropic_messages"},
        "api_mode",
    ),
    (
        {
            "base_url": _V12_URL,
            "key_env": _V12_KEY_ENV,
            "command": "v12-acp-command-sentinel",
            "args": ["--v12-acp-arg"],
        },
        "acp_transport",
    ),
]


@pytest.mark.parametrize(
    "record,label",
    _V12_SIGNATURE_MUTATIONS,
    ids=[label for _r, label in _V12_SIGNATURE_MUTATIONS],
)
def test_v12_cache_signature_tracks_the_provider_configuration(monkeypatch, record, label):
    """Editing the raw record mints a NEW agent instead of reusing the cached one.

    The cache is keyed by session, so a signature blind to any part of the
    provider configuration would keep serving an agent built on the PREVIOUS
    endpoint, credential, wire protocol or ACP transport for the rest of the
    session — the user changes a setting and nothing happens.
    """
    _v12_env(monkeypatch)
    baseline = {"base_url": _V12_URL, "key_env": _V12_KEY_ENV}

    sig_before = _v12_send_signature(
        monkeypatch,
        _v12_raw_cfg(baseline),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        "session-1806-v12-sig-baseline",
    )
    sig_after = _v12_send_signature(
        monkeypatch,
        _v12_raw_cfg(record),
        dict(_AMBIENT_SIDE_FIELD_RUNTIME),
        "session-1806-v12-sig-mutated",
    )

    assert sig_before != sig_after, (
        f"the signature ignores the record's {label}, so a cached agent built on "
        f"the previous one would be reused for the rest of the session"
    )


# ── the WebUI and the installed Agent must select the SAME bundle ────────────
#
# ``hermes_cli`` is not importable here, so the oracle below is written from the
# Agent's documented algorithm rather than by calling it — and deliberately NOT
# by calling ``api.config``'s mirrors of it, which would make the comparison a
# tautology. Two independent implementations agreeing on the same config is the
# only evidence available that a v12 send lands in the same place whichever of
# the two resolves it. That agreement is the entire point of the fix: the bug was
# precisely the WebUI and the Agent disagreeing about a stock v12 config.


def _oracle_agent_aliases(display_name, provider_key):
    """``hermes_cli.providers.custom_provider_aliases()``, reimplemented."""
    aliases = set()
    for value in (display_name, provider_key):
        raw = str(value or "").strip().lower()
        if not raw:
            continue
        dashed = raw.replace(" ", "-")
        aliases.add(raw)
        aliases.add(dashed)
        aliases.add(dashed if dashed.startswith("custom:") else "custom:" + dashed)
        if dashed.startswith("custom:"):
            bare = dashed.split(":", 1)[1]
            if bare:
                aliases.add(bare)
                aliases.add("custom:" + dashed)
    aliases.discard("")
    return aliases


def _oracle_agent_enabled(record):
    """``hermes_cli.config_providers.is_provider_enabled()``, reimplemented."""
    flag = record.get("enabled", True)
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        return flag.strip().lower() not in {"false", "0", "no", "off"}
    return bool(flag)


def _oracle_agent_selection(cfg_dict, requested):
    """``_match_new_style_provider()``: first ENABLED raw record whose aliases match.

    Returns the complete connection bundle the Agent would construct with, or
    ``None`` when nothing in ``providers:`` names ``requested``.
    """
    wanted = str(requested or "").strip().lower()
    providers_cfg = cfg_dict.get("providers") or {}
    for provider_key, record in providers_cfg.items():
        if not isinstance(record, dict) or not record:
            continue
        if not _oracle_agent_enabled(record):
            continue
        if wanted not in _oracle_agent_aliases(record.get("name") or provider_key, provider_key):
            continue
        url = None
        for field in ("api", "url", "base_url"):  # _entry_url precedence
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                url = value.strip()
                break
        api_key = record.get("api_key")
        if not api_key:
            env_name = record.get("key_env") or record.get("api_key_env")
            if env_name:
                api_key = os.environ.get(str(env_name))
        return {
            # The Agent routes a resolved named endpoint through the generic
            # OpenAI-compatible custom client, exactly as the WebUI bundle does.
            "provider": "custom",
            "base_url": url,
            "api_key": api_key or None,
            "api_mode": record.get("api_mode") or record.get("transport") or None,
            "acp_command": record.get("acp_command") or record.get("command") or None,
            "acp_args": record.get("acp_args") or record.get("args") or None,
        }
    return None


_V12_ORACLE_RECORDS = [
    ({"base_url": _V12_URL, "key_env": _V12_KEY_ENV}, "base_url+key_env"),
    ({"api": _V12_URL, "api_key_env": _V12_KEY_ENV}, "api+api_key_env"),
    ({"url": _V12_URL, "api_key": _V12_KEY}, "url+static_api_key"),
    ({"name": "My Omni", "base_url": _V12_URL, "key_env": _V12_KEY_ENV}, "aliased-name"),
    (
        {
            "base_url": _V12_URL,
            "key_env": _V12_KEY_ENV,
            "api_mode": "anthropic_messages",
            "command": "v12-acp-command-sentinel",
            "args": ["--v12-acp-arg"],
        },
        "side-fields-owned",
    ),
    ({"enabled": False, "base_url": _V12_URL, "key_env": _V12_KEY_ENV}, "disabled"),
]


@pytest.mark.parametrize(
    "record,label",
    _V12_ORACLE_RECORDS,
    ids=[label for _r, label in _V12_ORACLE_RECORDS],
)
def test_webui_and_agent_select_the_same_complete_bundle(monkeypatch, record, label):
    """One config, two resolvers, one connection bundle — including the side fields.

    Not just the URL and the key: ``api_mode`` decides the wire protocol and
    ``acp_command``/``acp_args`` the transport, so a WebUI that agreed on the
    endpoint and disagreed on those would still talk to it differently than the
    CLI does. The disabled case is in the same table on purpose — agreeing that
    a row owns NOTHING is as much a part of the contract as agreeing where it
    points.
    """
    _v12_env(monkeypatch)
    cfg_dict = _v12_raw_cfg(record)
    _with_direct_config(monkeypatch, cfg_dict)

    expected = _oracle_agent_selection(cfg_dict, "custom:omni")

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        copy.deepcopy(_AMBIENT_SIDE_FIELD_RUNTIME),
        lookup_provider="custom:omni",
    )
    fields = ("provider", "base_url", "api_key", "api_mode", "acp_command", "acp_args")

    if expected is None:
        # The Agent routes this nowhere, so neither may the WebUI — and it must
        # say so terminally rather than hand back a holed bundle.
        verdict = bundle[config.CUSTOM_ROUTE_ERROR_FIELD]
        assert verdict is not None, label
        assert verdict["reason"] == config.CUSTOM_ROUTE_UNOWNED, label
        assert bundle["base_url"] is None and bundle["api_key"] is None, label
        return

    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None, label
    actual = {field: bundle[field] for field in fields}
    assert actual == expected, (
        f"{label}: the WebUI and the Agent would reach this provider differently"
    )
    # ``credential_pool`` has no counterpart in the raw record, so the ambient
    # provider's must not ride along with an endpoint it does not belong to.
    assert bundle["credential_pool"] is None, label


def test_the_agent_oracle_disagrees_with_the_prefixed_only_reader(monkeypatch):
    """Guard the oracle: it must actually resolve what the old reader could not.

    If ``_oracle_agent_selection`` quietly returned ``None`` for the raw shape,
    every comparison above would collapse into the unowned branch and prove
    nothing. Pin that it resolves the stock v12 record — and that it is reading
    the RAW key, not a ``custom:``-prefixed one, by checking it also refuses a
    record that names a different identity.
    """
    _v12_env(monkeypatch)
    cfg_dict = _v12_raw_cfg({"base_url": _V12_URL, "key_env": _V12_KEY_ENV})

    selection = _oracle_agent_selection(cfg_dict, "custom:omni")
    assert selection is not None, "the oracle cannot resolve the stock v12 shape"
    assert selection["base_url"] == _V12_URL
    assert selection["api_key"] == _V12_KEY

    assert _oracle_agent_selection(cfg_dict, "custom:ghost") is None


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer probe: a DISABLED exact-keyed record must not route either
#
# ``providers['custom:<slug>']`` is the one candidate that names the slug by its
# own exact config key, and it was the one rung of the ladder that never ran the
# ``enabled`` check. A switched-off keyed record therefore kept handing out a
# complete, routable bundle — its endpoint AND its secret — while every other
# shape of the same disable failed closed.
# ─────────────────────────────────────────────────────────────────────────────


_EXACT_KEYED_URL = "https://exact-keyed-disabled-sentinel.example/v1"
_EXACT_KEYED_KEY = "exact-keyed-disabled-key-sentinel"


def _exact_keyed_disabled_cfg(flag):
    """``providers['custom:omni']`` switched off, beside a live ambient provider.

    The generic ``custom`` record and the ``model:`` block are the less-specific
    candidates the disabled key must NOT fall through to: honouring the disable
    by routing the turn through the ACTIVE provider's endpoint and credential is
    the same leak from the other direction.
    """
    return {
        "model": {"default": "active/model", "provider": "custom:active"},
        "providers": {
            "custom:omni": {
                "enabled": flag,
                "base_url": _EXACT_KEYED_URL,
                "api_key": _EXACT_KEYED_KEY,
            },
            "custom": {
                "base_url": _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
                "api_key": _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
            },
        },
    }


@pytest.mark.parametrize("flag", _V12_DISABLED_FLAGS, ids=[str(f) for f in _V12_DISABLED_FLAGS])
def test_exact_keyed_disabled_provider_fails_closed(monkeypatch, flag):
    """A disabled ``providers['custom:<slug>']`` owns nothing, at every boundary.

    Pinned across all three surfaces a send can enter through — the connection
    view, the streaming bundle merge and the non-streaming chokepoint — because
    the selection bug sat below all of them and a fix at one would leave the
    other two routing a row the user switched off.
    """
    import api.routes as routes

    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _exact_keyed_disabled_cfg(flag))
    label = f"enabled: {flag!r}"

    # 1. Selection and the connection view: nothing owns the slug.
    assert config.resolve_custom_provider_connection("custom:omni") == (None, None), label
    selection = config.resolve_custom_provider_bundle("custom:omni")
    assert selection["status"] == config.CUSTOM_SELECTION_MISSING, label
    assert selection["record"] is None, label
    assert selection["base_url"] is None and selection["api_key"] is None, label

    # 2. The streaming bundle merge: terminal, and stripped of every authority.
    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        _ambient_runtime(provider="custom:active"),
        lookup_provider="custom:omni",
    )
    verdict = bundle[config.CUSTOM_ROUTE_ERROR_FIELD]
    assert verdict is not None, f"{label}: a disabled keyed record still looked routable"
    assert verdict["reason"] == config.CUSTOM_ROUTE_UNOWNED, label
    assert verdict["provider"] == "custom:omni", label
    assert bundle["base_url"] is None and bundle["api_key"] is None, label
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY, label
    _assert_side_fields(bundle, _FOREIGN_AMBIENT_SIDE_FIELDS, label)

    # Neither the disabled record's own pair nor the ambient one it must not
    # inherit may survive anywhere in the bundle.
    blob = str(bundle)
    for leaked in (
        _EXACT_KEYED_URL,
        _EXACT_KEYED_KEY,
        _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
        _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
    ):
        assert leaked not in blob, f"{label}: {leaked!r} leaked onto the disabled route"

    # 3. The non-streaming chokepoint raises rather than returning a holed bundle.
    with pytest.raises(config.CustomProviderRouteError) as excinfo:
        routes._resolve_agent_connection_bundle(
            "custom:omni",
            _AMBIENT_SIDE_FIELD_RUNTIME["api_key"],
            _AMBIENT_SIDE_FIELD_RUNTIME["base_url"],
            _ambient_runtime(provider="custom:active"),
        )
    err = excinfo.value
    assert err.reason == config.CUSTOM_ROUTE_UNOWNED, label
    assert err.provider == "custom:omni", label
    assert err.hint, f"{label}: the refusal named no fix"


def test_exact_keyed_enabled_provider_still_owns_its_slug(monkeypatch):
    """Negative control: only a FALSEY ``enabled`` hides the keyed record.

    A guard that read ``enabled`` as "present means off" would take every
    explicitly-enabled keyed provider offline — the same outage as the leak, from
    the opposite direction.
    """
    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _exact_keyed_disabled_cfg(True))

    assert config.resolve_custom_provider_connection("custom:omni") == (
        _EXACT_KEYED_KEY,
        _EXACT_KEYED_URL,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Reviewer probe: a shared endpoint is not shared credential authority
#
# ``_custom_bundle_endpoint_matches()`` identified the runtime as same-authority
# from the URL alone, so a record that merely declared the SAME base_url as the
# active provider was handed the active provider's key. Two providers sharing a
# host — a gateway fronting two accounts, a local proxy — is an ordinary setup,
# and there the record's URL rode out beside somebody else's secret.
# ─────────────────────────────────────────────────────────────────────────────


_SHARED_HOST_URL = "https://shared.example/v1"
_SHARED_HOST_AMBIENT_KEY = "ambient-secret-sentinel"
_SHARED_HOST_RECORD_KEY = "record-secret-sentinel"


def _shared_host_cfg(record):
    """Raw provider ``omni`` on the shared host, while ``active-other`` is live."""
    return {
        "model": {"default": "active/model", "provider": "active-other"},
        "providers": {"omni": copy.deepcopy(record)},
    }


def _shared_host_runtime():
    """The ambient runtime: a DIFFERENT provider that reached the same host."""
    return {
        "provider": "active-other",
        "base_url": _SHARED_HOST_URL,
        "api_key": _SHARED_HOST_AMBIENT_KEY,
    }


def test_shared_endpoint_different_provider_does_not_split_credential(monkeypatch):
    """The record's OWN credential rides with the record's endpoint.

    Identical URLs plus different keys is exactly the case URL-equality cannot
    tell apart: the ambient runtime names itself ``active-other``, so it is
    provably a different authority no matter which host it reached.
    """
    _clear_credential_env(monkeypatch)
    record = {"base_url": _SHARED_HOST_URL, "api_key": _SHARED_HOST_RECORD_KEY}
    _with_direct_config(monkeypatch, _shared_host_cfg(record))

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _SHARED_HOST_AMBIENT_KEY,
        _SHARED_HOST_URL,
        _shared_host_runtime(),
        lookup_provider="custom:omni",
    )

    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None
    assert bundle["base_url"] == _SHARED_HOST_URL
    assert bundle["api_key"] == _SHARED_HOST_RECORD_KEY, (
        "the non-active record inherited the ambient provider's secret across a "
        "shared endpoint"
    )
    assert bundle["api_key"] != _SHARED_HOST_AMBIENT_KEY
    # The ambient provider is a foreign authority here, so its side fields go too.
    assert bundle["credential_pool"] is None


def test_shared_endpoint_unresolved_key_fails_closed_not_borrow_ambient(monkeypatch):
    """A declared-but-unresolved credential fails closed; it does not borrow.

    The dangerous shape: the record declares ``key_env`` and the variable is
    unset, so its own ladder yields nothing. Filling that hole from the ambient
    runtime just because both name one host is precisely the split-authority
    send — the active provider's key against the record's route.
    """
    _clear_credential_env(monkeypatch)
    monkeypatch.delenv(_MISSING_ENV_VAR, raising=False)
    record = {"base_url": _SHARED_HOST_URL, "key_env": _MISSING_ENV_VAR}
    _with_direct_config(monkeypatch, _shared_host_cfg(record))

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _SHARED_HOST_AMBIENT_KEY,
        _SHARED_HOST_URL,
        _shared_host_runtime(),
        lookup_provider="custom:omni",
    )

    verdict = bundle[config.CUSTOM_ROUTE_ERROR_FIELD]
    assert verdict is not None, "an unresolvable credential still looked routable"
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_CREDENTIAL
    assert verdict["provider"] == "custom:omni"
    assert bundle["api_key"] is None
    assert bundle["api_key"] != _SHARED_HOST_AMBIENT_KEY
    # Not the keyless placeholder either: the record DOES declare a credential
    # source, so an unauthenticated send would be a silent 401, not a keyless one.
    assert bundle["api_key"] != config.KEYLESS_CUSTOM_API_KEY
    assert _SHARED_HOST_AMBIENT_KEY not in str(bundle)


def test_same_provider_sharing_its_endpoint_still_keeps_the_runtime_fields(monkeypatch):
    """Negative control: the identity test must not orphan the provider's OWN runtime.

    The ambient runtime dict normally names the very slug being resolved (that is
    the active-custom-provider shape the WebUI writes). A provenance test that
    rejected it would clear the side fields of every live custom provider.
    """
    _clear_credential_env(monkeypatch)
    record = {"base_url": _SHARED_HOST_URL, "api_key": _SHARED_HOST_RECORD_KEY}
    _with_direct_config(monkeypatch, _shared_host_cfg(record))

    runtime = _shared_host_runtime()
    runtime["provider"] = "custom:omni"
    runtime["api_mode"] = "chat_completions"
    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _SHARED_HOST_AMBIENT_KEY,
        _SHARED_HOST_URL,
        runtime,
        lookup_provider="custom:omni",
    )

    assert bundle[config.CUSTOM_ROUTE_ERROR_FIELD] is None
    assert bundle["base_url"] == _SHARED_HOST_URL
    # Still the record's own credential — same authority does not make the
    # ambient key authoritative over one the record declares for itself.
    assert bundle["api_key"] == _SHARED_HOST_RECORD_KEY
    assert bundle["api_mode"] == "chat_completions", (
        "the provider's own runtime side field was cleared as foreign"
    )


# ─────────────────────────────────────────────────────────────────────────────
# A credential-only record borrows NO endpoint (PR #7319)
#
# The shape that started this: ``providers: {omni: {api_key: ...}}`` — a raw v12
# record that declares a CREDENTIAL and no ``base_url`` — while the process is
# already talking to a different, ACTIVE provider whose URL and key are sitting
# in the runtime dict. Selection legitimately picks the omni record (it owns the
# slug, and a declared-but-endpoint-less record is not "missing"), and the merge
# then had no positive statement about where that record's endpoint came from.
# The seeded ``base_url`` was simply still there, so the bundle handed the
# constructor the ACTIVE provider's URL beside the omni record's secret: a
# non-active provider's credential, and the user's prompt, delivered to an
# endpoint neither the record nor the user ever named.
#
# ``endpoint_owned`` is the positive half of that provenance, and it is False
# here. Absence of contradiction is not a tie: the caller's ``resolved_base_url``
# and a missing runtime dict are both silence. So the route is terminal on its
# own name — :data:`config.CUSTOM_ROUTE_NO_ENDPOINT` — and keeps neither the
# ambient endpoint nor a credential to pair with it, because pairing them is the
# whole defect.
#
# These assert the property at the boundaries that can actually send: the
# production-composed streaming send (all three agent-constructing regions), the
# non-streaming chokepoint, and the auxiliary client — which bypasses AIAgent
# entirely, so ``main_runtime`` would be the only place the stolen pair reached
# the wire. The last two tests are the controls that keep the refusal narrow: a
# COMPLETE record still sends, and a genuinely keyless one still gets the
# placeholder.
# ─────────────────────────────────────────────────────────────────────────────


# The non-active record's own secret: the thing that must never leave with the
# active provider's URL.
_CREDENTIAL_ONLY_KEY = "credential-only-omni-key-sentinel"
# The endpoint the record declares in the two control cases below.
_OMNI_OWN_URL = "https://omni-own-endpoint-sentinel.example/v1"

# The ACTIVE provider: truthy URL and key, plus truthy side fields, all resolved
# for somebody else entirely. Named ``custom:active-other`` so identity settles
# the provenance question without relying on the URLs differing.
_ACTIVE_OTHER_URL = "https://active-other-sentinel.example/v1"
_ACTIVE_OTHER_KEY = "active-other-key-sentinel"
_ACTIVE_OTHER_POOL = ["active-other-pool-sentinel"]
_ACTIVE_OTHER_ACP = "active-other-acp-sentinel"

_ACTIVE_OTHER_RUNTIME = {
    "provider": "custom:active-other",
    "base_url": _ACTIVE_OTHER_URL,
    "api_key": _ACTIVE_OTHER_KEY,
    "api_mode": "chat_completions",
    "command": _ACTIVE_OTHER_ACP,
    "args": ["--active-other-arg-sentinel"],
    "credential_pool": _ACTIVE_OTHER_POOL,
}

# Everything the refused route could have walked away with. Asserted as a set
# over the whole captured harness, so a leak through ANY field — bundle, error
# payload, constructor kwargs, aux handoff — fails rather than only the fields a
# test remembered to name.
_ACTIVE_OTHER_SENTINELS = (
    _ACTIVE_OTHER_URL,
    _ACTIVE_OTHER_KEY,
    _ACTIVE_OTHER_POOL[0],
    _ACTIVE_OTHER_ACP,
)

_CREDENTIAL_ONLY_MODEL = "@custom:omni:antigravity/gemini-3.7-flash-tiered"


def _credential_only_cfg():
    """The raw v12 record that declares a credential and NO endpoint."""
    return _v12_raw_cfg({"api_key": _CREDENTIAL_ONLY_KEY})


def _assert_no_borrowed_pair(blob, label):
    """Neither the record's own secret nor the active provider's connection."""
    for leaked in _ACTIVE_OTHER_SENTINELS:
        assert leaked not in blob, (
            f"{label}: the ACTIVE provider's {leaked!r} survived a route whose "
            "record declares no endpoint"
        )
    assert _CREDENTIAL_ONLY_KEY not in blob, (
        f"{label}: the non-active record's credential was handed on without an "
        "endpoint of its own to send it to"
    )
    assert config.KEYLESS_CUSTOM_API_KEY not in blob, (
        f"{label}: a record that DECLARES a credential was called keyless"
    )


def test_credential_only_record_refuses_the_active_providers_endpoint(monkeypatch):
    """The merge verdict itself: terminal, and holding neither half of the pair."""
    _clear_credential_env(monkeypatch)
    _with_direct_config(monkeypatch, _credential_only_cfg())

    selection = config.resolve_custom_provider_bundle("custom:omni")
    assert selection["record"] is not None, (
        "a credential-only record must still OWN its slug — treating it as "
        "missing is a different bug with the same symptom"
    )
    assert selection["endpoint_owned"] is False
    assert selection["api_key"] == _CREDENTIAL_ONLY_KEY
    assert selection["keyless"] is False

    bundle = config.merge_custom_provider_runtime_bundle(
        "custom:omni",
        _ACTIVE_OTHER_KEY,
        _ACTIVE_OTHER_URL,
        copy.deepcopy(_ACTIVE_OTHER_RUNTIME),
        lookup_provider="custom:omni",
    )

    verdict = bundle[config.CUSTOM_ROUTE_ERROR_FIELD]
    assert verdict is not None, "an endpoint-less record still looked routable"
    assert verdict["reason"] == config.CUSTOM_ROUTE_NO_ENDPOINT
    assert verdict["provider"] == "custom:omni"
    assert verdict["hint"], "the refusal named no setting to fix"
    assert bundle["base_url"] is None
    assert bundle["api_key"] is None
    # Still the NAMED slug: rewriting it to generic ``custom`` would present an
    # unresolvable route as a resolved one.
    assert bundle["provider"] == "custom:omni"
    # The refused endpoint's side fields go with it; this record declares none.
    _assert_side_fields(bundle, _FOREIGN_AMBIENT_SIDE_FIELDS, "credential-only record")
    _assert_no_borrowed_pair(str(bundle), "merged bundle")


def test_credential_only_record_refuses_the_production_composed_send(monkeypatch):
    """Refuse on initial send before any constructor, client, or cache write.

    A raw record declaring only a credential and no endpoint must fail closed
    at initial resolution as ``CUSTOM_ROUTE_NO_ENDPOINT``
    (custom_provider_endpoint_unresolved), constructing no agent, making no
    explicit client call, sending no turn, and writing no cache entry.
    """
    _clear_credential_env(monkeypatch)

    captured, apperrors = _run_composed_send_expecting_refusal(
        monkeypatch,
        _credential_only_cfg(),
        copy.deepcopy(_ACTIVE_OTHER_RUNTIME),
        "session-1806-credential-only-omni",
        model=_CREDENTIAL_ONLY_MODEL,
    )

    payload = apperrors[-1]
    # The STRUCTURED verdict, not the prose: a generic failure whose message
    # happens to read "resolved no endpoint" would pass a message-only check,
    # and the message is free to be reworded. This is the same terminal reason
    # the merge-level and auxiliary-client regressions assert.
    # The literal is pinned alongside the constant because it is the wire value
    # the client branches on; renaming it silently would be a breaking change.
    assert payload["reason"] == config.CUSTOM_ROUTE_NO_ENDPOINT == (
        "custom_provider_endpoint_unresolved"
    ), f"the failure did not carry {config.CUSTOM_ROUTE_NO_ENDPOINT}: {payload}"
    assert "custom:omni" in payload["message"], payload
    assert "resolved no endpoint" in payload["message"], (
        f"the failure did not name {config.CUSTOM_ROUTE_NO_ENDPOINT}: {payload}"
    )
    assert "base_url" in payload["hint"], payload

    assert not captured.get("init_kwargs_history"), (
        "an agent was constructed for a record that declares no endpoint"
    )
    assert not captured.get("explicit_client_kwargs_calls"), (
        "a client was configured with the active provider's borrowed connection"
    )

    _assert_no_borrowed_pair(str(payload) + str(captured), "composed send")


@pytest.mark.parametrize("fail_first", ["returned_error", "raised"])
def test_credential_only_record_refuses_the_production_composed_retry(
    monkeypatch, fail_first
):
    """A standard-v12 record losing only its endpoint stops a 401 self-heal.

    The initial send is routable and cached with the complete raw record. During
    the self-heal, only that record's ``base_url`` is removed; its credential and
    the foreign ambient runtime bundle remain available. The refreshed terminal
    route must therefore prevent every retry construction and cache write.
    """
    _clear_credential_env(monkeypatch)
    session_id = f"session-1806-credential-only-omni-{fail_first}"
    label = f"credential-only record / {fail_first}"
    cfg_dict = _v12_raw_cfg({"base_url": _V12_URL, "api_key": _V12_KEY})

    def remove_only_the_endpoint():
        config.cfg["providers"]["omni"]["base_url"] = ""

    captured, apperrors, cache_at_heal, cache_after = (
        _run_composed_retry_expecting_abandoned_heal(
            monkeypatch,
            cfg_dict,
            copy.deepcopy(_ACTIVE_OTHER_RUNTIME),
            session_id,
            fail_first=fail_first,
            heal_mutate=remove_only_the_endpoint,
        )
    )

    _assert_retry_abandoned(
        captured,
        apperrors,
        cache_at_heal,
        cache_after,
        session_id,
        label,
        expected_cause="resolved no endpoint",
        expected_initial_url=_V12_URL,
        expected_initial_key=_V12_KEY,
    )

    payload = apperrors[-1]
    assert payload["reason"] == config.CUSTOM_ROUTE_NO_ENDPOINT, payload
    assert "base_url" in payload["hint"], payload
    assert _V12_KEY not in str(payload), f"{label}: leaked the record's own key into payload"
    blob = str(captured) + str(apperrors)
    for leaked in _ACTIVE_OTHER_SENTINELS:
        assert leaked not in blob, f"{label}: {leaked!r} leaked into the retry path"
    assert config.KEYLESS_CUSTOM_API_KEY not in blob


# The two consumers whose auxiliary client can answer outright. There AIAgent is
# never built, so ``main_runtime`` is the ONLY carrier of the resolved authority
# — and the only place a borrowed pair could still reach the wire after every
# constructor assertion above passes.
_CREDENTIAL_ONLY_AUX_DRIVERS = [
    (
        "git commit message",
        lambda routes, session: routes._llm_git_commit_message("sys", "user", session=session),
    ),
    (
        "update summary",
        lambda routes, _session: routes._llm_update_summary("sys", "user", active_profile=None),
    ),
]


@pytest.mark.parametrize(
    "label,invoke",
    _CREDENTIAL_ONLY_AUX_DRIVERS,
    ids=[d[0] for d in _CREDENTIAL_ONLY_AUX_DRIVERS],
)
def test_credential_only_record_reaches_no_auxiliary_client(monkeypatch, label, invoke):
    """The refusal is terminal BEFORE ``main_runtime`` is built, so the aux client
    is never handed the active provider's connection.

    ``_resolve_agent_connection_bundle`` raises at the chokepoint, which is
    upstream of ``_auxiliary_main_runtime``. Returning the holed bundle instead
    would let the aux path send with the borrowed pair while bypassing every
    AIAgent-side guard entirely.
    """
    import api.routes as routes

    _clear_credential_env(monkeypatch)
    cfg_dict = _credential_only_cfg()
    cfg_dict["model"] = {"default": _CREDENTIAL_ONLY_MODEL, "provider": "custom:omni"}

    captured, fake_session = _setup_route_consumer_runtime(
        monkeypatch,
        cfg_dict=cfg_dict,
        runtime_dict=copy.deepcopy(_ACTIVE_OTHER_RUNTIME),
    )
    recorded_main_runtime = {}
    _decline_auxiliary_client(monkeypatch, recorded_main_runtime)

    with pytest.raises(config.CustomProviderRouteError) as excinfo:
        invoke(routes, fake_session)

    assert excinfo.value.reason == config.CUSTOM_ROUTE_NO_ENDPOINT, label
    assert excinfo.value.provider == "custom:omni", label
    assert excinfo.value.hint, f"{label}: the refusal named no setting to fix"

    assert not recorded_main_runtime, (
        f"{label}: the auxiliary client was handed {recorded_main_runtime!r} for a "
        "route with no endpoint of its own"
    )
    assert "init_kwargs" not in captured, f"{label}: an agent was constructed anyway"
    _assert_no_borrowed_pair(
        str(excinfo.value) + str(recorded_main_runtime) + str(captured), label
    )


# ── controls: the refusal is about MISSING provenance, not about raw records ──


def test_complete_raw_record_still_sends_through_its_own_endpoint(monkeypatch):
    """Control: the same record WITH a ``base_url`` routes exactly as before.

    Without this, "refuse when ``endpoint_owned`` is False" is equally satisfied
    by refusing every raw ``providers:<key>`` record — which would break the
    standard v12 shape outright.
    """
    _clear_credential_env(monkeypatch)

    init_kwargs = _run_composed_send(
        monkeypatch,
        _v12_raw_cfg({"base_url": _OMNI_OWN_URL, "api_key": _CREDENTIAL_ONLY_KEY}),
        copy.deepcopy(_ACTIVE_OTHER_RUNTIME),
        "session-1806-credential-only-control-complete",
    )

    assert init_kwargs["base_url"] == _OMNI_OWN_URL
    assert init_kwargs["api_key"] == _CREDENTIAL_ONLY_KEY
    assert init_kwargs["provider"] == "custom"
    # The record owns an endpoint the active provider did not resolve, so the
    # active provider's side fields are provably foreign and go.
    _assert_side_fields(init_kwargs, _FOREIGN_AMBIENT_SIDE_FIELDS, "complete raw record")
    for leaked in _ACTIVE_OTHER_SENTINELS:
        assert leaked not in str(init_kwargs), (
            f"the active provider's {leaked!r} reached a record-owned route"
        )


def test_genuinely_keyless_raw_record_still_sends_with_the_placeholder(monkeypatch):
    """Control: a record declaring an endpoint and NO credential is still keyless.

    The mirror of the case under test — endpoint but no credential, rather than
    credential but no endpoint. This one is a complete statement about the route
    ("that endpoint is unauthenticated"), so it must still route with
    :data:`config.KEYLESS_CUSTOM_API_KEY` and must NOT borrow the active
    provider's key on the way.
    """
    _clear_credential_env(monkeypatch)

    init_kwargs = _run_composed_send(
        monkeypatch,
        _v12_raw_cfg({"base_url": _OMNI_OWN_URL}),
        copy.deepcopy(_ACTIVE_OTHER_RUNTIME),
        "session-1806-credential-only-control-keyless",
    )

    assert init_kwargs["base_url"] == _OMNI_OWN_URL
    assert init_kwargs["api_key"] == config.KEYLESS_CUSTOM_API_KEY
    assert init_kwargs["api_key"] != _ACTIVE_OTHER_KEY
    assert init_kwargs["provider"] == "custom"
