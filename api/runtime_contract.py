"""Stable WebUI runtime event and status contract.

This module defines the canonical ``RuntimeEvent`` and ``RuntimeStatus`` shapes
that future WebUI, Hermes Agent, and Hermex clients can share.  It is
intentionally dependency-light: it does not import ``api/streaming.py``,
``api/routes.py``, or any live runtime globals.

The contract is currently used as a serialization/validation layer only.
Journal, route, and adapter wiring are deferred to later phases.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

_EXPECTED_STATUSES = frozenset({
    "queued",
    "running",
    "awaiting_approval",
    "awaiting_clarify",
    "paused",
    "cancelling",
    "cancelled",
    "failed",
    "completed",
    "expired",
})

_EXPECTED_EVENT_TYPES = frozenset({
    "run.started",
    "run.status",
    "token.delta",
    "reasoning.delta",
    "reasoning.done",
    "progress",
    "tool.started",
    "tool.updated",
    "tool.done",
    "approval.requested",
    "approval.resolved",
    "clarify.requested",
    "clarify.resolved",
    "title.updated",
    "usage.updated",
    "usage.final",
    "error",
    "done",
})

_SENSITIVE_PAYLOAD_KEYS = frozenset({
    "api_key",
    "api_token",
    "authorization",
    "auth_token",
    "bearer",
    "cookie",
    "oauth_token",
    "openai_api_key",
    "password",
    "secret",
    "token",
})


def _redact_payload(payload: dict) -> dict:
    sanitized: dict = {}
    for key, value in payload.items():
        low = str(key).lower()
        if low in _SENSITIVE_PAYLOAD_KEYS or any(
            candidate in low for candidate in ("secret", "token", "password", "api_key")
        ):
            sanitized[key] = "[REDACTED]"
        elif isinstance(value, dict):
            sanitized[key] = _redact_payload(value)
        elif isinstance(value, list):
            sanitized[key] = [
                _redact_payload(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            sanitized[key] = value
    return sanitized


@dataclass(frozen=True)
class RuntimeEvent:
    event_id: str
    seq: int
    run_id: str
    session_id: str
    type: str
    created_at: float
    terminal: bool = False
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "type": self.type,
            "created_at": self.created_at,
            "terminal": self.terminal,
            "payload": _redact_payload(dict(self.payload)),
        }


@dataclass(frozen=True)
class RuntimeStatus:
    run_id: str
    session_id: str
    status: str = "unknown"
    last_event_id: str | None = None
    last_seq: int | None = None
    terminal: bool = False
    controls: list[str] = field(default_factory=list)
    pending_approval_ids: list[str] = field(default_factory=list)
    pending_clarify_ids: list[str] = field(default_factory=list)
    error: str | None = None
    result: str | None = None

    def to_dict(self) -> dict:
        d: dict = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "status": self.status,
            "last_event_id": self.last_event_id,
            "last_seq": self.last_seq,
            "terminal": self.terminal,
            "controls": list(self.controls),
            "pending_approval_ids": list(self.pending_approval_ids),
            "pending_clarify_ids": list(self.pending_clarify_ids),
            "error": self.error,
            "result": self.result,
        }
        if self.error is not None and any(
            candidate in str(self.error).lower()
            for candidate in ("secret", "token", "password", "api_key")
        ):
            d["error"] = "[REDACTED]"
        return d


def make_event(
    *,
    run_id: str,
    session_id: str,
    seq: int,
    type: str,
    created_at: float | None = None,
    terminal: bool = False,
    payload: dict | None = None,
) -> RuntimeEvent:
    event_id = f"{run_id}:{seq}"
    return RuntimeEvent(
        event_id=event_id,
        seq=seq,
        run_id=str(run_id),
        session_id=str(session_id),
        type=str(type),
        created_at=float(created_at if created_at is not None else time.time()),
        terminal=bool(terminal),
        payload=dict(payload or {}),
    )


def is_valid_event_type(value: str) -> bool:
    return str(value or "") in _EXPECTED_EVENT_TYPES


def is_valid_status(value: str) -> bool:
    return str(value or "") in _EXPECTED_STATUSES


def make_status(
    *,
    run_id: str,
    session_id: str,
    status: str = "unknown",
    last_event_id: str | None = None,
    last_seq: int | None = None,
    terminal: bool = False,
    controls: list[str] | None = None,
    pending_approval_ids: list[str] | None = None,
    pending_clarify_ids: list[str] | None = None,
    error: str | None = None,
    result: str | None = None,
) -> RuntimeStatus:
    return RuntimeStatus(
        run_id=str(run_id),
        session_id=str(session_id),
        status=str(status),
        last_event_id=last_event_id,
        last_seq=last_seq,
        terminal=bool(terminal),
        controls=list(controls or []),
        pending_approval_ids=list(pending_approval_ids or []),
        pending_clarify_ids=list(pending_clarify_ids or []),
        error=error,
        result=result,
    )
