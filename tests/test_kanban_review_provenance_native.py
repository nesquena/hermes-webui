"""#8650 gate, REVIEW-PROVENANCE-F1, against the installed Agent's real kanban verbs.

A Running card claimed FROM Review (a reviewer run) can't be sent for review again:
the move is refused and the original implementer stays the routing authority, so the
next ``request_changes`` goes to the implementer, not the reviewer. Skipped where hermes-agent is absent;
``tests/test_kanban_bridge.py`` covers the same routing with the fake board.
"""
import functools

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


def test_implementer_handoff_with_run_proof_releases_the_run(board):
    """Ownership proof (expected_run_id) does what force=True did for the run it names:
    claim and worker cleared, the run closed as review_requested, Alice recorded."""
    bridge, conn = board
    tid = kb.create_task(conn, title="proof", assignee="alice", created_by="test")
    assert kb.claim_task(conn, tid, claimer="worker-alice") is not None
    run_id = kb.get_task(conn, tid).current_run_id

    bridge._patch_task(conn, tid, {"status": "review", "assignee": "bob", "summary": "please"})
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee, task.claim_lock, task.worker_pid, task.current_run_id) == \
        ("review", "bob", None, None, None)
    outcome = conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0]
    assert outcome == "review_requested"
    assert _review_events(conn, tid) == [("review_requested", "alice", "bob")]


@pytest.mark.parametrize("via", ["patch", "bulk"])
def test_run_swapped_before_the_handoff_is_refused(board, monkeypatch, via):
    """Read as Running under Alice's run; before the handoff Alice finishes and Bob
    claims the review. The handoff must be refused, not close Bob's run as implementer."""
    bridge, conn = board
    tid = kb.create_task(conn, title="race", assignee="alice", created_by="test")
    assert kb.claim_task(conn, tid, claimer="worker-alice") is not None
    alice_run = kb.get_task(conn, tid).current_run_id
    real = kb.request_review
    fired = []

    @functools.wraps(real)
    def swap_then_request(c, task_id, **kwargs):
        if not fired:
            fired.append(True)
            assert real(c, task_id, reviewer="bob", summary="done", expected_run_id=alice_run)
            assert kb.claim_review_task(c, task_id, claimer="worker-bob") is not None
        return real(c, task_id, **kwargs)

    monkeypatch.setattr(kb, "request_review", swap_then_request)
    if via == "patch":
        with pytest.raises(ValueError):
            bridge._patch_task(conn, tid, {"status": "review", "assignee": "carol"})
    else:
        entry, = bridge._bulk_tasks_payload({"ids": [tid], "status": "review"})["results"]
        assert entry["ok"] is False, entry
    monkeypatch.setattr(kb, "request_review", real)
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee) == ("running", "bob")
    assert _review_events(conn, tid) == [("review_requested", "alice", "bob")]
    assert kb.request_changes(conn, tid, reason="needs work") == (True, "alice")


@pytest.mark.parametrize("via", ["patch", "bulk"])
def test_card_read_as_review_is_refused(board, monkeypatch, via):
    """Read as Review; Bob claims the review before the handoff would run. The move is
    refused up front, so Bob's reviewer run is never re-handed."""
    bridge, conn = board
    tid = kb.create_task(conn, title="in review", assignee="alice", created_by="test")
    assert kb.claim_task(conn, tid, claimer="worker-alice") is not None
    assert kb.request_review(conn, tid, reviewer="bob", summary="done",
                             expected_run_id=kb.get_task(conn, tid).current_run_id)
    real = kb.request_review

    @functools.wraps(real)
    def claim_then_request(c, task_id, **kwargs):
        assert kb.claim_review_task(c, task_id, claimer="worker-bob") is not None
        return real(c, task_id, **kwargs)

    monkeypatch.setattr(kb, "request_review", claim_then_request)
    if via == "patch":
        with pytest.raises(ValueError, match="already being reviewed"):
            bridge._patch_task(conn, tid, {"status": "review", "assignee": "carol"})
    else:
        entry, = bridge._bulk_tasks_payload({"ids": [tid], "status": "review"})["results"]
        assert entry["ok"] is False and "already being reviewed" in entry["error"]
    monkeypatch.setattr(kb, "request_review", real)
    if kb.get_task(conn, tid).status == "review":
        assert kb.claim_review_task(conn, tid, claimer="worker-bob") is not None
    assert _review_events(conn, tid) == [("review_requested", "alice", "bob")]
    assert kb.request_changes(conn, tid, reason="needs work") == (True, "alice")


def test_ready_card_with_a_stale_claim_still_hands_off(board):
    """No force: a Ready card whose old claim no longer protects a live worker hands off."""
    bridge, conn = board
    tid = kb.create_task(conn, title="stale", assignee="alice", created_by="test")
    conn.execute("UPDATE tasks SET claim_lock = ?, claim_expires = 1 WHERE id = ?", ("old-host:1:dead", tid))
    conn.commit()
    bridge._patch_task(conn, tid, {"status": "review", "assignee": "bob"})
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee, task.claim_lock) == ("review", "bob", None)
    assert _review_events(conn, tid) == [("review_requested", "alice", "bob")]



def _alice_task_in_review_for_bob(conn):
    tid = kb.create_task(conn, title="leave review", body="details", assignee="alice", created_by="test")
    assert kb.claim_task(conn, tid, claimer="worker-alice") is not None
    assert kb.request_review(conn, tid, reviewer="bob", summary="done",
                             expected_run_id=kb.get_task(conn, tid).current_run_id)
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee) == ("review", "bob")
    return tid


def _snapshot(conn, tid):
    row = tuple(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    events = [(e.kind, e.payload) for e in kb.list_events(conn, tid)]
    return row, events


@pytest.mark.parametrize("target", ["ready", "todo", "triage"])
@pytest.mark.parametrize("shape", ["patch", "bulk", "dialog"])
def test_review_card_raw_exit_is_refused_before_any_write(board, target, shape):
    """A card read as Review moved to Ready/Todo/Triage would be a raw status write that
    keeps the reviewer as assignee. It is refused with nothing written: no row change,
    no event, no assignee change. The dialog shape is the edit dialog's save: every
    field plus the unchanged assignee, and the status because the user changed it."""
    bridge, conn = board
    tid = _alice_task_in_review_for_bob(conn)
    before = _snapshot(conn, tid)
    if shape == "bulk":
        entry, = bridge._bulk_tasks_payload({"ids": [tid], "status": target})["results"]
        assert entry["ok"] is False and "in review" in entry["error"], entry
    else:
        body = {"status": target}
        if shape == "dialog":
            body = {"title": "edited title", "body": "edited", "assignee": "bob", "tenant": None,
                    "status": target, "priority": 3}
        with pytest.raises(ValueError, match="in review"):
            bridge._patch_task(conn, tid, body)
    assert _snapshot(conn, tid) == before


def test_review_card_bulk_with_an_assignee_is_refused_without_the_assignee(board):
    bridge, conn = board
    tid = _alice_task_in_review_for_bob(conn)
    before = _snapshot(conn, tid)
    entry, = bridge._bulk_tasks_payload({"ids": [tid], "status": "ready", "assignee": "carol"})["results"]
    assert entry["ok"] is False and "in review" in entry["error"], entry
    assert _snapshot(conn, tid) == before


def test_review_to_done_still_completes(board):
    bridge, conn = board
    tid = _alice_task_in_review_for_bob(conn)
    bridge._patch_task(conn, tid, {"status": "done", "result": "approved"})
    assert kb.get_task(conn, tid).status == "done"


def test_review_card_edit_without_a_status_change_still_saves(board):
    """The refusal is only for a status move: editing a Review card's text still works."""
    bridge, conn = board
    tid = _alice_task_in_review_for_bob(conn)
    bridge._patch_task(conn, tid, {"title": "renamed", "body": "new body", "assignee": "bob"})
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee, task.title) == ("review", "bob", "renamed")
