"""Behavioral regressions for effective reasoning/runtime attribution (#6644)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from api import config, gateway_chat, models, streaming
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


_DEFAULT_RUNTIME = object()


def _runtime_terminal_lines(*, runs_api: bool, runtime=_DEFAULT_RUNTIME):
    if runtime is _DEFAULT_RUNTIME:
        runtime = _terminal_runtime()
    if runs_api:
        terminal = {
            "event": "run.completed",
            "output": "answered",
            "usage": {"input_tokens": 7, "output_tokens": 2},
        }
        if runtime is not None:
            terminal["runtime"] = runtime
        payloads = [
            {"event": "message.delta", "delta": "answered"},
            terminal,
        ]
    else:
        terminal = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }
        if runtime is not None:
            terminal["runtime"] = runtime
        payloads = [
            {"choices": [{"delta": {"content": "answered"}}]},
            terminal,
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


_GATEWAY_PARSE_SCRIPT = """
import json
import sys

sys.path.insert(0, sys.argv[1])
from gateway.platforms.api_server import (
    _REQUEST_OPTION_MISSING,
    _request_agent_overrides,
    _request_reasoning_config,
    _request_service_tier,
)

results = []
for body in json.loads(sys.stdin.read()):
    overrides = _request_agent_overrides(body, virtual_model="hermes-agent")
    model_options = overrides.get("model_options")
    service_tier = _request_service_tier(model_options)
    results.append({
        "reasoning": _request_reasoning_config(model_options),
        "service_tier_missing": service_tier is _REQUEST_OPTION_MISSING,
        "service_tier": None if service_tier is _REQUEST_OPTION_MISSING else service_tier,
    })
print(json.dumps(results))
"""


def _installed_gateway_parser(request_bodies):
    candidates = []
    configured = str(os.environ.get("HERMES_WEBUI_AGENT_DIR") or "").strip()
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path.home() / ".hermes" / "hermes-agent",
            Path("/usr/local/lib/hermes-agent"),
        ]
    )
    agent_dir = next(
        (
            candidate
            for candidate in candidates
            if (candidate / "gateway" / "platforms" / "api_server.py").is_file()
        ),
        None,
    )
    if agent_dir is None:
        pytest.skip("installed Hermes Agent Gateway parser is unavailable")
    python = next(
        (
            candidate
            for candidate in (
                agent_dir / "venv" / "bin" / "python",
                agent_dir / "venv" / "Scripts" / "python.exe",
            )
            if candidate.is_file()
        ),
        None,
    )
    if python is None:
        pytest.skip("installed Hermes Agent Python is unavailable")
    proc = subprocess.run(
        [str(python), "-c", _GATEWAY_PARSE_SCRIPT, str(agent_dir)],
        input=json.dumps(request_bodies),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _capture_gateway_request_body(
    monkeypatch,
    tmp_path,
    *,
    runs_api,
    configured_effort,
    configured_service_tier,
    suffix,
):
    cfg = {
        "model": {
            "provider": "openai",
            "default": "gpt-5.5",
        },
    }
    if configured_effort is not None:
        cfg["agent"] = {"reasoning_effort": configured_effort}
    if configured_service_tier is not None:
        cfg["model"]["service_tier"] = configured_service_tier

    stream_id = f"gateway-options-{suffix}"
    session_id = f"gateway-options-session-{suffix}"
    session = SimpleNamespace(
        active_stream_id=stream_id,
        workspace=str(tmp_path),
        profile=None,
        context_messages=[],
        _approval_notice_emitted=False,
        save=lambda **_kwargs: None,
    )
    channel = create_stream_channel()
    subscriber = channel.subscribe()
    STREAMS[stream_id] = channel
    captured = []

    def fake_urlopen(req, timeout=0):
        if req.data:
            captured.append(json.loads(req.data.decode("utf-8")))
        if runs_api and req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": f"run-{suffix}"})
        return _SseResponse([b"data: [DONE]\n", b"\n"])

    monkeypatch.setattr(gateway_chat, "RunJournalWriter", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_chat, "get_session", lambda _session_id: session)
    monkeypatch.setattr(gateway_chat.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(config, "get_config", lambda: cfg)
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
    monkeypatch.setattr(streaming, "_load_webui_prefill_context", lambda _cfg: {})
    monkeypatch.setattr(
        streaming,
        "_prefill_messages_with_webui_context",
        lambda _context, _cfg: [],
    )
    monkeypatch.setattr(
        streaming,
        "_normalize_prefill_messages_before_user_turn",
        lambda messages: messages,
    )
    monkeypatch.setattr(streaming, "_public_prefill_context_status", lambda _context: {})
    monkeypatch.setattr(
        streaming,
        "_webui_ephemeral_system_prompt",
        lambda *_args, **_kwargs: "system",
    )
    monkeypatch.setenv(
        "HERMES_WEBUI_GATEWAY_USE_RUNS_API",
        "1" if runs_api else "0",
    )

    gateway_chat._run_gateway_chat_streaming(
        session_id,
        "exercise request options",
        "gpt-5.5",
        str(tmp_path),
        stream_id,
        [],
        model_provider="openai",
    )

    assert len(captured) == 1
    return captured[0], _event_pairs(subscriber)


def test_gateway_request_options_compose_with_installed_parser_for_both_transports(
    tmp_path, monkeypatch
):
    cases = [
        ("high", "  HIGH  ", "priority", {"enabled": True, "effort": "high"}, "priority"),
        ("off", "none", "priority", {"enabled": False}, "priority"),
        ("absent", None, None, None, None),
        ("invalid", "definitely-invalid", "turbo", None, None),
    ]
    bodies = []
    expected = []

    for runs_api in (True, False):
        transport = "runs" if runs_api else "legacy"
        for case_name, effort, service_tier, reasoning, accepted_tier in cases:
            with monkeypatch.context() as scoped:
                body, events = _capture_gateway_request_body(
                    scoped,
                    tmp_path,
                    runs_api=runs_api,
                    configured_effort=effort,
                    configured_service_tier=service_tier,
                    suffix=f"{transport}-{case_name}",
                )
            assert "reasoning_effort" not in body
            assert "service_tier" not in body
            bodies.append(body)
            expected.append(
                {
                    "reasoning": reasoning,
                    "service_tier_missing": accepted_tier is None,
                    "service_tier": accepted_tier,
                }
            )
            # Accepted request options are still intent. Without terminal
            # runtime evidence they must not be surfaced as observed run_meta.
            assert [payload for event, payload in events if event == "run_meta"] == []

    assert _installed_gateway_parser(bodies) == expected


@pytest.mark.parametrize("runs_api", [True, False], ids=["runs-api", "legacy-stream"])
@pytest.mark.parametrize(
    ("terminal_runtime", "expected_runtime"),
    [
        (
            {"model": "answered-model-b", "provider": "provider-c", "reasoning": {"enabled": False}},
            {"model": "answered-model-b", "provider": "provider-c", "reasoning_effort": "off"},
        ),
        (
            {"model": "answered-model-b", "provider": "provider-c"},
            {"model": "answered-model-b", "provider": "provider-c"},
        ),
        (None, {}),
    ],
    ids=["observed-runtime-with-effort", "observed-runtime-without-effort", "runtime-unknown"],
)
def test_gateway_terminal_runtime_reconciles_live_persisted_done_and_reload(
    tmp_path, monkeypatch, runs_api, terminal_runtime, expected_runtime
):
    """Only terminal observations are attributed; the selected route remains A/provider-A."""
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

    gateway_requests = []

    def fake_urlopen(req, timeout=0):
        if req.data and (
            req.full_url.endswith("/v1/runs")
            or req.full_url.endswith("/v1/chat/completions")
        ):
            gateway_requests.append(json.loads(req.data.decode("utf-8")))
        if runs_api and req.full_url.endswith("/v1/runs"):
            return _JsonResponse({"run_id": "run-effective-runtime"})
        return _SseResponse(
            _runtime_terminal_lines(runs_api=runs_api, runtime=terminal_runtime)
        )

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
    assert "reasoning_effort" not in gateway_requests[0]
    assert gateway_requests[0]["model_options"]["reasoning"] == {
        "enabled": True,
        "effort": "high",
    }
    assert _installed_gateway_parser([gateway_requests[0]]) == [
        {
            "reasoning": {"enabled": True, "effort": "high"},
            "service_tier_missing": True,
            "service_tier": None,
        }
    ]
    run_meta = [payload for event, payload in events if event == "run_meta"]
    expected_run_meta = []
    if expected_runtime:
        expected_run_meta.append({
            "session_id": session.session_id,
            "model": expected_runtime.get("model"),
            "provider": expected_runtime.get("provider"),
            "reasoning_effort": expected_runtime.get("reasoning_effort"),
        })
    assert run_meta == expected_run_meta

    saved = models.get_session(session.session_id)
    assistant = saved.messages[-1]
    if expected_runtime:
        assert assistant["_usedModel"] == expected_runtime["model"]
        assert assistant["_gatewayRouting"]["used_model"] == expected_runtime["model"]
        assert assistant["_gatewayRouting"]["used_provider"] == expected_runtime["provider"]
    else:
        assert "_usedModel" not in assistant
        assert "_gatewayRouting" not in assistant
    if "reasoning_effort" in expected_runtime:
        assert assistant["_reasoningEffort"] == expected_runtime["reasoning_effort"]
    else:
        assert "_reasoningEffort" not in assistant
    assert saved.model == "requested-model-a"
    assert saved.model_provider == "provider-a"

    done = [payload for event, payload in events if event == "done"]
    assert len(done) == 1
    usage = done[0]["usage"]
    if expected_runtime:
        assert usage["used_model"] == expected_runtime["model"]
        assert usage["used_provider"] == expected_runtime["provider"]
    else:
        assert "used_model" not in usage
        assert "used_provider" not in usage
    if "reasoning_effort" in expected_runtime:
        assert usage["reasoning_effort"] == expected_runtime["reasoning_effort"]
    else:
        assert "reasoning_effort" not in usage
    assert "_effective_runtime" not in done[0]["usage"]

    models.SESSIONS.clear()
    reloaded = models.get_session(session.session_id)
    assert reloaded.model == "requested-model-a"
    assert reloaded.model_provider == "provider-a"
    reloaded_assistant = reloaded.messages[-1]
    if expected_runtime:
        assert reloaded_assistant["_usedModel"] == expected_runtime["model"]
        assert reloaded_assistant["_gatewayRouting"]["used_provider"] == expected_runtime["provider"]
    else:
        assert "_usedModel" not in reloaded_assistant
        assert "_gatewayRouting" not in reloaded_assistant
    if "reasoning_effort" in expected_runtime:
        assert reloaded_assistant["_reasoningEffort"] == expected_runtime["reasoning_effort"]
    else:
        assert "_reasoningEffort" not in reloaded_assistant

    # The next turn is built from the reloaded request-owned selection, not from
    # the prior turn's effective runtime attribution.
    next_stream_id = f"next-effective-runtime-{'runs' if runs_api else 'legacy'}"
    reloaded.active_stream_id = next_stream_id
    reloaded.pending_user_message = "ask again after fallback"
    reloaded.pending_attachments = []
    reloaded.pending_started_at = 456.0
    reloaded.save()
    next_channel = create_stream_channel()
    STREAMS[next_stream_id] = next_channel
    gateway_chat._run_gateway_chat_streaming(
        reloaded.session_id,
        "ask again after fallback",
        reloaded.model,
        str(tmp_path),
        next_stream_id,
        [],
        model_provider=reloaded.model_provider,
    )

    assert len(gateway_requests) == 2
    assert gateway_requests[1]["model"] == "requested-model-a"
    assert gateway_requests[1]["provider"] == "provider-a"


def test_gateway_does_not_treat_plain_terminal_model_as_effective_runtime():
    """OpenAI-compatible terminal ``model`` is requested-side unless runtime says otherwise."""
    assert gateway_chat._gateway_effective_runtime_metadata(
        {
            "model": "requested-model-a",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    ) == {}


def test_completed_run_reattach_merges_terminal_runtime_before_return(monkeypatch):
    """A completed Runs status owns observed attribution after WebUI restart."""
    monkeypatch.setattr(
        gateway_chat,
        "_get_gateway_run_status",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "output": "reattached answer",
            "runtime": {"model": "answered-model-b", "provider": "provider-c"},
            "usage": {"input_tokens": 9, "output_tokens": 3},
        },
    )
    monkeypatch.setattr(gateway_chat, "_publish_gateway_run_id", lambda *_args: None)
    monkeypatch.setattr(gateway_chat, "update_active_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "api.route_approvals.settle_gateway_pending_run",
        lambda *_args, **_kwargs: None,
    )

    output, usage = gateway_chat._await_gateway_run_result(
        "reattach-session",
        "reattach-stream",
        "reattach-run",
        "http://gateway.test",
        "",
        put_gateway_event=lambda *_args: None,
        cancel_event=threading.Event(),
    )

    assert output == "reattached answer"
    assert usage["input_tokens"] == 9
    assert usage["output_tokens"] == 3
    assert usage["_effective_runtime"] == {
        "model": "answered-model-b",
        "provider": "provider-c",
    }
