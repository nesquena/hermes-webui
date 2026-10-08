"""Backups of journal-recovered text must survive guarded restoration."""
import copy
import json

import pytest

from api import models, session_recovery as recovery
from api.helpers import public_session_projection
from api.run_journal import RunJournalWriter
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 -- disposable production recovery state
    _persist_recovery_boundary_turn,
    _simulate_restart,
)


@pytest.mark.parametrize("char", ["\ud83d", "\udc00", "普通🙂"])
@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("entry", ["manual", "startup", "orphan-startup"])
def test_journal_answer_restores_losslessly_from_actual_shrink_backup(char, duplicate, entry):
    sid = f"restore-{ord(char[0])}-{duplicate}-{entry}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, "crash")
    answer = "PREFIX" + char + "AFTER"
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": answer})
    writer.append_sse_event("done", {"session": public_session_projection(session.__dict__)})
    _simulate_restart()
    session = models.get_session(sid)
    persisted = json.loads(session.path.read_text(encoding="utf-8"))
    ordinary = {"role": "assistant", "id": "ordinary-event", "timestamp": 30.5,
                "content": "ordinary row"}
    persisted["messages"].append(ordinary)
    if duplicate:
        persisted["messages"].append(copy.deepcopy(ordinary))
    persisted["message_count"] = len(persisted["messages"])
    persisted["unknown_metadata"] = {"nested": [answer, {"ordinary": "普通🙂"}]}
    session.path.write_text(json.dumps(persisted, ensure_ascii=True), encoding="utf-8")
    session.messages = [copy.deepcopy(persisted["messages"][0])]
    session.save()
    backup_path = session.path.with_suffix(".json.bak")
    backup_bytes = backup_path.read_bytes()
    expected = json.loads(backup_bytes)
    expected["messages"], _ = models._deduplicate_exact_stable_messages(expected["messages"])
    expected["message_count"] = len(expected["messages"])
    assert any(row.get("content") == answer for row in expected["messages"])
    if entry == "orphan-startup":
        session.path.unlink()
    assert recovery.inspect_session_recovery_status(session.path)["recommend"] == "restore"
    if entry == "manual":
        result = recovery.recover_session(session.path)
        assert result["restored"] is True, result
    else:
        result = recovery.recover_all_sessions_on_startup(session.path.parent, rebuild_index=True)
        assert result["restored"] == 1, result
        index = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        assert next(row for row in index if row["session_id"] == sid)["message_count"] == len(expected["messages"])
    assert json.loads(session.path.read_text(encoding="utf-8")) == expected
    assert backup_path.read_bytes() == backup_bytes
    assert models.Session.load(sid).messages == expected["messages"]
    assert recovery.recover_session(session.path)["restored"] is False
    assert not list(session.path.parent.glob("*.recover.tmp.*"))
    if char == "普通🙂":
        assert char in session.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("char", ["\ud83d", "\udc00"])
@pytest.mark.parametrize("marker", ["clear_generation", "intentional_shrink_generation"])
def test_surrogate_backup_does_not_override_intentional_shrink(tmp_path, char, marker):
    live_path = tmp_path / "intentional.json"
    live = {"session_id": "intentional", "messages": [], marker: "a" * 12 + "4" + "b" * 3 + "8" + "c" * 15}
    if marker == "clear_generation":
        live.update(truncation_watermark=0.0, truncation_boundary=0.0, context_messages=[],
                    active_stream_id=None, pending_user_message=None, pending_attachments=[],
                    pending_started_at=None, pending_user_source=None)
    backup = {"session_id": "intentional", "messages": [{"role": "assistant", "content": char}]}
    live_bytes = json.dumps(live).encode()
    backup_bytes = json.dumps(backup).encode()
    live_path.write_bytes(live_bytes)
    live_path.with_suffix(".json.bak").write_bytes(backup_bytes)
    assert recovery.recover_session(live_path)["restored"] is False
    assert live_path.read_bytes() == live_bytes
    assert live_path.with_suffix(".json.bak").read_bytes() == backup_bytes
    assert not list(tmp_path.glob("*.recover.tmp.*"))


@pytest.mark.parametrize("char", ["\ud83d", "\udc00"])
@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_surrogate_restore_filesystem_failure_preserves_both_snapshots(tmp_path, monkeypatch, char, operation):
    live_path = tmp_path / "failed.json"
    live_bytes = json.dumps({"session_id": "failed", "messages": []}).encode()
    backup_bytes = json.dumps({"session_id": "failed", "messages": [{"role": "assistant", "content": char}]}).encode()
    live_path.write_bytes(live_bytes)
    live_path.with_suffix(".json.bak").write_bytes(backup_bytes)

    def fail(*args):
        raise OSError("injected restore " + operation)

    monkeypatch.setattr(recovery.os, operation, fail)
    result = recovery.recover_session(live_path)
    assert result["restored"] is False
    assert result["error"] == "injected restore " + operation
    assert live_path.read_bytes() == live_bytes
    assert live_path.with_suffix(".json.bak").read_bytes() == backup_bytes
    assert not list(tmp_path.glob("*.recover.tmp.*"))
