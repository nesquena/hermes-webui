"""Regression coverage for #2551 stale sidebar after Move-to-Project.

The single-session project picker (`_showProjectPicker` in `static/sessions.js`)
used to update only the pre-index `_allSessions` cache and then call
`renderSessionListFromCache()`. The Projects/Chats sidebar now renders from
`_sessionIndexGroups[].sessions`, so the optimistic update must keep both
caches in sync until the next `/api/session-index` refresh.
"""

from pathlib import Path
import json
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[1]
SESSIONS_SRC = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _show_project_picker_body() -> str:
    start = SESSIONS_SRC.find("function _showProjectPicker(")
    assert start != -1, "_showProjectPicker not found in sessions.js"
    # Pick a stable downstream sentinel that lives after the function ends.
    end = SESSIONS_SRC.find("function _resizeProjectInput(", start)
    assert end != -1, "_resizeProjectInput sentinel not found after picker"
    return SESSIONS_SRC[start:end]


PICKER_BODY = _show_project_picker_body()


def test_no_project_branch_updates_rendered_session_caches():
    """The 'No project' callback must update both session caches after the
    /api/session/move call so the immediate re-render reflects the move."""
    none_idx = PICKER_BODY.find("'Removed from project'")
    assert none_idx != -1, "'Removed from project' branch not located"
    # Look back over the callback body
    window = PICKER_BODY[max(0, none_idx - 600): none_idx]
    assert "_updateSessionProjectCache(session.session_id,null)" in window, (
        "No-project branch must update _allSessions and _sessionIndexGroups, "
        "not just the old pre-index render cache (issue #2551)."
    )


def test_existing_project_branch_updates_rendered_session_caches():
    """The existing-project callback must update both session caches after the
    /api/session/move call so the immediate re-render reflects the move."""
    moved_idx = PICKER_BODY.find("'Moved to '+p.name")
    assert moved_idx != -1, "'Moved to '+p.name branch not located"
    window = PICKER_BODY[max(0, moved_idx - 600): moved_idx]
    assert "_updateSessionProjectCache(session.session_id,p.project_id)" in window, (
        "Existing-project branch must update _allSessions and "
        "_sessionIndexGroups, not just the old pre-index render cache "
        "(issue #2551)."
    )


def test_picker_callbacks_do_not_rely_on_shallow_copy_mutation():
    """Pinning the failure mode: the picker callbacks must not return without
    updating the authoritative caches. The previous bug looked like
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
    """Behavioural check: simulate the cache-write helper from each picker
    branch and confirm both the legacy list cache and the grouped render cache
    reflect the new project_id.
    """
    if NODE is None:
        pytest.skip("node not on PATH")
    script = """
let _allSessions = [
  {session_id: 'sa', project_id: 'proj-old', title: 'A'},
  {session_id: 'sb', project_id: null, title: 'B'},
];
let _sessionIndexGroups = [
  {group_id: 'chats', sessions: [
    {session_id: 'sa', project_id: 'proj-old', title: 'A'},
    {session_id: 'sb', project_id: null, title: 'B'},
  ]},
];

function _updateSessionProjectCache(sessionId, projectId){
  if(!sessionId) return;
  if(Array.isArray(_allSessions)){
    const idx=_allSessions.findIndex(s=>s&&s.session_id===sessionId);
    if(idx>=0) _allSessions[idx]={..._allSessions[idx],project_id:projectId};
  }
  for(const group of Array.isArray(_sessionIndexGroups)?_sessionIndexGroups:[]){
    for(const row of Array.isArray(group&&group.sessions)?group.sessions:[]){
      if(row&&row.session_id===sessionId) row.project_id=projectId;
    }
  }
}

_updateSessionProjectCache('sa', null);
_updateSessionProjectCache('sb', 'proj-new');

console.log(JSON.stringify({
  all: _allSessions.map(s => ({id: s.session_id, project_id: s.project_id})),
  grouped: _sessionIndexGroups[0].sessions.map(s => ({id: s.session_id, project_id: s.project_id})),
}));
"""
    result = subprocess.run(
        [NODE, "-e", script], check=True, capture_output=True, text=True
    )
    observed = json.loads(result.stdout)
    expected = [
        {"id": "sa", "project_id": None},
        {"id": "sb", "project_id": "proj-new"},
    ]
    assert observed == {"all": expected, "grouped": expected}


def test_new_project_branch_still_uses_authoritative_refetch():
    """The '+ New project' path was already correct: it calls
    `await renderSessionList()` (a full /api/sessions refetch) after
    creating the project. The minimal fix must not change that.
    """
    create_idx = PICKER_BODY.find("'+ New project'")
    assert create_idx != -1, "'+ New project' branch not located"
    window = PICKER_BODY[create_idx: create_idx + 900]
    assert "await renderSessionList()" in window, (
        "'+ New project' branch must keep its authoritative refetch — the "
        "new project_id is only known to the server until /api/sessions is "
        "re-fetched."
    )
