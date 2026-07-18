"""Stopgap drift guard: the frontend ``_MSG_LIMIT_MAX`` fallback must match the
backend ``_MAX_MSG_LIMIT`` ceiling.

PR #6152 introduced the server-side ``_MAX_MSG_LIMIT`` ceiling on
``GET /api/session ?msg_limit=``. PR #6154 added a hand-mirrored
``_MSG_LIMIT_MAX`` in ``static/sessions.js`` so the frontend knows when to
switch from the growing-tail strategy to ``msg_before`` paging. If the two
drift, load-older silently stalls at the old value — the exact regression #6154
fixed. The durable fix (#6177) exposes the ceiling via ``/api/session`` metadata
(``_msg_limit_max`` field) so the frontend reads it dynamically and the
``_MSG_LIMIT_MAX`` becomes just a fallback for older servers. Until the metadata
field is universally available, this static-source assertion catches the drift.

Placement note: this test ships in #6152 (where ``_MAX_MSG_LIMIT`` is defined).
The frontend ``_MSG_LIMIT_MAX`` only exists once #6154 lands, so the assertion
SKIPS when the JS constant is absent — it activates automatically once both
branches are on master, and bites if the two values ever diverge.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _backend_max_msg_limit() -> int:
    """Extract ``_MAX_MSG_LIMIT = <int>`` from api/routes.py."""
    src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    m = re.search(r"^_MAX_MSG_LIMIT\s*=\s*(\d+)\s*$", src, re.MULTILINE)
    assert m, "_MAX_MSG_LIMIT not found in api/routes.py"
    return int(m.group(1))


def _frontend_msg_limit_max():
    """Extract ``_MSG_LIMIT_MAX = <int>`` from static/sessions.js, or return
    None when the constant is absent (PR #6154 not yet landed)."""
    sessions = ROOT / "static" / "sessions.js"
    if not sessions.exists():
        return None
    src = sessions.read_text(encoding="utf-8")
    m = re.search(r"const\s+_MSG_LIMIT_MAX\s*=\s*(\d+)\s*;", src)
    return int(m.group(1)) if m else None


def test_frontend_msg_limit_max_matches_backend_ceiling():
    """When both constants exist (i.e. #6152 + #6154 have both landed), the
    frontend fallback MUST equal the backend ceiling or load-older silently
    stalls for long sessions. Skips gracefully when the JS constant is absent
    (the #6152-only state) so this test ships atomically with the backend
    constant without depending on the #6154 branch."""
    backend = _backend_max_msg_limit()
    frontend = _frontend_msg_limit_max()
    if frontend is None:
        # PR #6154 (the frontend mirror) hasn't landed yet — skip rather than
        # fail. Once both are on master this branch is exercised and bites on
        # any drift.
        import pytest
        pytest.skip("_MSG_LIMIT_MAX not yet present in static/sessions.js "
                    "(lands in #6154); drift guard activates once both are on master")
    assert frontend == backend, (
        f"frontend _MSG_LIMIT_MAX ({frontend}) != backend _MAX_MSG_LIMIT ({backend}): "
        f"load-older will silently stall at the wrong value. Update both together, "
        f"or land #6177 (metadata-exposure) to retire the mirror."
    )


def test_backend_max_msg_limit_is_reasonable():
    """The backend ceiling must exist and stay in a sane range (the frontend's
    real pagination grows by ~30, so anything in [100, 2000] is generous)."""
    backend = _backend_max_msg_limit()
    assert 100 <= backend <= 2000, f"_MAX_MSG_LIMIT out of expected range: {backend}"
