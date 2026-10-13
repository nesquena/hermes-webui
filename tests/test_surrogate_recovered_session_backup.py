"""A later replay-cleaning shrink must save a journal-recovered surrogate."""
import copy
import json

import pytest

from api import models
from api.helpers import public_session_projection
from api.run_journal import RunJournalWriter
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 -- production repair in disposable state
    _persist_recovery_boundary_turn,
    _simulate_restart,
    _stream_output,
)


@pytest.mark.parametrize("char", ["\ud83d", "\udc00", "普通🙂"])
@pytest.mark.parametrize("duplicate", ["ordinary", "surrogate", "none"])
@pytest.mark.parametrize("skip_index", [False, True])
def test_later_shrink_preserves_recovered_answer_in_live_and_backup(char, duplicate, skip_index):
    sid = f"surrogate-backup-{ord(char[0])}-{duplicate}-{skip_index}"
    stream = sid + "-run"
    session = _persist_recovery_boundary_turn(sid, stream, "crash")
    answer = "PREFIX" + char + "AFTER"
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": answer})
    writer.append_sse_event("done", {"session": public_session_projection(session.__dict__)})
    _simulate_restart()
    recovered = models.get_session(sid)
    assert [row["content"] for row in _stream_output(recovered, stream)] == [answer]
    assert any(row.get("content") == answer for row in models.Session.load(sid).messages)
    # A persisted legacy/external replay copy is the review trigger. Save()'s
    # normal incoming guard already prevents it, so seed the existing sidecar
    # explicitly with valid JSON, while retaining the real recovered answer.
    persisted = json.loads(recovered.path.read_text(encoding="utf-8"))
    normal = {"role": "assistant", "id": "ordinary-event", "timestamp": 30.5,
              "content": "ordinary duplicate", "reasoning": "must survive"}
    if duplicate == "surrogate":
        normal["content"] = char
    existing = persisted["messages"] + [normal]
    if duplicate != "none":
        existing.append(copy.deepcopy(normal))
    # A distinct row ensures every case actually shrinks and takes a backup.
    extra = {"role": "user", "id": "extra-event", "timestamp": 31.5,
             "content": "distinct backup-only row"}
    existing.append(extra)
    persisted["messages"] = existing
    persisted["message_count"] = len(existing)
    old_text = json.dumps(persisted, ensure_ascii=True, indent=2)
    recovered.path.write_text(old_text, encoding="utf-8")
    recovered.messages.append(copy.deepcopy(normal))
    recovered.title = "saved after replay cleanup"
    recovered.save(skip_index=skip_index)
    live = json.loads(recovered.path.read_text(encoding="utf-8"))
    backup_path = recovered.path.with_suffix(".json.bak")
    backup_text = backup_path.read_text(encoding="utf-8")
    backup = json.loads(backup_text)
    # Surrogate-bearing event fingerprints cannot prove exact deletion and
    # remain distinct. Do not change that fail-closed deduplication policy.
    removed = int(duplicate == "ordinary" or (duplicate == "surrogate" and not char.startswith(("\ud83d", "\udc00"))))
    expected_backup = persisted["messages"][:-1 - removed] + [extra]
    assert backup["messages"] == expected_backup
    assert backup["message_count"] == len(expected_backup)
    assert live["title"] == "saved after replay cleanup"
    assert live["messages"] == recovered.messages
    assert live["message_count"] == len(recovered.messages)
    assert any(row.get("content") == answer for row in backup["messages"])
    assert not list(recovered.path.parent.glob("*.tmp.*"))
    assert not list(recovered.path.parent.glob("*.bak.tmp.*"))
    if not removed:
        assert backup_text == old_text  # The raw-copy backup remains unchanged.
    if not skip_index:
        entries = json.loads(models.SESSION_INDEX_FILE.read_text(encoding="utf-8"))
        indexed = next(row for row in entries if row["session_id"] == sid)
        assert indexed["message_count"] == live["message_count"]
    loaded = models.Session.load(sid)
    assert loaded.title == live["title"]
    assert loaded.messages == live["messages"]


@pytest.mark.parametrize("char", ["\ud83d", "\udc00"])
@pytest.mark.parametrize("skip_index", [False, True])
def test_rewritten_backup_preserves_surrogate_removed_from_current_transcript(char, skip_index):
    # The old backup encoder also fails when the current payload has no
    # surrogate at all. This isolates the pre-existing dependency from the
    # new primary-payload fallback, without changing deletion authority.
    sid = f"backup-only-surrogate-{ord(char)}-{skip_index}"
    normal = {"role": "user", "id": "ordinary-event", "timestamp": 30.5,
              "content": "current ordinary prompt"}
    answer = {"role": "assistant", "content": "PREFIX" + char + "AFTER",
              "_recovered_from_run_journal": True, "timestamp": 20.5}
    session = models.Session(session_id=sid, title="updated current", messages=[normal])
    existing = {"session_id": sid, "message_count": 3,
                "messages": [answer, normal, copy.deepcopy(normal)]}
    session.path.write_text(json.dumps(existing, ensure_ascii=True), encoding="utf-8")
    session.save(skip_index=skip_index)
    live = json.loads(session.path.read_text(encoding="utf-8"))
    backup = json.loads(session.path.with_suffix(".json.bak").read_text(encoding="utf-8"))
    assert live["messages"] == [normal]
    assert live["title"] == "updated current"
    assert backup["messages"] == [answer, normal]
    assert backup["message_count"] == 2
    assert not list(session.path.parent.glob("*.bak.tmp.*"))
    assert models.Session.load(sid).messages == [normal]
