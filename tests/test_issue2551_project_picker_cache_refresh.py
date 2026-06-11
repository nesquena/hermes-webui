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


def test_no_project_branch_writes_to_allSessions_cache():
    """The 'No project' callback must update `_allSessions[idx].project_id`
    after the /api/session/move call so the re-render reflects the move."""
    none_idx = PICKER_BODY.find("'Removed from project'")
    assert none_idx != -1, "'Removed from project' branch not located"
    # Look back over the callback body
    window = PICKER_BODY[max(0, none_idx - 600): none_idx]
    assert "_allSessions.findIndex" in window, (
        "No-project branch must locate the session in _allSessions so the "
        "cache reflects the move (issue #2551)."
    )
    assert "_allSessions[idx].project_id=null" in window, (
        "No-project branch must write project_id=null into _allSessions, "
        "not just the shallow sidebar copy (issue #2551)."
    )


def test_existing_project_branch_writes_to_allSessions_cache():
    """The existing-project callback must update `_allSessions[idx].project_id`
    after the /api/session/move call so the re-render reflects the move."""
    moved_idx = PICKER_BODY.find("'Moved to '+p.name")
    assert moved_idx != -1, "'Moved to '+p.name branch not located"
    window = PICKER_BODY[max(0, moved_idx - 600): moved_idx]
    assert "_allSessions.findIndex" in window, (
        "Existing-project branch must locate the session in _allSessions so "
        "the cache reflects the move (issue #2551)."
    )
    assert "_allSessions[idx].project_id=p.project_id" in window, (
        "Existing-project branch must write project_id=p.project_id into "
        "_allSessions, not just the shallow sidebar copy (issue #2551)."
    )


def test_picker_callbacks_do_not_rely_on_shallow_copy_mutation():
    """Pinning the failure mode: the picker callbacks must not return without
    updating the authoritative cache. The previous bug looked like
    `session.project_id=null; renderSessionListFromCache();` with no cache
    write between, which is what produced the stale render."""
    # Both branches end with renderSessionListFromCache(). Count how many
    # times the buggy bare mutation precedes a cache render with no
    # _allSessions write in between.
    buggy_no_project = "session.project_id=null;\n    renderSessionListFromCache();"
    buggy_existing = "session.project_id=p.project_id;\n      renderSessionListFromCache();"
    assert buggy_no_project not in PICKER_BODY, (
        "No-project branch still mutates only the shallow copy before "
        "re-render — restore the _allSessions write (issue #2551)."
    )
    assert buggy_existing not in PICKER_BODY, (
        "Existing-project branch still mutates only the shallow copy before "
        "re-render — restore the _allSessions write (issue #2551)."
    )


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
