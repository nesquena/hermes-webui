"""Round-7 re-review of the merge-pushed heads (Greptile 2026-10-10T10:11:13Z).

P2 "Checkbox restores an old setting" (``static/sessions.js:10849``):
``_saveProjectBindings`` refreshed the in-memory project CACHE
(``_allProjects[idx]=updated``) but left the OPEN dialog's own ``proj`` object at
its pre-save values.  The dialog's snapshots read ``proj``, so a stale ``proj``
silently put a just-saved setting back to its PRE-save value on the next Save.

Fix: after a successful bind, copy the persisted fields onto the dialog's
``proj`` IN PLACE (the dialog closure holds that object, so replacing the
reference would detach the dialog from the cache).  The dialog's unsaved
controls are DOM state and are left untouched.

Follow-up: maintainer review 5478955688 (2026-10-10T12:35:21Z, [SILENT]
``static/sessions.js:10862``) — the copy loop only handled keys the response
CARRIES, but ``/api/projects/bind`` POPS a cleared field instead of echoing it
as ``false``/``''`` (``proj.pop("default_workspace", None)`` and friends).  So a
CLEAR has to be copied as a DELETE: without it, the dialog snapshot kept the old
value of a field the server had just cleared.
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
    model: 'gpt', model_provider: 'openai',
  }};
}
const _allProjects = [{project_id: 'p1', name: 'proj'}];
function showToast(){}
function t(k){ return 'T:' + k; }
function assert(cond, msg){ if(!cond) throw new Error(msg); }
const renderSessionListFromCache = undefined;
const renderSessionList = undefined;
__SAVE__
(async () => {
  // The dialog holds THIS object; its snapshots read proj.
  const proj = {project_id: 'p1', name: 'proj'};
  const ok = await _saveProjectBindings(proj, {default_workspace: '/tmp/ws'});
  assert(ok === true, 'an accepted bind must report true');
  assert(proj.default_workspace === '/tmp/ws',
    'the dialog snapshot must carry the STORED default workspace, got '
      + proj.default_workspace);
  // The unsaved controls live in the DOM, not on proj; the synced fields are
  // exactly the persisted ones.
  assert(proj.workspaces && proj.workspaces[0] === '/tmp/ws', 'workspaces synced');
  assert(proj.model === 'gpt' && proj.model_provider === 'openai', 'model synced');

  // A failed request must NOT touch the snapshot (the dialog stays open with
  // the user's edits and the stored value unchanged).
  accepted = false;
  let threw = false;
  try { await _saveProjectBindings(proj, {default_workspace: '/tmp/other'}); } catch(_) { threw = true; }
  assert(threw === false, '_saveProjectBindings must swallow the error and return false');
  assert(proj.default_workspace === '/tmp/ws', 'a rejected bind must not rewrite the snapshot');

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
    # The loop's key list covers every persisted field the dialog can clear.
    assert ("'name','workspaces','default_workspace','model','model_provider'"
            in fn.replace(" ", "")), fn


# The server clears a field by POPPING it, so a save that turns something OFF
# answers with a project that simply lacks the key.  The probe replays that
# exact response and checks the dialog snapshot loses the stale value.
_SAVE_CLEARED_FIELD_PROBE = r"""
let response;
async function api(){
  return {ok: true, project: response};
}
const _allProjects = [{project_id: 'p1', name: 'proj', default_workspace: '/tmp/ws',
  model: 'gpt', model_provider: 'openai'}];
function showToast(){}
function t(k){ return 'T:' + k; }
function assert(cond, msg){ if(!cond) throw new Error(msg); }
const renderSessionListFromCache = undefined;
const renderSessionList = undefined;
__SAVE__
(async () => {
  // The dialog holds THIS object; its snapshots read proj.
  const proj = {project_id: 'p1', name: 'proj', default_workspace: '/tmp/ws',
    model: 'gpt', model_provider: 'openai'};
  // Clearing the default workspace / the model pair POPS those keys
  // server-side, so the bind response carries only what is still stored.
  response = {project_id: 'p1', name: 'proj'};
  const ok = await _saveProjectBindings(proj, {default_workspace: null, model: null});
  assert(ok === true, 'an accepted bind must report true');
  assert(!('default_workspace' in proj),
    'a cleared default workspace must be DELETED from the snapshot, got '
      + proj.default_workspace);
  assert(!('model' in proj) && !('model_provider' in proj),
    'a cleared model pair must be deleted too');
  // A field the response still carries is copied, and keys the loop never
  // manages (project_id / color) are untouched.
  assert(proj.name === 'proj', 'a carried field is copied');
  assert(proj.project_id === 'p1', 'unmanaged keys stay');
  // The stale value can no longer be resurrected by a later Save.
  assert(proj.default_workspace !== '/tmp/ws', 'the stale value must not survive');

  console.log('ok');
})().catch(function (e) { console.error(e && e.stack || e); process.exit(1); });
"""


def test_save_deletes_a_field_the_server_cleared(tmp_path):
    """Maintainer review 5478955688: ``/api/projects/bind`` pops a cleared
    field, so the snapshot copy must DELETE it instead of keeping the pre-save
    value (a later Save would otherwise re-submit the value the user cleared)."""
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
    # Every persisted field must stay in the list (a popped key must be
    # re-deletable). `auto_assign` is deliberately absent: the dialog never
    # submits it (the field is stored but dormant), so the snapshot never has to
    # track it.
    assert (
        "for(const _k of ['name','workspaces','default_workspace','model',"
        "'model_provider']){" in fn
    ), fn
    assert "'auto_assign'" not in fn.replace(" ", ""), fn
