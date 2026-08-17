"""Behavioral regressions for effective reasoning/runtime attribution (#6644)."""

from __future__ import annotations

import json
from collections import OrderedDict

import pytest

from api import gateway_chat, models, streaming
from api.config import STREAMS, create_stream_channel


class _JsonResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class _SseResponse:
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _terminal_runtime():
    return {
        "model": "answered-model-b",
        "provider": "provider-c",
        "reasoning": {"enabled": False},
    }


def _runtime_terminal_lines(*, runs_api: bool):
    runtime = _terminal_runtime()
    if runs_api:
        payloads = [
            {"event": "message.delta", "delta": "answered"},
            {
                "event": "run.completed",
                "output": "answered",
                "runtime": runtime,
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
        ]
    else:
        payloads = [
            {"choices": [{"delta": {"content": "answered"}}]},
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "runtime": runtime,
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        ]
    lines = []
    for payload in payloads:
        lines.extend(
            [
                f"data: {json.dumps(payload)}\n".encode("utf-8"),
                b"\n",
            ]
        )
    lines.extend([b"data: [DONE]\n", b"\n"])
    return lines


def _event_pairs(subscriber):
    events = []
    while not subscriber.empty():
        item = subscriber.get_nowait()
        events.append((item[0], item[1]))
    return events


@pytest.mark.parametrize("runs_api", [True, False], ids=["runs-api", "legacy-stream"])
def test_gateway_terminal_runtime_reconciles_live_persisted_done_and_reload(
    tmp_path, monkeypatch, runs_api
):
    """Requested A/high must become answered B/C/off on every consumer surface."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gateway_chat,
        "_gateway_reasoning_effort_for_request",
        lambda *_args, **_kwargs: "high",
    )
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda _cfg: {
        "status": "not_configured",
        "source": "none",
        "label": "",
        "message_count": 0,
        "messages": [],
    })
    monkeypatch.setattr(
        streaming,
        "_prefill_messages_with_webui_context",
        lambda _context, _cfg: [],
    )
    monkeypatch.setattr(
        gateway_chat,
        "gateway_supports_approval",
        lambda *_args, **_kwargs: runs_api,
    )
    monkeypatch.setattr(
        gateway_chat,
        "gateway_approval_unavailable_reason",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setenv("HERMES_WEBUI_GATEWAY_BASE_URL", "http://gateway.test")
    monkeypatch.setenv(
        "HERMES_WEBUI_GATEWAY_USE_RUNS_API", "1" if runs_api else "0"
    )

    def fake_urlopen(req, timeout=0):
        if runs_api and req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-effective-runtime"})
        return _SseResponse(_runtime_terminal_lines(runs_api=runs_api))

    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)

    session = models.new_session()
    session.model = "requested-model-a"
    session.model_provider = "provider-a"
    stream_id = f"effective-runtime-{'runs' if runs_api else 'legacy'}"
    session.active_stream_id = stream_id
    session.pending_user_message = "answer using the gateway"
    session.pending_attachments = []
    session.pending_started_at = 123.0
    session.save()
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel

    gateway_chat._run_gateway_chat_streaming(
        session.session_id,
        "answer using the gateway",
        "requested-model-a",
        str(tmp_path),
        stream_id,
        [],
        model_provider="provider-a",
    )

    events = _event_pairs(subscriber)
    run_meta = [payload for event, payload in events if event == "run_meta"]
    assert run_meta == [
        {
            "session_id": session.session_id,
            "model": "requested-model-a",
            "provider": "provider-a",
            "reasoning_effort": "high",
        },
        {
            "session_id": session.session_id,
            "model": "answered-model-b",
            "provider": "provider-c",
            "reasoning_effort": "off",
        },
    ]

    saved = models.get_session(session.session_id)
    assistant = saved.messages[-1]
    assert assistant["_usedModel"] == "answered-model-b"
    assert assistant["_reasoningEffort"] == "off"
    assert assistant["_gatewayRouting"]["used_model"] == "answered-model-b"
    assert assistant["_gatewayRouting"]["used_provider"] == "provider-c"
    assert saved.model == "answered-model-b"
    assert saved.model_provider == "provider-c"

    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1
    assert done[0]["usage"]["used_model"] == "answered-model-b"
    assert done[0]["usage"]["used_provider"] == "provider-c"
    assert done[0]["usage"]["reasoning_effort"] == "off"
    assert "_effective_runtime" not in done[0]["usage"]

    reloaded = models.Session.load(session.session_id)
    assert reloaded is not None
    assert reloaded.model == "answered-model-b"
    assert reloaded.model_provider == "provider-c"
    reloaded_assistant = reloaded.messages[-1]
    assert reloaded_assistant["_usedModel"] == "answered-model-b"
    assert reloaded_assistant["_reasoningEffort"] == "off"
    assert reloaded_assistant["_gatewayRouting"]["used_provider"] == "provider-c"


def test_gateway_does_not_treat_plain_terminal_model_as_effective_runtime():
    """OpenAI-compatible terminal ``model`` is requested-side unless runtime says otherwise."""
    assert gateway_chat._gateway_effective_runtime_metadata(
        {
            "model": "requested-model-a",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    ) == {}
