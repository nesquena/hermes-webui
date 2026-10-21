"""#6765: PATCH field edits commit all-or-nothing against the REAL kanban DB.

test_kanban_bridge.py drives the bridge through an in-memory fake with no
transactions, so it cannot see a write from another connection landing between
the bridge's checks and its writes. These tests use the installed
``hermes_cli.kanban_db`` on a throwaway SQLite file, with a second real
connection doing the intervening write, at two points:

- just before the PATCH's write transaction begins (the latest moment any
  unlocked snapshot could have been taken): the PATCH must see the intervening
  write and reject without mutating anything;
- after the PATCH has read the row it decides on: the intervening writer must
  be held off until the PATCH commits, so it lands after it instead of
  between the check and the writes.
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest

from tests.conftest import HERMES_AGENT

# hermes_cli imports top-level agent modules (e.g. hermes_state_identity), so
# the agent checkout itself must be importable, not just the hermes_cli package.
_AGENT_DIR = HERMES_AGENT or pathlib.Path("~/.hermes/hermes-agent").expanduser()
if _AGENT_DIR.is_dir() and str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

kb = pytest.importorskip("hermes_cli.kanban_db", reason="hermes-agent not installed")

import api.kanban_bridge as bridge  # noqa: E402

# Current hermes-agent defines connect_closing in kanban_db_connect without
# re-exporting it from kanban_db.
try:
    from hermes_cli.kanban_db_connect import connect_closing
except ImportError:
    connect_closing = getattr(kb, "connect_closing", None)
if connect_closing is None:
    pytest.skip("hermes-agent kanban_db has no connect_closing", allow_module_level=True)


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """Override session test_server autouse: native atomicity tests are pure SQLite."""
    return None


@pytest.fixture
def native_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    # conftest resets sys.path after every test, dropping the module-level
    # insert above; the agent still imports top-level modules lazily.
    if _AGENT_DIR.is_dir() and str(_AGENT_DIR) not in sys.path:
        monkeypatch.syspath_prepend(str(_AGENT_DIR))
    # When the suite itself runs inside a Kanban worker, the inherited
    # delegated-child marker would fence the pinned test DB read-only.
    from agent.delegation_context import DELEGATED_CHILD_ENV_MARKER

    monkeypatch.delenv(DELEGATED_CHILD_ENV_MARKER, raising=False)
    # Another test module may have swapped in a fake; the bridge must resolve
    # the real one imported above.
    monkeypatch.setattr(bridge, "_kb", lambda: kb)
    # Pin the bridge to the native closing connection on the throwaway DB.
    monkeypatch.setattr(bridge, "_conn", lambda board=None: connect_closing())
    return tmp_path


def _create(title="Original", model="model-a", provider="openai"):
    return bridge._create_task_payload(
        {"title": title, "model_override": model, "provider_override": provider}
    )["task"]["id"]


def _row(task_id):
    with connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        events = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
            ).fetchall()
        ]
    return task, events


def _intervene_before_write_txn(monkeypatch, write):
    """Run ``write`` on a second connection right before the PATCH's first
    write transaction opens."""
    real = kb.write_txn
    state = {"fired": False}

    def hooked(conn, *args, **kwargs):
        if not state["fired"]:
            state["fired"] = True
            with connect_closing() as other:
                write(other)
        return real(conn, *args, **kwargs)

    monkeypatch.setattr(kb, "write_txn", hooked)
    return state


def _intervene_after_snapshot(monkeypatch, write):
    """Start ``write`` on a second connection in another thread as soon as the
    PATCH has read the task row, and record whether it was still waiting half
    a second later (it must be: the PATCH holds the write lock)."""
    real = kb.get_task
    main = threading.get_ident()
    state = {"thread": None, "blocked": None, "error": None}

    def run():
        try:
            with connect_closing() as other:
                write(other)
        except Exception as exc:  # surfaced by the test, not swallowed
            state["error"] = exc

    def hooked(conn, task_id):
        task = real(conn, task_id)
        if state["thread"] is None and threading.get_ident() == main:
            state["thread"] = threading.Thread(target=run, daemon=True)
            state["thread"].start()
            state["thread"].join(0.5)
            state["blocked"] = state["thread"].is_alive()
        return task

    monkeypatch.setattr(kb, "get_task", hooked)
    return state


_PICK_C = {
    "title": "Edited",
    "model_override": "model-c",
    "provider_override": "openai",
    "expected_model_override": "model-a",
    "expected_provider_override": "openai",
}


# ── Window 1: routing changed to B by another connection ─────────────────────


def test_routing_change_before_the_write_rejects_without_mutation(native_board, monkeypatch):
    task_id = _create()
    state = _intervene_before_write_txn(
        monkeypatch, lambda other: kb.set_model_override(other, task_id, "model-b", provider="anthropic")
    )

    with pytest.raises(RuntimeError, match="changed since it was opened"):
        bridge._patch_task_payload(task_id, dict(_PICK_C))

    assert state["fired"]
    task, events = _row(task_id)
    assert task.title == "Original", "title committed before the routing conflict"
    assert (task.model_override, task.provider_override) == ("model-b", "anthropic"), "intervening B was overwritten"
    assert "updated" not in events
    assert events.count("model_override_set") == 1, "only the intervening write may be recorded"


def test_routing_change_after_the_snapshot_waits_for_the_patch(native_board, monkeypatch):
    task_id = _create()
    state = _intervene_after_snapshot(
        monkeypatch, lambda other: kb.set_model_override(other, task_id, "model-b", provider="anthropic")
    )

    bridge._patch_task_payload(task_id, dict(_PICK_C))
    state["thread"].join(30)

    assert state["blocked"], "a second connection wrote between the PATCH's check and its writes"
    assert state["error"] is None
    task, events = _row(task_id)
    # Serialized: the PATCH (validated against A) committed first, then B.
    assert task.title == "Edited"
    assert (task.model_override, task.provider_override) == ("model-b", "anthropic"), "intervening B was lost"
    assert events.count("model_override_set") == 2


# ── Window 2: task archived by another connection ────────────────────────────


def test_archive_before_the_write_rejects_without_committing_the_title(native_board, monkeypatch):
    task_id = _create()
    state = _intervene_before_write_txn(monkeypatch, lambda other: kb.archive_task(other, task_id))

    with pytest.raises(RuntimeError, match="archived"):
        bridge._patch_task_payload(task_id, {
            "title": "Edited",
            "priority": 7,
            "assignee": "someone-else",
            "model_override": "model-c",
            "provider_override": "openai",
        })

    assert state["fired"]
    task, events = _row(task_id)
    assert task.status == "archived"
    assert task.title == "Original", "title committed before the archived rejection"
    assert task.priority == 0
    assert task.assignee is None
    assert (task.model_override, task.provider_override) == ("model-a", "openai")
    assert "updated" not in events and "assigned" not in events and "model_override_set" not in events


def test_archive_after_the_snapshot_waits_for_the_patch(native_board, monkeypatch):
    task_id = _create()
    state = _intervene_after_snapshot(monkeypatch, lambda other: kb.archive_task(other, task_id))

    bridge._patch_task_payload(task_id, dict(_PICK_C))
    state["thread"].join(30)

    assert state["blocked"], "a second connection archived between the PATCH's check and its writes"
    assert state["error"] is None
    task, events = _row(task_id)
    # Serialized: the edit was valid when it committed; the archive followed.
    assert task.status == "archived"
    assert task.title == "Edited"
    assert (task.model_override, task.provider_override) == ("model-c", "openai")
    assert events.index("model_override_set") < events.index("archived")


# ── Retained positives / guards on the native path ───────────────────────────


def test_untouched_routing_edit_applies_on_an_archived_task(native_board):
    task_id = _create()
    bridge._patch_task_payload(task_id, {"status": "archived"})

    patched = bridge._patch_task_payload(task_id, {"title": "Edited while archived"})["task"]

    assert patched["title"] == "Edited while archived"
    assert patched["status"] == "archived"
    assert (patched["model_override"], patched["provider_override"]) == ("model-a", "openai")
    # Re-sending the same pair (picker-prefixed spelling) is not a change either.
    resaved = bridge._patch_task_payload(task_id, {
        "title": "Resaved",
        "model_override": "@openai:model-a",
        "provider_override": "openai",
    })["task"]
    assert resaved["title"] == "Resaved"
    _, events = _row(task_id)
    assert events.count("model_override_set") == 0


def test_sequential_conflict_rejects_and_matching_pair_applies(native_board):
    task_id = _create()
    with connect_closing() as other:
        kb.set_model_override(other, task_id, "model-b", provider="anthropic")

    with pytest.raises(RuntimeError, match="changed since it was opened"):
        bridge._patch_task_payload(task_id, dict(_PICK_C))
    task, _ = _row(task_id)
    assert task.title == "Original"
    assert (task.model_override, task.provider_override) == ("model-b", "anthropic")

    applied = bridge._patch_task_payload(task_id, {
        **_PICK_C,
        "expected_model_override": "model-b",
        "expected_provider_override": "anthropic",
    })["task"]
    assert applied["title"] == "Edited"
    assert (applied["model_override"], applied["provider_override"]) == ("model-c", "openai")


def _claim(task_id):
    with connect_closing() as other:
        other.execute(
            "UPDATE tasks SET status = 'running', claim_lock = 'host:1', assignee = 'worker' "
            "WHERE id = ?", (task_id,),
        )


def test_assignee_rejection_rolls_back_the_rest_of_the_edit(native_board):
    task_id = _create()
    _claim(task_id)

    with pytest.raises(RuntimeError, match="currently running"):
        bridge._patch_task_payload(task_id, {
            "title": "Edited",
            "priority": 7,
            "assignee": "someone-else",
            "model_override": "model-c",
            "provider_override": "openai",
        })

    task, events = _row(task_id)
    assert task.title == "Original"
    assert task.priority == 0
    assert task.assignee == "worker"
    assert (task.model_override, task.provider_override) == ("model-a", "openai")
    assert "updated" not in events and "assigned" not in events and "model_override_set" not in events


def test_unchanged_assignee_does_not_reject_an_edit_of_a_claimed_task(native_board):
    task_id = _create()
    _claim(task_id)

    # The edit modal always re-sends the assignee; re-saving the current one
    # is not a reassignment, so the running refusal must not fire.
    patched = bridge._patch_task_payload(task_id, {"title": "Edited", "assignee": "Worker"})["task"]

    assert patched["title"] == "Edited"
    assert patched["assignee"] == "worker"
    _, events = _row(task_id)
    assert "assigned" not in events


def test_running_status_rejects_without_mutation(native_board):
    task_id = _create()

    with pytest.raises(ValueError, match="running"):
        bridge._patch_task_payload(task_id, {"title": "Edited", "status": "running"})

    task, events = _row(task_id)
    assert task.title == "Original"
    assert "updated" not in events


def test_explicit_clear_both_applies(native_board):
    task_id = _create()

    cleared = bridge._patch_task_payload(task_id, {
        "model_override": "",
        "provider_override": "",
        "expected_model_override": "model-a",
        "expected_provider_override": "openai",
    })["task"]

    assert (cleared["model_override"], cleared["provider_override"]) == (None, None)
    _, events = _row(task_id)
    assert events.count("model_override_set") == 1


def _assignment(task_id):
    with connect_closing() as conn:
        row = conn.execute(
            "SELECT assignee, consecutive_failures, last_failure_error FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned' "
            "ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
    return tuple(row), event["payload"]


def test_assignee_edit_matches_native_assign_task(native_board):
    ours, native = _create(), _create()
    with connect_closing() as other:
        for task_id in (ours, native):
            kb.assign_task(other, task_id, "first")
            other.execute(
                "UPDATE tasks SET consecutive_failures = 3, last_failure_error = 'boom' WHERE id = ?",
                (task_id,),
            )

    bridge._patch_task_payload(ours, {"title": "Edited", "assignee": "Someone-Else"})
    with connect_closing() as other:
        kb.assign_task(other, native, "Someone-Else")

    assert _assignment(ours) == _assignment(native)
    task, events = _row(ours)
    assert task.title == "Edited"
    assert events[-2:] == ["updated", "assigned"]
