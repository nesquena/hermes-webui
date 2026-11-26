"""An open "Move to project" picker keeps the alphabetical order when a project
is renamed or created while it is open (#6577).

The picker refreshes its rows in place so the focused row is never re-inserted
(Chromium blurs a focused node that is moved). Before the reorder pass, a rename
that changed a project's sort position left the open picker in the old order
while the sidebar chips re-sorted, and every later refresh kept reporting a
change. The other rows now move around the focused one.
"""
import json
import subprocess

from tests import test_project_picker_viewport_position as harness


def test_open_picker_refresh_reorders_renames_without_losing_project_focus():
    script = harness._DRIVER_PREFIX + harness._REFRESH_FAKE_EXTENSIONS + harness._show_project_picker_source() + r'''
    _allProjects.splice(0,_allProjects.length,
      {project_id:'p-a',name:'Alpha',profile:'work'},
      {project_id:'p-b',name:'Bravo',profile:'work'},
      {project_id:'p-z',name:'Zulu',profile:'work'});
    openPicker(260);
    const rows=()=>mountedPicker.querySelectorAll('.project-picker-item[data-project-id]').filter(r=>r.getAttribute('data-project-id'));
    const focused=rows().find(r=>r.getAttribute('data-project-id')==='p-a');
    focused.focus();
    focusCalls.length=0;
    _allProjects.find(p=>p.project_id==='p-a').name='Zzz renamed';
    refreshMoves.length=0;
    _openProjectPicker.refreshChoices();
    const afterRename=rows().map(r=>r.getAttribute('data-project-id'));
    const renameMoves=[...refreshMoves];
    const noOpAfterRename=_openProjectPicker.refreshChoices()===false;
    _allProjects.push({project_id:'p-new',name:'Aardvark',profile:'work'});
    refreshMoves.length=0;
    _openProjectPicker.refreshChoices();
    const afterCreate=rows().map(r=>r.getAttribute('data-project-id'));
    const createMoves=[...refreshMoves];
    console.log(JSON.stringify({afterRename,afterCreate,noOpAfterRename,renameMoves,createMoves,
      sameNode:rows().find(r=>r.getAttribute('data-project-id')==='p-a')===focused,
      keptFocus:document.activeElement===focused,focusAdded:focusCalls.length}));
    '''
    assert harness.NODE is not None
    result = subprocess.run([harness.NODE, '-e', script], capture_output=True, text=True, check=True, timeout=20)
    actual = json.loads(result.stdout)
    # The renamed, focused project moves to the end, and the refresh after it is
    # a no-op again (the in-place order now matches the sorted order).
    assert actual['afterRename'] == ['p-b', 'p-z', 'p-a'], actual
    assert actual['noOpAfterRename'] is True
    # Only the rows that had to move were re-inserted (the focused one never is).
    assert actual['renameMoves'] == ['p-z', 'p-b'], actual
    # A project created while the picker is open is inserted in its sorted place,
    # and no existing row is touched.
    assert actual['afterCreate'] == ['p-new', 'p-b', 'p-z', 'p-a'], actual
    assert actual['createMoves'] == ['p-new'], actual
    # The focused node was never re-inserted and kept focus without a new focus call.
    assert actual['sameNode'] is True
    assert actual['keptFocus'] is True
    assert actual['focusAdded'] == 0
