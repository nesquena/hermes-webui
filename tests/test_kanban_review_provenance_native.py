"""#8650 gate, REVIEW-PROVENANCE-F1, against the installed Agent's real kanban verbs.

A Running card claimed FROM Review (a reviewer run) can't be sent for review again:
the move is refused and the original implementer stays the routing authority, so the
next ``request_changes`` goes to the implementer, not the reviewer. Skipped where hermes-agent is absent;
``tests/test_kanban_bridge.py`` covers the same routing with the fake board.
"""
import pytest

kb = pytest.importorskip("hermes_cli.kanban_db", reason="hermes-agent not installed")
if not all(hasattr(kb, name) for name in ("claim_review_task", "request_changes", "_retry_status_for_run")):
    pytest.skip("installed hermes-agent has no native review lifecycle", allow_module_level=True)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("HERMES_KANBAN_ATTACHMENTS_ROOT", str(tmp_path / "attachments"))
    (tmp_path / "home").mkdir()
    from api import kanban_bridge as bridge
    monkeypatch.setattr(bridge, "_kb", lambda: kb)
    with bridge._conn() as conn:
        yield bridge, conn


def _alice_task_under_bobs_review(conn):
    tid = kb.create_task(conn, title="provenance", assignee="alice", created_by="test")
    assert kb.claim_task(conn, tid, claimer="worker-alice") is not None
    assert kb.request_review(conn, tid, reviewer="bob", summary="done",
                             expected_run_id=kb.get_task(conn, tid).current_run_id)
    assert kb.claim_review_task(conn, tid, claimer="worker-bob") is not None
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee) == ("running", "bob")
    return tid


def _review_events(conn, tid):
    return [(e.kind, e.payload.get("implementer"), e.payload.get("reviewer"))
            for e in kb.list_events(conn, tid) if e.kind in ("review_requested", "changes_requested")]


@pytest.mark.parametrize("body", [{"status": "review"},
                                  {"status": "review", "assignee": "carol", "summary": "again"}])
def test_reviewer_run_sent_to_review_is_refused_and_keeps_the_implementer(board, body):
    bridge, conn = board
    tid = _alice_task_under_bobs_review(conn)
    before = kb.get_task(conn, tid)

    with pytest.raises(ValueError, match="already being reviewed"):
        bridge._patch_task(conn, tid, dict(body))
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee, task.claim_lock, task.current_run_id) == \
        ("running", "bob", before.claim_lock, before.current_run_id)
    assert _review_events(conn, tid) == [("review_requested", "alice", "bob")]

    # The reviewer's own verdict still routes to the implementer.
    assert kb.request_changes(conn, tid, reason="needs work") == (True, "alice")
    assert kb.get_task(conn, tid).assignee == "alice"


def test_implementer_run_to_review_is_still_a_handoff(board):
    bridge, conn = board
    tid = kb.create_task(conn, title="ordinary", assignee="alice", created_by="test")
    assert kb.claim_task(conn, tid, claimer="worker-alice") is not None

    bridge._patch_task(conn, tid, {"status": "review", "assignee": "bob"})
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee) == ("review", "bob")
    assert _review_events(conn, tid) == [("review_requested", "alice", "bob")]

    assert kb.claim_review_task(conn, tid, claimer="worker-bob") is not None
    assert kb.request_changes(conn, tid, reason="needs work") == (True, "alice")
