"""Regression test for #6017: "Approve once" must not persist as a session
approval.

Bug
---
In the local (non-gateway) approval path, ``_resolve_approval_legacy`` in
``api/routes.py`` grouped ``"once"`` together with ``"session"`` and called
``approve_session()`` for both. ``approve_session()`` records the pattern in the
session-wide allowlist that the guard later consults, so a single "Approve
once" click silently approved every later matching tool call for the whole
session -- a destructive tool could then run with no approval card.

Fix
---
Only ``"session"``/``"always"`` persist. ``"once"`` releases the currently
parked call via ``resolve_gateway_approval()`` (which unblocks the waiting agent
thread for every choice) and writes nothing to ``_session_approved``.

State layer: ``tools.approval._session_approved`` -- the session-scoped approval
allowlist that ``is_approved()`` consults. These tests drive the real
``_resolve_approval_legacy`` against a seeded gateway approval entry and assert
the persistence outcome directly via ``is_approved()``.
"""
from __future__ import annotations

import importlib
import uuid

from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules


def _seed_gateway_entry(ta, sid, pattern_key):
    """Park one guarded command in the gateway queue for ``sid``.

    Mirrors the local in-process agent path: a guarded command blocks on an
    ``_ApprovalEntry`` in ``_gateway_queues`` waiting for the user's choice.
    """
    entry = ta._ApprovalEntry({
        "command": "rm -rf /tmp/6017",
        "pattern_key": pattern_key,
        "pattern_keys": [pattern_key],
        "description": "recursive delete",
    })
    with ta._lock:
        ta._pending.pop(sid, None)
        ta._gateway_queues.setdefault(sid, []).append(entry)
    return entry


def _cleanup(ta, sid):
    with ta._lock:
        ta._gateway_queues.pop(sid, None)
        ta._session_approved.pop(sid, None)
        ta._pending.pop(sid, None)


def test_approve_once_releases_call_but_does_not_persist():
    """'once' unblocks the current call yet leaves nothing session-approved, so
    the next matching guarded call still re-prompts."""
    routes = importlib.import_module("api.routes")
    ta = importlib.import_module("tools.approval")

    sid = f"issue6017-once-{uuid.uuid4().hex[:8]}"
    key = f"pattern-{uuid.uuid4().hex[:8]}"
    entry = _seed_gateway_entry(ta, sid, key)
    try:
        assert not ta.is_approved(sid, key), "precondition: pattern not yet approved"
        assert not entry.event.is_set(), "precondition: parked call not yet released"

        routes._resolve_approval_legacy(sid, "", "once")

        assert entry.event.is_set(), "'once' must release the currently parked call"
        assert ta.is_approved(sid, key) is False, (
            "'once' must NOT persist a session approval -- the next matching "
            "guarded call must re-prompt (#6017)"
        )
    finally:
        _cleanup(ta, sid)


def test_approve_session_persists_session_approval():
    """Regression guard: 'session' still persists, so a later matching call in
    the same session is not re-prompted."""
    routes = importlib.import_module("api.routes")
    ta = importlib.import_module("tools.approval")

    sid = f"issue6017-session-{uuid.uuid4().hex[:8]}"
    key = f"pattern-{uuid.uuid4().hex[:8]}"
    entry = _seed_gateway_entry(ta, sid, key)
    try:
        routes._resolve_approval_legacy(sid, "", "session")

        assert entry.event.is_set(), "'session' must release the currently parked call"
        assert ta.is_approved(sid, key) is True, (
            "'session' must persist so later matching calls are not re-prompted"
        )
    finally:
        _cleanup(ta, sid)
