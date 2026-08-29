"""Tests: the display-path state.db row backstop is GATED on the absence of a
truncation_boundary prefix.

Regression (reviewer): ``_STATE_DB_DISPLAY_ROW_BACKSTOP`` was applied
unconditionally to the ``GET /api/session`` display path's state.db read. But
``merge_session_messages_append_only`` needs the rows at/around the session's
``truncation_boundary`` (the preserved prefix) to reconcile correctly. A
newest-N-only SQL cap drops those boundary rows for a >N-row session and
corrupts the merge (silently losing the preserved prefix), and also prevents
older-page (``msg_before``) requests from reaching them.

The fix gates the backstop via ``_state_db_backstop_limit_for_display`` on the
same conditions ``_state_db_since_timestamp_for_limited_display`` uses to decide
a read is boundary-free: NOT ``msg_before`` paging, and NO
``truncation_watermark`` / ``truncation_boundary``. Those cases stay on the full
(uncapped) read. The helper is the unit under test (driving the full handler
end-to-end is brittle across its many dependencies).
"""
from __future__ import annotations

from api.routes import _STATE_DB_DISPLAY_ROW_BACKSTOP, _state_db_backstop_limit_for_display


class _StubSession:
    """Minimal object exposing the boundary attributes the gate checks."""
    def __init__(self, **attrs):
        self.truncation_watermark = attrs.get("truncation_watermark", None)
        self.truncation_boundary = attrs.get("truncation_boundary", None)


def test_backstop_applied_when_no_boundary_prefix():
    """An uncompressed, initial-tail session (no truncation_watermark /
    truncation_boundary, no msg_before) gets the defensive row backstop."""
    assert _state_db_backstop_limit_for_display(_StubSession(), msg_before=None) == _STATE_DB_DISPLAY_ROW_BACKSTOP


def test_backstop_skipped_when_truncation_boundary_set():
    """Regression: a compressed session (truncation_boundary set) needs its
    preserved-prefix rows for the merge, so the backstop must NOT cap the read."""
    stub = _StubSession(truncation_boundary="some-boundary-marker")
    assert _state_db_backstop_limit_for_display(stub, msg_before=None) is None


def test_backstop_skipped_when_truncation_watermark_set():
    """Same gate for truncation_watermark — the merge needs the prefix rows."""
    stub = _StubSession(truncation_watermark=12345)
    assert _state_db_backstop_limit_for_display(stub, msg_before=None) is None


def test_backstop_skipped_when_msg_before_paging():
    """Older-page (msg_before) requests need to reach the boundary rows, so the
    backstop must not cap them either."""
    stub = _StubSession()
    assert _state_db_backstop_limit_for_display(stub, msg_before=500) is None


def test_backstop_boundary_check_handles_empty_string_as_unset_or_set():
    """The gate treats empty-string boundary markers as 'not set' (matches the
    existing since_timestamp helper's `in (None, "")` check). A real marker
    (non-empty) triggers the skip."""
    # Empty string = treated as unset → backstop applies.
    assert _state_db_backstop_limit_for_display(
        _StubSession(truncation_boundary=""), msg_before=None
    ) == _STATE_DB_DISPLAY_ROW_BACKSTOP
    # Non-empty string = boundary present → backstop skipped.
    assert _state_db_backstop_limit_for_display(
        _StubSession(truncation_watermark="wm"), msg_before=None
    ) is None
