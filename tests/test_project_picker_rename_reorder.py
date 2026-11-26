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

import pytest

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


# Picker box: top 100, 300 tall (bottom 400); the sticky "+ New project" row
# covers 370-400, so for every other row the visible box is 100-370 (1px slack).
# Each case: where the focused row sat before the refresh, where it sits after
# (a rename elsewhere ran the refresh), and the picker's scrollTop after the
# reveal frame (it starts at 50).
_REVEAL_CASES = {
    # Visible before, pushed below the box: scrolled until it clears the sticky row.
    'visible-then-below': ({'top': 200, 'bottom': 228}, {'top': 600, 'bottom': 628}, 50 + (628 - 370)),
    # A fraction of a pixel above the top edge still counts as visible.
    'fraction-above-edge': ({'top': 99.5, 'bottom': 127.5}, {'top': 600, 'bottom': 628}, 50 + (628 - 370)),
    # More than 1px above: the user had scrolled it away, so the picker stays put.
    'hidden-above': ({'top': 97, 'bottom': 125}, {'top': 600, 'bottom': 628}, 50),
    # Behind the sticky "+ New project" row is hidden too, though inside the border box.
    'behind-sticky-row': ({'top': 372, 'bottom': 400}, {'top': 600, 'bottom': 628}, 50),
    # Half a pixel under the sticky row's top still counts as visible.
    'fraction-into-sticky': ({'top': 342.5, 'bottom': 370.5}, {'top': 600, 'bottom': 628}, 50 + (628 - 370)),
    # Visible before and still within 1px after: no scroll for a sub-pixel shift.
    'still-visible-after': ({'top': 200, 'bottom': 228}, {'top': 342.6, 'bottom': 370.8}, 50),
    # Visible before, pushed above the box: scrolled up until its top is at the edge.
    'visible-then-above': ({'top': 200, 'bottom': 228}, {'top': -200, 'bottom': -172}, 50 - (100 - -200)),
}


@pytest.mark.parametrize('case', sorted(_REVEAL_CASES))
def test_open_picker_reveal_keeps_only_a_visible_focused_row_visible(case):
    before, after, expected_scroll = _REVEAL_CASES[case]
    script = harness._DRIVER_PREFIX + harness._REFRESH_FAKE_EXTENSIONS + harness._show_project_picker_source() + r'''
    _allProjects.splice(0,_allProjects.length,
      {project_id:'p-a',name:'Alpha',profile:'work'},
      {project_id:'p-b',name:'Bravo',profile:'work'},
      {project_id:'p-z',name:'Zulu',profile:'work'});
    openPicker(260);
    flushFrames();
    const picker=mountedPicker;
    const createItem=picker.children.find(c=>String(c.className).split(/\s+/).includes('project-picker-create'));
    const focused=picker.querySelectorAll('.project-picker-item[data-project-id]')
      .find(r=>r.getAttribute('data-project-id')==='p-b');
    const BEFORE=__BEFORE__, AFTER=__AFTER__;
    let focusedRect=BEFORE;
    Object.defineProperty(picker,'clientHeight',{value:300,configurable:true});
    picker.getBoundingClientRect=()=>({top:100,bottom:400,left:0,right:300,width:300,height:300});
    createItem.getBoundingClientRect=()=>({top:370,bottom:400,left:0,right:300,width:300,height:30});
    focused.getBoundingClientRect=()=>({left:0,right:300,width:300,height:focusedRect.bottom-focusedRect.top,...focusedRect});
    picker.scrollTop=50;
    focused.focus();
    // Rename another project so the open picker refreshes (and re-sorts).
    _allProjects.find(p=>p.project_id==='p-a').name='Yankee';
    const changed=_openProjectPicker.refreshChoices();
    focusedRect=AFTER;
    flushFrames();
    console.log(JSON.stringify({changed,scrollTop:picker.scrollTop,keptFocus:document.activeElement===focused}));
    '''.replace('__BEFORE__', json.dumps(before)).replace('__AFTER__', json.dumps(after))
    assert harness.NODE is not None
    result = subprocess.run([harness.NODE, '-e', script], capture_output=True, text=True, check=True, timeout=20)
    actual = json.loads(result.stdout)
    assert actual['changed'] is True, actual
    assert actual['keptFocus'] is True, actual
    assert actual['scrollTop'] == expected_scroll, (case, actual)
