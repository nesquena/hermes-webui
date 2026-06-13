"""POST /api/actions -- HTTP adapter for the Hermes Action Bus.

Kept deliberately thin: validate the request body, build an
:class:`~api.actions.ActionContext`, dispatch, and return a normalized
``(body_dict, status_int)`` pair. CSRF and authentication run in
``api/routes.py::handle_post`` before this is called, matching the
pattern of every other POST handler in the WebUI.

The registry is injected so unit tests can supply a clean instance
without leaking module-level state between tests. Production callers
pass ``default_registry`` (also the default value here).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from api.actions import (
    ActionContext,
    ActionNotFound,
    ActionRegistry,
    default_registry,
)


def _resolve_emit_event() -> Callable[[str, dict], None]:
    """Return the production SSE publisher, or a no-op if it is absent.

    The publisher is resolved lazily on each request so test runs that
    monkey-patch ``api.session_events`` (or do not import it at all)
    still get a working bus. When ``publish_session_event`` lands in a
    follow-up PR this will pick it up automatically; until then we drop
    events on the floor, which matches the v1 surface where no builtin
    emits events.
    """
    try:
        # publish_session_event intentionally does not yet exist on this
        # PR's branch -- it lands in the session.nudge follow-up. Grepping
        # for the symbol on master today will only return this import; the
        # ImportError fallback below resolves to a no-op for every request
        # until then, which matches the v1 surface where no builtin emits
        # SSE events. Function-level docstring above documents the same
        # behavior at the API level.
        from api.session_events import publish_session_event  # type: ignore[attr-defined]
    except ImportError:
        return lambda _name, _payload: None
    return publish_session_event


def handle_actions_post(
    handler: Any,
    body: dict,
    registry: ActionRegistry = default_registry,
    *,
    emit_event: Optional[Callable[[str, dict], None]] = None,
) -> tuple[dict, int]:
    """Validate, dispatch, and serialize.

    Returns ``(response_body_dict, http_status_int)``. The caller in
    ``api/routes.py`` is responsible for the actual ``j(handler, ...)``
    write so all routes share one response helper.

    *handler* is the ``BaseHTTPRequestHandler`` instance and is used
    only to read non-required request metadata (remote address, user
    id) into ``ActionContext.request_meta``. It is fine for *handler*
    to be ``None`` in tests; this function does not write to it.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "request body must be a JSON object"}, 400

    action = body.get("action")
    if not isinstance(action, str) or not action.strip():
        return {"ok": False, "error": "action required"}, 400

    payload = body.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload must be an object"}, 400

    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return {"ok": False, "error": "session_id must be a string"}, 400

    idempotency_key = body.get("idempotency_key")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        return {"ok": False, "error": "idempotency_key must be a string"}, 400

    resolved_emit = emit_event if emit_event is not None else _resolve_emit_event()

    context = ActionContext(
        session_id=session_id,
        user_id=getattr(handler, "user_id", None),
        source="webui_api",
        emit_event=resolved_emit,
        dispatch=registry.dispatch,
        request_meta={
            "remote": _safe_remote(handler),
        },
    )

    try:
        result = registry.dispatch(
            action=action,
            payload=payload,
            context=context,
            idempotency_key=idempotency_key,
        )
    except ActionNotFound:
        return {"ok": False, "error": f"unknown action: {action}"}, 404

    return result.to_dict(), 200


def _safe_remote(handler: Any) -> Optional[str]:
    if handler is None:
        return None
    addr = getattr(handler, "client_address", None)
    if isinstance(addr, tuple) and addr:
        return str(addr[0])
    remote = getattr(handler, "remote", None)
    return str(remote) if remote is not None else None
