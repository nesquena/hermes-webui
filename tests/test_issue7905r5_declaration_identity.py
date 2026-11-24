"""#7905 round 5 — declaration identity, independent of enabled/connection usability.

The gate certificate of 2026-11-18T10:28:34Z reproduced one residual regression on
top of the accepted generic colon-tag repair: ``_configured_custom_provider_identity``
recognised a record's mapping KEY and its ``name``, then leaned on connection
SELECTION for every other declaration field — and selection excludes records that
are disabled or declare no connection.  A ``providers:`` record that names the
identity through ``provider_key`` / ``provider`` / ``custom_provider`` was
therefore invisible, ``@custom:omni:latest`` on a generic ``custom`` session was
peeled to ``custom`` + model ``omni:latest``, and the request went to the generic
endpoint instead of keeping the named lane's controlled refusal.

This file pins the declaration rule in two layers:

* the identity helper — every identity field ``_CUSTOM_RECORD_IDENTITY_FIELDS``
  already treats as ownership counts as a DECLARATION here too, whether the record
  is enabled, disabled, connected or bare;
* the two real entry paths — sync ``/api/chat`` (``_handle_chat_sync``) and the
  dispatched streaming worker (``_run_agent_streaming``) must refuse those rows
  with ZERO Agent constructions, beside the generic/named positive controls and
  the disabled ``name``-field control the previous round already passes.

The controls are what separate "declares an identity it cannot use" from
"declares nothing": without them a helper that returned True for every slug would
pass every refusal here.
"""
from __future__ import annotations

import contextlib
import copy
import queue
import sys
import types
from unittest import mock

import pytest

import api.config as config
import api.oauth
import api.routes as routes
import api.streaming as streaming

GENERIC_URL = "https://generic.example/v1"
OMNI_URL = "https://omni.example/v1"

# ── layer 1: the identity helper ────────────────────────────────────────────
#
# id -> (providers records, extra model block, expected _configured_custom_provider_identity)
DECLARATION_ROWS = [
    (
        "provider_key-field-disabled",
        {"proxy": {"provider_key": "omni", "enabled": False, "base_url": OMNI_URL}},
        None,
        True,
    ),
    (
        "provider_key-field-bare",
        {"proxy": {"provider_key": "omni"}},
        None,
        True,
    ),
    (
        "custom_provider-field-disabled",
        {"proxy": {"custom_provider": "omni", "enabled": False}},
        None,
        True,
    ),
    (
        "provider-field-colon-disabled",
        {"proxy": {"provider": "custom:omni", "enabled": False}},
        None,
        True,
    ),
    (
        "model-block-names-the-identity",
        {},
        {"provider": "custom:omni", "default": "latest"},
        True,
    ),
    (
        "model-block-custom-provider-field",
        {},
        {"provider": "custom", "default": "latest", "custom_provider": "omni"},
        True,
    ),
    (
        "model-block-provider-key-field",
        {},
        {"provider": "custom", "default": "latest", "provider_key": "omni"},
        True,
    ),
    # controls
    (
        "control-name-field-disabled",
        {"proxy": {"name": "omni", "enabled": False}},
        None,
        True,
    ),
    (
        "control-keyed-record-enabled",
        {"omni": {"base_url": OMNI_URL, "api_key": "k"}},
        None,
        True,
    ),
    (
        "control-no-identity-peels-to-generic",
        {},
        None,
        False,
    ),
    (
        "control-identity-of-another-slug",
        {"proxy": {"provider_key": "acme", "enabled": False}},
        None,
        False,
    ),
    (
        "control-model-block-name-is-the-model-not-the-provider",
        {},
        {"provider": "custom", "default": "latest", "name": "omni"},
        False,
    ),
]

DECLARATION_IDS = [row[0] for row in DECLARATION_ROWS]


def _declaration_cfg(records, model_block=None):
    providers = {"custom": {"base_url": GENERIC_URL, "api_key": "k"}}
    providers.update(copy.deepcopy(records))
    cfg = {
        "model": {"provider": "deepseek", "default": "deepseek-chat"},
        "providers": providers,
    }
    if model_block is not None:
        cfg["model"] = copy.deepcopy(model_block)
    return cfg


@pytest.mark.parametrize(
    "row_id,records,model_block,expected", DECLARATION_ROWS, ids=DECLARATION_IDS
)
def test_declaration_identity_ignores_enabled_and_connection(row_id, records, model_block, expected):
    """A record that NAMES the identity declares it, usable or not."""
    cfg = _declaration_cfg(records, model_block)

    assert config._configured_custom_provider_identity("omni", cfg) is expected, row_id


# ── layer 2: the real entry paths ───────────────────────────────────────────
#
# id -> (extra providers records, launches?, expected (model, provider, base_url))
ROWS = [
    (
        "1-none-peels-to-generic-7904",
        {},
        True,
        ("omni:latest", "custom", GENERIC_URL),
    ),
    (
        "2-omni-with-connection",
        {"omni": {"base_url": OMNI_URL, "api_key": "k"}},
        True,
        ("latest", "custom", OMNI_URL),
    ),
    (
        "3-name-field-disabled",
        {"proxy": {"name": "omni", "enabled": False}},
        False,
        None,
    ),
    (
        "4-provider_key-field-disabled",
        {"proxy": {"provider_key": "omni", "enabled": False, "base_url": OMNI_URL}},
        False,
        None,
    ),
    (
        "5-provider_key-field-bare",
        {"proxy": {"provider_key": "omni"}},
        False,
        None,
    ),
    (
        "6-custom_provider-field-disabled",
        {"proxy": {"custom_provider": "omni", "enabled": False}},
        False,
        None,
    ),
    (
        "7-provider-field-colon-disabled",
        {"proxy": {"provider": "custom:omni", "enabled": False}},
        False,
        None,
    ),
]

ROW_IDS = [row[0] for row in ROWS]
REFUSAL_ROWS = [row for row in ROWS if not row[2]]
LAUNCH_ROWS = [row for row in ROWS if row[2]]


def _cfg(extra):
    providers = {"custom": {"base_url": GENERIC_URL, "api_key": "k"}}
    providers.update(copy.deepcopy(extra))
    return {
        "model": {"provider": "deepseek", "default": "deepseek-chat"},
        "providers": providers,
    }


def _install_cfg(monkeypatch, extra):
    cfg = _cfg(extra)
    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()
    return cfg


def _runtime_dict(extra):
    for record in extra.values():
        if isinstance(record, dict) and record.get("base_url") and record.get("api_key"):
            return {"provider": "custom:omni", "base_url": OMNI_URL, "api_key": "k"}
    return {"provider": "custom", "base_url": GENERIC_URL, "api_key": "k"}


def _stub_runtime_provider(monkeypatch, runtime_dict):
    fake_runtime_module = types.ModuleType("hermes_cli.runtime_provider")
    fake_runtime_module.resolve_runtime_provider = mock.Mock(return_value=dict(runtime_dict))
    fake_hermes_cli = types.ModuleType("hermes_cli")
    fake_hermes_cli.__path__ = []
    fake_hermes_cli.runtime_provider = fake_runtime_module
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake_runtime_module)
    monkeypatch.setattr(
        api.oauth,
        "resolve_runtime_provider_with_anthropic_env_lock",
        lambda resolver, **kwargs: resolver(**kwargs),
    )


class _AgentLaunched(Exception):
    def __init__(self, init_kwargs):
        super().__init__("agent launched")
        self.init_kwargs = init_kwargs


def _make_capturing_agent(record, *, raise_on_launch):
    class _CapturingAgent:
        def __init__(
            self,
            model=None,
            provider=None,
            base_url=None,
            api_key="k",
            api_mode=None,
            acp_command=None,
            acp_args=None,
            credential_pool="k",
            **kwargs,
        ):
            record.update(
                {
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
            )
            if raise_on_launch:
                raise _AgentLaunched(dict(record))
            self.session_id = kwargs.get("session_id")
            self.context_compressor = None
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            return {
                "messages": [
                    {"role": "user", "content": kwargs.get("persist_user_message", "")},
                    {"role": "assistant", "content": "ok"},
                ]
            }

        def interrupt(self, _message):
            return None

    return _CapturingAgent


class _FakeSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.title = "Test"
        self.workspace = "/tmp"
        self.model = "@custom:omni:latest"
        self.model_provider = "custom"
        self.messages = []
        self.context_messages = []
        self.active_stream_id = None
        self.pending_user_message = None
        self.pending_attachments = []
        self.pending_started_at = None
        self.pending_user_source = None
        self.profile = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.estimated_cost = None
        self.tool_calls = []
        self.personality = None

    def save(self, *_args, **_kwargs):
        return None

    def compact(self):
        return {"session_id": self.session_id, "messages": self.messages}


def _drive_sync(monkeypatch, extra):
    _install_cfg(monkeypatch, extra)
    _stub_runtime_provider(monkeypatch, _runtime_dict(extra))
    session = _FakeSession("session-7905r5-sync")
    launched = {}
    _sync_agent = _make_capturing_agent(launched, raise_on_launch=True)

    monkeypatch.setattr(routes, "require_ai_agent_class", lambda: _sync_agent)
    monkeypatch.setattr(routes, "ensure_agent_runtime_current", lambda *_a, **_k: None)
    monkeypatch.setattr(routes, "_resolve_cli_toolsets", lambda *_a, **_k: [])
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda *_a, **_k: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_agent_runtime_barrier_response", lambda **_k: None)
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda ws: "/tmp")
    monkeypatch.setattr(routes, "_get_session_agent_lock", lambda _sid: contextlib.nullcontext())
    monkeypatch.setattr(routes, "_read_profile_model_config", lambda *_a, **_k: (None, None, None))
    monkeypatch.setattr(
        routes,
        "_resolve_compatible_session_model_state",
        lambda model, provider, **_k: (model, provider),
    )

    seen = {}

    def _j(_handler, payload, **kw):
        seen["payload"] = payload
        seen["status"] = kw.get("status")
        return {"payload": payload, "status": kw.get("status")}

    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", lambda _h, message, *_a, **_k: {"error": message})

    try:
        response = routes._handle_chat_sync(
            object(), {"session_id": session.session_id, "message": "hi"}
        )
    except _AgentLaunched:
        response = None
    return {"response": response, "seen": seen, "launched": launched}


def _drive_stream(monkeypatch, extra, *, session_id):
    cfg = _install_cfg(monkeypatch, extra)
    _stub_runtime_provider(monkeypatch, _runtime_dict(extra))
    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "hermes_state", fake_state)

    session = _FakeSession(session_id)
    stream_id = f"stream-{session_id}"
    session.active_stream_id = stream_id
    stream_queue = queue.Queue()
    launched = {}
    _stream_agent = _make_capturing_agent(launched, raise_on_launch=False)

    monkeypatch.setattr(streaming, "get_session", lambda _sid: session)
    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: _stream_agent)
    monkeypatch.setattr("api.config.get_config", lambda: cfg)
    monkeypatch.setattr("api.config._resolve_cli_toolsets", lambda *_a, **_k: [])

    events = []
    streaming.STREAMS[stream_id] = stream_queue
    try:
        try:
            streaming._run_agent_streaming(
                session_id=session_id,
                msg_text="hello",
                model="@custom:omni:latest",
                workspace="/tmp",
                stream_id=stream_id,
                model_provider="custom",
            )
        except _AgentLaunched as exc:
            launched.update(exc.init_kwargs)
        while True:
            try:
                events.append(stream_queue.get_nowait())
            except queue.Empty:
                break
    finally:
        streaming.STREAMS.pop(stream_id, None)
        streaming.AGENT_INSTANCES.pop(stream_id, None)
        with config.SESSION_AGENT_CACHE_LOCK:
            config.SESSION_AGENT_CACHE.clear()
    return {"events": events, "launched": launched}


@pytest.mark.parametrize(
    "row_id,extra,_launches,expected", LAUNCH_ROWS, ids=[row[0] for row in LAUNCH_ROWS]
)
def test_sync_chat_launches_the_generic_and_named_controls(row_id, extra, _launches, expected, monkeypatch):
    result = _drive_sync(monkeypatch, extra)

    assert result["launched"], f"{row_id}: the turn never reached the Agent constructor"
    assert (
        result["launched"]["model"],
        result["launched"]["provider"],
        result["launched"]["base_url"],
    ) == expected


@pytest.mark.parametrize(
    "row_id,extra,_launches,_expected", REFUSAL_ROWS, ids=[row[0] for row in REFUSAL_ROWS]
)
def test_sync_chat_refuses_a_declared_but_unusable_identity(
    row_id, extra, _launches, _expected, monkeypatch
):
    result = _drive_sync(monkeypatch, extra)

    assert not result["launched"], f"{row_id}: the refusal let a send through"
    payload = result["seen"].get("payload") or {}
    assert result["seen"].get("status") == 400, f"{row_id}: expected the controlled 400, got {result}"
    assert payload.get("type") == "custom_provider_unroutable"
    assert payload.get("reason") == "unowned_custom_provider"
    assert payload.get("error")


@pytest.mark.parametrize(
    "row_id,extra,_launches,expected", LAUNCH_ROWS, ids=[row[0] for row in LAUNCH_ROWS]
)
def test_streaming_start_launches_the_generic_and_named_controls(
    row_id, extra, _launches, expected, monkeypatch
):
    result = _drive_stream(monkeypatch, extra, session_id=f"7905r5-{row_id}")

    assert result["launched"], f"{row_id}: the worker never reached the Agent constructor"
    assert (
        result["launched"]["model"],
        result["launched"]["provider"],
        result["launched"]["base_url"],
    ) == expected
    kinds = [event[0] for event in result["events"]]
    assert "apperror" not in kinds, f"{row_id}: a launchable row answered with an error event"


@pytest.mark.parametrize(
    "row_id,extra,_launches,_expected", REFUSAL_ROWS, ids=[row[0] for row in REFUSAL_ROWS]
)
def test_streaming_start_refuses_a_declared_but_unusable_identity(
    row_id, extra, _launches, _expected, monkeypatch
):
    result = _drive_stream(monkeypatch, extra, session_id=f"7905r5-{row_id}")

    assert not result["launched"], f"{row_id}: the refusal let a streaming turn through"
    apperrors = [payload for kind, payload in result["events"] if kind == "apperror"]
    assert len(apperrors) == 1, f"{row_id}: expected one terminal apperror, got {result['events']}"
    assert apperrors[0].get("type") == "provider_unroutable"
    assert apperrors[0].get("reason") == "unowned_custom_provider"
