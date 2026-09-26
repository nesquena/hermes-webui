"""#6350 review follow-up — server-materialized recovery turns carry metadata.

The wakeup summary card prefers server-stamped ``_wakeup_meta`` (authoritative)
over the client parser. The maintainer review flagged that two older recovery
paths stamped ``_source`` but skipped the metadata helper, so a future
prompt-format change would silently drop back to the raw notice only on those
paths:

  * ``api.models._append_recovered_pending_turn`` (restart / core-resync recovery)
  * the ``cancel_stream`` outer-finally pending-turn recovery

Both now route through ``stamp_message_source``. These tests assert each path
emits ``_source == "process_wakeup"`` AND a populated ``_wakeup_meta`` for a
real (parseable) wakeup body, and leaves ordinary ``webui`` turns unmarked.
"""

import queue
import threading
from unittest.mock import Mock

import pytest

import api.config as config
import api.models as models
from api.background_process import format_wakeup_prompt
from api.models import Session, _append_recovered_pending_turn
from api.streaming import cancel_stream


def _wakeup_body(exit_code=0):
    body = format_wakeup_prompt(
        {
            "type": "completion",
            "session_id": "proc_42",
            "command": "npm run build",
            "exit_code": exit_code,
            "output": "all green",
        }
    )
    assert body is not None
    return body


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    for name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "SESSION_AGENT_LOCKS",
    ):
        if hasattr(config, name):
            getattr(config, name).clear()
    yield
    for name in (
        "STREAMS",
        "CANCEL_FLAGS",
        "AGENT_INSTANCES",
        "STREAM_PARTIAL_TEXT",
        "SESSION_AGENT_LOCKS",
    ):
        if hasattr(config, name):
            getattr(config, name).clear()


# ── _append_recovered_pending_turn (restart / core-resync recovery) ──────────

def test_append_recovered_pending_turn_stamps_wakeup_meta():
    s = Session(
        session_id="recover-meta-1",
        pending_user_message=_wakeup_body(),
        pending_user_source="process_wakeup",
    )
    recovered = _append_recovered_pending_turn(s)
    assert recovered is not None
    assert recovered["_source"] == "process_wakeup"
    assert recovered["_wakeup_meta"] == {
        "type": "completion",
        "task_id": "proc_42",
        "command": "npm run build",
        "exit_code": 0,
    }


def test_append_recovered_pending_turn_webui_gets_no_meta():
    s = Session(
        session_id="recover-meta-2",
        pending_user_message="ordinary message",
        pending_user_source="webui",
    )
    recovered = _append_recovered_pending_turn(s)
    assert recovered is not None
    assert "_source" not in recovered
    assert "_wakeup_meta" not in recovered


# ── cancel_stream outer-finally pending-turn recovery ────────────────────────

def _wire_cancel_state(session_id, stream_id="stream-meta"):
    models.SESSIONS[session_id].active_stream_id = stream_id
    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    agent = Mock()
    agent.session_id = session_id
    agent.interrupt = Mock()
    config.AGENT_INSTANCES[stream_id] = agent
    return stream_id


def test_cancel_stream_recovery_stamps_wakeup_meta():
    stream_id = "stream-meta"
    s = Session(
        session_id="cancel-meta-1",
        title="t",
        messages=[{"role": "assistant", "content": "prior report"}],
    )
    s.pending_user_message = _wakeup_body(exit_code=1)
    s.pending_user_source = "process_wakeup"
    s.pending_attachments = []
    s.pending_started_at = None
    s.active_stream_id = stream_id
    s.save()
    models.SESSIONS[s.session_id] = s
    _wire_cancel_state(s.session_id, stream_id)

    assert cancel_stream(stream_id) is True

    recovered = next(
        m for m in reversed(models.SESSIONS[s.session_id].messages)
        if isinstance(m, dict) and m.get("role") == "user"
    )
    assert recovered["_source"] == "process_wakeup"
    assert recovered["_wakeup_meta"]["task_id"] == "proc_42"
    assert recovered["_wakeup_meta"]["exit_code"] == 1


def test_cancel_stream_recovery_webui_turn_unmarked():
    stream_id = "stream-meta"
    s = Session(
        session_id="cancel-meta-2",
        title="t",
        messages=[{"role": "assistant", "content": "prior report"}],
    )
    s.pending_user_message = "ordinary message"
    s.pending_user_source = "webui"
    s.pending_attachments = []
    s.pending_started_at = None
    s.active_stream_id = stream_id
    s.save()
    models.SESSIONS[s.session_id] = s
    _wire_cancel_state(s.session_id, stream_id)

    assert cancel_stream(stream_id) is True

    recovered = next(
        m for m in reversed(models.SESSIONS[s.session_id].messages)
        if isinstance(m, dict) and m.get("role") == "user"
    )
    assert "_source" not in recovered
    assert "_wakeup_meta" not in recovered
