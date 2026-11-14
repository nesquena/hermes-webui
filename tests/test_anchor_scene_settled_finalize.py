"""Server-owned settled anchor scene finalization (#8105).

The worklog fold for a finished turn used to exist only if the browser that
streamed it POSTed /api/session/anchor-scene while still attached. A turn that
finished while the user was looking at another session was never persisted, so
a hard reload fell back to plain prose. These tests cover the server-side
finalization: rebuild from the durable run journal, settle terminal truth
(truthful terminal statuses + final-answer separation), and persist the record
conservatively (never for active runs, never over an existing record).
"""
from urllib.parse import urlparse


def _write_run_journal(session_dir, session_id, stream_id, *, terminal_event="done", terminal_payload=None):
    from api.run_journal import append_run_event

    append_run_event(
        session_id, stream_id, "token", {"text": "first progress"}, session_dir=session_dir
    )
    append_run_event(
        session_id,
        stream_id,
        "reasoning",
        {"text": "thinking through plan"},
        session_dir=session_dir,
    )
    append_run_event(
        session_id,
        stream_id,
        "tool",
        {"name": "terminal", "tid": "call-1", "args": {"command": "pytest"}},
        session_dir=session_dir,
    )
    append_run_event(
        session_id,
        stream_id,
        "tool_complete",
        {"name": "terminal", "tid": "call-1", "preview": "ok"},
        session_dir=session_dir,
    )
    append_run_event(
        session_id, stream_id, "token", {"text": " checkpoint tail"}, session_dir=session_dir
    )
    if terminal_event:
        append_run_event(
            session_id,
            stream_id,
            terminal_event,
            terminal_payload or {},
            session_dir=session_dir,
        )


def _write_turn_journal(session_dir, session_id, stream_id, assistant_index, *, event="completed"):
    from api.turn_journal import append_turn_journal_event

    append_turn_journal_event(
        session_id,
        {
            "event": "assistant_started",
            "stream_id": stream_id,
            "assistant_message_index": assistant_index,
            "turn_id": "turn-1",
        },
        session_dir=session_dir,
    )
    append_turn_journal_event(
        session_id,
        {
            "event": event,
            "stream_id": stream_id,
            "assistant_message_index": assistant_index,
            "turn_id": "turn-1",
        },
        session_dir=session_dir,
    )


def _settled_session(session_dir, monkeypatch):
    from api import models, routes
    from api.models import Session

    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    session = Session(
        session_id="finalize1",
        title="Detached settle",
        messages=[
            {"role": "user", "content": "question", "timestamp": 1000},
            {
                "role": "assistant",
                "content": "first progress",
                "reasoning": "thinking through plan",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "terminal",
                        "args": {"command": "pytest"},
                        "preview": "pytest",
                        "snippet": "ok",
                    }
                ],
                "timestamp": 1001,
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "ok", "timestamp": 1002},
            {
                "role": "assistant",
                "content": "Final answer: keep the activity above this answer.",
                "timestamp": 1003,
            },
        ],
    )
    session.save()
    return session


FINAL_ANSWER = "Final answer: keep the activity above this answer."


def test_finalize_persists_settled_scene_from_terminal_run_journal(tmp_path, monkeypatch):
    from api import routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = _settled_session(session_dir, monkeypatch)
    stream_id = "stream-finalize-1"
    _write_run_journal(session_dir, session.session_id, stream_id)

    record = routes._finalize_settled_anchor_scene_from_journal(session, stream_id)

    assert record is not None, "terminal run journal must produce a persisted record"
    assert record["message_index"] == 3
    assert record["stream_id"] == stream_id

    scene = record["scene"]
    assert scene["terminal_state"] == "completed"
    assert scene["lifecycle"]["status"] == "terminal"
    assert scene["lifecycle"]["terminal_state"] == "completed"
    assert scene.get("final_answer") == FINAL_ANSWER

    rows = scene["activity_rows"]
    prose_texts = [row.get("text") for row in rows if row.get("role") != "terminal"]
    assert FINAL_ANSWER not in prose_texts, "final answer must be separated from the fold"
    terminal_rows = [row for row in rows if row.get("role") == "terminal"]
    assert len(terminal_rows) == 1
    assert terminal_rows[0]["kind"] == "terminal_status"
    assert terminal_rows[0]["source_event_type"] == "done"
    assert terminal_rows[0]["status"] == "completed"
    assert not any(row.get("status") == "running" for row in rows), "no running rows may survive settling"

    stored = session.anchor_activity_scenes.get(record["message_ref"] or "index:3")
    assert stored is not None, "record must be persisted on the session for hydration"
    assert stored["scene"]["terminal_state"] == "completed"


def test_finalize_skips_active_run(tmp_path, monkeypatch):
    from api import routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = _settled_session(session_dir, monkeypatch)
    stream_id = "stream-active-1"
    _write_run_journal(session_dir, session.session_id, stream_id, terminal_event=None)

    record = routes._finalize_settled_anchor_scene_from_journal(session, stream_id)

    assert record is None, "runs without a terminal event must not be finalized"
    assert not getattr(session, "anchor_activity_scenes", None)


def test_finalize_does_not_clobber_existing_record(tmp_path, monkeypatch):
    from api import routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = _settled_session(session_dir, monkeypatch)
    stream_id = "stream-clobber-1"
    _write_run_journal(session_dir, session.session_id, stream_id)

    ref = routes._assistant_anchor_scene_message_ref(session.messages[3])
    session.anchor_activity_scenes = {
        ref: {
            "version": "anchor_activity_scene_record_v1",
            "message_index": 3,
            "message_ref": ref,
            "stream_id": "browser-stream",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [],
                "marker": "kept",
            },
            "updated_at": 123.0,
        }
    }

    record = routes._finalize_settled_anchor_scene_from_journal(session, stream_id)

    assert record is None, "an existing browser POSTed record must never be clobbered"
    assert session.anchor_activity_scenes[ref]["scene"].get("marker") == "kept"


def test_finalize_cancel_reports_interrupted_state(tmp_path, monkeypatch):
    from api import routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = _settled_session(session_dir, monkeypatch)
    stream_id = "stream-cancel-1"
    _write_run_journal(session_dir, session.session_id, stream_id, terminal_event="cancel")

    record = routes._finalize_settled_anchor_scene_from_journal(session, stream_id)

    assert record is not None
    assert record["scene"]["terminal_state"] == "interrupted"
    terminal_row = [r for r in record["scene"]["activity_rows"] if r.get("role") == "terminal"][0]
    assert terminal_row["source_event_type"] == "cancel"
    assert terminal_row["status"] == "interrupted"


def test_finalize_error_reports_error_state(tmp_path, monkeypatch):
    from api import routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = _settled_session(session_dir, monkeypatch)
    stream_id = "stream-error-1"
    _write_run_journal(
        session_dir,
        session.session_id,
        stream_id,
        terminal_event="apperror",
        terminal_payload={"type": "RuntimeError", "message": "boom"},
    )

    record = routes._finalize_settled_anchor_scene_from_journal(session, stream_id)

    assert record is not None
    assert record["scene"]["terminal_state"] == "error"
    terminal_row = [r for r in record["scene"]["activity_rows"] if r.get("role") == "terminal"][0]
    assert terminal_row["source_event_type"] == "apperror"
    assert terminal_row["status"] == "error"


def test_finalize_skips_read_only_session(tmp_path, monkeypatch):
    from api import routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = _settled_session(session_dir, monkeypatch)
    session.read_only = True
    stream_id = "stream-ro-1"
    _write_run_journal(session_dir, session.session_id, stream_id)

    record = routes._finalize_settled_anchor_scene_from_journal(session, stream_id)

    assert record is None
    assert not getattr(session, "anchor_activity_scenes", None)


def test_session_load_repairs_settled_scene_from_journal(tmp_path, monkeypatch):
    """Cold-load repair: a turn finished while another session was open, no
    browser record exists — GET /api/session must still hydrate the worklog."""
    import api.routes as routes

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = _settled_session(session_dir, monkeypatch)
    stream_id = "stream-loadrepair-1"
    _write_run_journal(session_dir, session.session_id, stream_id)
    _write_turn_journal(session_dir, session.session_id, stream_id, 3)

    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "_clear_stale_stream_state", lambda _s: None)
    monkeypatch.setattr(routes, "redact_session_data", lambda payload: payload)
    monkeypatch.setattr(
        routes, "j", lambda _handler, payload, status=200, extra_headers=None: payload
    )

    response = routes.handle_get(
        object(),
        urlparse("/api/session?session_id=finalize1&messages=1&resolve_model=0"),
    )

    hydrated = response["session"]["messages"][-1]
    assert hydrated["role"] == "assistant"
    scene = hydrated.get("_anchor_activity_scene")
    assert isinstance(scene, dict), "load must repair + attach the settled scene"
    assert scene.get("terminal_state") == "completed"
    assert any(row.get("role") == "terminal" for row in scene.get("activity_rows") or [])

    assert getattr(session, "anchor_activity_scenes", None), "repair must persist the record"
    record = next(iter(session.anchor_activity_scenes.values()))
    assert record["stream_id"] == stream_id
    assert record["scene"]["terminal_state"] == "completed"
