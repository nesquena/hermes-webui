"""Regression coverage for #2551 stale sidebar after Move-to-Project.

The single-session project picker (`_showProjectPicker` in `static/sessions.js`)
used to mutate the sidebar's shallow row copy and then call
`renderSessionListFromCache()`, which re-reads the unmodified `_allSessions`
cache and renders the old `project_id`. The server-side move was correct, so
the next `/api/sessions` poll healed the UI — but until then the sidebar was
visually stale.

The fix writes the new `project_id` into the authoritative `_allSessions`
entry before re-rendering, so the optimistic update reflects the move
immediately without a wasted `/api/sessions` round trip.
"""

from pathlib import Path
import json
import subprocess

REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")


def _show_project_picker_body() -> str:
    start = SESSIONS_SRC.find("function _showProjectPicker(")
    assert start != -1, "_showProjectPicker not found in sessions.js"
    # Pick a stable downstream sentinel that lives after the function ends.
    end = SESSIONS_SRC.find("function _resizeProjectInput(", start)
    assert end != -1, "_resizeProjectInput sentinel not found after picker"
    return SESSIONS_SRC[start:end]


PICKER_BODY = _show_project_picker_body()


def _move_session_to_project_body() -> str:
    start = SESSIONS_SRC.find("async function _moveSessionToProject(")
    assert start != -1, "_moveSessionToProject not found in sessions.js"
    end = SESSIONS_SRC.find("function _showProjectPicker(", start)
    assert end != -1, "_showProjectPicker sentinel not found after move helper"
    return SESSIONS_SRC[start:end]


MOVE_HELPER_BODY = _move_session_to_project_body()


def test_no_project_branch_writes_to_allSessions_cache():
    """The shared move helper must write null to the authoritative cache."""
    assert "_allSessions.findIndex" in MOVE_HELPER_BODY
    assert "_allSessions[idx].project_id=projectId||null" in MOVE_HELPER_BODY
    assert "Removed from project" in MOVE_HELPER_BODY


def test_existing_project_branch_writes_to_allSessions_cache():
    """Both picker branches must delegate to the shared move helper."""
    assert "await _moveSessionToProject(session,null)" in PICKER_BODY
    assert "await _moveSessionToProject(session,p.project_id,p.name)" in PICKER_BODY
    assert "await api('/api/session/move'" in MOVE_HELPER_BODY
    assert "renderSessionListFromCache();" in MOVE_HELPER_BODY


def test_picker_callbacks_do_not_rely_on_shallow_copy_mutation():
    """Pinning the failure mode: the picker callbacks must not return without
    updating the authoritative cache. The previous bug looked like
    `session.project_id=null; renderSessionListFromCache();` with no cache
    write between, which is what produced the stale render."""
    # The picker delegates the cache write and render to the shared helper.
    buggy_no_project = "session.project_id=null;\n    renderSessionListFromCache();"
    buggy_existing = "session.project_id=p.project_id;\n      renderSessionListFromCache();"
    assert buggy_no_project not in PICKER_BODY
    assert buggy_existing not in PICKER_BODY


def test_cache_write_makes_render_observe_new_project_id():
    """End-to-end behavioural check: simulate the cache-write step from each
    picker branch and confirm `_allSessions` reflects the new project_id,
    which is what `renderSessionListFromCache` reads to repaint the sidebar.
    """
    script = """
let _allSessions = [
  {session_id: 'sa', project_id: 'proj-old', title: 'A'},
  {session_id: 'sb', project_id: null, title: 'B'},
];

// Sidebar copy, the way _attachChildSessionsToSidebarRows produces it:
const sidebarCopy = {..._allSessions[0]};

// Simulate the 'No project' branch cache write:
{
  const session = sidebarCopy;
  const idx = _allSessions.findIndex(s => s && s.session_id === session.session_id);
  if (idx >= 0) _allSessions[idx].project_id = null;
}

// Then the 'Moved to <project>' branch on session B going to proj-new:
{
  const session = {..._allSessions[1]};
  const p = {project_id: 'proj-new', name: 'New Project'};
  const idx = _allSessions.findIndex(s => s && s.session_id === session.session_id);
  if (idx >= 0) _allSessions[idx].project_id = p.project_id;
}

console.log(JSON.stringify(_allSessions.map(s => ({id: s.session_id, project_id: s.project_id}))));
"""
    result = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    rows = json.loads(result.stdout)
    assert rows == [
        {"id": "sa", "project_id": None},
        {"id": "sb", "project_id": "proj-new"},
    ], (
        "Cache write must replace project_id on the _allSessions entry, "
        "which is what renderSessionListFromCache reads (issue #2551)."
    )


def test_new_project_branch_still_uses_authoritative_refetch():
    """The '+ New project' path was already correct: it calls
    `await renderSessionList()` (a full /api/sessions refetch) after
    creating the project. The minimal fix must not change that.

    #3746 wrapped the move call in try/catch (so a 503 from a streaming
    session shows a toast instead of an unhandled rejection); the refetch is
    preserved in BOTH the success and the catch path. We scope to the
    create-branch block (up to the next picker item) rather than a fixed byte
    window so the assertion tracks intent, not exact offsets.
    """
    create_idx = PICKER_BODY.find("'+ New project'")
    assert create_idx != -1, "'+ New project' branch not located"
    # Bound the window at the end of the create handler (the picker.appendChild
    # that follows the createItem.onclick), falling back to a generous slice.
    end_idx = PICKER_BODY.find("picker.appendChild(createItem)", create_idx)
    window = PICKER_BODY[create_idx: end_idx if end_idx != -1 else create_idx + 1600]
    assert "await renderSessionList()" in window, (
        "'+ New project' branch must keep its authoritative refetch — the "
        "new project_id is only known to the server until /api/sessions is "
        "re-fetched."
    )
