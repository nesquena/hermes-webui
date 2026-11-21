"""#7905 round 4 — the six identity rows through the REAL entry paths.

Round 3 pinned the rule at the resolver (`resolve_model_provider`,
`canonical_model_provider_lane`) and at the connection-bundle helper.  The
maintainer's re-gate of 2026-11-17T05:04:25Z accepted the production repair and
the `GET /api/session` tests, and asked for the remaining gap: those two
docstrings claim sync/stream entry behavior but neither test drives the entry
paths themselves.  This file drives them:

* sync   — ``api/routes.py`` ``_handle_chat_sync`` (POST /api/chat), the
  non-streaming send, whose refusal is the controlled 400 built from
  ``CustomProviderRouteError``;
* stream — ``api/streaming.py`` ``_run_agent_streaming``, the worker the
  streaming start dispatches, whose refusal is the ``apperror`` event it puts on
  the stream queue.

Both arms assert **zero Agent launch on refusal**: the capturing double records
its constructor kwargs, so a refused turn leaves that record empty.  The two
launching rows are the positive controls — without them, "refuses the unusable
identity" and "refuses everything" would look identical.
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

# id -> (extra providers entries, launches?, expected (model, provider, base_url))
ROWS = [
    (
        "1-none-peels-to-generic-7904",
        {},
        True,
        ("omni:latest", "custom", GENERIC_URL),
    ),
    (
        "2-omni-with-connection",
        {"omni": {"base_url": OMNI_URL, "api_key": "fixture-omni"}},
        True,
        ("latest", "custom", OMNI_URL),
    ),
    (
        "3-exact-key-disabled",
        {"custom:omni": {"enabled": False, "base_url": OMNI_URL}},
        False,
        None,
    ),
    (
        "4-omni-models-only",
        {"omni": {"models": ["latest"]}},
        False,
        None,
    ),
    (
        "5-omni-disabled",
        {"omni": {"enabled": False, "base_url": OMNI_URL}},
        False,
        None,
    ),
    (
        "6-proxy-name-only",
        {"proxy": {"name": "omni"}},
        False,
        None,
    ),
]

ROW_IDS = [row[0] for row in ROWS]
REFUSAL_ROWS = [row for row in ROWS if not row[2]]
LAUNCH_ROWS = [row for row in ROWS if row[2]]


def _cfg(extra):
    providers = {"custom": {"base_url": GENERIC_URL, "api_key": "fixture-generic"}}
    providers.update(copy.deepcopy(extra))
    return {
        "model": {"provider": "deepseek", "default": "deepseek-chat"},
        "providers": providers,
    }


def _install_cfg(monkeypatch, extra):
    """Install the row's config as both the ambient object and the loader result."""
    cfg = _cfg(extra)
    monkeypatch.setattr(config, "cfg", cfg, raising=False)
    monkeypatch.setattr(config, "get_config", lambda *a, **k: cfg)
    with config.SESSION_AGENT_CACHE_LOCK:
        config.SESSION_AGENT_CACHE.clear()
    return cfg


def _runtime_dict(extra):
    """What ``resolve_runtime_provider`` reports for the row's own record."""
    if "omni" in extra and extra["omni"].get("base_url") and extra["omni"].get("api_key"):
        return {"provider": "custom:omni", "base_url": OMNI_URL, "api_key": "fixture-omni"}
    return {"provider": "custom", "base_url": GENERIC_URL, "api_key": "fixture-generic"}


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
    """Raised from the double's constructor once its kwargs are captured.

    The sync handler lets an exception escape; the streaming worker wraps the
    construction in a broad ``except`` (it logs a stream error), so that arm
    records the kwargs and returns normally instead.
    """

    def __init__(self, init_kwargs):
        super().__init__("agent launched")
        self.init_kwargs = init_kwargs


def _make_capturing_agent(record, *, raise_on_launch):
    """Build an AIAgent double that records the constructor bundle.

    Every constructor-routing field is a NAMED parameter: both entry paths gate
    each optional kwarg on ``inspect.signature(AIAgent.__init__)``, so a double
    that swallowed them into ``**kwargs`` would filter out exactly the fields
    pinned here and pass vacuously.
    """

    class _CapturingAgent:
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


# ── sync entry path: POST /api/chat ─────────────────────────────────────────


def _drive_sync(monkeypatch, extra):
    """Run the real ``_handle_chat_sync`` and report how the turn ended."""
    _install_cfg(monkeypatch, extra)
    _stub_runtime_provider(monkeypatch, _runtime_dict(extra))
    session = _FakeSession("session-7905r4-sync")
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


# ── streaming entry path: the dispatched worker ─────────────────────────────


def _drive_stream(monkeypatch, extra, *, session_id):
    """Run the real ``_run_agent_streaming`` worker and drain its queue."""
    cfg = _install_cfg(monkeypatch, extra)
    _stub_runtime_provider(monkeypatch, _runtime_dict(extra))
    fake_state = types.ModuleType("hermes_state")
    fake_state.SessionDB = mock.Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "hermes_state", fake_state)

    session = _FakeSession(session_id)
    stream_id = f"stream-{session_id}"
    # /api/chat/start stamps ``active_stream_id`` before dispatching the worker;
    # the stale-writeback guard only lets the owning worker persist and emit, so
    # a terminal refusal would otherwise reach the queue as nothing at all.
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


# ── sync assertions ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "row_id,extra,_launches,expected", [row for row in LAUNCH_ROWS], ids=[row[0] for row in LAUNCH_ROWS]
)
def test_sync_chat_launches_the_generic_and_named_controls(row_id, extra, _launches, expected, monkeypatch):
    """Positive controls: the #7904 generic peel and the named endpoint both send."""
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
def test_sync_chat_refuses_a_disabled_or_incomplete_identity(
    row_id, extra, _launches, _expected, monkeypatch
):
    """The named-but-unusable rows answer 400 and never construct an Agent."""
    result = _drive_sync(monkeypatch, extra)

    assert not result["launched"], f"{row_id}: the refusal let a send through"
    payload = result["seen"].get("payload") or {}
    assert result["seen"].get("status") == 400, f"{row_id}: expected the controlled 400, got {result}"
    assert payload.get("type") == "custom_provider_unroutable"
    assert payload.get("reason") == "unowned_custom_provider"
    assert payload.get("error")


# ── streaming assertions ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "row_id,extra,_launches,expected", [row for row in LAUNCH_ROWS], ids=[row[0] for row in LAUNCH_ROWS]
)
def test_streaming_start_launches_the_generic_and_named_controls(row_id, extra, _launches, expected, monkeypatch):
    """Same two controls through the dispatched streaming worker."""
    result = _drive_stream(monkeypatch, extra, session_id=f"7905r4-{row_id}")

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
def test_streaming_start_refuses_a_disabled_or_incomplete_identity(
    row_id, extra, _launches, _expected, monkeypatch
):
    """The refusal is one terminal apperror, and the Agent is never constructed."""
    result = _drive_stream(monkeypatch, extra, session_id=f"7905r4-{row_id}")

    assert not result["launched"], f"{row_id}: the refusal let a streaming turn through"
    apperrors = [payload for kind, payload in result["events"] if kind == "apperror"]
    assert len(apperrors) == 1, f"{row_id}: expected one terminal apperror, got {result['events']}"
    assert apperrors[0].get("type") == "provider_unroutable"
    assert apperrors[0].get("reason") == "unowned_custom_provider"
