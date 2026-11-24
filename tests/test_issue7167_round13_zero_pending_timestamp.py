"""Round-13 review finding on #7167 (nesquena-hermes 2026-11-24).

A persisted ``pending_started_at = 0`` must not be used as conflicting time
evidence.

``build_active_turn_token()`` rejects non-positive timestamps (``started <= 0
-> None``) and the legacy recovery path treats zero as a missing time. The
round-12 conflict predicate only checked ``math.isfinite()``, so a zero
pending timestamp compared as a real turn identity: a core row holding the
same prompt was declared a different (older) turn, reuse was refused, and the
prompt was appended a second time.

The reviewer reproduced it through the real journal append, the recovery entry
point and ``Session.load()``:

- exact source: two saved user rows instead of one; the unstamped positive
  control passes;
- current-master integration: the same failure and passing control;
- previous reviewed production ``92983454cdbf`` with identical test/helper
  bytes: both tests pass.

Fix shape: require a valid POSITIVE pending timestamp before using it as
conflicting time evidence, consistent with the token builder. This is a change
to the new conflict predicate, not a rollback of the repaired ownership guard.
"""

from __future__ import annotations

import json

import pytest

import api.models as models
import api.profiles as profiles
from api.models import Session, _apply_core_sync_or_error_marker
from api.run_journal import append_run_event

PROMPT = "zero-time prompt"
OLD_ANSWER = "zero-time answer"
OLD_USER_TS = 100.0
OLD_ASSISTANT_TS = 101.0
STREAM_ID = "stream-r13-zero"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME + the WebUI state dir so journal writes sandbox."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_WEBUI_STATE_DIR", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    # Session.load() resolves SESSION_DIR at import time; redirect the module
    # global so the fresh-file reload reads OUR sandbox (round-8 pattern).
    monkeypatch.setattr(models, "SESSION_DIR", home / "sessions")
    monkeypatch.setattr(
        models, "SESSION_INDEX_FILE", home / "sessions" / "_index.json"
    )
    return home


def _write_core_transcript(hermes_home, sid):
    core = {
        "messages": [
            {"role": "user", "content": PROMPT, "timestamp": OLD_USER_TS},
            {
                "role": "assistant",
                "content": OLD_ANSWER,
                "timestamp": OLD_ASSISTANT_TS,
            },
        ],
    }
    path = hermes_home / "sessions" / f"session_{sid}.json"
    path.write_text(json.dumps(core), encoding="utf-8")
    return path


def _make_pending_session(sid, started_at=0.0):
    """Empty-messages session whose persisted pending turn carries zero time."""
    session = Session(
        session_id=sid,
        title="r13 zero pending timestamp",
        messages=[],
        pending_user_message=PROMPT,
        pending_started_at=started_at,
        active_stream_id=STREAM_ID,
    )
    session.save()
    return session


def _write_new_stream_journal(sid):
    """The real run-journal writer records this stream's answer + completion."""
    append_run_event(sid, STREAM_ID, "token", {"text": OLD_ANSWER})
    append_run_event(sid, STREAM_ID, "done", {})


def test_zero_pending_timestamp_does_not_conflict_with_legacy_core_row():
    """Exact-source level: a zero pending timestamp proves nothing.

    ``build_active_turn_token`` rejects ``started <= 0``, so the predicate must
    not treat the row's earlier timestamp as a conflicting turn identity.
    """
    synced = Session(
        session_id="r13_predicate",
        title="r13 predicate shape",
        messages=[
            {"role": "user", "content": PROMPT, "timestamp": OLD_USER_TS},
            {
                "role": "assistant",
                "content": OLD_ANSWER,
                "timestamp": OLD_ASSISTANT_TS,
            },
        ],
        pending_user_message=PROMPT,
        pending_started_at=0.0,
        active_stream_id=STREAM_ID,
    )
    for row in synced.messages:
        row["_from_core_transcript"] = True

    assert models._core_row_conflicts_with_pending_turn(
        synced.messages[0], synced
    ) is False, (
        "a zero pending timestamp must not count as conflicting time evidence; "
        "the token builder rejects non-positive timestamps"
    )


def test_zero_pending_timestamp_repairs_to_a_single_user_row(hermes_home):
    """Repair + save + reload must keep exactly one user row, not a duplicate."""
    sid = "r13_zero_repair"
    _write_new_stream_journal(sid)

    session = _make_pending_session(sid, started_at=0.0)
    core_path = _write_core_transcript(hermes_home, sid)
    assert _apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=STREAM_ID
    ) is True

    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 1, (
        "the existing core prompt must be reused for a zero pending time, got "
        f"{len(user_rows)} user rows; transcript="
        f"{[(m.get('timestamp'), str(m.get('content'))[:20]) for m in user_rows]!r}"
    )
    session.save()

    loaded = Session.load(sid)
    assert loaded is not None
    saved_users = [m for m in loaded.messages if m.get("role") == "user"]
    assert len(saved_users) == 1, (
        "the saved transcript must hold one prompt row, got "
        f"{len(saved_users)}; rows="
        f"{[(m.get('timestamp'), str(m.get('content'))[:20]) for m in saved_users]!r}"
    )
    assert saved_users[0].get("content") == PROMPT


def test_zero_pending_timestamp_reload_is_idempotent(hermes_home):
    """A second repair pass on the reloaded sidecar must not append anything."""
    sid = "r13_zero_idempotent"
    _write_new_stream_journal(sid)

    session = _make_pending_session(sid, started_at=0.0)
    core_path = _write_core_transcript(hermes_home, sid)
    assert _apply_core_sync_or_error_marker(
        session, core_path, stream_id_for_recheck=STREAM_ID
    ) is True
    session.save()

    loaded = Session.load(sid)
    assert loaded is not None
    before = [dict(m) for m in loaded.messages]
    assert _apply_core_sync_or_error_marker(
        loaded, core_path, stream_id_for_recheck=STREAM_ID
    ) is False
    assert [dict(m) for m in loaded.messages] == before, (
        "a second repair pass must be a no-op once pending state is cleared"
    )
