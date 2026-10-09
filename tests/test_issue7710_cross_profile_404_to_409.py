"""Regression tests for issue #7710 — cross-profile session guards return 409 + ``session_profile_mismatch`` instead of masking a known-other-profile session as 404.

The detail-load endpoint (and the foreign-session synthesizer) already
distinguish "session owned by a KNOWN other profile" (return ``409
session_profile_mismatch``) from "session missing/legacy with no
profile stamped" (return ``404 Session not found`` so the frontend
self-heal clears the stale URL).

The generic request-guard
``_session_id_visible_to_request_profile`` (used by
``_guard_request_session_visibility``) flattened both cases to a
``404 Session not found``, so any cross-profile POST/PATCH/DELETE
(archive, rename, pin, delete, …) failed with a misleading "Session
not found" instead of the actionable 409 the detail endpoint emits.
The fix mirrors the detail endpoint's contract in the generic guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_helper_body() -> str:
    """Extract ``_session_id_visible_to_request_profile`` as importable text."""
    import textwrap
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    start = src.index("def _session_id_visible_to_request_profile(")
    end_marker = "def _stream_id_owner_session_id"
    end = src.index(end_marker, start)
    return textwrap.dedent(src[start:end])


def _exec_helper(scope: dict) -> None:
    """Execute the helper body inside ``scope``."""
    body = _read_helper_body()
    exec(body, scope)


# ---------------------------------------------------------------------------
# Profile-mismatch path: known other profile → 409 + session_profile_mismatch
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, profile: str | None) -> None:
        self.profile = profile


class _FakeHandler:
    """Minimal stand-in for the routes handler the helper writes through."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, dict | str | None]] = []


@pytest.fixture
def helper_scope():
    """Build an exec scope with the helper's dependencies stubbed.

    ``get_session`` is the only thing the helper actually calls into
    (besides the visibility predicate which is itself inlined). The
    visibility predicate is monkey-patched per-test so we can drive both
    the "match" and "mismatch" branches deterministically.
    """
    scope: dict = {
        # j / bad — the helper writes through these. Record instead of
        # emit so the test can assert status + body.
        "j": lambda handler, payload, status=200, **_: handler.writes.append((status, payload)),
        "bad": lambda handler, msg, status=400: handler.writes.append((status, msg)),
        # get_session is required so the helper can resolve the row.
        "get_session": lambda sid, metadata_only=True: scope["_resolve"](sid),
        # is_safe_session_id — accept anything that looks like a sid.
        "is_safe_session_id": lambda sid: bool(sid) and len(str(sid)) < 256,
    }
    return scope


def test_profile_mismatch_returns_409_with_code_and_profile(helper_scope) -> None:
    """A session owned by a known other profile MUST yield 409 ``session_profile_mismatch``."""
    handler = _FakeHandler()
    helper_scope["_resolve"] = lambda sid: _FakeSession(profile="alpha")
    # Visibility predicate: returns False (profile mismatch).
    helper_scope["_session_visible_to_active_profile"] = lambda session_profile, _h: False

    _exec_helper(helper_scope)
    fn = helper_scope["_session_id_visible_to_request_profile"]
    assert fn(handler, "sess-1") is False
    assert len(handler.writes) == 1, handler.writes
    status, body = handler.writes[0]
    assert status == 409, body
    assert body["code"] == "session_profile_mismatch"
    assert body["session_id"] == "sess-1"
    assert body["profile"] == "alpha"
    assert "different profile" in body["error"].lower()


def test_unknown_profile_returns_404_for_frontend_self_heal(helper_scope) -> None:
    """A session with ``profile=None`` MUST keep 404 so the frontend's self-heal fires."""
    handler = _FakeHandler()
    helper_scope["_resolve"] = lambda sid: _FakeSession(profile=None)
    helper_scope["_session_visible_to_active_profile"] = lambda session_profile, _h: False

    _exec_helper(helper_scope)
    fn = helper_scope["_session_id_visible_to_request_profile"]
    assert fn(handler, "sess-1") is False
    assert len(handler.writes) == 1, handler.writes
    status, msg = handler.writes[0]
    assert status == 404
    assert msg == "Session not found"


def test_visible_session_returns_no_error(helper_scope) -> None:
    """A session owned by the active profile MUST pass the guard silently."""
    handler = _FakeHandler()
    helper_scope["_resolve"] = lambda sid: _FakeSession(profile="default")
    helper_scope["_session_visible_to_active_profile"] = lambda session_profile, _h: True

    _exec_helper(helper_scope)
    fn = helper_scope["_session_id_visible_to_request_profile"]
    assert fn(handler, "sess-1") is True
    assert handler.writes == []


def test_missing_session_returns_no_error(helper_scope) -> None:
    """A session id that get_session cannot resolve MUST pass the guard silently (the route will 404 later)."""
    handler = _FakeHandler()
    helper_scope["_resolve"] = lambda sid: (_ for _ in ()).throw(KeyError(sid))

    _exec_helper(helper_scope)
    fn = helper_scope["_session_id_visible_to_request_profile"]
    assert fn(handler, "sess-1") is True
    assert handler.writes == []


def test_emit_error_false_suppresses_response(helper_scope) -> None:
    """``emit_error=False`` MUST return False without writing any response (used by the guard's exemption probe)."""
    handler = _FakeHandler()
    helper_scope["_resolve"] = lambda sid: _FakeSession(profile="alpha")
    helper_scope["_session_visible_to_active_profile"] = lambda session_profile, _h: False

    _exec_helper(helper_scope)
    fn = helper_scope["_session_id_visible_to_request_profile"]
    assert fn(handler, "sess-1", emit_error=False) is False
    assert handler.writes == []


# ---------------------------------------------------------------------------
# Source-shape: the synthesised 409 payload MUST keep the four contract
# fields and the status code, in the helper. A silent reversion to the
# original single-line ``bad(..., 404)`` fails the suite.
# ---------------------------------------------------------------------------


def test_source_emits_409_not_404_in_helper() -> None:
    """The helper body MUST contain a 409 ``session_profile_mismatch`` branch.

    Catches a future regression that folds the two cases back together
    (or drops the 409 status code in favour of 404). The 404 case is
    still preserved for the None-profile self-heal path, but only as a
    fall-through inside the same block — not as the sole outcome.
    """
    body = _read_helper_body()
    assert "status=409" in body, (
        "the helper no longer emits 409 for a known-other-profile session"
    )
    assert "session_profile_mismatch" in body, (
        "the helper no longer carries the ``session_profile_mismatch`` code"
    )
    assert "Session not found" in body, (
        "the None-profile 404 self-heal path was dropped — "
        "keep the 404 for the None-profile branch so the frontend "
        "self-heal still fires for actually-missing sids"
    )
