"""Round-7 re-review of the merge-pushed heads (Greptile 2026-10-10T10:11:13Z).

P2 "Checkbox restores an old setting" (``static/sessions.js:10849``):
``_saveProjectBindings`` refreshed the in-memory project CACHE
(``_allProjects[idx]=updated``) but left the OPEN dialog's own ``proj`` object at
its pre-save values.  Both "restore the stored value" paths read ``proj`` — the
``aaCb.onchange`` decline path and Save's own decline path both do
``aaCb.checked=!!proj.auto_assign`` — so after a Save that turned auto-assign ON
while the dialog stayed open (the user edited a control during the round-trip),
toggling the box again and DECLINING the confirmation put the box back to the
STALE ``false`` even though the server now stored ``true``; a following Save then
posted ``auto_assign:false`` and silently disabled the setting that had just
succeeded.

Fix: after a successful bind, copy the persisted fields onto the dialog's
``proj`` IN PLACE (the dialog closure holds that object, so replacing the
reference would detach the dialog from the cache).  The dialog's unsaved
controls are DOM state and are left untouched.

Follow-up: maintainer review 5478955688 (2026-10-10T12:35:21Z, [SILENT]
``static/sessions.js:10862``) — the copy loop only handled keys the response
CARRIES, but ``/api/projects/bind`` POPS a cleared field instead of echoing it
as ``false``/``''`` (``proj.pop("auto_assign", None)`` and friends).  So a
CLEAR now has to be copied as a DELETE: without it, Save-off left the old
``true`` on the dialog snapshot and the restore paths put it back.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_SAVE_FN_START = "async function _saveProjectBindings(proj, fields){"
_SAVE_FN_END = "\n// Custom combobox for the bindings dialog"


def _save_fn() -> str:
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    return src[src.index(_SAVE_FN_START) : src.index(_SAVE_FN_END)]


def _run_node(tmp_path: Path, name: str, script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is required for the frontend behavior probe")
    script_path = tmp_path / name
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


# The dialog's own proj object is what the decline/restore paths read.  A fake
# api() accepts the bind and echoes the stored project the way the server does
# ({"ok": true, "project": proj}); the probe then checks the OPEN dialog's
# snapshot reflects the accepted value WITHOUT its identity being replaced.
_SAVE_BINDINGS_PROBE = r"""
let accepted = true;
async function api(){
  if(!accepted) throw new Error('boom');
  return {ok: true, project: {
    project_id: 'p1', name: 'proj', color: 'blue',
    workspaces: ['/tmp/ws'], default_workspace: '/tmp/ws',
    model: 'gpt', model_provider: 'openai', auto_assign: true,
  }};
}
const _allProjects = [{project_id: 'p1', name: 'proj', auto_assign: false}];
function showToast(){}
function t(k){ return 'T:' + k; }
function assert(cond, msg){ if(!cond) throw new Error(msg); }
const renderSessionListFromCache = undefined;
const renderSessionList = undefined;
__SAVE__
(async () => {
  // The dialog holds THIS object; the decline paths read proj.auto_assign.
  const proj = {project_id: 'p1', name: 'proj', auto_assign: false};
  const ok = await _saveProjectBindings(proj, {auto_assign: true});
  assert(ok === true, 'an accepted bind must report true');
  assert(proj.auto_assign === true,
    'the dialog snapshot must carry the STORED auto_assign, got ' + proj.auto_assign);
  // The unsaved controls live in the DOM, not on proj; the synced fields are
  // exactly the persisted ones.
  assert(proj.workspaces && proj.workspaces[0] === '/tmp/ws', 'workspaces synced');
  assert(proj.model === 'gpt' && proj.model_provider === 'openai', 'model synced');

  // A failed request must NOT touch the snapshot (the dialog stays open with
  // the user's edits and the stored value unchanged).
  accepted = false;
  let threw = false;
  try { await _saveProjectBindings(proj, {auto_assign: false}); } catch(_) { threw = true; }
  assert(threw === false, '_saveProjectBindings must swallow the error and return false');
  assert(proj.auto_assign === true, 'a rejected bind must not rewrite the snapshot');

  console.log('ok');
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def test_save_refreshes_the_open_dialogs_project_in_place(tmp_path):
    """Greptile P2 (2026-10-10T10:11:13Z): the cached project was refreshed but
    the dialog's own ``proj`` was not, so a stale snapshot silently flipped a
    just-saved setting back off."""
    out = _run_node(
        tmp_path,
        "save_bindings_snapshot.js",
        _SAVE_BINDINGS_PROBE.replace("__SAVE__", _save_fn()),
    )
    assert out.strip().endswith("ok")


def test_save_snapshot_sync_is_in_place_and_persisted_only():
    """Source guard: the sync copies the persisted fields onto the SAME object
    (never replaces ``proj``), so the dialog closure stays attached."""
    fn = _save_fn()
    assert "proj[_k]=updated[_k]" in fn, fn
    assert "for(const _k of [" in fn, fn
    # It must mutate, not rebind: no `proj = updated` / `Object.assign(proj,`-free
    # reassignment of the closure variable.
    assert "proj=updated" not in fn.replace(" ", ""), fn
    # The restore paths still read proj (the fix depends on that).
    src = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    assert "aaCb.checked=!!proj.auto_assign;" in src


# The server clears a field by POPPING it, so a save that turns something OFF
# answers with a project that simply lacks the key.  The probe replays that
# exact response and checks the dialog snapshot loses the stale value.
_SAVE_CLEARED_FIELD_PROBE = r"""
let response;
async function api(){
  return {ok: true, project: response};
}
const _allProjects = [{project_id: 'p1', name: 'proj', auto_assign: true,
  model: 'gpt', model_provider: 'openai'}];
function showToast(){}
function t(k){ return 'T:' + k; }
function assert(cond, msg){ if(!cond) throw new Error(msg); }
const renderSessionListFromCache = undefined;
const renderSessionList = undefined;
__SAVE__
(async () => {
  // The dialog holds THIS object; the restore paths read proj.auto_assign.
  const proj = {project_id: 'p1', name: 'proj', auto_assign: true,
    model: 'gpt', model_provider: 'openai'};
  // Clearing auto_assign / the model pair POPS those keys server-side, so the
  // bind response carries only what is still stored.
  response = {project_id: 'p1', name: 'proj'};
  const ok = await _saveProjectBindings(proj, {auto_assign: false, model: null});
  assert(ok === true, 'an accepted bind must report true');
  assert(!('auto_assign' in proj),
    'a cleared auto_assign must be DELETED from the snapshot, got ' + proj.auto_assign);
  assert(!('model' in proj) && !('model_provider' in proj),
    'a cleared model pair must be deleted too');
  // A field the response still carries is copied, and keys the loop never
  // manages (project_id / color) are untouched.
  assert(proj.name === 'proj', 'a carried field is copied');
  assert(proj.project_id === 'p1', 'unmanaged keys stay');
  // The restore path can no longer resurrect the old ON.
  assert(proj.auto_assign !== true, 'the stale ON must not survive');

  console.log('ok');
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def test_save_deletes_a_field_the_server_cleared(tmp_path):
    """Maintainer review 5478955688: ``/api/projects/bind`` pops a cleared
    field, so the snapshot copy must DELETE it instead of keeping the pre-save
    value (the restore paths would otherwise silently re-enable auto-assign)."""
    out = _run_node(
        tmp_path,
        "save_bindings_cleared.js",
        _SAVE_CLEARED_FIELD_PROBE.replace("__SAVE__", _save_fn()),
    )
    assert out.strip().endswith("ok")


def test_save_snapshot_sync_deletes_absent_keys():
    """Source guard for the same defect: the copy loop carries an ``else``
    branch that removes a key the response does not include."""
    fn = _save_fn()
    assert "else delete proj[_k];" in fn, fn
    # The loop's key list is unchanged (a popped key must be re-deletable).
    assert (
        "for(const _k of ['name','workspaces','default_workspace','model',"
        "'model_provider','auto_assign']){" in fn
    ), fn
