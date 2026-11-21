"""#9041 item 1: a session file swapped after the rows were reserved must not
hold the delegation lease for the whole TTL.

``_claim_and_wake`` reserves the pending delivery rows first and only then
wakes the session. When the session's sidecar is swapped for another
session's body in that window, the wake raises
``SessionFileNamesOtherSession`` out of ``_cutoff`` / ``_held_reservation``,
and ``poll_once`` catches it as a plain ``Exception``:

- it logs a WARNING, which repeats on every poll, and
- the reserved rows keep their lease until ``RESERVATION_TTL_S`` (120 s), so
  the rows are never delivered.

The sibling reader that runs BEFORE the reserve
(``_live_webui_sid``) already treats this as ``OwnershipUnknown``: the row is
not woken, nothing is recorded as another profile's, and the poll stays
quiet. The two readers that run after the reserve must behave the same way,
and the reserved rows must be released instead of being parked for the TTL.

The tests here pin the window that the existing
``test_sidecar_holding_another_sessions_body_is_retried_quietly`` does not
cover: that one swaps the file BEFORE the reserve, so the rows are never
reserved in the first place.
"""

from __future__ import annotations

import json
import logging

import pytest

import api.gateway_delegation_wakeup as gdw
import api.models as models

from tests.test_gateway_delegation_wakeup import FakeDB, env  # noqa: F401


def _swap_body_to_another_session(sessions_dir, sid: str, victim: str) -> None:
    """Rewrite *sid*'s sidecar so its body names *victim* instead."""
    path = sessions_dir / f"{sid}.json"
    body = json.loads(path.read_text())
    body["session_id"] = victim
    body["messages"] = body["messages"] + [{"role": "user", "content": "the other conversation"}]
    path.write_text(json.dumps(body))
    models.SESSIONS.clear()


@pytest.fixture
def swap_after_reserve(monkeypatch, tmp_path):
    """Swap the sidecar right after the delivery rows are reserved."""
    real_reserve = FakeDB.reserve_caller_history_deliveries
    state = {"swapped": False}

    def reserve_and_swap(self, session_id, owner, ttl_seconds, limit=None):
        reserved = real_reserve(self, session_id, owner, ttl_seconds, limit)
        if not state["swapped"]:
            state["swapped"] = True
            _swap_body_to_another_session(tmp_path / "sessions", session_id, "victim-9041")
        return reserved

    monkeypatch.setattr(FakeDB, "reserve_caller_history_deliveries", reserve_and_swap)
    return state


def test_swapped_session_after_reserve_releases_the_lease(env, swap_after_reserve, tmp_path, caplog):  # noqa: F811
    """The reserved rows are released, not parked for the reservation TTL."""
    calls, _ = env
    FakeDB.add("sid1", "deleg_swap_after_reserve")

    with caplog.at_level(logging.DEBUG, logger=gdw.logger.name):
        assert gdw.poll_once(0) == 0

    assert swap_after_reserve["swapped"] is True
    row = FakeDB.rows[1]
    # The lease must not survive the poll: the rows stay deliverable.
    assert row["lease"] is None, row
    assert row["consumed"] is False


def test_swapped_session_after_reserve_delivers_nothing(env, swap_after_reserve, tmp_path, caplog):  # noqa: F811
    """No result reaches the swapped conversation."""
    calls, _ = env
    FakeDB.add("sid1", "deleg_swap_after_reserve")

    with caplog.at_level(logging.DEBUG, logger=gdw.logger.name):
        gdw.poll_once(0)

    # The wake may be attempted -- the file is only swapped after the rows are
    # reserved -- but nothing is published into the session's context.
    for call in calls:
        assert call["committed_before"] is False
    assert all(not r["consumed"] for r in FakeDB.rows.values())


def test_swapped_session_after_reserve_logs_no_warning(env, swap_after_reserve, caplog):  # noqa: F811
    """An unreadable owner is retried quietly, exactly like the pre-reserve reader."""
    FakeDB.add("sid1", "deleg_swap_after_reserve")

    with caplog.at_level(logging.DEBUG, logger=gdw.logger.name):
        gdw.poll_once(0)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, [r.getMessage() for r in warnings]


def test_a_repaired_file_delivers_on_the_next_poll(env, swap_after_reserve, tmp_path):  # noqa: F811
    """Repairing the sidecar lets the row out on the next poll."""
    calls, _ = env
    path = tmp_path / "sessions" / "sid1.json"
    original = path.read_text()
    FakeDB.add("sid1", "deleg_swap_after_reserve")

    assert gdw.poll_once(0) == 0
    assert FakeDB.rows[1]["lease"] is None, FakeDB.rows[1]
    # The first poll attempted the wake and found the swapped file.
    assert len(calls) == 1

    path.write_text(original)
    models.SESSIONS.clear()
    # The repaired file lets the row out on the next poll.
    assert gdw.poll_once(0) == 1
    assert len(calls) == 2 and calls[-1]["sid"] == "sid1"
    assert FakeDB.rows[1]["consumed"] is True


def test_cutoff_treats_a_swapped_sidecar_as_ownership_unknown(env, tmp_path, monkeypatch):  # noqa: F811
    """``_cutoff`` is one of the two post-reserve readers; it must not raise the
    raw file-mismatch error."""
    _swap_body_to_another_session(tmp_path / "sessions", "sid1", "victim-9041")

    with pytest.raises(gdw.OwnershipUnknown):
        gdw._cutoff("sid1")


def test_held_reservation_treats_a_swapped_sidecar_as_ownership_unknown(env, tmp_path):  # noqa: F811
    """``_held_reservation`` is the other post-reserve reader."""
    _swap_body_to_another_session(tmp_path / "sessions", "sid1", "victim-9041")

    with pytest.raises(gdw.OwnershipUnknown):
        gdw._held_reservation("sid1", None)
