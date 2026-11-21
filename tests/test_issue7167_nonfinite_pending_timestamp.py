"""Regression tests: a non-finite ``pending_started_at`` must not break repair.

The review finding reproduced (via the real production recovery entry
point) that a session with existing history and ``pending_started_at=+inf``
raised ``OverflowError: cannot convert float infinity to integer`` inside
``_apply_core_sync_or_error_marker`` BEFORE the prompt/journal was persisted:
repair aborted, the pending turn stayed resident, and every later repair
attempt hit the same crash. Upstream resolves the same input through the
shared ``_recovered_pending_timestamp`` helper (finite value retained,
non-finite value replaced by ``int(time.time())``).

The non-empty-history branch re-derived its timestamps with inline logic
(``int(session.pending_started_at)``) instead of that helper, so ``+inf``
crashed there while the two sibling branches (core-sync, no-core) were
already safe.

These tests drive the PRODUCTION entry point
(``_apply_core_sync_or_error_marker``) with a real run journal, the same way
the WebUI re-repairs a sidecar on each cache-miss read. They also pin the
two invariants the fix must not trade away:

* a finite fractional timestamp keeps its full-precision identity (500.9
  stays 500.9, so an adjacent same-text turn stamped 500.0 is NOT merged
  into the current turn); and
* the persisted sidecar never contains a non-finite JSON literal
  (``Infinity`` / ``NaN``), which standard JSON parsers reject.
"""

from __future__ import annotations

import json
import math

import pytest

import api.profiles as profiles
import api.run_journal as run_journal
import api.models as models
from api.models import Session, _apply_core_sync_or_error_marker

PROMPT = "crash-turn prompt"
ANSWER = "recovered answer"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + the WebUI session dir so journal writes sandbox."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def _completed_journal(sid: str, stream_id: str) -> None:
    """One journaled token plus a terminal ``done``: the completed shape."""
    run_journal.append_run_event(sid, stream_id, "token", {"text": ANSWER})
    run_journal.append_run_event(sid, stream_id, "done", {})


def _make_session(sid, stream_id, *, pending_started_at, previous_messages=None):
    messages = (
        [dict(m) for m in previous_messages]
        if previous_messages
        else [
            {"role": "user", "content": "earlier turn", "timestamp": 100.0},
            {"role": "assistant", "content": "earlier reply", "timestamp": 100.5},
        ]
    )
    s = Session(session_id=sid, title="nonfinite pending ts", messages=messages)
    s.pending_user_message = PROMPT
    s.active_stream_id = stream_id
    s.pending_attachments = []
    s.pending_started_at = pending_started_at
    s.pending_user_source = None
    return s


def _sidecar_path(hermes_home, sid) -> str:
    # Session.path is SESSION_DIR / f"{session_id}.json" (no "session_" prefix).
    return str(hermes_home / "sessions" / f"{sid}.json")


def _repair_once(hermes_home, sid, stream_id, *, pending_started_at, previous_messages=None):
    session = _make_session(
        sid, stream_id, pending_started_at=pending_started_at,
        previous_messages=previous_messages,
    )
    result = _apply_core_sync_or_error_marker(
        session,
        _sidecar_path(hermes_home, sid),
        stream_id_for_recheck=stream_id,
    )
    return session, result


def _recovered_user_rows(session):
    return [
        m
        for m in session.messages
        if isinstance(m, dict)
        and m.get("role") == "user"
        and str(m.get("content") or "") == PROMPT
    ]


@pytest.mark.parametrize("bad_ts", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_pending_started_at_completes_repair(hermes_home, bad_ts):
    """Repair must succeed (not raise) for every non-finite pending stamp.

    ``+inf`` is the reproduced crash: the inline ``int()`` conversion raised
    OverflowError before anything was persisted. ``-inf``/``NaN`` never
    satisfied the ``> 0`` gate, so they took the fallback path already; they
    are pinned here so a future edit cannot re-break the fail-closed branch.
    """
    sid = f"nonfinite_repair_{bad_ts}"
    stream_id = f"dead-stream-nonfinite-{bad_ts}"
    _completed_journal(sid, stream_id)

    session, result = _repair_once(hermes_home, sid, stream_id, pending_started_at=bad_ts)

    assert result is True, "repair must report success, not abort with an exception"
    assert session.pending_user_message is None
    assert session.pending_started_at is None
    rows = _recovered_user_rows(session)
    assert len(rows) == 1, f"expected exactly one recovered prompt row: {rows}"
    recovered_ts = rows[0].get("timestamp")
    assert isinstance(recovered_ts, (int, float)) and not isinstance(recovered_ts, bool)
    assert math.isfinite(recovered_ts), (
        f"recovered row must carry a finite timestamp, got {recovered_ts!r}"
    )


def test_positive_infinity_repair_survives_repeated_cycles(hermes_home):
    """The crash must stay fixed across repeated cache-miss repair cycles."""
    sid = "nonfinite_repeat_inf"
    stream_id = "dead-stream-nonfinite-repeat"
    _completed_journal(sid, stream_id)

    session, result = _repair_once(hermes_home, sid, stream_id, pending_started_at=float("inf"))
    assert result is True
    first_rows = _recovered_user_rows(session)
    assert len(first_rows) == 1

    # Read the saved sidecar back instead of inventing another pending turn.
    # The settled pending state must stay cleared on a real cold reload.
    reloaded = Session.load(sid)
    assert reloaded is not None
    assert reloaded.pending_user_message is None
    assert reloaded.pending_started_at is None
    assert _apply_core_sync_or_error_marker(reloaded, session.path) is False
    rows = _recovered_user_rows(reloaded)
    assert len(rows) == 1, rows
    assert isinstance(rows[0]["timestamp"], (int, float))
    assert math.isfinite(rows[0]["timestamp"])
    assert sum(m.get("content") == ANSWER for m in reloaded.messages) == 1


def test_nonfinite_pending_started_at_persists_valid_json(hermes_home):
    """The saved sidecar must be standard JSON: no Infinity/NaN literals."""
    sid = "nonfinite_save_inf"
    stream_id = "dead-stream-nonfinite-save"
    _completed_journal(sid, stream_id)

    session, result = _repair_once(hermes_home, sid, stream_id, pending_started_at=float("inf"))
    assert result is True

    sidecar = hermes_home / "sessions" / f"{sid}.json"
    assert sidecar.exists(), "repair must persist the session sidecar"

    def _reject_constant(name):
        raise AssertionError(f"sidecar contains non-finite JSON literal {name!r}")

    persisted = json.loads(sidecar.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    saved_ts = [
        m.get("timestamp")
        for m in persisted["messages"]
        if isinstance(m, dict) and str(m.get("content") or "") == PROMPT
    ]
    assert len(saved_ts) == 1, saved_ts
    assert math.isfinite(saved_ts[0]), saved_ts


def test_finite_fractional_pending_started_at_keeps_identity_precision(hermes_home):
    """A finite fractional stamp keeps full precision; same-text turns stay apart.

    An older turn used the identical prompt text at ts=500.0; the current
    pending turn is stamped 500.9. The recovered row must persist 500.9
    exactly (its timestamp IS the turn identity once pending state clears),
    and the historical 500.0 row must not be mistaken for the current turn
    (no merge, no suppression of the recovered prompt).
    """
    sid = "finite_fractional_identity"
    stream_id = "dead-stream-finite-fractional"
    _completed_journal(sid, stream_id)

    previous = [
        {"role": "user", "content": PROMPT, "timestamp": 500.0},
        {"role": "assistant", "content": "old answer", "timestamp": 500.5},
    ]
    session, result = _repair_once(
        hermes_home, sid, stream_id,
        pending_started_at=500.9,
        previous_messages=previous,
    )
    assert result is True

    rows = _recovered_user_rows(session)
    assert len(rows) == 2, (
        "the historical 500.0 row and the recovered 500.9 row must both survive: "
        f"{[(r.get('timestamp'), r.get('_recovered')) for r in rows]}"
    )
    recovered = [r for r in rows if r.get("_recovered")]
    assert len(recovered) == 1, rows
    assert recovered[0]["timestamp"] == 500.9, (
        "full-precision identity must survive: int() truncation let the 500.0 "
        f"row win ownership; got {recovered[0]['timestamp']!r}"
    )
    historical = [r for r in rows if not r.get("_recovered")]
    assert historical and historical[0]["timestamp"] == 500.0
