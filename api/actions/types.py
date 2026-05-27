"""Typed action contracts for the Hermes Action Bus.

The bus is intentionally synchronous: Hermes WebUI runs on
``ThreadingHTTPServer`` and the rest of ``api/*`` is plain ``def``. Actions
follow the same style so dispatch can happen inside any POST handler
thread without spinning an event loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol


SILENT_SENTINEL = "[SILENT]"


def _noop_emit(event_type: str, payload: dict) -> None:
    """Default ``emit_event`` that drops the event on the floor.

    Real entry points (the ``/api/actions`` HTTP adapter, cron, webhooks)
    pass a real publisher backed by ``api/session_events.py``. Unit tests
    pass their own capturing closure.
    """


def _no_dispatch(*args, **kwargs):  # pragma: no cover - guard against accidental nested dispatch
    raise RuntimeError(
        "ActionContext.dispatch was not wired by the caller. "
        "Pass registry.dispatch (or a bound equivalent) when constructing "
        "the context if your action needs to chain into another action."
    )


@dataclass
class ActionContext:
    """Runtime context handed to an action at dispatch time.

    *session_id* and *user_id* are optional; some actions (e.g. webhook
    health pings) have no session. *source* is a short string identifying
    the entry point: ``"webui_api"``, ``"cron"``, ``"webhook"``,
    ``"gateway"``, ``"internal"``. *emit_event* is a synchronous callable
    that publishes a typed event onto the WebUI SSE channel (see
    ``api/session_events.py``); the default is a no-op so unit tests do
    not need a real publisher. *dispatch* lets an action chain into the
    bus; the default raises so that an unwired context fails loudly the
    first time an action tries to nest.
    """

    session_id: Optional[str] = None
    user_id: Optional[str] = None
    source: str = "unknown"
    emit_event: Callable[[str, dict], None] = field(default=_noop_emit)
    dispatch: Callable[..., "ActionResult"] = field(default=_no_dispatch)
    request_meta: dict = field(default_factory=dict)
    # Open slot for adapter-specific injections (db handles, agent runners,
    # session stores) without committing to a typed shape in v1. Follow-up
    # PRs that add real session-touching actions will use this and we will
    # tighten the type then.
    extras: dict = field(default_factory=dict)


@dataclass
class ActionResult:
    """Normalized return shape for every action.

    The three concerns are kept separate on purpose:

    - ``ok``: did the action complete without an unrecoverable error
    - ``silent`` / ``assistant_message``: should anything surface to chat
    - ``refresh_chat``: should open WebUI clients refresh session state

    ``error`` is set when ``ok=False`` so callers can surface a short
    string to the user or to logs without parsing exceptions.
    """

    ok: bool = True
    silent: bool = True
    assistant_message: Optional[str] = None
    refresh_chat: bool = False
    error: Optional[str] = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "silent": self.silent,
            "assistant_message": self.assistant_message,
            "refresh_chat": self.refresh_chat,
            "error": self.error,
            "meta": self.meta,
        }


class Action(Protocol):
    """Every registered action implements this protocol.

    ``name`` is the dotted identifier used at the dispatch site
    (``"echo.test"``, ``"session.nudge"``, etc). ``run`` is synchronous;
    long-running work belongs in a worker thread spawned by the action,
    not in an event loop.
    """

    name: str

    def run(self, payload: dict, context: ActionContext) -> ActionResult:
        ...
