"""Recovery must not lose the pending turn's attachments/source when it reuses an
already-materialized user row (#7167 re-gate, 2026-10-13).

Both repair branches prove the pending prompt is already in the transcript
(matched on text + timestamp) and therefore REUSE that row instead of
materializing a new one. Such a row can have been written by a path that carried
no ``attachments`` / ``_source`` (state.db reconciliation, cancel-journal
recovery). The pending state is cleared right after, so the pending copy was the
only copy — without a backfill the turn's uploads and origin are lost for good
and the loss survives a cold reload.
"""
import pytest

from api.models import _apply_core_sync_or_error_marker
from api.run_journal import append_run_event

import api.profiles as profiles


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolate HERMES_HOME so Session.save() + run-journal writes are sandboxed."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    (home / "sessions").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    return home


def _make_session(sid, stream_id):
    from api.models import Session

    session = Session(
        session_id=sid,
        title="regate",
        messages=[
            {"role": "user", "content": "run the check", "timestamp": 900.5},
            {"role": "assistant", "content": "the answer", "timestamp": 900.6},
        ],
    )
    session.pending_user_message = "run the check"
    session.pending_started_at = 900.5
    session.pending_attachments = ["/tmp/upload.png"]
    session.pending_user_source = "cli"
    session.active_stream_id = stream_id
    return session


def test_core_sync_reuse_keeps_attachments_and_source(hermes_home):
    """The core-sync branch reuses the saved user row — metadata must survive."""
    sid = "regate_meta_coresync"
    stream_id = "regate-meta-coresync-stream"
    append_run_event(sid, stream_id, "token", {"text": "the answer"})

    session = _make_session(sid, stream_id)
    session.save()

    core = {
        "messages": [
            {"role": "user", "content": "run the check", "timestamp": 900.5},
            {"role": "assistant", "content": "the answer", "timestamp": 900.6},
        ],
        "stream_id": stream_id,
    }
    _apply_core_sync_or_error_marker(session, core, stream_id)

    user_rows = [m for m in session.messages if m.get("role") == "user"]
    assert len(user_rows) == 1, "the reused row must not be duplicated"
    row = user_rows[0]
    assert row.get("attachments") == ["/tmp/upload.png"], (
        "the pending turn's attachments must be backfilled onto the reused row"
    )
    assert row.get("_source") == "cli", (
        "the pending turn's source must be backfilled onto the reused row"
    )
    # the transcript still holds exactly one prompt and one answer
    assert sum(1 for m in session.messages if m.get("content") == "run the check") == 1
    assert sum(1 for m in session.messages if m.get("content") == "the answer") == 1


def test_backfill_does_not_overwrite_existing_metadata(hermes_home):
    """A row that already carries its own metadata keeps it (dedupe unchanged)."""
    from api.models import (
        Session as _Session,
        _backfill_pending_metadata_onto_reused_row,
    )

    session = _Session(session_id="regate_meta_keep", title="t", messages=[])
    session.pending_user_message = "run the check"
    session.pending_attachments = ["/tmp/pending.png"]
    session.pending_user_source = "cli"

    row = {
        "role": "user",
        "content": "run the check",
        "attachments": ["/tmp/already.png"],
        "_source": "webui",
    }
    _backfill_pending_metadata_onto_reused_row(session, row)
    assert row["attachments"] == ["/tmp/already.png"], "existing attachments win"
    assert row["_source"] == "webui", "an existing source is never overwritten"


def test_backfill_is_a_noop_without_a_pending_turn(hermes_home):
    from api.models import Session as _Session, _backfill_pending_metadata_onto_reused_row

    session = _Session(session_id="regate_meta_nopending", title="t", messages=[])
    row = {"role": "user", "content": "run the check"}
    _backfill_pending_metadata_onto_reused_row(session, row)
    assert "attachments" not in row and "_source" not in row, "nothing to copy"
