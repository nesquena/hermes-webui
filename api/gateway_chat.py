"""Default-off Hermes Gateway bridge for browser-originated chat turns."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from api.config import (
    CANCEL_FLAGS,
    STREAMS,
    STREAMS_LOCK,
    STREAM_LAST_EVENT_ID,
    STREAM_LIVE_TOOL_CALLS,
    STREAM_PARTIAL_TEXT,
    STREAM_REASONING_TEXT,
    _get_session_agent_lock,
    gateway_supports_approval,
    register_active_run,
    unregister_active_run,
    update_active_run,
)
from api.helpers import _redact_text, redact_session_data
from api.models import get_session, merge_session_messages_append_only
from api.run_journal import RunJournalWriter

logger = logging.getLogger(__name__)

# Maps stream_id -> gateway run_id for approval response relay.
_STREAM_RUN_IDS: dict[str, str] = {}

_WEBUI_CHAT_BACKEND_ENV = "HERMES_WEBUI_CHAT_BACKEND"
_WEBUI_GATEWAY_BASE_URL_ENV = "HERMES_WEBUI_GATEWAY_BASE_URL"
_WEBUI_GATEWAY_API_KEY_ENV = "HERMES_WEBUI_GATEWAY_API_KEY"
_WEBUI_GATEWAY_USE_RUNS_API_ENV = "HERMES_WEBUI_GATEWAY_USE_RUNS_API"
_GATEWAY_CHAT_BACKENDS = {"gateway", "api_server", "api-server"}


def webui_chat_backend_mode(config_data=None, environ: dict[str, str] | None = None) -> str:
    """Return the explicitly selected browser chat backend.

    The default remains the in-process WebUI runtime. Only explicit gateway
    values opt browser chat into the Hermes API server bridge; generic truthy
    strings are deliberately ignored so deployments do not change execution
    ownership by accident.
    """
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_CHAT_BACKEND_ENV)
        or cfg.get("webui_chat_backend")
        or ""
    ).strip().lower()
    if raw in _GATEWAY_CHAT_BACKENDS:
        return "gateway"
    return "legacy"


def webui_gateway_chat_enabled(config_data=None, environ: dict[str, str] | None = None) -> bool:
    return webui_chat_backend_mode(config_data, environ) == "gateway"


def _gateway_base_url(config_data=None, environ: dict[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_GATEWAY_BASE_URL_ENV)
        or cfg.get("webui_gateway_base_url")
        or "http://127.0.0.1:8642"
    ).strip()
    return raw.rstrip("/") or "http://127.0.0.1:8642"


def _gateway_api_key(environ: dict[str, str] | None = None) -> str:
    source = os.environ if environ is None else environ
    return str(
        source.get(_WEBUI_GATEWAY_API_KEY_ENV)
        or source.get("API_SERVER_KEY")
        or ""
    ).strip()


def _gateway_use_runs_api_enabled(config_data=None, environ: dict[str, str] | None = None) -> bool:
    """Return True only when the operator has explicitly opted into the runs API path."""
    source = os.environ if environ is None else environ
    cfg = config_data if isinstance(config_data, dict) else {}
    raw = str(
        source.get(_WEBUI_GATEWAY_USE_RUNS_API_ENV)
        or cfg.get("webui_gateway_use_runs_api")
        or ""
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def gateway_chat_config_status(config_data=None, environ: dict[str, str] | None = None) -> dict:
    """Return redacted Gateway-backed chat configuration status."""
    mode = webui_chat_backend_mode(config_data, environ)
    base_url = _gateway_base_url(config_data, environ)
    return {
        "enabled": mode == "gateway",
        "backend": mode,
        "base_url_configured": bool(base_url),
        "api_key_configured": bool(_gateway_api_key(environ)),
    }


def _gateway_http_error_event(exc: urllib.error.HTTPError, err_body: str, *, api_key_configured: bool) -> dict:
    safe = _redact_text(err_body or str(exc))[:500]
    if exc.code == 401:
        return {
            "label": "Gateway authentication failed",
            "type": "gateway_auth_error",
            "message": "Gateway rejected the WebUI API key (HTTP 401).",
            "hint": (
                "Set HERMES_WEBUI_GATEWAY_API_KEY to the same value as the Hermes Gateway "
                "API_SERVER_KEY, or disable HERMES_WEBUI_CHAT_BACKEND=gateway."
                if not api_key_configured
                else "Check that HERMES_WEBUI_GATEWAY_API_KEY matches the Hermes Gateway API_SERVER_KEY."
            ),
        }
    return {
        "label": "Gateway request failed",
        "type": "gateway_http_error",
        "message": f"Gateway returned HTTP {exc.code}.",
        "hint": safe or "Check the configured Gateway API server.",
    }


def _gateway_sse_delta(payload: dict) -> str:
    """Extract assistant text from an OpenAI-compatible streaming chunk."""
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        content = delta.get("content")
        if isinstance(content, str):
            return content
        message = choice.get("message") or {}
        content = message.get("content")
        return content if isinstance(content, str) else ""
    except Exception:
        return ""


def _gateway_sse_reasoning_delta(payload: dict) -> str:
    """Extract reasoning text from OpenAI-compatible streaming chunks."""
    try:
        choices = payload.get("choices") or []
        if not choices:
            return ""
        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content")
        return reasoning if isinstance(reasoning, str) and reasoning.strip() else ""
    except Exception:
        return ""


def _gateway_stream_usage(payload: dict) -> dict:
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
        "estimated_cost": usage.get("estimated_cost") or usage.get("estimated_cost_usd") or 0,
    }


def _gateway_reasoning_delta(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("text", "preview", "delta", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _gateway_tool_progress_event(payload: dict) -> tuple[str, dict] | None:
    """Translate Hermes Gateway tool-progress SSE payloads to WebUI events."""
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("event") or "").strip().lower()
    if event_type == "reasoning.available":
        reason_delta = _gateway_reasoning_delta(payload)
        if not reason_delta:
            return None
        return "reasoning", {"text": reason_delta}
    name = str(payload.get("tool") or payload.get("name") or payload.get("function_name") or "").strip()
    if not name:
        return None
    if name == "_thinking":
        reason_delta = _gateway_reasoning_delta(payload)
        if not reason_delta:
            return None
        return "reasoning", {"text": reason_delta}
    if name.startswith("_"):
        return None
    status = str(payload.get("status") or "running").strip().lower()
    tid = payload.get("toolCallId") or payload.get("tool_call_id") or payload.get("id")
    is_complete = event_type == "tool.completed" or status in {"completed", "complete", "success", "error", "failed"}
    event_payload = {
        "event_type": "tool.completed" if is_complete else "tool.started",
        "name": name,
        "preview": payload.get("label") or payload.get("preview"),
        "args": payload.get("args") if isinstance(payload.get("args"), dict) else {},
        "is_error": bool(payload.get("error")) or status in {"error", "failed"},
    }
    if tid:
        event_payload["tid"] = str(tid)
    return ("tool_complete" if is_complete else "tool"), event_payload


def _gateway_runs_approval_event(payload: dict) -> dict | None:
    """Map a runs-API approval.request payload to the WebUI approval contract."""
    if not isinstance(payload, dict):
        return None
    tool = str(payload.get("tool") or payload.get("function_name") or payload.get("pattern_key") or "").strip()
    command = str(payload.get("command") or "").strip()
    description = str(payload.get("description") or "").strip()
    pattern_keys = payload.get("pattern_keys") if isinstance(payload.get("pattern_keys"), list) else []
    pattern_key = str(payload.get("pattern_key") or "").strip()
    args = payload.get("args") if isinstance(payload.get("args"), (list, dict)) else []
    run_id = str(payload.get("run_id") or "").strip()
    approval_id = str(payload.get("approval_id") or payload.get("id") or "").strip()
    risk = str(payload.get("risk_level") or "high").strip()
    choices = payload.get("choices") if isinstance(payload.get("choices"), list) else []
    allow_permanent = payload.get("allow_permanent")
    if allow_permanent is None:
        allow_permanent = "always" in choices
    if not (tool or command or description):
        return None
    return {
        "tool": tool,
        "command": command,
        "description": description,
        "pattern_key": pattern_key,
        "pattern_keys": pattern_keys or ([pattern_key] if pattern_key else []),
        "args": args,
        "risk_level": risk,
        "run_id": run_id,
        "approval_id": approval_id,
        "choices": choices,
        "allow_permanent": bool(allow_permanent),
    }


def _run_gateway_runs_api_streaming(
    session_id, msg_text, model, workspace, stream_id,
    base_url, api_key, prefill_messages, body_extras,
    *, put_gateway_event, cancel_event,
    attachments=None, cfg=None,
):
    """Submit via POST /v1/runs and relay SSE events including approval."""
    url_runs = f"{base_url.rstrip('/')}/v1/runs"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Hermes-Session-Id": session_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-Hermes-Session-Key"] = f"webui:{session_id}"
    message_content: Any = str(msg_text or "")
    if attachments:
        try:
            from api.streaming import _build_native_multimodal_message

            message_content = _build_native_multimodal_message("", str(msg_text or ""), attachments, str(workspace), cfg=cfg)
        except Exception:
            logger.debug("Failed to build runs-API multimodal attachment payload", exc_info=True)
            message_content = str(msg_text or "")
    instructions_parts = []
    conversation_history = []
    for entry in prefill_messages or []:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()
        content = entry.get("content")
        if role == "system":
            if isinstance(content, str) and content.strip():
                instructions_parts.append(content)
            elif content is not None:
                instructions_parts.append(str(content))
            continue
        if role not in {"user", "assistant"}:
            continue
        conversation_history.append({"role": role, "content": content})
    run_input = message_content
    if isinstance(run_input, list):
        run_input = [{"role": "user", "content": run_input}]
    run_body = {
        "model": model or "default",
        "input": run_input,
        **body_extras,
    }
    if instructions_parts:
        run_body["instructions"] = "\n\n".join(part for part in instructions_parts if part)
    if conversation_history:
        run_body["conversation_history"] = conversation_history
    req = urllib.request.Request(
        url_runs,
        data=json.dumps(run_body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    update_active_run(stream_id, phase="gateway-request")
    with urllib.request.urlopen(req, timeout=30) as resp:
        run_data = json.loads(resp.read(65536))
    run_id = str(run_data.get("run_id") or run_data.get("id") or "").strip()
    if not run_id:
        raise ValueError(f"Gateway runs API returned no run_id: {run_data!r}")

    _STREAM_RUN_IDS[stream_id] = run_id

    url_events = f"{base_url.rstrip('/')}/v1/runs/{run_id}/events"
    headers_sse = dict(headers)
    headers_sse["Accept"] = "text/event-stream"
    req_events = urllib.request.Request(url_events, headers=headers_sse, method="GET")
    final_text = ""
    usage: dict = {}
    sse_event = "message"
    with urllib.request.urlopen(req_events, timeout=600) as resp:
        for raw_line in resp:
            if cancel_event.is_set():
                put_gateway_event("cancel", {"message": "Cancelled by user"})
                return None, usage
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                sse_event = "message"
                continue
            if line.startswith("event:"):
                sse_event = line[6:].strip() or "message"
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                payload = json.loads(data)
            except json.JSONDecodeError:
                continue
            payload_event = str(payload.get("event") or payload.get("type") or sse_event).strip() or "message"
            if payload_event == "approval.request":
                approval_data = _gateway_runs_approval_event(payload)
                if approval_data:
                    approval_data["run_id"] = run_id
                    put_gateway_event("approval", approval_data)
                sse_event = "message"
                continue
            if payload_event in {"tool.started", "tool.completed", "reasoning.available"}:
                translated = _gateway_tool_progress_event(payload)
                if translated:
                    event_name, event_payload = translated
                    if event_name == "reasoning":
                        reason_delta = event_payload.get("text")
                        if reason_delta and stream_id in STREAM_REASONING_TEXT:
                            STREAM_REASONING_TEXT[stream_id] += reason_delta
                    elif stream_id in STREAM_LIVE_TOOL_CALLS:
                        if event_name == "tool":
                            STREAM_LIVE_TOOL_CALLS[stream_id].append({
                                "name": event_payload.get("name"),
                                "args": event_payload.get("args") or {},
                                "done": False,
                                **({"tid": event_payload.get("tid")} if event_payload.get("tid") else {}),
                            })
                        elif event_name == "tool_complete":
                            for shared_tc in reversed(STREAM_LIVE_TOOL_CALLS[stream_id]):
                                if shared_tc.get("done"):
                                    continue
                                if (
                                    event_payload.get("tid") and shared_tc.get("tid") == event_payload.get("tid")
                                ) or shared_tc.get("name") == event_payload.get("name"):
                                    shared_tc["done"] = True
                                    shared_tc["is_error"] = bool(event_payload.get("is_error"))
                                    break
                    put_gateway_event(event_name, event_payload)
                    if event_name != "reasoning":
                        update_active_run(stream_id, phase="gateway-tool", latest_tool=event_payload.get("name"))
                sse_event = "message"
                continue
            if payload_event == "message.delta":
                delta = str(payload.get("delta") or "")
                if delta:
                    final_text += delta
                    if stream_id in STREAM_PARTIAL_TEXT:
                        STREAM_PARTIAL_TEXT[stream_id] += delta
                    put_gateway_event("token", {"text": delta})
                sse_event = "message"
                continue
            if payload_event == "run.completed":
                output = str(payload.get("output") or "")
                if output and not final_text:
                    final_text = output
                    if stream_id in STREAM_PARTIAL_TEXT:
                        STREAM_PARTIAL_TEXT[stream_id] = output
                usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
                sse_event = "message"
                continue
            if payload_event == "run.failed":
                raise RuntimeError(str(payload.get("error") or "Gateway run failed"))
            if payload_event == "run.cancelled":
                put_gateway_event("cancel", {"message": "Cancelled by gateway"})
                return None, usage
            reasoning_delta = _gateway_sse_reasoning_delta(payload)
            if reasoning_delta:
                if stream_id in STREAM_REASONING_TEXT:
                    STREAM_REASONING_TEXT[stream_id] += reasoning_delta
                put_gateway_event("reasoning", {"text": reasoning_delta})
            delta = _gateway_sse_delta(payload)
            if delta:
                final_text += delta
                if stream_id in STREAM_PARTIAL_TEXT:
                    STREAM_PARTIAL_TEXT[stream_id] += delta
                put_gateway_event("token", {"text": delta})
            usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
    return final_text, usage


def _stream_writeback_is_current(session: Any, stream_id: str) -> bool:
    return bool(stream_id and getattr(session, "active_stream_id", None) == stream_id)


def _clear_gateway_pending_state(session: Any, stream_id: str) -> None:
    if not _stream_writeback_is_current(session, stream_id):
        return
    session.active_stream_id = None
    session.pending_user_message = None
    session.pending_attachments = None
    session.pending_started_at = None
    session.save()


def _run_gateway_chat_streaming(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    model_provider=None,
):
    """Bridge a WebUI chat turn through Hermes Gateway's API server.

    This default-off path keeps the browser contract unchanged: /api/chat/start
    still returns a local stream_id and /api/chat/stream still receives WebUI SSE
    event names. The worker translates OpenAI-compatible streaming chunks from
    the configured Gateway API server into those local events and persists the
    final user/assistant turn back into the WebUI session.
    """
    q = STREAMS.get(stream_id)
    if q is None:
        return
    register_active_run(
        stream_id,
        session_id=session_id,
        started_at=time.time(),
        phase="gateway-starting",
        workspace=str(workspace),
        model=model,
        provider=model_provider,
        backend="gateway",
    )
    try:
        run_journal = RunJournalWriter(session_id, stream_id)
    except Exception:
        run_journal = None
        logger.debug("Failed to initialize gateway run journal for stream %s", stream_id, exc_info=True)
    cancel_event = threading.Event()
    with STREAMS_LOCK:
        CANCEL_FLAGS[stream_id] = cancel_event
        STREAM_PARTIAL_TEXT[stream_id] = ""
        STREAM_REASONING_TEXT[stream_id] = ""
        STREAM_LIVE_TOOL_CALLS[stream_id] = []

    def put_gateway_event(event, data):
        if cancel_event.is_set() and event not in ("cancel", "error", "apperror"):
            return
        event_id = None
        if run_journal is not None:
            try:
                journaled = run_journal.append_sse_event(event, data)
                event_id = (journaled or {}).get("event_id") if isinstance(journaled, dict) else None
                if event_id:
                    STREAM_LAST_EVENT_ID[stream_id] = event_id
            except Exception:
                logger.debug("Failed to append gateway event %s for stream %s", event, stream_id, exc_info=True)
        if event_id and hasattr(q, "note_last_event_id"):
            try:
                q.note_last_event_id(event_id)
            except Exception:
                logger.debug("Failed to note gateway event_id %s for stream %s", event_id, stream_id, exc_info=True)
        try:
            queue_item = (event, data, event_id) if event_id and hasattr(q, "subscribe_with_snapshot") else (event, data)
            q.put_nowait(queue_item)
        except Exception:
            logger.debug("Failed to put gateway event to queue")

    s = None
    final_text = ""
    usage = {"input_tokens": 0, "output_tokens": 0, "estimated_cost": 0}
    try:
        s = get_session(session_id)
        from api.config import get_config  # imported lazily to avoid config-cycle churn

        cfg = get_config()
        try:
            from api.streaming import (
                _load_webui_prefill_context,
                _prefill_messages_with_webui_context,
                _normalize_prefill_messages_before_user_turn,
                _public_prefill_context_status,
                _webui_ephemeral_system_prompt,
            )

            prefill_context = _load_webui_prefill_context(cfg)
            # #3324: the WebUI session/delivery context (connected platforms,
            # home channels, delivery hints, session framing) is now carried in
            # the ephemeral system prompt rather than a prefill `user` message.
            # The gateway-backed path must build the SAME system prompt so that
            # context is not silently dropped on Gateway-routed WebUI chats.
            _gateway_system_prompt = _webui_ephemeral_system_prompt(
                None,
                surface_context={
                    "source": "webui",
                    "session_id": session_id,
                    "profile": getattr(s, "profile", None),
                    "workspace": s.workspace if s is not None else str(workspace),
                },
                config_data=cfg,
            )
            prefill_messages = _prefill_messages_with_webui_context(prefill_context, cfg)
            prefill_messages = _normalize_prefill_messages_before_user_turn(prefill_messages)
            prefill_messages = [
                {"role": "system", "content": _gateway_system_prompt},
                *prefill_messages,
            ]
            put_gateway_event("context_status", {
                "session_id": session_id,
                "prefill": _public_prefill_context_status(prefill_context),
            })
        except Exception:
            logger.debug("Failed to load WebUI gateway prefill context", exc_info=True)
            prefill_messages = []
        base_url = _gateway_base_url(cfg)
        api_key = _gateway_api_key()
        # Capability gate: use runs API when gateway advertises approval support.
        _use_runs_api = _gateway_use_runs_api_enabled(cfg) and gateway_supports_approval(base_url, api_key)
        if _use_runs_api:
            body_extras = {}
            if model_provider:
                body_extras["provider"] = model_provider
            try:
                final_text, usage = _run_gateway_runs_api_streaming(
                    session_id, msg_text, model, workspace, stream_id,
                    base_url, api_key, prefill_messages, body_extras,
                    put_gateway_event=put_gateway_event,
                    cancel_event=cancel_event,
                    attachments=attachments,
                    cfg=cfg,
                )
            except Exception as exc:
                put_gateway_event("apperror", {
                    "label": "Gateway runs API error",
                    "type": "gateway_runs_error",
                    "message": str(exc)[:400],
                    "hint": "Check that the Hermes Gateway runs API (/v1/runs) is available.",
                })
                return
            if final_text is None:
                return
        else:
            url = f"{base_url}/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "X-Hermes-Session-Id": session_id,
            }
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
                # Scope Gateway long-term continuity to this WebUI conversation
                # without exposing the browser's auth cookie or CSRF material.
                headers["X-Hermes-Session-Key"] = f"webui:{session_id}"
            message_content: Any = str(msg_text or "")
            if attachments:
                try:
                    from api.streaming import _build_native_multimodal_message

                    message_content = _build_native_multimodal_message("", str(msg_text or ""), attachments, str(workspace), cfg=cfg)
                except Exception:
                    logger.debug("Failed to build gateway multimodal attachment payload", exc_info=True)
                    message_content = str(msg_text or "")
            body = {
                "model": model or "default",
                "stream": True,
                "messages": [*prefill_messages, {"role": "user", "content": message_content}],
            }
            if model_provider:
                body["provider"] = model_provider
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            update_active_run(stream_id, phase="gateway-request")
            last_payload = {}
            sse_event = "message"
            with urllib.request.urlopen(req, timeout=600) as resp:
                for raw_line in resp:
                    if cancel_event.is_set():
                        put_gateway_event("cancel", {"message": "Cancelled by user"})
                        return
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        sse_event = "message"
                        continue
                    if line.startswith("event:"):
                        sse_event = line[6:].strip() or "message"
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if sse_event == "hermes.tool.progress":
                        translated = _gateway_tool_progress_event(payload)
                        if translated:
                            event_name, event_payload = translated
                            if event_name == "reasoning":
                                reason_delta = event_payload.get("text")
                                if reason_delta and stream_id in STREAM_REASONING_TEXT:
                                    STREAM_REASONING_TEXT[stream_id] += reason_delta
                            elif stream_id in STREAM_LIVE_TOOL_CALLS:
                                if event_name == "tool":
                                    STREAM_LIVE_TOOL_CALLS[stream_id].append({
                                        "name": event_payload.get("name"),
                                        "args": event_payload.get("args") or {},
                                        "done": False,
                                        **({"tid": event_payload.get("tid")} if event_payload.get("tid") else {}),
                                    })
                                else:
                                    for shared_tc in reversed(STREAM_LIVE_TOOL_CALLS[stream_id]):
                                        if shared_tc.get("done"):
                                            continue
                                        if (
                                            event_payload.get("tid") and shared_tc.get("tid") == event_payload.get("tid")
                                        ) or shared_tc.get("name") == event_payload.get("name"):
                                            shared_tc["done"] = True
                                            shared_tc["is_error"] = bool(event_payload.get("is_error"))
                                            break
                            put_gateway_event(event_name, event_payload)
                            if event_name != "reasoning":
                                update_active_run(stream_id, phase="gateway-tool", latest_tool=event_payload.get("name"))
                        sse_event = "message"
                        continue
                    if sse_event == "reasoning.available":
                        reason_delta = _gateway_reasoning_delta(payload)
                        if reason_delta:
                            if stream_id in STREAM_REASONING_TEXT:
                                STREAM_REASONING_TEXT[stream_id] += reason_delta
                            put_gateway_event("reasoning", {"text": reason_delta})
                        sse_event = "message"
                        continue
                    last_payload = payload
                    reasoning_delta = _gateway_sse_reasoning_delta(payload)
                    if reasoning_delta:
                        if stream_id in STREAM_REASONING_TEXT:
                            STREAM_REASONING_TEXT[stream_id] += reasoning_delta
                        put_gateway_event("reasoning", {"text": reasoning_delta})
                    delta = _gateway_sse_delta(payload)
                    if delta:
                        final_text += delta
                        if stream_id in STREAM_PARTIAL_TEXT:
                            STREAM_PARTIAL_TEXT[stream_id] += delta
                        put_gateway_event("token", {"text": delta})
                    usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
            usage.update({k: v for k, v in _gateway_stream_usage(last_payload).items() if v})
        assistant_text = final_text.strip()
        if not assistant_text:
            put_gateway_event("apperror", {
                "label": "Gateway returned no response",
                "type": "gateway_empty_response",
                "message": "Gateway returned no assistant message for this turn.",
                "hint": "Check that Hermes Gateway API server is running and reachable.",
            })
            return
        with _get_session_agent_lock(session_id):
            s = get_session(session_id)
            if not _stream_writeback_is_current(s, stream_id):
                return
            now = time.time()
            # Preserve subsecond ordering for gateway-backed turns. Using an
            # integer seconds timestamp gives the user and assistant rows the
            # same sort key; later transcript merges can then fall back to
            # role/content ordering instead of turn order.
            assistant_ts = now + 0.000001
            user_msg = {"role": "user", "content": str(msg_text or ""), "timestamp": now}
            if attachments:
                user_msg["attachments"] = list(attachments)
            assistant_msg = {"role": "assistant", "content": assistant_text, "timestamp": assistant_ts}
            saved_reasoning = STREAM_REASONING_TEXT.get(stream_id, "")
            if saved_reasoning:
                assistant_msg["reasoning"] = saved_reasoning
            previous_context = list(getattr(s, "context_messages", None) or getattr(s, "messages", None) or [])
            s.context_messages = previous_context + [user_msg, assistant_msg]
            try:
                from api.streaming import _is_context_compression_marker

                display_context = [
                    msg
                    for msg in previous_context
                    if not _is_context_compression_marker(msg)
                ]
            except Exception:
                logger.debug("Failed to filter gateway display context markers", exc_info=True)
                display_context = previous_context
            display = merge_session_messages_append_only(
                list(getattr(s, "messages", None) or []),
                display_context,
            )
            try:
                from api.streaming import _merge_display_messages_after_agent_result

                s.messages = _merge_display_messages_after_agent_result(
                    display,
                    previous_context,
                    s.context_messages,
                    str(msg_text or ""),
                )
            except Exception:
                logger.debug("Failed to merge gateway display transcript", exc_info=True)
                # Avoid duplicating the eager-save checkpointed user message.
                if display:
                    latest = display[-1]
                    if isinstance(latest, dict) and latest.get("role") == "user":
                        latest_text = " ".join(str(latest.get("content") or "").split())
                        msg_norm = " ".join(str(msg_text or "").split())
                        if latest_text == msg_norm:
                            display = display[:-1]
                s.messages = display + [user_msg, assistant_msg]
            s.active_stream_id = None
            s.pending_user_message = None
            s.pending_attachments = None
            s.pending_started_at = None
            s.workspace = str(workspace)
            s.model = model
            s.model_provider = model_provider
            s.save()
        from api.streaming import _session_payload_with_full_messages
        gateway_session_payload = _session_payload_with_full_messages(s, tool_calls=[])
        put_gateway_event("done", {"session": redact_session_data(gateway_session_payload), "usage": usage})
        put_gateway_event("stream_end", {"session_id": session_id})
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        put_gateway_event(
            "apperror",
            _gateway_http_error_event(exc, err_body, api_key_configured=bool(_gateway_api_key())),
        )
    except Exception as exc:
        safe = _redact_text(str(exc))[:500]
        put_gateway_event("apperror", {
            "label": "Gateway request failed",
            "type": "gateway_error",
            "message": safe or "Gateway request failed.",
            "hint": "Check HERMES_WEBUI_GATEWAY_BASE_URL and Gateway API server health.",
        })
    finally:
        if s is not None:
            try:
                with _get_session_agent_lock(session_id):
                    _clear_gateway_pending_state(get_session(session_id), stream_id)
            except Exception:
                logger.debug("Failed to clear gateway stream state", exc_info=True)
        with STREAMS_LOCK:
            CANCEL_FLAGS.pop(stream_id, None)
            STREAM_PARTIAL_TEXT.pop(stream_id, None)
            STREAM_REASONING_TEXT.pop(stream_id, None)
            STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)
            STREAM_LAST_EVENT_ID.pop(stream_id, None)
            STREAMS.pop(stream_id, None)
        _STREAM_RUN_IDS.pop(stream_id, None)
        unregister_active_run(stream_id)
