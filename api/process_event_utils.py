"""Shared helpers for WebUI completion/delegation delivery."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def completion_delivery_id(evt: Any) -> str:
    """Return the stable WebUI delivery/dedupe id for a completion event.

    Terminal background-process events use ``session_id`` for the process id.
    Async ``delegate_task`` completions carry ``delegation_id`` instead, so both
    WebUI delivery paths must key those events by ``delegation_id``.
    """
    if not isinstance(evt, dict):
        return ""
    if evt.get("type") == "async_delegation":
        return str(evt.get("delegation_id") or "").strip()
    return str(evt.get("session_id") or "").strip()


def mark_async_delegation_record_consumed(delegation_id: str) -> bool:
    """Best-effort marker for Hermes async-delegation recovery records.

    Newer Hermes Agent builds expose
    ``tools.async_delegation.mark_async_delegation_consumed`` so recovery
    sweeps do not re-inject a delegation result that WebUI already delivered.
    WebUI must not hard-require that API while mixed deployments are common, so
    absence or failure is a safe no-op.
    """
    deleg_id = str(delegation_id or "").strip()
    if not deleg_id:
        return False
    try:
        from tools.async_delegation import mark_async_delegation_consumed
    except Exception:
        logger.debug(
            "Async delegation consumed marker unavailable; skipping record marker",
            exc_info=True,
        )
        return False
    try:
        mark_async_delegation_consumed(deleg_id)
        return True
    except Exception:
        logger.debug(
            "Failed to mark async delegation record consumed for %s",
            deleg_id,
            exc_info=True,
        )
        return False
