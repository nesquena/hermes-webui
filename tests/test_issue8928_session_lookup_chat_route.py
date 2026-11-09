"""#8928: GET /api/session's context-length lookup asks the host the session chats with, with the chat's key.

Since .597 (#8448) the lookup could ask a different endpoint from the chat. In the "mixed listing" class, the native
record ``providers["custom:<name>"]`` lists the model and declares a key but no URL, the legacy ``custom_providers``
entry declares URL L with its key, and the model block points elsewhere (A or B). The chat routes to L with the legacy
entry's key; the lookup asked the model block's URL and, after #8917, sent no key there. The window then came from the
wrong endpoint or the default.

The lookup now takes both halves from WebUI's own half of the chat's routing chain
(``merge_custom_provider_runtime_bundle`` + ``raise_for_custom_provider_route``) for a named custom provider. That
chain runs with ``lookup_only``: GET is a read path, so it never selects or refreshes a credential pool entry, never
builds a ``key_cmd`` token provider, never sends a host-gated env key, and never calls the Agent's
``resolve_runtime_provider``. The key is the route's or none (a session that can't chat, or a keyless endpoint's
placeholder, looks up without one); config selection can't add one. Other providers keep the previous path.
"""
import socket
import sys
import types
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest

import api.config as config
from api import routes

A = "https://a.model-url.test/v1"
B = "https://b.record-url.test/v1"
L = "https://l.legacy-url.test/v1"
PROVIDER = "custom:my-proxy"


def _mixed_listing_cfg(record_key, legacy_key, model_url, model_key):
    """#8928's class: the record lists the model and holds a key but no URL; the legacy entry holds L and its key."""
    model = {"default": "m-x", "provider": PROVIDER, "base_url": model_url}
    if model_key:
        model["api_key"] = model_key
    return {
        "model": model,
        "providers": {PROVIDER: {"api_key": record_key, "models": {"m-x": {}}}},
        "custom_providers": [{"name": "my-proxy", "base_url": L, "api_key": legacy_key}],
    }


# The 24 shapes where v0.52.544's lookup sent the chat key to the chat host and .597 through .657 asked the model
# block's URL instead (issue #8928's measurement).
REGRESSED = [
    (rk, lk, mu, mk)
    for rk in ("K1", "K2")
    for lk in ("K1", "K2")
    for mu in (A, B)
    for mk in (None, "K1", "K2")
]
assert len(REGRESSED) == 24


def _stub_session(model="m-x", provider=PROVIDER):
    s = MagicMock()
    s.context_length = 0
    s.threshold_tokens = 0
    s.read_only = False
    s._loaded_metadata_only = False
    s.profile = None
    s.last_prompt_tokens = 0
    s.input_tokens = 0
    s.output_tokens = 0
    s.model = model
    s.model_provider = provider
    s.title = "t8928"
    s.session_id = "t8928"
    s.messages = []
    s.tool_calls = []
    s.active_stream_id = None
    s.pending_user_message = None
    s.pending_attachments = []
    s.pending_started_at = None
    s.compact.return_value = {
        "session_id": "t8928",
        "title": "t8928",
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "context_length": 0,
        "threshold_tokens": 0,
        "last_prompt_tokens": 0,
    }
    return s


def _get_session(cfg, monkeypatch, *, provider=PROVIDER):
    """Run GET /api/session (resolve_model=1, no persisted window) and return every context-length lookup call."""
    cfg.setdefault("providers", {})
    cfg.setdefault("custom_providers", [])
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    calls = []

    def fake_get_model_context_length(model, base_url="", api_key="", **kwargs):
        calls.append({"model": model, "base_url": base_url, "api_key": api_key, **kwargs})
        return 123_456

    fake_metadata = types.ModuleType("agent.model_metadata")
    fake_metadata.get_model_context_length = fake_get_model_context_length
    captured = {}
    with patch("api.routes.get_session", return_value=_stub_session(provider=provider)), \
         patch("api.routes.j", side_effect=lambda h, data, status=200: captured.update(data=data, status=status)), \
         patch.dict(sys.modules, {"agent.model_metadata": fake_metadata}):
        routes.handle_get(MagicMock(), urlparse("/api/session?session_id=t8928&messages=0&resolve_model=1"))
    assert captured.get("status", 200) == 200, captured
    return calls


# ── #8928's 24 regressed shapes ─────────────────────────────────────────────────


@pytest.mark.parametrize("record_key,legacy_key,model_url,model_key", REGRESSED)
def test_get_session_lookup_asks_the_chat_host_with_the_chat_key(monkeypatch, record_key, legacy_key, model_url,
                                                                 model_key):
    calls = _get_session(_mixed_listing_cfg(record_key, legacy_key, model_url, model_key), monkeypatch)
    assert len(calls) == 1, calls
    assert (calls[0]["base_url"], calls[0]["api_key"]) == (L, legacy_key)


def test_the_regressed_shapes_are_where_the_chat_route_goes(monkeypatch):
    """The expectation above is the chat's own route, not a fixture choice: check one shape against it."""
    cfg = _mixed_listing_cfg("K1", "K2", A, "K1")
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    route = config.merge_custom_provider_runtime_bundle(
        PROVIDER, None, A, {}, lookup_provider=PROVIDER,
        connection_resolver=config.resolve_custom_provider_connection,
    )
    config.raise_for_custom_provider_route(route)
    assert (route["base_url"], route["api_key"]) == (L, "K2")


# ── The key is the route's or none ─────────────────────────────────────────────


def test_a_session_that_cannot_chat_looks_up_without_a_key(monkeypatch):
    # The record declares a key and no URL, nothing else declares an endpoint for it: the chat refuses the route.
    calls = _get_session({"model": {"default": "m-x", "provider": PROVIDER, "base_url": A, "api_key": "K1"},
                          "providers": {PROVIDER: {"api_key": "K1", "models": {"m-x": {}}}}}, monkeypatch)
    assert len(calls) == 1, calls
    assert calls[0]["api_key"] == ""


def test_a_session_that_cannot_chat_sends_no_legacy_entry_key(monkeypatch):
    # The legacy entry holds K1 but no URL, the model block declares L with K1: the chat refuses the route (the
    # entry has no endpoint of its own). The lookup's legacy-entry scan must not pick K1 up for L.
    calls = _get_session({"model": {"default": "m-x", "provider": PROVIDER, "base_url": L, "api_key": "K1"},
                          "providers": {PROVIDER: {"models": {"m-x": {}}}},
                          "custom_providers": [{"name": "my-proxy", "api_key": "K1", "models": {"m-x": {}}}]},
                         monkeypatch)
    assert len(calls) == 1, calls
    assert calls[0]["api_key"] == ""


def test_a_refused_route_never_lends_its_key(monkeypatch):
    """Whatever a refused route still carries, the lookup treats it as a session that can't chat."""
    refused = {"provider": PROVIDER, "base_url": None, "api_key": "KREFUSED",
               config.CUSTOM_ROUTE_ERROR_FIELD: config._custom_route_verdict(config.CUSTOM_ROUTE_NO_ENDPOINT, PROVIDER)}
    with pytest.raises(config.CustomProviderRouteError):
        config.raise_for_custom_provider_route(refused)
    monkeypatch.setattr(config, "merge_custom_provider_runtime_bundle", lambda *a, **k: dict(refused))
    calls = _get_session(_mixed_listing_cfg("K1", "K1", A, "K1"), monkeypatch)
    assert len(calls) == 1, calls
    assert calls[0]["api_key"] == ""


def test_a_refused_route_is_not_a_warning_on_get(monkeypatch, caplog):
    """The chat logs a refused route as a warning before failing the turn; a GET that builds nothing logs at debug."""
    import logging

    caplog.set_level(logging.DEBUG, logger="api.config")
    calls = _get_session({"model": {"default": "m-x", "provider": PROVIDER, "base_url": A, "api_key": "K1"},
                          "providers": {PROVIDER: {"api_key": "K1", "models": {"m-x": {}}}}}, monkeypatch)
    assert len(calls) == 1, calls
    refused = [r for r in caplog.records if r.name == "api.config" and "custom provider" in r.getMessage()]
    assert refused, "the refused route should still be logged (at debug)"
    assert all(r.levelno < logging.WARNING for r in refused), [(r.levelname, r.getMessage()) for r in refused]


def test_a_keyless_endpoint_looks_up_without_the_placeholder(monkeypatch):
    calls = _get_session({"model": {"default": "m-x", "provider": PROVIDER},
                          "custom_providers": [{"name": "my-proxy", "base_url": L, "models": {"m-x": {}}}]},
                         monkeypatch)
    assert len(calls) == 1, calls
    assert (calls[0]["base_url"], calls[0]["api_key"]) == (L, "")
    assert calls[0]["api_key"] != config.KEYLESS_CUSTOM_API_KEY


def test_only_named_custom_providers_take_the_chat_route():
    assert routes._session_context_lookup_uses_chat_route(PROVIDER)
    assert routes._session_context_lookup_uses_chat_route(" custom:other ")
    assert not routes._session_context_lookup_uses_chat_route("custom")
    assert not routes._session_context_lookup_uses_chat_route("openrouter")
    assert not routes._session_context_lookup_uses_chat_route("")
    assert not routes._session_context_lookup_uses_chat_route(None)


def test_other_providers_keep_the_config_selected_key(monkeypatch):
    """A built-in provider keeps the previous path: its record's key still reaches its record's URL."""
    url = "https://openrouter.ai/api/v1"
    calls = _get_session({"model": {"default": "m-x", "provider": "openrouter", "base_url": url},
                          "providers": {"openrouter": {"api_key": "KOR", "base_url": url}}},
                         monkeypatch, provider="openrouter")
    assert len(calls) == 1, calls
    assert calls[0]["api_key"] == "KOR"


# ── GET is a read path: no pool, no token command, no Agent resolver, no network ─


class _SideEffects:
    """Recorders for every credential-minting or network step GET must not take."""

    POOL_KEY = "POOL-KEY-8928"
    ENV_KEY = "ENV-KEY-8928"

    def __init__(self, monkeypatch):
        self.calls = []
        rp = self._module(monkeypatch, "hermes_cli.runtime_provider", parent="hermes_cli")
        monkeypatch.setattr(rp, "resolve_runtime_provider", self._rec("resolve_runtime_provider"), raising=False)
        monkeypatch.setattr(rp, "_try_resolve_from_custom_pool",
                            self._rec("_try_resolve_from_custom_pool",
                                      {"api_key": self.POOL_KEY, "credential_pool": object()}), raising=False)
        monkeypatch.setattr(rp, "_host_gated_env_key_candidates",
                            lambda base_url, ollama=False: [self.ENV_KEY], raising=False)
        cts = self._module(monkeypatch, "agent.command_token_source", parent="agent")
        monkeypatch.setattr(cts, "build_command_token_provider",
                            self._rec("build_command_token_provider", lambda: "CMD-KEY-8928"), raising=False)
        try:
            from agent.credential_pool import CredentialPool
        except Exception:
            CredentialPool = None
        if CredentialPool is not None:
            monkeypatch.setattr(CredentialPool, "select", self._rec("CredentialPool.select"))
        for name in ("connect", "connect_ex"):
            monkeypatch.setattr(socket.socket, name, self._rec(f"socket.{name}", raises=True))
        monkeypatch.setattr(socket, "create_connection", self._rec("socket.create_connection", raises=True))
        monkeypatch.setattr(socket, "getaddrinfo", self._rec("socket.getaddrinfo", raises=True))

    @staticmethod
    def _module(monkeypatch, name, *, parent):
        try:
            __import__(name)
            return sys.modules[name]
        except Exception:
            pass
        if parent not in sys.modules:
            try:
                __import__(parent)
            except Exception:
                pkg = types.ModuleType(parent)
                pkg.__path__ = []
                monkeypatch.setitem(sys.modules, parent, pkg)
        mod = types.ModuleType(name)
        monkeypatch.setitem(sys.modules, name, mod)
        monkeypatch.setattr(sys.modules[parent], name.rsplit(".", 1)[1], mod, raising=False)
        return mod

    def _rec(self, label, result=None, raises=False):
        def recorder(*args, **kwargs):
            self.calls.append(label)
            if raises:
                raise OSError(f"{label} is not allowed on GET /api/session")
            return result
        return recorder


def _pooled_cmd_env_cfg():
    """One record that the chat would serve from a pool, a key_cmd, or a host-gated env key, plus a static key."""
    return {
        "model": {"default": "m-x", "provider": PROVIDER},
        "providers": {PROVIDER: {"base_url": L, "api_key": "KSTATIC", "key_cmd": "printf token",
                                 "models": {"m-x": {}}}},
    }


def test_get_session_takes_no_credential_minting_or_network_step(monkeypatch):
    fx = _SideEffects(monkeypatch)
    calls = _get_session(_pooled_cmd_env_cfg(), monkeypatch)
    assert fx.calls == [], fx.calls
    assert len(calls) == 1, calls
    assert (calls[0]["base_url"], calls[0]["api_key"]) == (L, "KSTATIC")


def test_get_session_never_sends_a_pool_command_or_ambient_env_key(monkeypatch):
    """With no static key the chat would use the pool, the command or the env; the lookup sends no key instead."""
    fx = _SideEffects(monkeypatch)
    cfg = _pooled_cmd_env_cfg()
    del cfg["providers"][PROVIDER]["api_key"]
    calls = _get_session(cfg, monkeypatch)
    assert fx.calls == [], fx.calls
    assert len(calls) == 1, calls
    assert calls[0]["base_url"] == L
    assert calls[0]["api_key"] == ""


def test_the_chat_route_itself_still_uses_the_pool_the_env_key_and_the_token_command(monkeypatch):
    """The chat's default path keeps its full credential ladder (pool, then static, host-gated env, key_cmd)."""
    fx = _SideEffects(monkeypatch)
    cfg = _pooled_cmd_env_cfg()
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    chat = config.resolve_custom_provider_bundle(PROVIDER)
    assert "_try_resolve_from_custom_pool" in fx.calls
    assert chat["api_key"] == _SideEffects.POOL_KEY
    fx.calls.clear()
    del cfg["providers"][PROVIDER]["api_key"]
    cfg["providers"][PROVIDER].pop("key_cmd")
    monkeypatch.setattr(sys.modules["hermes_cli.runtime_provider"], "_try_resolve_from_custom_pool",
                        lambda *a, **k: None, raising=False)
    assert config.resolve_custom_provider_bundle(PROVIDER)["api_key"] == _SideEffects.ENV_KEY
    cfg["providers"][PROVIDER]["key_cmd"] = "printf token"
    assert callable(config.resolve_custom_provider_bundle(PROVIDER)["api_key"])
    assert "build_command_token_provider" in fx.calls


def test_lookup_only_skips_exactly_the_pool_the_env_key_and_the_token_command(monkeypatch):
    fx = _SideEffects(monkeypatch)
    cfg = _pooled_cmd_env_cfg()
    monkeypatch.setattr(config, "cfg", cfg)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    chat = config.resolve_custom_provider_bundle(PROVIDER)
    view = config.resolve_custom_provider_bundle(PROVIDER, lookup_only=True)
    assert fx.calls == ["_try_resolve_from_custom_pool"], fx.calls  # the chat's call only
    assert view["api_key"] == "KSTATIC"
    for field in ("provider_id", "slug", "source", "status", "is_exact", "base_url", "endpoint_owned"):
        assert view[field] == chat[field], field
    del cfg["providers"][PROVIDER]["api_key"]
    fx.calls.clear()
    view = config.resolve_custom_provider_bundle(PROVIDER, lookup_only=True)
    assert fx.calls == [], fx.calls
    assert view["api_key"] is None
    # A declared credential source that didn't resolve here is still declared: no keyless placeholder.
    assert view["keyless"] is False
