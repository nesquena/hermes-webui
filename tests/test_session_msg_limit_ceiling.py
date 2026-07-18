"""Tests: server-side ``msg_limit`` ceiling on ``GET /api/session``.

A client could request ``msg_limit=1000000`` (or the frontend's
``msg_limit=9999`` outline-jump path) and force the server to assemble and
serialize an unbounded message payload. The handler now clamps ``msg_limit`` to
``_MAX_MSG_LIMIT`` (generous — far above any legitimate visible-row window) so
real pagination is unaffected while the pathological/oversized request is
bounded. The existing ``_messages_truncated`` signal covers the clamped case.

The parse+clamp lives in ``_parse_msg_limit`` so the clamping expression has
direct test coverage (driving the handler end-to-end would require a live
session + state.db; the helper is the unit under test).
"""
from __future__ import annotations

from api.routes import _MAX_MSG_LIMIT, _parse_msg_limit


def test_max_msg_limit_constant_is_reasonable():
    """The ceiling must exist and be well above legitimate pagination sizes
    (the frontend grows windows by ~30, initial load is 30) but finite."""
    assert isinstance(_MAX_MSG_LIMIT, int)
    assert 100 <= _MAX_MSG_LIMIT <= 2000, _MAX_MSG_LIMIT


def test_parse_msg_limit_none_when_absent_or_empty():
    """No value (the bare no-msg_limit path) returns None — intentionally the
    'full transcript' escape hatch for branch/undo/jump-to-start flows."""
    assert _parse_msg_limit(None) is None
    assert _parse_msg_limit("") is None


def test_parse_msg_limit_none_when_malformed():
    """A non-numeric value returns None rather than raising (matches the
    pre-fix behavior where a ValueError fell through to msg_limit=None)."""
    assert _parse_msg_limit("not-a-number") is None
    assert _parse_msg_limit("abc") is None


def test_parse_msg_limit_passes_legit_sizes_unchanged():
    """Legitimate pagination sizes (5/30/60/100) are returned verbatim — the
    ceiling only caps oversized requests."""
    for legit in (1, 5, 30, 60, 100):
        if legit > _MAX_MSG_LIMIT:
            continue
        assert _parse_msg_limit(str(legit)) == legit, f"legit {legit} altered"


def test_parse_msg_limit_clamps_oversized_to_ceiling():
    """An over-ceiling request (the outline-jump 9999, or a hostile 1000000) is
    clamped down to _MAX_MSG_LIMIT — exactly the regression this PR fixes."""
    assert _parse_msg_limit("9999") == _MAX_MSG_LIMIT
    assert _parse_msg_limit("1000000") == _MAX_MSG_LIMIT
    assert _parse_msg_limit(str(_MAX_MSG_LIMIT + 1)) == _MAX_MSG_LIMIT


def test_parse_msg_limit_ceiling_boundary_itself_passes():
    """A request exactly at the ceiling is allowed (clamp is inclusive)."""
    assert _parse_msg_limit(str(_MAX_MSG_LIMIT)) == _MAX_MSG_LIMIT


def test_parse_msg_limit_zero_and_negative_clamp_to_one():
    """Non-positive values clamp to the minimum (1), not None — a caller asking
    for msg_limit=0 gets a 1-row window, not the full transcript."""
    assert _parse_msg_limit("0") == 1
    assert _parse_msg_limit("-5") == 1
