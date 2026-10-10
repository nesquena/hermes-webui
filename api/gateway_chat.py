"""Default-off Hermes Gateway bridge for browser-originated chat turns."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from api.config import (
    AGENT_INSTANCES,
    CANCEL_FLAGS,
    PENDING_GOAL_CONTINUATION,
    STREAM_GOAL_RELATED,
    STREAMS,
    STREAMS_LOCK,
    STREAM_LAST_EVENT_ID,
    STREAM_LIVE_TOOL_CALLS,
    STREAM_PARTIAL_TEXT,
    STREAM_REASONING_TEXT,
    _get_session_agent_lock,
    _parse_provider_qualified_model_id,
    clear_session_writeback_owner_if_owned,
    coerce_reasoning_effort_for_model,
    gateway_approval_unavailable_reason,
    gateway_supports_approval,
    peek_stream,
    register_active_run,
    release_stream_owned_registries,
    unregister_active_run,
    unregister_active_run_if_owned,
    unregister_stream_owner,
    update_active_run,
)
from api.helpers import _redact_text, redact_session_data
from api.models import clear_process_wakeup_pause, get_session, merge_session_messages_append_only
from api.run_journal import RunJournalWriter, bound_run_journal_snapshot_args
from api.turn_journal import append_turn_journal_event_for_stream

logger = logging.getLogger(__name__)

# Maps stream_id -> gateway run_id for approval response relay.
_STREAM_RUN_IDS: dict[str, str] = {}
_STREAM_RUN_LIFECYCLE: dict[str, dict[str, Any]] = {}
_STREAM_RUN_STARTING_CONDITION = threading.Condition()
GATEWAY_RUN_ID_WAIT_TIMEOUT = 5.0
# stream_id -> (base_url, api_key) of the Gateway the stream's run lives on, for Stop
# and approval replies; a reattached run's Gateway need not be the process profile's.
_STREAM_ENDPOINTS: dict[str, tuple[str, str]] = {}


def gateway_run_endpoint(run_id: str) -> tuple[str, str]:
    """URL and key of the Gateway that owns run_id; the process Gateway if no live stream holds it."""
    run_id = str(run_id or "").strip()
    with _STREAM_RUN_STARTING_CONDITION:
        for stream_id, mapped in list(_STREAM_RUN_IDS.items()):
            if run_id and mapped == run_id and stream_id in _STREAM_ENDPOINTS:
                return _STREAM_ENDPOINTS[stream_id]
    from api.config import get_config

    return _gateway_base_url(get_config()), _gateway_api_key()


def _mark_gateway_run_starting(stream_id: str) -> None:
    with _STREAM_RUN_STARTING_CONDITION:
        _STREAM_RUN_IDS.pop(stream_id, None)
        _STREAM_RUN_LIFECYCLE[stream_id] = {
            "phase": "pending",
            "run_id": "",
            "waiters": 0,
            "owner_done": False,
        }


def _publish_gateway_run_id(stream_id: str, run_id: str) -> None:
    with _STREAM_RUN_STARTING_CONDITION:
        _STREAM_RUN_IDS[stream_id] = run_id
        state = _STREAM_RUN_LIFECYCLE.get(stream_id) or {}
        _STREAM_RUN_LIFECYCLE[stream_id] = {
            "phase": "ready",
            "run_id": run_id,
            "waiters": int(state.get("waiters") or 0),
            "owner_done": bool(state.get("owner_done")),
        }
        _STREAM_RUN_STARTING_CONDITION.notify_all()


def _finish_gateway_run_starting(stream_id: str, *, result: str = "failed") -> None:
    with _STREAM_RUN_STARTING_CONDITION:
        state = _STREAM_RUN_LIFECYCLE.get(stream_id) or {}
        if str(state.get("phase") or "").strip().lower() == "ready":
            return
        _STREAM_RUN_IDS.pop(stream_id, None)
        _STREAM_RUN_LIFECYCLE[stream_id] = {
            "phase": "fallback" if result == "fallback" else "failed",
            "run_id": "",
            "waiters": int(state.get("waiters") or 0),
            "owner_done": bool(state.get("owner_done")),
        }
        _STREAM_RUN_STARTING_CONDITION.notify_all()


def _retire_gateway_run_starting_if_done(stream_id: str) -> bool:
    state = _STREAM_RUN_LIFECYCLE.get(stream_id)
    if not state:
        return False
    if int(state.get("waiters") or 0) > 0:
        return False
    if not bool(state.get("owner_done")):
        return False
    _STREAM_RUN_LIFECYCLE.pop(stream_id, None)
    _STREAM_RUN_IDS.pop(stream_id, None)
    return True


def _clear_gateway_run_starting(stream_id: str) -> None:
    with _STREAM_RUN_STARTING_CONDITION:
        state = _STREAM_RUN_LIFECYCLE.get(stream_id)
        if state:
            state["owner_done"] = True
        _retire_gateway_run_starting_if_done(stream_id)
        _STREAM_RUN_STARTING_CONDITION.notify_all()


def release_gateway_stream_state(stream_id: str, *, finish_pending: bool = True) -> None:
    """No-op-safe release of the Gateway-owned rows for ``stream_id``.

    THE release path for ``_STREAM_RUN_LIFECYCLE`` / ``_STREAM_RUN_IDS`` /
    ``_STREAM_ENDPOINTS``: the canonical Gateway worker teardown and the
    chat/start orphan recovery both call this, so a dead stream cannot leak a
    lifecycle row, a run-id mapping or an endpoint that no worker will ever free
    (#7302 re-gate).

    Uses the existing lifecycle/waiter protocol instead of popping rows raw: a
    request parked in ``wait_for_gateway_run_id`` is never stranded -- it wakes on
    the terminal phase, and retires the row itself on the way out
    (``_retire_gateway_run_starting_if_done``), which is why the row can be left
    in place while ``waiters`` is non-zero.

    ``finish_pending`` publishes the terminal phase for a run that never reached
    ``ready``. It is a pure no-op for a stream that never touched the Gateway
    (no lifecycle row, no run id, no endpoint).
    """
    stream_id = str(stream_id or "").strip()
    if not stream_id:
        return
    if finish_pending and gateway_run_id_pending(stream_id):
        _finish_gateway_run_starting(stream_id)
    _clear_gateway_run_starting(stream_id)
    with _STREAM_RUN_STARTING_CONDITION:
        _STREAM_ENDPOINTS.pop(stream_id, None)


def gateway_run_id_pending(stream_id: str) -> bool:
    with _STREAM_RUN_STARTING_CONDITION:
        return str((_STREAM_RUN_LIFECYCLE.get(stream_id) or {}).get("phase") or "").strip().lower() == "pending"


def wait_for_gateway_run_id(stream_id: str, timeout: float) -> tuple[bool, str | None]:
    deadline = time.monotonic() + max(0.0, float(timeout))
    with _STREAM_RUN_STARTING_CONDITION:
        state = _STREAM_RUN_LIFECYCLE.get(stream_id)
        if state:
            state["waiters"] = int(state.get("waiters") or 0) + 1
        try:
            while True:
                state = _STREAM_RUN_LIFECYCLE.get(stream_id)
                phase = str((state or {}).get("phase") or "").strip().lower()
                if phase == "fallback":
                    return False, None
                if phase == "failed":
                    return True, None
                run_id = str(_STREAM_RUN_IDS.get(stream_id) or "").strip()
                if phase == "ready":
                    stored_run_id = str((state or {}).get("run_id") or "").strip()
                    return True, run_id or stored_run_id or None
                if run_id:
                    return True, run_id
                if not state:
                    return False, None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True, None
                _STREAM_RUN_STARTING_CONDITION.wait(timeout=remaining)
        finally:
            state = _STREAM_RUN_LIFECYCLE.get(stream_id)
            if state:
                waiters = max(0, int(state.get("waiters") or 0) - 1)
                state["waiters"] = waiters
                if _retire_gateway_run_starting_if_done(stream_id):
                    _STREAM_RUN_STARTING_CONDITION.notify_all()

_WEBUI_CHAT_BACKEND_ENV = "HERMES_WEBUI_CHAT_BACKEND"
_WEBUI_GATEWAY_BASE_URL_ENV = "HERMES_WEBUI_GATEWAY_BASE_URL"
_WEBUI_GATEWAY_API_KEY_ENV = "HERMES_WEBUI_GATEWAY_API_KEY"
_WEBUI_GATEWAY_USE_RUNS_API_ENV = "HERMES_WEBUI_GATEWAY_USE_RUNS_API"
_GATEWAY_CHAT_BACKENDS = {"gateway", "api_server", "api-server"}
# Backend tag of the in-process WebUI runtime. Local workers register their
# active run with it; cache-only Steer only enqueues on this explicit value.
WEBUI_LOCAL_CHAT_BACKEND = "legacy"


def _gateway_model_field(model: str | None) -> str:
    """Return the bare model name to put in a gateway request body.

    The picker and ``_resolve_compatible_session_model_state`` intentionally
    keep the full ``@provider:model`` string for internal routing (#1253), but
    the gateway expects a bare model name and carries the provider separately.
    Sent verbatim, ``@ollama-cloud:minimax-m2.7`` is forwarded to the upstream
    provider API, which 404s on the ``@``-prefixed string (#6722).

    Parsing is delegated to ``config._parse_provider_qualified_model_id()`` so
    a multi-segment custom provider ID (``@custom:backup:model-a``) yields the
    real model (``model-a``) instead of a positional-split fragment.
    """
    if not model:
        return ""
    value = str(model).strip()
    parsed = _parse_provider_qualified_model_id(value)
    if parsed:
        return str(parsed[0] or "").strip()
    return value


# Total byte-silence budget (seconds) for the gateway SSE socket, applied via
# ``urlopen(timeout=...)``. A stream that emits ANY byte within the window never
# trips it, so a genuinely alive (if slow) token stream is untouched; only more
# than this much *total* byte-silence is treated as a dead/stalled gateway.
#
# This is a TERMINAL budget, not a per-read grace: CPython's ``socket.makefile``
# latches ``_timeout_occurred`` on the first ``socket.timeout``, after which every
# further read raises a bare ``OSError`` — the connection cannot be resumed. So a
# read timeout ends the turn (surfacing Stop if pressed). It replaces the old flat
# 600s timeout, under which a half-open gateway (TCP open, zero bytes) pinned the
# worker for the full 10 minutes and ignored Stop (cancel is only re-checked
# between SSE lines). We KEEP the 600s default budget: there is no Gateway
# protocol heartbeat guaranteeing sub-600s progress bytes, so a legitimately
# long/fully-silent server-side tool call must not be terminated early — reducing
# the default below 600s would kill currently-working turns (gate finding, #5789).
# The win here is that a read timeout is now TERMINAL and Stop-honoring (the old
# flat timeout ignored Stop on a half-open gateway); the budget itself stays 600s
# for backward compatibility. Deployments that want a tighter dead-gateway cap can
# lower ``HERMES_WEBUI_GATEWAY_READ_TIMEOUT``.
_GATEWAY_READ_TIMEOUT_ENV = "HERMES_WEBUI_GATEWAY_READ_TIMEOUT"
_GATEWAY_READ_TIMEOUT_DEFAULT = 600.0


def _gateway_read_timeout_secs() -> float:
    """Total byte-silence budget for gateway SSE reads (default 600s, env-tunable)."""
    raw = os.environ.get(_GATEWAY_READ_TIMEOUT_ENV)
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return _GATEWAY_READ_TIMEOUT_DEFAULT


def _iter_sse_lines_cancellable(resp, cancel_event):
    """Yield raw SSE lines from ``resp``, unblocking cleanly on a read timeout.

    ``resp``'s socket carries a read timeout (``urlopen(timeout=...)``). A read
    that blocks past it raises ``socket.timeout``, and that timeout is TERMINAL:
    CPython's ``socket.makefile`` latches ``_timeout_occurred`` on the first
    timeout, so every subsequent read raises a bare ``OSError`` ("cannot read
    from timed out object") — there is no multi-read grace to reclaim. So on a
    read timeout (or the poisoned-socket ``OSError``/any read error) this either
    surfaces the user's Stop or tears the stalled turn down:

      - cancel set -> yield ``b""`` (the caller's ``if cancel_event.is_set()``
        branch emits its cancel event), then stop. This is why the old flat 600s
        pin — where a stalled gateway ignored Stop until it eventually errored —
        is gone: Stop is honored within one timeout window.
      - otherwise -> re-raise, so the caller's error handling reports the stall.

    A stream that keeps emitting bytes within the timeout window never trips it,
    so a genuinely alive (if slow) token stream is untouched. Emitting ``b""`` is
    safe: the SSE loops decode it to an empty line and ``continue`` (same as a
    real blank line).

    Iterates ``resp`` via the iterator protocol so a real ``HTTPResponse`` and the
    test fakes (which implement ``__iter__``) behave identically.
    """
    resp_iter = iter(resp)
    while True:
        try:
            raw_line = next(resp_iter)
        except StopIteration:
            return  # EOF
        except OSError:
            # socket.timeout / TimeoutError are OSError subclasses, as is the
            # post-timeout poisoned-socket "cannot read" error. All are terminal
            # for this connection.
            if cancel_event.is_set():
                yield b""  # let the caller emit its cancel event
                return
            raise
        yield raw_line


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
    return WEBUI_LOCAL_CHAT_BACKEND


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


def _gateway_reasoning_effort_for_request(cfg, *, model=None, model_provider=None):
    """Read and coerce user-configured reasoning effort for a gateway request."""
    try:
        cfg_data = cfg if isinstance(cfg, dict) else {}
        effort_cfg = cfg_data.get("agent", {}) if isinstance(cfg_data, dict) else {}
        effort_raw = effort_cfg.get("reasoning_effort") if isinstance(effort_cfg, dict) else None
        coerced = coerce_reasoning_effort_for_model(
            effort_raw,
            model,
            provider_id=model_provider,
        )
        # Preserve explicit "none" while still omitting absent or invalid effort.
        return None if not coerced else str(coerced)
    except Exception:
        return None


def _gateway_session_yolo_enabled(session_id: str) -> bool:
    """Return the WebUI-owned, in-memory YOLO state for a browser session."""
    try:
        from tools.approval import is_session_yolo_enabled

        return bool(is_session_yolo_enabled(str(session_id or "")))
    except Exception:
        return False


def _settle_gateway_run_approval(
    session_id: str,
    approval_data: dict,
    base_url: str,
    api_key: str,
) -> tuple[bool, dict | None, int]:
    """Auto-approve or mirror one run approval at a session-linearized point."""
    from api.route_approvals import gateway_yolo_handoff, submit_gateway_pending_mirror

    run_id = str(approval_data.get("run_id") or "").strip()
    identity_v1 = bool(approval_data.get("_gateway_agent_identity_v1"))
    with gateway_yolo_handoff(session_id):
        if _gateway_session_yolo_enabled(session_id):
            try:
                _auto_approve_gateway_run(
                    base_url,
                    api_key,
                    run_id,
                    approval_data["approval_id"] if identity_v1 else "",
                )
                return True, None, 0
            except Exception:
                # Fail closed: if remote approval fails, surface the real card
                # before allowing a same-session toggle to pass the handoff.
                logger.warning(
                    "WebUI YOLO could not auto-approve run %s; showing approval card",
                    run_id,
                    exc_info=True,
                )
        head, total = submit_gateway_pending_mirror(session_id, approval_data)
        return False, head, total


def _auto_approve_gateway_run(
    base_url: str,
    api_key: str,
    run_id: str,
    approval_id: str,
) -> None:
    """Resolve one Runs API prompt using only the shipped approval contract.

    This is a WebUI-owned compatibility path: the Runs API does not yet expose
    session YOLO, so WebUI answers each approval request while its own session
    flag is enabled. Native Agent-side YOLO would be preferable because it can
    bypass gates before they pause and also covers Agent-owned computer-use
    policy; https://github.com/NousResearch/hermes-agent/pull/61946 tracks that
    API capability. Until then, do not send speculative fields to the Agent.
    """
    from api.runner_client import HttpRunnerClient

    HttpRunnerClient(base_url=base_url, api_key=api_key).respond_approval(
        run_id,
        approval_id,
        "once",
    )


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
        "args": bound_run_journal_snapshot_args(payload.get("args"))
        if isinstance(payload.get("args"), dict)
        else {},
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
    raw_approval_id = str(payload.get("approval_id") or payload.get("id") or "").strip()
    approval_id = raw_approval_id
    if not approval_id:
        approval_id = uuid.uuid4().hex
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
        "_gateway_raw_approval_id_present": bool(raw_approval_id),
        "choices": choices,
        "allow_permanent": bool(allow_permanent),
    }


def _gateway_approval_key(payload) -> str:
    """Stable id for one gateway approval, shared by the status probe and the event relay."""
    return str(payload.get("approval_id") or payload.get("id") or payload.get("timestamp") or "")


def _relay_gateway_run_approval(session_id, run_id, payload, base_url, api_key, *, put_gateway_event) -> bool:
    """Auto-approve or surface one runs-API approval request as a WebUI approval card.

    Returns True when the payload was relayed (settled or surfaced as a
    card) and False when the translator rejected it — callers that register
    dedupe keys use that to avoid masking a later, well-formed replay of
    the same approval (round-4 maintainer minor)."""
    approval_data = _gateway_runs_approval_event(payload)
    if not approval_data:
        return False
    approval_data["run_id"] = run_id
    from api.config import gateway_supports_approval_identity_v1
    identity_v1 = bool(approval_data.get("_gateway_raw_approval_id_present")) and gateway_supports_approval_identity_v1(base_url, api_key)
    approval_data["_gateway_agent_identity_v1"] = identity_v1
    auto_approved, head, total = _settle_gateway_run_approval(
        session_id,
        approval_data,
        base_url,
        api_key,
    )
    if not auto_approved:
        put_gateway_event("approval", {**(head or approval_data), "pending_count": total})
    return True


def _seed_gateway_stream_text(stream_id: str) -> str:
    """Seed a relay connection's local text carrier from the stream's shared partial buffer.

    Round-4 review (Greptile item 1): this is the SEED arm of the single
    seed/append/adopt trio that keeps the two answer-text carriers — the
    relay's local ``final_text`` and the shared ``STREAM_PARTIAL_TEXT``
    writeback buffer — in lockstep at every helper boundary, so a mid-stream
    reconnect always resumes from exactly what the browser has seen.
    """
    return STREAM_PARTIAL_TEXT.get(stream_id, "")


def _append_gateway_stream_text(stream_id: str, text: str, delta: str) -> str:
    """Append one delta to BOTH answer-text carriers together; return the new local carrier.

    Round-4 review (Greptile item 1): append arm of the seed/append/adopt trio
    — the relay's local ``final_text`` and ``STREAM_PARTIAL_TEXT`` move in one
    step and cannot diverge mid-stream.
    """
    text += delta
    if stream_id in STREAM_PARTIAL_TEXT:
        STREAM_PARTIAL_TEXT[stream_id] += delta
    return text


def _adopt_gateway_stream_text(stream_id: str, text: str) -> str:
    """Adopt an authoritative answer into BOTH carriers; return the adopted text.

    Round-4 review (Greptile item 1): adopt arm of the seed/append/adopt trio
    — ``run.completed``/durable-status outputs overwrite both carriers in one
    step so the UI writeback always matches the settled turn text.
    """
    if stream_id in STREAM_PARTIAL_TEXT:
        STREAM_PARTIAL_TEXT[stream_id] = text
    return text


def _note_live_gateway_event(stream_id: str, event_name: str, event_payload: dict) -> None:
    """Mirror a relayed reasoning/tool event into the per-stream reconnect snapshot."""
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


def _open_gateway_run_events(base_url, headers, run_id, last_seq: int = -1, read_timeout_secs: float | None = None):
    headers_sse = {**headers, "Accept": "text/event-stream"}
    if last_seq >= 0:
        headers_sse["Last-Event-ID"] = str(last_seq)
    # Round-4 review (Greptile item 3): the wall-clock stall check only runs
    # when a line ARRIVES, so a socket that goes byte-SILENT blocks inside the
    # read for the full configured read timeout — 5x past the watchdog budget
    # — before the stall check or Stop handling can run. The streaming caller
    # bounds this read by the watchdog budget (+ small epsilon); the reattach
    # poller passes nothing and keeps the configured timeout.
    timeout = (
        min(_gateway_read_timeout_secs(), read_timeout_secs)
        if read_timeout_secs
        else _gateway_read_timeout_secs()
    )
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/runs/{urllib.parse.quote(run_id, safe='')}/events",
        headers=headers_sse,
        method="GET",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def _relay_gateway_run_events(
    resp, session_id, stream_id, run_id, base_url, api_key,
    *, put_gateway_event, cancel_event, on_seq=None, final_text="", stop_on_truncated=False,
    surfaced_approval_ids=None, output_is_authoritative=False, approval_is_current=None,
    watchdog_secs=None,
):
    """Relay one /v1/runs/{id}/events stream.

    Returns ``(text or None if cancelled, usage, outcome, accepted_frame)``,
    where ``accepted_frame`` is True when THIS connection accepted and
    relayed any real frame (delta, reasoning, tool, approval — some accepted
    frames carry no seq, so a cursor compare alone misses them); the
    streaming caller uses it to reset the clean-EOF reconnect backoff
    (round-7 review: cumulative text cannot drive the reset, because the
    relay is seeded from the turn-wide buffer).


    outcome is "ended", "eof", "truncated" (the gateway dropped events after our cursor;
    only returned with ``stop_on_truncated``, otherwise the retained events keep relaying),
    or "stalled" (only with ``watchdog_secs``). Relayed payloads carry ``gateway_seq`` so
    the WebUI run journal records the gateway cursor; ``on_seq`` commits each seq as soon
    as its event is relayed, so a reconnect never re-emits it.
    ``surfaced_approval_ids`` is shared with the reattach status probe so one approval surfaces once.
    ``output_is_authoritative`` lets ``run.completed.output`` replace streamed text (reattach: the
    Agent may transform its answer after streaming). ``approval_is_current`` drops replayed approvals
    the Gateway no longer has pending. ``watchdog_secs`` (streaming watchdog only) treats a
    connection that delivers nothing but comment/keepalive frames for that long as stalled and
    returns so the caller can consult the durable run status: keepalives are liveness, not
    progress (this exact case pinned a single-connection loop forever — #7978). Once
    ``run.completed`` is seen the outcome latches to "ended": the relay breaks
    out on the frame itself (trailing keepalives/[DONE] are post-terminal
    noise), and as a backstop any keepalive-only interval that slips past the
    latch finishes the turn as "ended" instead of probing (round-4 review: the
    frame is terminal; Greptile round-3: the latch must leave an exit).
    """
    usage: dict = {}
    outcome = "eof"
    seq = None
    sse_event = "message"
    # True when this connection accepted and relayed any real frame (delta,
    # reasoning, tool, approval). Round-7 review: the streaming caller's
    # clean-EOF backoff reset must key on per-connection progress, not on
    # the cumulative turn text (which seeds every reconnect non-empty).
    accepted_frame = False
    # Keepalives are liveness, not progress: a stream that emits nothing but
    # comment frames past watchdog_secs is treated as stalled even though the
    # socket read never blocks past its timeout.
    last_progress = time.monotonic() if watchdog_secs is not None else None
    # Round-4 review (Greptile item 2): run.completed is terminal. Once the
    # frame is seen the outcome latches to "ended" for this connection — a
    # subsequent keepalive-only interval must not flip an already-terminal
    # lane back to "stalled" (which would force a status probe and could FAIL
    # an already-completed turn when the probe is unavailable).
    terminal_frame_seen = False

    def emit(event_name, data):
        if seq is not None and isinstance(data, dict):
            data = {**data, "gateway_seq": seq}
        put_gateway_event(event_name, data)

    def commit():
        nonlocal seq
        if seq is not None and on_seq is not None:
            on_seq(seq)
        seq = None

    for raw_line in _iter_sse_lines_cancellable(resp, cancel_event):
        # Stop wins over stall classification: a Stop landing during a silent
        # read (surfaced by the read timeout as a blank line) cancels without
        # burning a status-probe round trip first.
        if cancel_event.is_set():
            put_gateway_event("cancel", {"message": "Cancelled by user"})
            return None, usage, "ended", False
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            # Blank / "event:" / "id:" / comment-keepalive frames carry no
            # payload. The wall-clock stall budget is checked HERE — after
            # classification (round-4 maintainer nit) — so protocol noise
            # still trips the watchdog (keepalives are liveness, not
            # progress) while a REAL data frame arriving after a silent gap
            # is processed as progress below instead of being discarded and
            # re-fetched via replay. Once run.completed has been seen the
            # outcome is latched (round-4 item 2): a keepalive interval past
            # the frame cannot flip the lane to "stalled" — post-terminal
            # stall means the stream refused to close after a terminal
            # frame, and the turn is already complete, so it finishes with
            # the relayed text instead of probing/failing (Greptile round-3:
            # the round-4 latch must not leave the loop without an exit).
            if watchdog_secs is not None and time.monotonic() - last_progress > watchdog_secs:
                if terminal_frame_seen:
                    outcome = "ended"
                    break
                logger.warning(
                    "Gateway events stream for run %s stalled "
                    "(no real events past %ss); probing run status",
                    run_id, watchdog_secs)
                outcome = "stalled"
                break
            if not line:
                sse_event = "message"
            elif line.startswith("event:"):
                sse_event = line[6:].strip() or "message"
            continue
        if watchdog_secs is not None:
            # A real (non-comment) frame: this is stream progress.
            last_progress = time.monotonic()
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        payload_event = str(payload.get("event") or payload.get("type") or sse_event).strip() or "message"
        seq = payload.get("seq") if isinstance(payload.get("seq"), int) else None
        # Any parsed, non-replay-control frame counts as per-connection
        # progress for the clean-EOF backoff (round-7 review): delta,
        # reasoning, tool, approval and terminal frames all qualify — some
        # carry no seq, so this is deliberately not a cursor compare.
        if payload_event != "replay.truncated":
            accepted_frame = True
        if payload_event == "replay.truncated":
            if stop_on_truncated:
                return final_text, usage, "truncated", False
            logger.info("Gateway replay for run %s truncated; relaying retained events", run_id)
            sse_event = "message"
            continue
        if payload_event == "approval.request":
            approval_key = _gateway_approval_key(payload)
            already = surfaced_approval_ids is not None and approval_key in surfaced_approval_ids
            if not already and (approval_is_current is None or approval_is_current(payload)):
                if surfaced_approval_ids is not None and approval_key:
                    surfaced_approval_ids.add(approval_key)
                _relay_gateway_run_approval(
                    session_id, run_id, payload, base_url, api_key,
                    put_gateway_event=emit,
                )
            commit()
            sse_event = "message"
            continue
        if payload_event in {"tool.started", "tool.completed", "reasoning.available"}:
            translated = _gateway_tool_progress_event(payload)
            if translated:
                event_name, event_payload = translated
                _note_live_gateway_event(stream_id, event_name, event_payload)
                emit(event_name, event_payload)
                if event_name != "reasoning":
                    update_active_run(stream_id, phase="gateway-tool", latest_tool=event_payload.get("name"))
            commit()
            sse_event = "message"
            continue
        if payload_event == "message.delta":
            delta = str(payload.get("delta") or "")
            if delta:
                final_text = _append_gateway_stream_text(stream_id, final_text, delta)
                emit("token", {"text": delta})
            commit()
            sse_event = "message"
            continue
        if payload_event == "run.completed":
            from api.route_approvals import settle_gateway_pending_run
            settle_gateway_pending_run(
                session_id,
                run_id,
                reason="Gateway run completed before approval resolution",
            )
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            output = str(payload.get("output") or "")
            if output and (output_is_authoritative or not final_text):
                final_text = _adopt_gateway_stream_text(stream_id, output)
            usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
            outcome = "ended"
            terminal_frame_seen = True
            commit()
            # A terminal frame definitionally ends the run: break instead of
            # reading on (Greptile round-3). Trailing frames on a healthy
            # gateway — including [DONE] — are post-terminal noise, and
            # `with resp:` closes the socket on the way out. Continuing here
            # stranded the completed turn inside the relay: the round-4 latch
            # disables the stall check, so endless keepalives pinned the loop
            # forever. (The watchdog branch above keeps an exit as a backstop
            # for any future path that re-enters the read loop latched.)
            break
        if payload_event == "run.failed":
            from api.route_approvals import settle_gateway_pending_run
            settle_gateway_pending_run(
                session_id,
                run_id,
                reason="Gateway run failed before approval resolution",
            )
            raise RuntimeError(str(payload.get("error") or "Gateway run failed"))
        if payload_event == "run.cancelled":
            from api.route_approvals import settle_gateway_pending_run
            settle_gateway_pending_run(
                session_id,
                run_id,
                reason="Gateway run was cancelled before approval resolution",
            )
            put_gateway_event("cancel", {"message": "Cancelled by gateway"})
            return None, usage, "ended", False
        reasoning_delta = _gateway_sse_reasoning_delta(payload)
        if reasoning_delta:
            if stream_id in STREAM_REASONING_TEXT:
                STREAM_REASONING_TEXT[stream_id] += reasoning_delta
            emit("reasoning", {"text": reasoning_delta})
        delta = _gateway_sse_delta(payload)
        if delta:
            final_text = _append_gateway_stream_text(stream_id, final_text, delta)
            emit("token", {"text": delta})
        usage.update({k: v for k, v in _gateway_stream_usage(payload).items() if v})
        commit()
    commit()
    return final_text, usage, outcome, accepted_frame


def _admit_gateway_run(url_runs, headers, run_body, stream_id) -> str:
    """POST /v1/runs; the same stream and body return the originally admitted run id."""
    req = urllib.request.Request(
        url_runs,
        data=json.dumps(run_body).encode("utf-8"),
        # Durable run record on the gateway: GET /v1/runs/{id} survives either side restarting.
        headers={**headers, "Idempotency-Key": f"webui-{stream_id}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        run_data = json.loads(resp.read(65536))
    run_id = str(run_data.get("run_id") or run_data.get("id") or "").strip()
    if not run_id:
        raise ValueError(f"Gateway runs API returned no run_id: {run_data!r}")
    return run_id


def _gateway_run_headers(session_id, api_key) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Hermes-Session-Id": session_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["X-Hermes-Session-Key"] = f"webui:{session_id}"
    return headers


def _run_gateway_runs_api_streaming(
    session_id, msg_text, model, workspace, stream_id,
    base_url, api_key, prefill_messages, body_extras,
    *, put_gateway_event, cancel_event,
    attachments=None, cfg=None, session=None,
    active_provider: str = "",
    on_run_id=None,
    on_seq=None,
):
    """Submit via POST /v1/runs and relay SSE events including approval."""
    try:
        url_runs = f"{base_url.rstrip('/')}/v1/runs"
        headers = _gateway_run_headers(session_id, api_key)
        message_content: Any = str(msg_text or "")
        if attachments:
            try:
                from api.streaming import _build_native_multimodal_message

                message_content = _build_native_multimodal_message("", str(msg_text or ""), attachments, str(workspace), cfg=cfg, active_provider=active_provider, active_model=(model or ""), requested_provider=active_provider, profile=getattr(session, "profile", None))
            except Exception:
                logger.debug("Failed to build runs-API multimodal attachment payload", exc_info=True)
                message_content = str(msg_text or "")
        from api.streaming import (
            _is_non_replayable_history_row,
            _is_reasoning_only_assistant_message,
            _recovered_user_row_is_kept,
            _strip_oob_blocks,
        )

        instructions_parts = []
        conversation_history = []
        # (role, content, recovered) for each session row that may be sent.
        history_rows = []
        for entry in getattr(session, "context_messages", None) or []:
            if not isinstance(entry, dict):
                continue
            # The same rows the legacy path drops: error markers, empty partials
            # and reasoning-only assistant rows.
            if _is_non_replayable_history_row(entry) or _is_reasoning_only_assistant_message(entry):
                continue
            role = str(entry.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            content = entry.get("content")
            if content is not None:
                content = _strip_oob_blocks(content)
                history_rows.append((role, content, role == "user" and bool(entry.get("_recovered"))))
        # A _recovered user row is sent only where it opens an answered turn
        # (after an assistant turn or as the first row sent), as the legacy path decides it.
        for index, (role, content, recovered) in enumerate(history_rows):
            if recovered:
                prev_role = conversation_history[-1]["role"] if conversation_history else None
                next_role = history_rows[index + 1][0] if index + 1 < len(history_rows) else None
                if not _recovered_user_row_is_kept(prev_role, next_role):
                    continue
            conversation_history.append({"role": role, "content": content})
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
            if content is not None:
                content = _strip_oob_blocks(content)
            conversation_history.append({"role": role, "content": content})
        run_input = message_content
        if isinstance(run_input, list):
            run_input = [{"role": "user", "content": run_input}]
        run_body = {
            "model": _gateway_model_field(model) or "default",
            "input": run_input,
            **body_extras,
            "session_id": session_id,
        }
        if instructions_parts:
            run_body["instructions"] = "\n\n".join(part for part in instructions_parts if part)
        if conversation_history:
            run_body["conversation_history"] = conversation_history
        update_active_run(stream_id, phase="gateway-request")
        # Persist the exact body first: a restart before the run id is saved replays this admission.
        if on_run_id is not None:
            on_run_id("", request=run_body)
        run_id = _admit_gateway_run(url_runs, headers, run_body, stream_id)
    except Exception:
        _finish_gateway_run_starting(stream_id)
        raise

    _publish_gateway_run_id(stream_id, run_id)
    if on_run_id is not None:
        on_run_id(run_id)

    # Watchdog consume loop (#7978, ported onto the relay-helper architecture):
    # the browser-facing turn must never outlive its gateway run. A single SSE
    # connection is fragile — a lost terminal frame (run.completed / close race)
    # left a keepalive-only stream pinned forever. Each connection now carries a
    # short no-real-events budget; when it trips (or the socket EOFs/drops)
    # without a terminal frame, the pollable run status is the source of truth:
    #   terminal   -> finalize from what already streamed (Fix 1)
    #   cancelled  -> surface cancellation (buffers persisted by the caller's
    #                 cancelled-turn settle)
    #   still live -> reconnect with Last-Event-ID and keep relaying
    # Probe failures share the reattach poller's budget
    # (GATEWAY_REATTACH_MAX_POLL_FAILURES / GATEWAY_REATTACH_POLL_INTERVAL) so a
    # transient status blip cannot kill a run that is alive and streaming
    # (Fix 2); Stop is honoured between attempts and the events stream keeps
    # reconnecting between probe rounds. A durable-status 404 is exempt from
    # that budget: it is the gateway's definitive "no record of this run"
    # (unlike a transport error or 5xx, where the run may still be alive), so
    # it must not spin the turn for ~5 minutes (round-2 review).
    _WATCHDOG_SECS = _GATEWAY_WATCHDOG_SECS
    from api.route_approvals import settle_gateway_pending_run
    last_seq = [-1]
    status_404_streak = 0
    probe_failures = 0
    final_text = ""
    usage: dict = {}
    # Round-4 review (Greptile item 4): shared with the events relay so one
    # approval surfaces exactly once across reconnects, status re-probes, and
    # both lanes (same contract as the reattach status probe).
    surfaced_approval_ids: set[str] = set()

    def _settle_by_status(status: dict):
        """Decide the turn from the durable run status (Fix 1: the only success
        arbiter). Returns ``(action, value)`` with action in
        ``{"continue", "cancel", "raise", "success"}``; ``"success"`` carries
        the usage delta and has already adopted the final answer per Fix 3."""
        nonlocal final_text
        state = str(status.get("status") or "").strip().lower()
        if state not in _GATEWAY_RUN_TERMINAL_STATUSES:
            return "continue", None
        settle_gateway_pending_run(
            session_id,
            run_id,
            reason=f"Gateway run {state} before approval resolution",
        )
        if state in ("cancelled", "interrupted"):
            return "cancel", None
        if state != "completed":
            return "raise", RuntimeError(str(status.get("error") or f"Gateway run {state}"))
        output = str(status.get("output") or "")
        if not output:
            # Round-4 review (Greptile item 1): the settle path is
            # deterministic — final answer = status output when non-empty,
            # ELSE the accumulated partial text (either carrier), never ""
            # when partial text exists.
            output = final_text or STREAM_PARTIAL_TEXT.get(stream_id, "")
        if output:
            # Fix 3: prefer the non-empty durable output over whatever the
            # stream carried; adopt into BOTH carriers so the UI writeback
            # matches the settled turn text.
            final_text = _adopt_gateway_stream_text(stream_id, output)
        return "success", {k: v for k, v in _gateway_stream_usage(status).items() if v}

    def _surface_status_approval(status: dict):
        """Round-4 review (Greptile item 4): mirror the reattach path's
        parked-approval surface — when the durable status is
        ``waiting_for_approval`` and carries the approval payload, surface the
        card from status (the events feed may have lost it or be unable to
        replay it), deduped by approval key so reconnects and re-probes never
        double-card. ``waiting_for_approval`` is not terminal, so the caller
        then continues the loop.

        Round-4 maintainer must-fix: the status payload carries only the
        gateway's LATEST pending approval. Mirroring it ahead of the
        cursor-ordered /events replay inverts the FIFO queue when several
        approvals are pending (a stall surfaces B, the replay then delivers
        A -> B, and the approved card can resolve a different command than
        the one shown). Exact-id recovery is trustworthy only when the
        payload carries a non-blank raw ``approval_id``/``id`` AND the
        gateway advertises ``approval_identity_v1``; without both, skip the
        status-card insert entirely so the replay alone orders the queue
        A -> B."""
        approval = status.get("approval")
        if (
            str(status.get("status") or "").strip().lower() == "waiting_for_approval"
            and isinstance(approval, dict)
        ):
            raw_approval_id = str(approval.get("approval_id") or approval.get("id") or "").strip()
            from api.config import gateway_supports_approval_identity_v1
            if not (raw_approval_id and gateway_supports_approval_identity_v1(base_url, api_key)):
                return
            approval_key = _gateway_approval_key(approval)
            if approval_key and approval_key not in surfaced_approval_ids:
                relayed = _relay_gateway_run_approval(
                    session_id, run_id, approval, base_url, api_key,
                    put_gateway_event=put_gateway_event,
                )
                # Register only after a successful relay: a payload the
                # translator rejects must not suppress the later event-feed
                # replay of the same approval (round-4 maintainer minor).
                if relayed:
                    surfaced_approval_ids.add(approval_key)

    clean_eof_reconnects = 0
    while True:
        if cancel_event.is_set():
            put_gateway_event("cancel", {"message": "Cancelled by user"})
            return None, usage
        resp = None
        outcome = "eof"
        events_unreachable = False
        stream_text = None
        accepted_frame = False
        try:
            resp = _open_gateway_run_events(
                base_url, headers, run_id, last_seq[0],
                # Round-4 review (Greptile item 3): bound the per-read wait by
                # the watchdog budget so a byte-silent socket surfaces within
                # ~budget and the stall/status/Stop logic can run.
                read_timeout_secs=_WATCHDOG_SECS + _GATEWAY_WATCHDOG_READ_EPSILON,
            )
        except urllib.error.HTTPError as exc:
            # Fix 1: the events stream is gone (404 after a restart) or the
            # endpoint refuses; the durable run status — not any already-
            # streamed text — decides the outcome. Fall through to the probe.
            logger.warning(
                "Gateway events stream for run %s connect failed (HTTP %s); probing durable run status",
                run_id, exc.code)
            events_unreachable = True
        except (urllib.error.URLError, OSError):
            events_unreachable = True  # connect failure: fall through to the status probe below
        if resp is not None:
            try:
                with resp:
                    stream_text, stream_usage, outcome, accepted_frame = _relay_gateway_run_events(
                        resp, session_id, stream_id, run_id, base_url, api_key,
                        put_gateway_event=put_gateway_event, cancel_event=cancel_event,
                        # Cursor commit: the internal reconnect cursor ALWAYS
                        # advances (the next /events reopen must send
                        # Last-Event-ID or delivered tokens replay into the
                        # answer — round-5 review), then the optional
                        # observer (harness/test seam) is notified.
                        on_seq=lambda event_seq: (
                            last_seq.__setitem__(0, event_seq),
                            on_seq(event_seq) if on_seq is not None else None,
                        ),
                        # Seed/accumulate/adopt all run through the shared
                        # trio so the local carrier and STREAM_PARTIAL_TEXT
                        # cannot diverge (round-4 Greptile item 1).
                        final_text=_seed_gateway_stream_text(stream_id),
                        # Round-2 Fix 3 for this lane: a non-empty
                        # run.completed/output replaces the streamed text
                        # (overwrite, not fill-if-empty).
                        output_is_authoritative=True,
                        surfaced_approval_ids=surfaced_approval_ids,
                        watchdog_secs=_WATCHDOG_SECS,
                    )
            except (urllib.error.URLError, OSError):
                # read timeout / reset mid-stream: fall through to the status
                # probe, and pace the reconnect below it
                events_unreachable = True
                # Maintainer must-fix (round-3 re-review): the relay died with
                # the exception, so its LOCAL final_text carrier is lost — the
                # streamed deltas survive only in the shared buffer it appended
                # to line by line. Re-sync before durable status arbitration;
                # the deterministic settle below then keeps (or adopts) the
                # accumulated text.
                final_text = STREAM_PARTIAL_TEXT.get(stream_id, final_text)
            else:
                final_text = stream_text
                usage.update({k: v for k, v in stream_usage.items() if v})
        if outcome == "ended":
            # Terminal frame relayed (or the user/gateway cancelled); the
            # caller settles the turn from the returned text.
            return final_text, usage
        # Per-connection progress (any real frame accepted on THIS
        # connection) resets the clean-EOF reconnect backoff. Round-7
        # review: this must NOT key on cumulative turn text — the relay is
        # seeded from the turn-wide buffer, so after the first delta every
        # clean EOF would return non-empty text and the backoff would
        # never double (a flat 0.5s forever, ~110 reconnects/minute).
        # A watchdog STALL is a connection that stayed live for the whole
        # budget (keepalives flowing), not a clean EOF the gateway slammed
        # shut: it must not accumulate the pacing either (round-8 review).
        if accepted_frame or outcome == "stalled":
            clean_eof_reconnects = 0
        # ---- durable status probe: the only success arbiter (Fix 1) ----
        # Round-3 review: the 404 grace re-probe lives HERE, inside status
        # arbitration — before any /events reopen. The first 404 is re-probed
        # immediately (no reconnect in between, no poll-interval sleep); a
        # second consecutive 404 fails the turn closed; any non-404 outcome is
        # authoritative through the settle table below and resets the streak.
        # Only a non-terminal arbitration reaches the loop top, which is what
        # reopens /events. Transport and 5xx probe failures keep the paced
        # reattach budget and reset the 404 streak (the run may still be
        # alive there, unlike after a definitive 404).
        status = None
        probe_paced = False
        for _grace_attempt in range(_STATUS_404_GRACE_PROBES):
            try:
                status = _get_gateway_run_status(base_url, api_key, run_id)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    # Credentials cannot self-heal; fail now (mirrors the reattach poller).
                    raise RuntimeError(
                        f"Gateway rejected the WebUI credentials (HTTP {exc.code}) while polling run {run_id}"
                    ) from exc
                if exc.code == 404:
                    # Terminal, not retryable (round-2 review): a durable-status
                    # 404 is the gateway's definitive "I have no record of this
                    # run", so it must not spend the long reattach budget that
                    # exists for transport errors and 5xx (where the run may
                    # still be alive). Only a small grace guards the status-
                    # before-registration race.
                    status_404_streak += 1
                    if status_404_streak >= _STATUS_404_GRACE_PROBES:
                        raise RuntimeError(
                            "Gateway no longer has the run; failing the turn rather "
                            "than settling partial streamed output") from exc
                    # First consecutive 404: immediately re-probe the status
                    # endpoint. No /events reopen, no poll-interval sleep;
                    # Stop (cancel_event) is honoured between the two probes.
                    if cancel_event.is_set():
                        put_gateway_event("cancel", {"message": "Cancelled by user"})
                        return None, usage
                    continue
                # Non-404 HTTP (5xx/blip): Fix 2 — a probe failure is not
                # proof the run is gone. Wait out the poll interval (Stop is
                # honoured between attempts) and reconnect the events stream
                # from the top of the loop.
                probe_failures += 1
                status_404_streak = 0
                if probe_failures >= GATEWAY_REATTACH_MAX_POLL_FAILURES:
                    raise RuntimeError(
                        "Gateway became unreachable while waiting for the run to finish") from exc
                cancel_event.wait(GATEWAY_REATTACH_POLL_INTERVAL)
                probe_paced = True
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                probe_failures += 1
                status_404_streak = 0
                if probe_failures >= GATEWAY_REATTACH_MAX_POLL_FAILURES:
                    raise RuntimeError(
                        "Gateway became unreachable while waiting for the run to finish") from exc
                cancel_event.wait(GATEWAY_REATTACH_POLL_INTERVAL)
                probe_paced = True
                break
            probe_failures = 0
            status_404_streak = 0
            break
        if probe_paced:
            continue
        # Round-4 review (Greptile item 4): surface a parked approval carried
        # by the durable status BEFORE arbitrating — waiting_for_approval is
        # not terminal, so the settle below returns "continue" and the loop
        # reconnects /events with the card already up.
        _surface_status_approval(status)
        action, value = _settle_by_status(status)
        if action == "continue":
            # Still running: reconnect and resume from the last seen event id,
            # pacing the reconnect when the events channel just failed so a
            # broken endpoint cannot hot-loop the probe.
            if events_unreachable:
                cancel_event.wait(GATEWAY_REATTACH_POLL_INTERVAL)
            else:
                # Round-5/6 review (maintainer, shipping gate for #8030): a
                # CLEAN EOF — the connection accepted then closed with no
                # terminal frame and no transport error — reconnects
                # immediately, so a gateway or proxy that closes every few
                # milliseconds hot-loops this thread (his witness: 3,000
                # reconnects / 3,001 probes in 60s, zero waits). Back off
                # between consecutive clean-EOF reconnects, doubling to a
                # cap; any real progress (a data frame committed a new seq)
                # resets the backoff below, and Stop is honoured during the
                # wait. The status probe still runs every cycle, so a run
                # that completes while we are backed off is still observed.
                clean_eof_reconnects += 1
                # Bound the EXPONENT before multiplying: unbounded,
                # 2 ** 1024 overflows float conversion at reconnect ~1,025
                # (~8.5h into a stuck turn) and kills the turn with
                # OverflowError (round-7 review, CORE). 6 * ln2 doubling
                # steps (x64) already exceed the 30s cap from the 0.5s base.
                backoff = min(
                    _CLEAN_EOF_BACKOFF_BASE_SECS
                    * (2 ** min(clean_eof_reconnects - 1, 6)),
                    _CLEAN_EOF_BACKOFF_MAX_SECS,
                )
                cancel_event.wait(backoff)
            continue
        if action == "cancel":
            # Maintainer should-fix (round-3 re-review): persist the cancelled
            # turn BEFORE emitting the browser-facing cancel event — the
            # browser handler fetches the session as soon as the event lands
            # and must not read pre-persistence state. The caller's own
            # cancelled-turn settle below is a no-op after this one (the
            # ownership guard short-circuits, and partial/marker inserts are
            # signature-deduped), so no extra handshake is needed.
            _settle_gateway_cancelled_turn(session_id, stream_id)
            put_gateway_event("cancel", {"message": "Cancelled by gateway"})
            return None, usage
        if action == "raise":
            raise value
        usage.update(value)
        break
    return final_text, usage


def stop_gateway_run(run_id: str) -> bool:
    """Request gateway interruption and report whether it was acknowledged."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return False
    base_url, api_key = gateway_run_endpoint(run_id)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/runs/{urllib.parse.quote(run_id, safe='')}/stop",
        data=b"{}",
        headers=headers,
        method="POST",
    )
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(req, timeout=10) as response:
            final_url = str(getattr(response, "geturl", lambda: req.full_url)() or "")
            status = int(getattr(response, "status", getattr(response, "code", 0)) or 0)
            return 200 <= status < 300 and final_url == req.full_url
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        logger.debug("Gateway stop failed for run %s", run_id, exc_info=True)
        return False


_GATEWAY_RUN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
GATEWAY_REATTACH_POLL_INTERVAL = 2.0
# Consecutive unreachable polls tolerated before the reattached turn is failed (~5 min at 2s).
GATEWAY_REATTACH_MAX_POLL_FAILURES = 150
# Streaming watchdog budget (#7978): a connection delivering nothing but
# keepalive/comment frames for this long is treated as stalled and the durable
# run status is consulted; keepalives are liveness, not progress. The same
# budget (plus a small epsilon) also bounds the per-read wait on the streaming
# events connection (round-4 review): the wall-clock stall check only runs
# when a line ARRIVES, so a socket that goes byte-SILENT would otherwise block
# inside the read for the full 600s read timeout — 5x past this budget —
# before the stall check or Stop handling could run. Other readers (reattach
# poller) keep the configured read timeout.
_GATEWAY_WATCHDOG_SECS = 120.0
_GATEWAY_WATCHDOG_READ_EPSILON = 2.0
# Consecutive durable-status 404s tolerated inside the streaming watchdog
# before the turn fails closed: a 404 is terminal (round-2 review); the extra
# probe only covers a status-before-registration race, and the re-probe runs
# immediately with no poll-interval sleep.
_STATUS_404_GRACE_PROBES = 2
# Consecutive clean-EOF reconnects (no terminal frame, no transport error:
# the gateway or a proxy accepts the connection and closes it immediately)
# pace themselves before reopening /events. Backoff doubles from
# _CLEAN_EOF_BACKOFF_BASE_SECS up to _CLEAN_EOF_BACKOFF_MAX_SECS; any REAL
# progress (a data frame committed a new seq, or durable status reporting the
# run no longer streaming) resets it. Stop is honoured during every wait.
_CLEAN_EOF_BACKOFF_BASE_SECS = 0.5
_CLEAN_EOF_BACKOFF_MAX_SECS = 30.0
_REATTACH_SCAN_HEAD_BYTES = 16 * 1024


def _record_gateway_run(session_id: str, stream_id: str, run_id: str, request=None, **extra) -> None:
    """Persist the run id (or, before admission, the exact request) so a restarted WebUI can reattach."""
    try:
        with _get_session_agent_lock(session_id):
            session = get_session(session_id)
            if not _stream_writeback_is_current(session, stream_id):
                return
            session.gateway_run = {"run_id": run_id, "stream_id": stream_id, **extra}
            if not run_id:
                session.gateway_run["request"] = request
            session.save(touch_updated_at=False)
    except Exception:
        logger.warning("Failed to persist gateway run %s for session %s", run_id, session_id, exc_info=True)
        if not run_id:
            raise  # never admit a run a restart could not recover


def _get_gateway_run_status(base_url: str, api_key: str, run_id: str) -> dict:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/runs/{urllib.parse.quote(run_id, safe='')}",
        headers=headers,
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read(1024 * 1024) or b"{}")
    return payload if isinstance(payload, dict) else {}


def _restore_relayed_gateway_state(session_id: str, stream_id: str) -> int | None:
    """Rebuild live text/reasoning/tool state from this stream's run journal; return the replay cursor.

    Relayed rows carry ``gateway_seq``, so the journal is the replay cursor; None (poll only) for rows without one
    or when the journal cannot be read (an unread journal proves no cursor, so replaying from -1 would duplicate).
    """
    from api.run_journal import read_run_events

    try:
        events = read_run_events(session_id, stream_id).get("events") or []
    except Exception:
        logger.debug("Failed to read run journal for reattached stream %s", stream_id, exc_info=True)
        return None
    stateful, cursor = [], -1
    for row in events:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        seq = payload.get("gateway_seq")
        if row.get("event") in {"token", "reasoning", "tool", "tool_complete"}:
            if not isinstance(seq, int):
                return None
            stateful.append((row["event"], payload))
        # Every relayed row (approval included) advances the cursor, not just the stateful ones.
        if isinstance(seq, int):
            cursor = max(cursor, seq)
    for name, payload in stateful:
        if name == "token":
            STREAM_PARTIAL_TEXT[stream_id] = STREAM_PARTIAL_TEXT.get(stream_id, "") + str(payload.get("text") or "")
        else:
            _note_live_gateway_event(stream_id, name, payload)
    return cursor


def _await_gateway_run_result(
    session_id, stream_id, run_id, base_url, api_key,
    *, put_gateway_event, cancel_event, resume_seq=None,
):
    """Resume a run started by a previous WebUI process; same return contract as _run_gateway_runs_api_streaming.

    Streams /events from ``resume_seq``; polls run status when replay is truncated, unavailable, or unaligned.
    """
    from api.route_approvals import settle_gateway_pending_run

    _publish_gateway_run_id(stream_id, run_id)
    update_active_run(stream_id, phase="gateway-reattached")
    surfaced_approval_ids: set[str] = set()
    failures = 0
    last_seq = [resume_seq if resume_seq is not None else -1]
    streaming = resume_seq is not None

    def approval_is_current(payload):
        # Auto-approved before the restart leaves no journal row; only the Gateway knows it is settled.
        try:
            status = _get_gateway_run_status(base_url, api_key, run_id)
        except (urllib.error.URLError, OSError, ValueError):
            return True  # fail open: a stale card 409s, a missing one stalls the run
        approval = status.get("approval")
        return (
            str(status.get("status") or "").strip().lower() == "waiting_for_approval"
            and isinstance(approval, dict)
            and _gateway_approval_key(approval) == _gateway_approval_key(payload)
        )

    def surface_parked_approval(status):
        approval = status.get("approval")
        if str(status.get("status") or "").strip().lower() == "waiting_for_approval" and isinstance(approval, dict):
            approval_key = _gateway_approval_key(approval)
            if approval_key not in surfaced_approval_ids:
                surfaced_approval_ids.add(approval_key)
                _relay_gateway_run_approval(
                    session_id, run_id, approval, base_url, api_key, put_gateway_event=put_gateway_event,
                )

    # Poll status before the first blocking /events read: an approval relayed before the restart sits
    # behind the replay cursor, and a gateway without cursor replay never re-sends it at all.
    probed = False
    while True:
        if cancel_event.is_set():
            put_gateway_event("cancel", {"message": "Cancelled by user"})
            return None, {}
        if streaming and probed:
            try:
                with _open_gateway_run_events(base_url, _gateway_run_headers(session_id, api_key), run_id, last_seq[0]) as resp:
                    text, usage, outcome, _accepted = _relay_gateway_run_events(
                        resp, session_id, stream_id, run_id, base_url, api_key,
                        put_gateway_event=put_gateway_event, cancel_event=cancel_event,
                        on_seq=lambda seq: last_seq.__setitem__(0, seq), final_text=STREAM_PARTIAL_TEXT.get(stream_id, ""),
                        stop_on_truncated=True, surfaced_approval_ids=surfaced_approval_ids,
                        output_is_authoritative=True, approval_is_current=approval_is_current,
                    )
                if outcome == "ended":
                    return text, usage
                if outcome == "truncated":
                    logger.info("Gateway replay for run %s is truncated past seq %s; polling status", run_id, last_seq[0])
                    streaming = False
            except urllib.error.HTTPError:
                streaming = False  # the status poll below classifies 404/401/403
            except (urllib.error.URLError, OSError, ValueError):
                logger.debug("Gateway event stream for run %s dropped; checking status", run_id, exc_info=True)
        first_probe, probed = not probed, True
        try:
            status = _get_gateway_run_status(base_url, api_key, run_id)
            failures = 0
        except (urllib.error.URLError, OSError, ValueError) as exc:
            code = getattr(exc, "code", None)
            if code == 404:
                raise RuntimeError("Gateway no longer has this run; its result could not be recovered after the WebUI restart.") from exc
            if code in (401, 403):
                raise RuntimeError(f"Gateway rejected the WebUI credentials (HTTP {code}) while reattaching to a run after the restart.") from exc
            failures += 1
            if failures >= GATEWAY_REATTACH_MAX_POLL_FAILURES:
                raise RuntimeError("Gateway became unreachable while waiting for a run that outlived the WebUI restart.") from exc
            cancel_event.wait(GATEWAY_REATTACH_POLL_INTERVAL)
            continue
        state = str(status.get("status") or "").strip().lower()
        surface_parked_approval(status)
        if state in _GATEWAY_RUN_TERMINAL_STATUSES:
            settle_gateway_pending_run(session_id, run_id, reason=f"Gateway run {state} before approval resolution")
            if state == "cancelled":
                # Persist before emit (same round-3 should-fix as the
                # streaming watchdog's status lane): the browser refetches on
                # this event, so the cancelled-turn settle must have landed.
                _settle_gateway_cancelled_turn(session_id, stream_id)
                put_gateway_event("cancel", {"message": "Cancelled by gateway"})
                return None, {}
            if state != "completed":
                raise RuntimeError(str(status.get("error") or f"Gateway run {state}"))
            output = str(status.get("output") or "")
            if output and stream_id in STREAM_PARTIAL_TEXT:
                STREAM_PARTIAL_TEXT[stream_id] = output
            return output, {k: v for k, v in _gateway_stream_usage(status).items() if v}
        if not (streaming and first_probe):
            cancel_event.wait(GATEWAY_REATTACH_POLL_INTERVAL)


def resume_gateway_runs_after_restart() -> list[str]:
    """Reattach pending turns whose gateway run outlived the previous WebUI process; never raises.

    Call before serving so stale-pending repair does not mark these turns interrupted.
    """
    from api import models as _models

    try:
        candidates = _sidecars_with_active_stream(_models.SESSION_DIR)
    except Exception:
        logger.warning("gateway reattach: could not scan session sidecars", exc_info=True)
        return []
    resumed: list[str] = []
    for sid in candidates:
        try:
            if _resume_gateway_run_for_session(_models.Session.load_metadata_only(sid)):
                resumed.append(sid)
        except Exception:
            logger.warning("gateway reattach failed for session %s", sid, exc_info=True)
    if resumed:
        logger.info("Reattached %d gateway run(s) that outlived the previous WebUI process", len(resumed))
    return resumed


def _sidecars_with_active_stream(session_dir) -> list[str]:
    """Ids of sidecars (not the index, which can lag a save) whose header shows an active stream."""
    ids = []
    for path in sorted(session_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            with path.open("rb") as fp:
                head = fp.read(_REATTACH_SCAN_HEAD_BYTES)
        except OSError:
            continue
        if b'"active_stream_id": null' not in head:
            ids.append(path.stem)
    return ids


def _gateway_endpoint_for_profile(profile_name) -> tuple[str, str]:
    """URL and key of the session's own profile, root included, never the process-active profile."""
    from api import profiles as _profiles
    from api.config import get_config_for_profile_home

    home = _profiles.get_hermes_home_for_profile(str(profile_name or "").strip())
    environ = {k: v for k, v in os.environ.items() if k not in _profiles._loaded_profile_env_keys}
    environ.update(_profiles.filter_runtime_env_for_gateway_parity(_profiles.get_profile_runtime_env(home)))
    return _gateway_base_url(get_config_for_profile_home(home), environ), _gateway_api_key(environ)


def _resume_gateway_run_for_session(session) -> bool:
    from api.config import create_stream_channel, register_session_writeback_owner, register_stream_owner

    run = (session.gateway_run if session is not None else None) or {}
    stream_id = str(run.get("stream_id") or "")
    # A persisted run is the authority whatever the process default backend is.
    if not stream_id or not (run.get("run_id") or run.get("request")) or session.active_stream_id != stream_id:
        return False
    sid = session.session_id
    endpoint = _gateway_endpoint_for_profile(session.profile)
    # Review #7302 (nesquena, 01-Oct-2026) finding 2 -- "CORE": publish the
    # reattached run's ownership (ACTIVE_RUNS row + retained cancel signal) on the
    # SAME STREAMS_LOCK -> ACTIVE_RUNS_LOCK edge that creates the STREAMS entry,
    # BEFORE the worker is scheduled. A run restored after a restart carries the
    # PREVIOUS process's session.pending_started_at, so the fresh-pending guard in
    # chat/start cannot cover this window: with the stream registered but no
    # ACTIVE_RUNS row, the orphan check classifies the reattach as an orphan,
    # clears the stream plus the Gateway state and admits a SECOND start while the
    # remote run keeps going. The edge and the lock order are the ones the worker
    # (below), Stop and Steer use.
    with STREAMS_LOCK:
        if stream_id in STREAMS:
            return False
        STREAMS[stream_id] = create_stream_channel()
        CANCEL_FLAGS[stream_id] = CANCEL_FLAGS.get(stream_id, threading.Event())
        # Ownership token: Stop can detach this stream (and a successor could even
        # register the same id) before the worker admits it, so the early-return
        # teardown must retire exactly THIS claim and never someone else's row.
        claim_token = uuid.uuid4().hex
        register_active_run(
            stream_id,
            session_id=sid,
            started_at=time.time(),
            phase="gateway-reattached",
            claim_token=claim_token,
            workspace=str(session.workspace or ""),
            model=session.model,
            provider=session.model_provider,
            backend="gateway",
        )
    register_stream_owner(stream_id, sid)
    register_session_writeback_owner(sid, stream_id)
    _mark_gateway_run_starting(stream_id)
    try:
        threading.Thread(
            target=_run_gateway_chat_streaming,
            args=(sid, session.pending_user_message or "", session.model, session.workspace,
                  stream_id, list(session.pending_attachments or [])),
            kwargs={
                "model_provider": session.model_provider,
                "goal_related": bool(run.get("goal_related")),
                "regeneration": bool(run.get("regeneration")),
                "reattach_run": run,
                "reattach_endpoint": endpoint,
                "reattach_claim_token": claim_token,
            },
            name=f"gateway-reattach-{stream_id[:12]}",
            daemon=True,
        ).start()
    except Exception:
        # A reattach whose worker could not be scheduled must not leave the
        # ownership claim behind (the caller is told to retry/fall back instead of
        # reporting a live reattach). Mirror the worker's own early-return
        # teardown, plus the ACTIVE_RUNS row published above.
        logger.warning(
            "gateway reattach: could not start the worker for stream %s", stream_id,
            exc_info=True,
        )
        _finish_gateway_run_starting(stream_id, result="failed")
        _clear_gateway_run_starting(stream_id)
        try:
            unregister_active_run_if_owned(stream_id, claim_token=claim_token)
        except Exception:
            logger.debug("gateway reattach: could not drop the active run for %s", stream_id, exc_info=True)
        try:
            release_stream_owned_registries(stream_id, session_id=sid)
        except Exception:
            logger.debug("gateway reattach: could not release the stream-owned registries for %s", stream_id, exc_info=True)
        try:
            release_gateway_stream_state(stream_id)
        except Exception:
            logger.debug("gateway reattach: could not release the Gateway state for %s", stream_id, exc_info=True)
        return False
    return True

def _settle_gateway_terminal_error(
    session_id,
    stream_id,
    workspace,
    model,
    model_provider,
    terminal_error,
    *,
    persisted_model=None,
    persisted_model_provider=None,
):
    from api.streaming import (
        _classify_provider_error,
        _materialize_pending_user_turn_before_error,
        _provider_error_payload,
        _session_payload_with_full_messages,
        _snapshot_and_append_partial_on_error,
        _terminal_turn_duration,
    )

    with _get_session_agent_lock(session_id):
        session = get_session(session_id)
        if not _stream_writeback_is_current(session, stream_id):
            return None
        error_classification = _classify_provider_error(terminal_error)
        error_payload = _provider_error_payload(
            terminal_error,
            error_classification["type"],
            error_classification.get("hint", ""),
        )
        turn_duration = _terminal_turn_duration(session)
        _materialize_pending_user_turn_before_error(session)
        session.active_stream_id = None
        session.gateway_run = None
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        try:
            _snapshot_and_append_partial_on_error(session, stream_id)
        except Exception:
            logger.debug("Failed to snapshot gateway partials on terminal error", exc_info=True)
        error_message = {
            "role": "assistant",
            "content": (
                f"**{error_classification['label']}:** "
                f"{error_payload.get('message') or error_classification['label']}"
            ) + (f"\n\n*{error_payload['hint']}*" if error_payload.get("hint") else ""),
            "timestamp": int(time.time()),
            "_error": True,
        }
        if turn_duration is not None:
            error_message["_turnDuration"] = turn_duration
        if error_payload.get("details"):
            error_message["provider_details"] = error_payload["details"]
        if not isinstance(session.messages, list):
            session.messages = []
        session.messages.append(error_message)
        session.workspace = str(workspace)
        session.model = persisted_model if persisted_model is not None else model
        session.model_provider = (
            persisted_model_provider
            if persisted_model_provider is not None
            else model_provider
        )
        terminal_session_persisted = False
        try:
            session.save()
            terminal_session_persisted = True
        except Exception:
            logger.debug("Failed to persist gateway terminal error settlement", exc_info=True)
        error_payload["session"] = redact_session_data(
            _session_payload_with_full_messages(session, tool_calls=[])
        )
        error_payload["session_id"] = session.session_id
        error_payload["terminal_session_persisted"] = terminal_session_persisted
        if terminal_session_persisted:
            error_payload["terminal_session_persisted_session_id"] = session.session_id
        return error_payload


def _settle_gateway_cancelled_turn(session_id, stream_id) -> None:
    """Keep the prompt, the streamed partial, and a cancel marker when the run ended cancelled and Stop did not already settle it."""
    from api.streaming import (
        _CANCEL_MARKER_PATTERNS,
        _build_partial_message,
        _materialize_pending_user_turn_before_error,
        _partial_marker_already_present,
        _persist_cancelled_turn,
    )

    with _get_session_agent_lock(session_id):
        session = get_session(session_id)
        if not _stream_writeback_is_current(session, stream_id):
            # Successor-owner control: the stream/turn ownership moved on
            # (reattach rotation or a successor admission owns
            # active_stream_id now) — never write over the successor's state.
            return
        # Status-lane cancel persistence (round-3 review): the durable-status
        # lane (cancelled/interrupted resolved by the watchdog's
        # GET /v1/runs/{id} probe) reaches this settle WITHOUT the browser
        # Stop path ever running, so cancel_stream()'s snapshot+upsert never
        # happened and the already-streamed partial answer / reasoning /
        # live tool buffers would be dropped by the teardown finally. This
        # runs the SAME canonical cancellation persistence the SSE-cancel
        # path uses (_build_partial_message + deduped upsert + cancel
        # marker), but at a NEW call site: unlike the inherited SSE-cancel
        # behaviour, the worker itself is settling here — its teardown
        # finally has not popped the buffers yet — so it reads its own
        # buffers directly, without cancel_stream's under-STREAMS_LOCK
        # snapshot dance against a live worker.
        # Empty-output control: _build_partial_message returns None when all
        # three buffers are empty, so nothing is appended — no empty rows.
        partial_msg = _build_partial_message(
            STREAM_PARTIAL_TEXT.get(stream_id, ""),
            STREAM_REASONING_TEXT.get(stream_id, ""),
            list(STREAM_LIVE_TOOL_CALLS.get(stream_id, []) or []),
        )
        # Row order matches cancel_stream(): the user turn is materialized
        # before the partial row is placed, and the partial is inserted
        # before any cancel marker that is already present. Clearing the
        # pending fields here keeps _persist_cancelled_turn's own
        # materialize idempotent (no duplicate user row).
        _materialize_pending_user_turn_before_error(session)
        session.pending_user_message = None
        session.pending_attachments = []
        session.pending_started_at = None
        session.pending_user_source = None
        if partial_msg is not None:
            if not isinstance(session.messages, list):
                session.messages = []
            marker_idx = len(session.messages)
            for idx in range(len(session.messages) - 1, -1, -1):
                row = session.messages[idx]
                # Maintainer must-fix (round-3 re-review): never cross the
                # current user-turn boundary — a cancel marker above it
                # belongs to a PREVIOUS cancelled turn, and inserting this
                # turn's partial before it scrambles the transcript order
                # after reload.
                if isinstance(row, dict) and row.get("role") == "user":
                    break
                if not isinstance(row, dict) or row.get("role") != "assistant":
                    continue
                normalized = str(row.get("content") or "").strip().lower()
                if any(pattern in normalized for pattern in _CANCEL_MARKER_PATTERNS):
                    marker_idx = idx
                    break
            if not _partial_marker_already_present(session.messages, partial_msg, before_idx=marker_idx):
                session.messages.insert(marker_idx, partial_msg)
        _persist_cancelled_turn(session, message="Cancelled by gateway")
        session.gateway_run = None
        session.save()


def _stream_writeback_is_current(session: Any, stream_id: str) -> bool:
    return bool(stream_id and getattr(session, "active_stream_id", None) == stream_id)


def _clear_gateway_pending_state(session: Any, stream_id: str) -> None:
    if not _stream_writeback_is_current(session, stream_id):
        # Cancel clears active_stream_id eagerly; still drop this stream's run record.
        if session is not None and (session.gateway_run or {}).get("stream_id") == stream_id:
            session.gateway_run = None
            session.save(touch_updated_at=False)
        return
    session.active_stream_id = None
    session.gateway_run = None
    session.pending_user_message = None
    session.pending_attachments = None
    session.pending_started_at = None
    session.pending_user_source = None
    session.save()


def _cleanup_gateway_pending_mirror(session_id: str) -> None:
    try:
        from api.route_approvals import (
            retire_gateway_pending_mirror,
        )

        retire_gateway_pending_mirror(session_id)
    except Exception:
        logger.debug("Failed to reconcile gateway pending mirror during teardown", exc_info=True)


def _run_gateway_chat_streaming(
    session_id,
    msg_text,
    model,
    workspace,
    stream_id,
    attachments=None,
    *,
    model_provider=None,
    persisted_model=None,
    persisted_model_provider=None,
    goal_related=False,
    regeneration=False,
    reattach_run=None,
    reattach_endpoint=None,
    reattach_claim_token=None,
):
    """Bridge a WebUI chat turn through Hermes Gateway's API server.

    This default-off path keeps the browser contract unchanged: /api/chat/start
    still returns a local stream_id and /api/chat/stream still receives WebUI SSE
    event names. The worker translates OpenAI-compatible streaming chunks from
    the configured Gateway API server into those local events and persists the
    final user/assistant turn back into the WebUI session.
    """
    cancel_event = threading.Event()
    q = peek_stream(stream_id)
    if q is not None:
        # A snapshot lookup is not admission. A concurrent chat/start can classify
        # this stream as an orphan (registered, no live worker, no pending turn in
        # the registration window) and clear it between peek_stream() and the
        # registration below; publishing ourselves active afterwards would run a
        # turn with no transport/owner state, allowing overlapping turns and
        # duplicate provider/tool effects. Claim the stream, its retained cancel
        # signal and the ACTIVE_RUNS registration on ONE STREAMS_LOCK ->
        # ACTIVE_RUNS_LOCK edge -- the order Stop/Steer use, and the same edge the
        # in-process worker uses (api/streaming.py) -- revalidating stream
        # membership and cancellation in-lock and failing closed when ownership is
        # already gone.
        with STREAMS_LOCK:
            cancel_event = CANCEL_FLAGS.get(stream_id, cancel_event)
            if stream_id not in STREAMS or cancel_event.is_set():
                q = None
            else:
                CANCEL_FLAGS[stream_id] = cancel_event
                STREAM_PARTIAL_TEXT[stream_id] = ""
                STREAM_REASONING_TEXT[stream_id] = ""
                STREAM_LIVE_TOOL_CALLS[stream_id] = []
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
                # Worker admission ends the launch phase (#7302 finding 5): retire
                # the claim published at registration. Ownership lives in
                # ACTIVE_RUNS from here on.
                from api.config import retire_pre_admission_claim_if_owned

                retire_pre_admission_claim_if_owned(stream_id, streams_lock_held=True)
    if q is None:
        _finish_gateway_run_starting(stream_id, result="fallback")
        _clear_gateway_run_starting(stream_id)
        # Cancelled or orphan-cleared before the worker was admitted: no teardown
        # finally runs on this early-return path, so release the COMPLETE set of
        # stream-owned registries the route layer registered (stream rows,
        # owners, writeback, goal classification) and then the Gateway-owned
        # lifecycle rows. release_gateway_stream_state() is no-op-safe and must
        # run OUTSIDE STREAMS_LOCK -- it owns the lifecycle/waiter protocol and
        # also drops the endpoint mapping the old owner-only release leaked.
        # Stop can detach this stream between the pre-admission claim and this
        # admission (review 2026-10-01, third finding): cancel_stream() marks the
        # row cancelling and pops STREAMS/CANCEL_FLAGS while this worker is still
        # unwinding, and this early return never reaches the worker's `finally`.
        # Retire exactly the claim we published, by identity -- a row a successor
        # registered for the same stream id must survive. Without this the ghost
        # row reports false liveness/busy in _run_lifecycle_health() and delays
        # background wakeups (LAST_RUN_FINISHED_AT never advances).
        if reattach_claim_token:
            try:
                if unregister_active_run_if_owned(stream_id, claim_token=reattach_claim_token):
                    logger.info(
                        "gateway reattach: retired the pre-admission claim for stream %s "
                        "after cancellation", stream_id,
                    )
            except Exception:
                logger.debug(
                    "gateway reattach: could not retire the pre-admission claim for %s",
                    stream_id, exc_info=True,
                )
        release_stream_owned_registries(stream_id, session_id=session_id)
        release_gateway_stream_state(stream_id)
        return
    try:
        run_journal = RunJournalWriter(session_id, stream_id)
    except Exception:
        run_journal = None
        logger.debug("Failed to initialize gateway run journal for stream %s", stream_id, exc_info=True)

    success_writeback_committed = False
    runs_api_pending_marked = True

    def put_gateway_event(event, data):
        if cancel_event.is_set() and not success_writeback_committed and event not in ("cancel", "error", "apperror"):
            return
        if event == "apperror" and isinstance(data, dict):
            data = data.copy()
            data.setdefault("session_id", session_id)
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
            queue_item = (event, data, event_id) if hasattr(q, "subscribe_with_snapshot") else (event, data)
            q.put_nowait(queue_item)
        except Exception:
            logger.debug("Failed to put gateway event to queue")

    s = None
    final_text = ""
    terminal_error = ""
    usage = {"input_tokens": 0, "output_tokens": 0, "estimated_cost": 0}
    try:
        s = get_session(session_id)
        from api.config import get_config  # imported lazily to avoid config-cycle churn

        cfg = get_config()
        reasoning_effort = _gateway_reasoning_effort_for_request(
            cfg,
            model=model,
            model_provider=model_provider,
        )
        base_url, api_key = reattach_endpoint or (_gateway_base_url(cfg), _gateway_api_key())
        with _STREAM_RUN_STARTING_CONDITION:
            _STREAM_ENDPOINTS[stream_id] = (base_url, api_key)
        try:
            from api.config import _main_model_request_overrides
            _gw_overrides = _main_model_request_overrides(
                cfg,
                effective_model=model,
                effective_provider=model_provider,
            )
        except Exception:
            _gw_overrides = {}
        _runs_api_enabled = _gateway_use_runs_api_enabled(cfg)
        _use_runs_api = bool(reattach_run) or (_runs_api_enabled and gateway_supports_approval(base_url, api_key))
        if not _use_runs_api and runs_api_pending_marked:
            _finish_gateway_run_starting(stream_id, result="fallback")
            runs_api_pending_marked = False
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
        if _use_runs_api:
            body_extras = {}
            if model_provider:
                body_extras["provider"] = model_provider
            if reasoning_effort is not None:
                body_extras["reasoning_effort"] = reasoning_effort
            if _gw_overrides.get("service_tier"):
                body_extras["service_tier"] = _gw_overrides["service_tier"]
            record_run = lambda run_id, request=None: _record_gateway_run(
                session_id, stream_id, run_id, request=request,
                regeneration=bool(regeneration), goal_related=bool(goal_related),
            )
            try:
                if reattach_run:
                    run_id = str(reattach_run.get("run_id") or "").strip()
                    if not run_id:
                        # Crashed between admission and saving the id: the same key returns the original run.
                        run_id = _admit_gateway_run(
                            f"{base_url.rstrip('/')}/v1/runs", _gateway_run_headers(session_id, api_key),
                            reattach_run["request"], stream_id,
                        )
                        record_run(run_id)
                    final_text, usage = _await_gateway_run_result(
                        session_id, stream_id, run_id, base_url, api_key,
                        put_gateway_event=put_gateway_event,
                        cancel_event=cancel_event,
                        resume_seq=_restore_relayed_gateway_state(session_id, stream_id),
                    )
                else:
                    final_text, usage = _run_gateway_runs_api_streaming(
                        session_id, msg_text, model, workspace, stream_id,
                        base_url, api_key, prefill_messages, body_extras,
                        put_gateway_event=put_gateway_event,
                        cancel_event=cancel_event,
                        attachments=attachments,
                        cfg=cfg,
                        session=s,
                        active_provider=(model_provider or ""),
                        on_run_id=record_run,
                    )
            except Exception as exc:
                error_payload = _settle_gateway_terminal_error(
                    session_id,
                    stream_id,
                    workspace,
                    model,
                    model_provider,
                    str(exc),
                    persisted_model=persisted_model,
                    persisted_model_provider=persisted_model_provider,
                )
                if error_payload is None:
                    return
                put_gateway_event("apperror", error_payload)
                return
            if final_text is None:
                _settle_gateway_cancelled_turn(session_id, stream_id)
                return
        else:
            # Legacy gateway path: emit unsupported approval notice once per session,
            # but only when the gateway genuinely lacks approval capability.
            approval_reason = gateway_approval_unavailable_reason(base_url, api_key)
            if approval_reason is not None:
                if not hasattr(s, "_approval_notice_emitted"):
                    s._approval_notice_emitted = False
                if not s._approval_notice_emitted:
                    approval_message = "Approvals require a newer gateway. Upgrade the connected Hermes gateway to enable this."
                    approval_type = "approval_gateway_unsupported"
                    if approval_reason == "unreachable":
                        approval_type = "approval_gateway_offline"
                        approval_message = "Gateway connection failed. Check that the connected Hermes gateway is running and reachable."
                    put_gateway_event("warning", {
                        "type": approval_type,
                        "message": approval_message,
                    })
                    s._approval_notice_emitted = True

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

                    message_content = _build_native_multimodal_message("", str(msg_text or ""), attachments, str(workspace), cfg=cfg, active_provider=(model_provider or ""), active_model=(model or ""), requested_provider=(model_provider or ""), profile=getattr(s, "profile", None))
                except Exception:
                    logger.debug("Failed to build gateway multimodal attachment payload", exc_info=True)
                    message_content = str(msg_text or "")
            body = {
                "model": _gateway_model_field(model) or "default",
                "stream": True,
                "messages": [*prefill_messages, {"role": "user", "content": message_content}],
            }
            if model_provider:
                body["provider"] = model_provider
            if reasoning_effort is not None:
                body["reasoning_effort"] = reasoning_effort
            if _gw_overrides.get("service_tier"):
                body["service_tier"] = _gw_overrides["service_tier"]
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            update_active_run(stream_id, phase="gateway-request")
            last_payload = {}
            sse_event = "message"
            with urllib.request.urlopen(req, timeout=_gateway_read_timeout_secs()) as resp:
                for raw_line in _iter_sse_lines_cancellable(resp, cancel_event):
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
                    _payload_event = str(payload.get("event") or payload.get("type") or sse_event).strip()
                    if _payload_event in {"hermes.approval.request", "approval.request"}:
                        approval_data = _gateway_runs_approval_event(payload)
                        if approval_data:
                            # Record the gateway run_id so /api/approval/respond
                            # can relay the choice back and resume the parked run
                            # (legacy path never creates a local run; without this
                            # the card renders but approve/deny returns ok:false).
                            # No-op when the payload omits run_id.
                            _approval_run_id = str(approval_data.get("run_id") or "").strip()
                            if _approval_run_id:
                                _STREAM_RUN_IDS[stream_id] = _approval_run_id
                            try:
                                from api.route_approvals import submit_gateway_pending_mirror
                                head, total = submit_gateway_pending_mirror(session_id, approval_data)
                                approval_data = {**(head or approval_data), "pending_count": total}
                            except Exception:
                                logger.debug("submit_gateway_pending_mirror failed", exc_info=True)
                            put_gateway_event("approval", approval_data)
                        else:
                            logger.debug("Ignoring malformed gateway approval payload")
                        sse_event = "message"
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
                    if payload.get("error"):
                        terminal_error = str(payload["error"])
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
        if terminal_error:
            error_payload = _settle_gateway_terminal_error(
                session_id,
                stream_id,
                workspace,
                model,
                model_provider,
                terminal_error,
                persisted_model=persisted_model,
                persisted_model_provider=persisted_model_provider,
            )
            if error_payload is None:
                return
            put_gateway_event("apperror", error_payload)
            return
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
            # A late Stop can land after Gateway has yielded a full answer but
            # before success writeback. Treat it as cancellation so any
            # credential-exhausted process-wakeup pause stays in place.
            if cancel_event.is_set():
                put_gateway_event("cancel", {"message": "Cancelled by user"})
                return
            now = time.time()
            # Preserve subsecond ordering for gateway-backed turns. Using an
            # integer seconds timestamp gives the user and assistant rows the
            # same sort key; later transcript merges can then fall back to
            # role/content ordering instead of turn order.
            assistant_ts = now + 0.000001
            pending_source = getattr(s, "pending_user_source", None) or "webui"
            from api.streaming import (
                _active_turn_authority,
                _active_turn_token_matches,
                _materialize_active_turn_user,
            )

            active_turn_identity = _active_turn_authority(s, stream_id, msg_text)
            user_msg = _materialize_active_turn_user(
                active_turn_identity,
                str(msg_text or ""),
                pending_source,
            )
            user_msg["timestamp"] = float(
                active_turn_identity.get("timestamp") or now
            )
            assistant_msg = {"role": "assistant", "content": assistant_text, "timestamp": assistant_ts}
            saved_reasoning = STREAM_REASONING_TEXT.get(stream_id, "")
            if saved_reasoning:
                assistant_msg["reasoning"] = saved_reasoning
            previous_messages = list(getattr(s, "messages", None) or [])
            stored_context = getattr(s, "context_messages", None)
            previous_context = list(
                stored_context
                if isinstance(stored_context, list) and (regeneration or stored_context)
                else getattr(s, "messages", None) or []
            )
            previous_process_wakeup_pause = dict(getattr(s, "process_wakeup_pause", {}) or {})
            # Stamp stable ids on the two new rows (shared with the display merge
            # below) so display and model-context copies share an id for the
            # fork/truncate aligner (#context-message-stable-id).
            try:
                from api.streaming import _assign_stable_message_ids

                _assign_stable_message_ids(
                    [user_msg, assistant_msg],
                    previous_context,
                    list(getattr(s, "messages", None) or []),
                )
            except Exception:
                logger.debug("Failed to stamp stable ids on gateway turn rows", exc_info=True)
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
                previous_messages,
                display_context,
            )
            try:
                from api.streaming import _merge_display_messages_after_agent_result

                s.messages = _merge_display_messages_after_agent_result(
                    display,
                    previous_context,
                    s.context_messages,
                    str(msg_text or ""),
                    source=pending_source,
                    verification_nudge_provenance={
                        "active_turn_identity": active_turn_identity,
                    },
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
            if active_turn_identity.get("token"):
                current_display_rows = [
                    message
                    for message in s.messages
                    if _active_turn_token_matches(message, active_turn_identity)
                ]
                if len(current_display_rows) == 1:
                    current_display_rows[0]["timestamp"] = user_msg["timestamp"]
            s.active_stream_id = None
            s.gateway_run = None
            s.pending_user_message = None
            s.pending_attachments = None
            s.pending_started_at = None
            s.pending_user_source = None
            s.workspace = str(workspace)
            s.model = persisted_model if persisted_model is not None else model
            s.model_provider = (
                persisted_model_provider
                if persisted_model_provider is not None
                else model_provider
            )

            def _restore_cancelled_success_writeback():
                if pending_source == "process_wakeup":
                    s.context_messages = previous_context
                    s.messages = previous_messages
                    s.process_wakeup_pause = dict(previous_process_wakeup_pause)
                elif previous_process_wakeup_pause:
                    s.process_wakeup_pause = dict(previous_process_wakeup_pause)
                else:
                    clear_process_wakeup_pause(s, reason="run_completed")
                s.save()
                put_gateway_event("cancel", {"message": "Cancelled by user"})

            # Recheck immediately before clearing the pause; Stop can arrive
            # while the success transcript is being assembled.
            if cancel_event.is_set():
                _restore_cancelled_success_writeback()
                return
            clear_process_wakeup_pause(s, reason="run_completed")
            if cancel_event.is_set():
                _restore_cancelled_success_writeback()
                return
            s.save()
            if cancel_event.is_set():
                _restore_cancelled_success_writeback()
                return
            # #6366 re-gate: record the durable same-stream completion
            # event in the crash-safe turn journal. The run journal's
            # terminal state is only reached on its own ``stream_end``
            # write path, so a Gateway run whose terminal write is lost
            # would otherwise leave no completion evidence at all and
            # stale-cancel recovery would re-append a duplicate
            # recovered row after the valid final answer.
            try:
                append_turn_journal_event_for_stream(
                    session_id,
                    stream_id,
                    {"event": "completed", "created_at": time.time()},
                )
            except Exception:
                logger.debug("Failed to append completed turn journal event", exc_info=True)
            success_writeback_committed = True
        try:
            from api.goals import evaluate_goal_after_turn, has_active_goal
            from api.profiles import get_hermes_home_for_profile

            profile_home = get_hermes_home_for_profile(getattr(s, "profile", None))
            if goal_related and has_active_goal(session_id, profile_home=profile_home):
                put_gateway_event("goal", {
                    "session_id": session_id,
                    "state": "evaluating",
                    "message": "Evaluating goal progress…",
                    "message_key": "goal_evaluating_progress",
                })
                decision = evaluate_goal_after_turn(
                    session_id,
                    assistant_text,
                    user_initiated=True,
                    profile_home=profile_home,
                ) or {}
                goal_message = str(decision.get("message") or "").strip()
                if goal_message:
                    put_gateway_event("goal", {
                        "session_id": session_id,
                        "state": "continuing" if decision.get("should_continue") else "idle",
                        "message": goal_message,
                        "message_key": decision.get("message_key") or (
                            "goal_continuing" if goal_message else ""
                        ),
                        "message_args": decision.get("message_args") or [],
                        "decision": decision,
                    })
                if decision.get("should_continue"):
                    continuation_prompt = str(decision.get("continuation_prompt") or "").strip()
                    if continuation_prompt:
                        PENDING_GOAL_CONTINUATION.add(session_id)
                        put_gateway_event("goal_continue", {
                            "session_id": session_id,
                            "continuation_prompt": continuation_prompt,
                            "text": continuation_prompt,
                            "message": goal_message,
                            "message_key": decision.get("message_key") or "goal_continuing",
                            "message_args": decision.get("message_args") or [],
                            "decision": decision,
                        })
        except Exception as goal_exc:
            logger.debug(
                "Gateway goal continuation hook failed for session %s: %s",
                session_id,
                goal_exc,
            )
        from api.streaming import _session_payload_with_full_messages
        gateway_session_payload = _session_payload_with_full_messages(s, tool_calls=[])
        put_gateway_event("done", {"session": redact_session_data(gateway_session_payload), "usage": usage})
        try:
            from api.web_push import notify_session_done

            notify_session_done(session_id, gateway_session_payload.get("messages"))
        except Exception:
            logger.debug("Web Push completion fanout failed", exc_info=True)
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
        mapped_run_id = str(_STREAM_RUN_IDS.get(stream_id) or "").strip()
        if mapped_run_id:
            try:
                from api.route_approvals import settle_gateway_pending_run
                settle_gateway_pending_run(
                    session_id,
                    mapped_run_id,
                    reason="Gateway run ended during teardown before approval resolution",
                )
            except Exception:
                logger.debug("Failed to settle gateway pending approvals during teardown", exc_info=True)
        if s is not None:
            try:
                with _get_session_agent_lock(session_id):
                    _clear_gateway_pending_state(get_session(session_id), stream_id)
            except Exception:
                logger.debug("Failed to clear gateway stream state", exc_info=True)
            _cleanup_gateway_pending_mirror(session_id)
        with STREAMS_LOCK:
            AGENT_INSTANCES.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)
            STREAM_GOAL_RELATED.pop(stream_id, None)
            STREAM_PARTIAL_TEXT.pop(stream_id, None)
            STREAM_REASONING_TEXT.pop(stream_id, None)
            STREAM_LIVE_TOOL_CALLS.pop(stream_id, None)
            STREAM_LAST_EVENT_ID.pop(stream_id, None)
            STREAMS.pop(stream_id, None)
        # Shared, no-op-safe release (#7302 re-gate): the chat/start orphan
        # recovery retires a dead stream's Gateway rows through this same path,
        # so both teardowns stay one implementation. ``runs_api_pending_marked``
        # keeps this call site's original finish-pending semantics.
        release_gateway_stream_state(stream_id, finish_pending=runs_api_pending_marked)
        unregister_stream_owner(stream_id)
        unregister_active_run(stream_id)
        # Release the writeback-owner entry the route layer registered for this
        # Gateway run so SESSION_WRITEBACK_OWNERS does not grow unbounded across
        # the process lifetime (compare-and-clear: only clears if still owned by
        # this stream, mirroring the local streaming teardown).
        clear_session_writeback_owner_if_owned(session_id, stream_id)
