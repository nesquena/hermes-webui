"""RuntimeAdapter seam for WebUI-owned run execution.

This is the #1925 Slice 2 seam only.  The default WebUI chat path remains the
legacy direct route; enabling ``HERMES_WEBUI_RUNTIME_ADAPTER=legacy-journal``
routes through this protocol-translator facade over the same legacy execution
path plus the Slice 1 run journal.  This module intentionally does not own
AIAgent instances, cancellation flags, approval callbacks, clarify callbacks, or
new long-lived queues.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

_RUNTIME_ADAPTER_ENV = "HERMES_WEBUI_RUNTIME_ADAPTER"
_RUNTIME_ADAPTER_DIRECT = "legacy-direct"
_RUNTIME_ADAPTER_JOURNAL = "legacy-journal"
_VALID_RUNTIME_ADAPTER_MODES = {_RUNTIME_ADAPTER_DIRECT, _RUNTIME_ADAPTER_JOURNAL}


@dataclass(frozen=True)
class StartRunRequest:
    session_id: str
    message: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    workspace: str | None = None
    profile: str | None = None
    provider: str | None = None
    model: str | None = None
    toolsets: list[str] = field(default_factory=list)
    source: str = "webui"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunStartResult:
    run_id: str
    session_id: str
    stream_id: str
    status: str = "started"
    started_at: float | None = None
    cursor: str | None = None
    active_controls: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunEventStream:
    run_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    cursor: str | None = None
    last_event_id: str | None = None


@dataclass(frozen=True)
class RunStatus:
    run_id: str
    session_id: str | None = None
    status: str = "unknown"
    last_event_id: str | None = None
    terminal_state: str | None = None
    active_controls: list[str] = field(default_factory=list)
    pending_approval_id: str | None = None
    pending_clarify_id: str | None = None


@dataclass(frozen=True)
class ControlResult:
    accepted: bool
    status: str = "accepted"
    event_id: str | None = None
    safe_message: str | None = None


class RuntimeAdapter(Protocol):
    def start_run(self, request: StartRunRequest) -> RunStartResult: ...
    def observe_run(self, run_id: str, *, cursor: str | None = None) -> RunEventStream: ...
    def get_run(self, run_id: str) -> RunStatus: ...
    def cancel_run(self, run_id: str) -> ControlResult: ...
    def respond_approval(self, run_id: str, approval_id: str, choice: str) -> ControlResult: ...
    def respond_clarify(self, run_id: str, clarify_id: str, response: str) -> ControlResult: ...


def runtime_adapter_mode(environ: dict[str, str] | None = None) -> str:
    """Return the configured adapter mode, defaulting safely to legacy-direct."""
    source = os.environ if environ is None else environ
    raw = str(source.get(_RUNTIME_ADAPTER_ENV, _RUNTIME_ADAPTER_DIRECT) or "").strip().lower()
    return raw if raw in _VALID_RUNTIME_ADAPTER_MODES else _RUNTIME_ADAPTER_DIRECT


def runtime_adapter_enabled(environ: dict[str, str] | None = None) -> bool:
    return runtime_adapter_mode(environ) == _RUNTIME_ADAPTER_JOURNAL


def _cursor_to_after_seq(cursor: str | None) -> int | None:
    if cursor in (None, ""):
        return None
    try:
        text = str(cursor)
        if ":" in text:
            text = text.rsplit(":", 1)[-1]
        return max(0, int(text))
    except (TypeError, ValueError):
        return 0


def _active_control_result(value: Any) -> ControlResult:
    accepted = bool(value)
    return ControlResult(
        accepted=accepted,
        status="accepted" if accepted else "not-active",
        safe_message=None if accepted else "Legacy control did not accept the request.",
    )


class LegacyJournalRuntimeAdapter:
    """Protocol-translator facade over the current legacy streaming path.

    Delegates keep Slice 2 honest: this adapter has no worker thread, AIAgent
    cache, cancellation registry, approval queue, or clarify queue of its own.
    """

    def __init__(
        self,
        *,
        start_run_delegate: Callable[[StartRunRequest], dict[str, Any]] | None = None,
        cancel_delegate: Callable[[str], Any] | None = None,
        approval_delegate: Callable[[str, str, str], Any] | None = None,
        clarify_delegate: Callable[[str, str, str], Any] | None = None,
        live_stream_lookup: Callable[[str], bool] | None = None,
        session_dir: Path | None = None,
    ):
        self._start_run_delegate = start_run_delegate
        self._cancel_delegate = cancel_delegate
        self._approval_delegate = approval_delegate
        self._clarify_delegate = clarify_delegate
        self._live_stream_lookup = live_stream_lookup or (lambda _run_id: False)
        self._session_dir = Path(session_dir) if session_dir is not None else None

    def start_run(self, request: StartRunRequest) -> RunStartResult:
        if self._start_run_delegate is None:
            raise NotImplementedError("LegacyJournalRuntimeAdapter.start_run requires a legacy delegate")
        payload = dict(self._start_run_delegate(request) or {})
        stream_id = str(payload.get("stream_id") or payload.get("run_id") or "")
        run_id = str(payload.get("run_id") or stream_id)
        session_id = str(payload.get("session_id") or request.session_id)
        active_controls = payload.get("active_controls")
        if not isinstance(active_controls, list):
            active_controls = ["cancel"] if stream_id else []
        return RunStartResult(
            run_id=run_id,
            session_id=session_id,
            stream_id=stream_id,
            status=str(payload.get("status") or "started"),
            started_at=payload.get("started_at"),
            cursor=payload.get("cursor"),
            active_controls=active_controls,
            payload=payload,
        )

    def observe_run(self, run_id: str, *, cursor: str | None = None) -> RunEventStream:
        from api.run_journal import find_run_summary, read_run_events

        summary = find_run_summary(run_id, session_dir=self._session_dir)
        if not summary:
            return RunEventStream(run_id=run_id, events=[], cursor=cursor, last_event_id=None)
        journal = read_run_events(
            str(summary.get("session_id") or ""),
            run_id,
            after_seq=_cursor_to_after_seq(cursor),
            session_dir=self._session_dir,
        )
        events = list(journal.get("events") or [])
        last_event_id = events[-1].get("event_id") if events else summary.get("last_event_id")
        return RunEventStream(
            run_id=run_id,
            events=events,
            cursor=str(events[-1].get("seq")) if events else cursor,
            last_event_id=last_event_id,
        )

    def get_run(self, run_id: str) -> RunStatus:
        from api.run_journal import find_run_summary

        live = bool(self._live_stream_lookup(run_id))
        summary = find_run_summary(run_id, session_dir=self._session_dir)
        if live:
            return RunStatus(
                run_id=run_id,
                session_id=str((summary or {}).get("session_id") or "") or None,
                status="running",
                last_event_id=(summary or {}).get("last_event_id"),
                terminal_state=None,
                active_controls=["cancel"],
            )
        if summary:
            terminal_state = summary.get("terminal_state")
            return RunStatus(
                run_id=run_id,
                session_id=str(summary.get("session_id") or "") or None,
                status=str(terminal_state or "unknown"),
                last_event_id=summary.get("last_event_id"),
                terminal_state=terminal_state,
                active_controls=[],
            )
        return RunStatus(run_id=run_id)

    def cancel_run(self, run_id: str) -> ControlResult:
        if self._cancel_delegate is None:
            return ControlResult(False, status="unsupported", safe_message="Cancel is not wired for this adapter.")
        return _active_control_result(self._cancel_delegate(run_id))

    def respond_approval(self, run_id: str, approval_id: str, choice: str) -> ControlResult:
        if self._approval_delegate is None:
            return ControlResult(False, status="unsupported", safe_message="Approval is delegated to the legacy path.")
        return _active_control_result(self._approval_delegate(run_id, approval_id, choice))

    def respond_clarify(self, run_id: str, clarify_id: str, response: str) -> ControlResult:
        if self._clarify_delegate is None:
            return ControlResult(False, status="unsupported", safe_message="Clarify is delegated to the legacy path.")
        return _active_control_result(self._clarify_delegate(run_id, clarify_id, response))
