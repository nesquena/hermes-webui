"""#7826 re-gate: the three remaining items, driven as behaviour.

The re-gate accepted eight of the previous findings and reproduced three:

1. **[CORE]** Read-only Gateway sessions become writable. ``api/routes.py``'s
   archive fallback matched a same-profile row and materialized a sidecar with
   ``read_only: false`` and null source identity, returning 200 where master
   returns 404 without creating a file. The owner-only lookup opens state.db
   read-only and never loads a sidecar, so it carries none of the metadata a
   write path needs.

2. **[SILENT]** Mixed-profile batch results depend on selection order. The
   preflight accepted foreign owners even though the server ignores the supplied
   ``profile``; the first 409 then stopped the batch, so ``[foreign, owned]``
   skipped the owned row while ``[owned, foreign]`` archived it.

3. **[SILENT]** A failed preflight discarded the entire selection.

These tests drive the REAL functions out of ``static/sessions.js`` in node and
pin the shipped expression rather than a restatement of it.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import pytest

_SESSIONS_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static",
    "sessions.js",
)
_ROUTES_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "api",
    "routes.py",
)

pytestmark = pytest.mark.skipif(
    subprocess.run(["which", "node"], capture_output=True).returncode != 0
    or not os.path.exists(_SESSIONS_JS),
    reason="node or static/sessions.js unavailable",
)


def _sessions_source() -> str:
    with open(_SESSIONS_JS, encoding="utf-8") as handle:
        return handle.read()


def _routes_source() -> str:
    with open(_ROUTES_PY, encoding="utf-8") as handle:
        return handle.read()


def _extract_function(src: str, name: str) -> str:
    start = src.find(f"function {name}(")
    assert start >= 0, f"function {name} not found"
    i = src.find("{", start)
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces after {name}")


def _run_js(js_code: str, payload: dict, *extra_args: str) -> object:
    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            ["node", tf.name, *extra_args, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"node error: {result.stderr}"
        return json.loads(result.stdout)
    finally:
        os.unlink(tf.name)


# ── 1. CORE: the archive fallback must not materialize a same-profile row ────


def test_the_archive_fallback_refuses_a_read_only_row():
    """A read-only row reaching this fallback must 404, not materialize.

    The owner-only lookup opens state.db read-only and never loads a sidecar, so
    it carries none of the metadata a write path needs — most visibly
    ``read_only``. A Gateway row older than 25 newer CLI rows detail-loads with
    ``read_only: true``, but after a cache eviction this fallback matched it,
    materialized a sidecar with ``read_only: false`` and null source identity, and
    returned 200 where master returns 404 without creating a file.
    """
    src = _routes_source()
    # The 409 branch for a foreign owner must remain.
    assert '"code": "session_profile_mismatch"' in src, (
        "the foreign-owner 409 branch is gone"
    )
    # And a read-only row must 404 — before any materialization.
    mismatch_idx = src.find('"code": "session_profile_mismatch"')
    read_only_idx = src.find('if cli_meta.get("read_only"):', mismatch_idx)
    assert read_only_idx > mismatch_idx, (
        "the read_only guard is gone — the fallback would materialize a "
        "read-only session again"
    )
    guard = src[read_only_idx : read_only_idx + 200]
    assert 'return bad(handler, "Session not found", 404)' in guard, (
        f"a read-only row must take the 404 self-heal path, got: {guard!r}"
    )
    # The old 400 for read-only rows is replaced by the 404 above.
    assert "Read-only imported sessions cannot be archived from WebUI" not in src[read_only_idx : read_only_idx + 400], (
        "the old 400 for read-only rows is still there"
    )


def test_the_archive_fallback_still_archives_a_writable_same_profile_row():
    """Counterpart: the legitimate same-profile CLI archive must keep working.

    Re-gate CORE: the owner-only fallback itself no longer materializes ANY
    same-profile match (it cannot prove writability — see the provenance
    guard). A writable same-profile CLI row archives through the NORMAL
    active-profile lookup: ``cli_meta`` from ``_lookup_cli_session_metadata``
    skips the fallback entirely, so the materialization path must still be
    reachable after the fallback's guards.
    """
    src = _routes_source()
    read_only_idx = src.find('if cli_meta.get("read_only"):')
    # The provenance guard sits immediately before the read_only guard.
    provenance_idx = src.find("if _owner_fallback_used:", read_only_idx - 400)
    assert provenance_idx >= 0, (
        "the owner-fallback provenance guard is gone — a same-profile fallback "
        "match would be materialized again"
    )
    assert provenance_idx < read_only_idx, (
        "the provenance guard must fire BEFORE the read_only guard so every "
        "same-profile fallback match 404s"
    )
    # The materialization path must still be reachable after the guards for
    # the normal-lookup (non-fallback) path.
    after = src[read_only_idx : read_only_idx + 3000]
    assert "import_cli_session(" in after, (
        "the materialization path is gone — a writable same-profile CLI session "
        "can no longer be archived through the normal lookup"
    )


# ── 2. SILENT: preflight must validate owners against the ACTIVE profile ─────

_PREFLIGHT_DRIVER = r"""
// Drives the REAL _archiveBatchOwners / _archiveBatchOwnerForRow /
// _archiveBatchOwnersMatch / _activeBatchProfile out of static/sessions.js.
const fs = require('fs');
// argv: [node, driver, sessionsJsPath, payloadJson]
const src = fs.readFileSync(process.argv[2], 'utf8');
const payload = JSON.parse(process.argv[3]);

function extract(name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

// Minimal S: only the fields the real helpers read.
const S = {activeProfile: payload.activeProfile, activeProfileIsDefault: payload.activeProfileIsDefault === true};

for (const name of ['_profileMatchesActiveProfile', '_archiveBatchOwnerForRow',
                    '_archiveBatchOwnersMatch', '_archiveBatchOwners']) {
  eval(extract(name));
}

// The row predicates the owner resolver consults.
global._isWebUiSourceSession = (snap) => snap && snap.__webui === true;

const byId = new Map(payload.rows.map(r => [r.sid, r]));
const out = {};
const preflight = _archiveBatchOwners(payload.ids, byId);
out.reason = preflight.reason || null;
out.ownerCount = preflight.owners.length;
out.activeBatchProfile = S.activeProfile;

// Which rows would actually be requested, following the shipped stop-on-failure
// contract: the first 409 stops the loop and every later row is abandoned.
out.requestedOrder = [];
for (const sid of payload.ids) {
  const resolved = _archiveBatchOwnerForRow(sid, byId);
  out.requestedOrder.push({sid, owner: resolved.owner, eligible: !!resolved.owner});
}
console.log(JSON.stringify(out));
"""


def _run_preflight(payload: dict) -> dict:
    return _run_js(_PREFLIGHT_DRIVER, payload, _SESSIONS_JS)


def _row(sid, profile=None, webui=False):
    row = {"sid": sid}
    if profile is not None:
        row["profile"] = profile
    if webui:
        row["__webui"] = True
    return row


def test_preflight_rejects_a_batch_containing_a_foreign_owner():
    """A foreign row makes the whole batch ineligible before any request."""
    out = _run_preflight({
        "activeProfile": "default",
        "activeProfileIsDefault": True,
        "ids": ["foreign", "owned"],
        "rows": [_row("foreign", profile="work"), _row("owned", profile="default")],
    })
    assert out["ownerCount"] == 0, (
        f"the preflight accepted a batch containing a foreign owner "
        f"(reason={out['reason']!r}); the server 409s that row and the "
        "stop-on-failure contract then abandons the owned row"
    )
    assert out["reason"] and out["reason"].startswith("foreign-owner:"), (
        f"expected a foreign-owner rejection, got {out['reason']!r}"
    )


def test_batch_outcome_no_longer_depends_on_selection_order():
    """[owned, foreign] and [foreign, owned] must both reject before any POST."""
    for order in (["owned", "foreign"], ["foreign", "owned"]):
        out = _run_preflight({
            "activeProfile": "default",
            "activeProfileIsDefault": True,
            "ids": order,
            "rows": [_row("foreign", profile="work"), _row("owned", profile="default")],
        })
        assert out["ownerCount"] == 0, (
            f"selection order {order} still produces an eligible batch — the "
            "owned row survives in one order and is skipped in the other"
        )


def test_a_root_alias_owner_is_still_eligible():
    """Counterpart: the root-alias rule must keep working.

    A row with no profile on its snapshot is a legacy root-owned WebUI sidecar,
    which the server resolves against the active profile. Rejecting it would
    break the ordinary case.
    """
    out = _run_preflight({
        "activeProfile": "default",
        "activeProfileIsDefault": True,
        "ids": ["a", "b"],
        "rows": [_row("a", webui=True), _row("b", profile="default")],
    })
    assert out["ownerCount"] > 0, (
        f"an all-eligible batch was rejected (reason={out['reason']!r})"
    )


def test_the_active_profile_is_the_server_side_benchmark():
    """The benchmark is S.activeProfile, never a client-supplied claim."""
    out = _run_preflight({
        "activeProfile": "work",
        "activeProfileIsDefault": False,
        "ids": ["a"],
        "rows": [_row("a", profile="work")],
    })
    assert out["activeBatchProfile"] == "work", (
        f"the preflight benchmarked against {out['activeBatchProfile']!r} "
        "instead of the active profile"
    )
    assert out["ownerCount"] > 0, "a row owned by the active profile was rejected"


# ── 3. SILENT: a failed preflight must not discard the selection ─────────────


def test_a_failed_preflight_keeps_the_selection():
    """The rejection branch must not call exitSessionSelectMode()."""
    src = _sessions_source()
    branch_idx = src.find("if(!preflight.owners.length){")
    assert branch_idx > 0, "the preflight rejection branch is gone"
    branch = src[branch_idx : branch_idx + 900]
    assert "exitSessionSelectMode()" not in branch, (
        "a failed preflight still clears the whole selection, so one "
        "ineligible row silently unselects every other row the user picked"
    )


def test_a_successful_archive_still_exits_selection_mode():
    """Counterpart: the success path must keep leaving selection mode."""
    src = _sessions_source()
    success_idx = src.find("showToast(outcome.retainedCount?t('session_archived_worktree')")
    assert success_idx > 0, "the success toast is gone"
    window = src[success_idx : success_idx + 300]
    assert "exitSessionSelectMode()" in window, (
        "a successful batch archive no longer leaves selection mode"
    )


# ── 4. should-fix: a foreign-owner preflight failure needs its own copy ────


def test_foreign_owner_preflight_failure_gets_its_own_toast():
    """A foreign-owner rejection must NOT reuse the mixed_profiles copy.

    That copy tells the user to archive the conversations one at a time —
    which for a foreign row hits the server's 409 again. The re-gate wants
    copy that suggests switching profile or deselecting instead.

    Driven through the REAL ``_renderBatchActionBar`` archive handler: the
    preflight runs against a foreign row and the toast key is recorded, so a
    refactor that re-collapses the two cases into one key fails here.
    """
    src = _sessions_source()
    bodies = {
        "owners": _extract_function(src, "_archiveBatchOwners"),
        "owner_row": _extract_function(src, "_archiveBatchOwnerForRow"),
        "match": _extract_function(src, "_archiveBatchOwnersMatch"),
        "render": _extract_function(src, "_renderBatchActionBar"),
    }
    driver = r"""
const events = [];
const rows = [
  { session_id: 'w1', profile: 'work', session_source: 'webui' },
  { session_id: 'h1', profile: 'home', session_source: 'webui' },
];
const sessionsById = new Map(rows.map(r => [r.session_id, r]));
const _allSessions = rows;
const _selectedSessions = new Set(['w1', 'h1']);
const S = { session: { session_id: 'root-chat' }, activeProfile: 'default',
            activeProfileIsDefault: true };
const _showArchived = false;
const _sessionSwipeReturnOffsets = { set() {} };
let _pendingSessionReflowPositions = null;
const bar = { style: {}, children: [], appendChild(n) { this.children.push(n); } };
function $(id) { return id === 'batchActionBar' ? bar : null; }
const document = { createElement(t) { return { tag: t, style: {}, className: '',
  textContent: '', appendChild() {}, onclick: null }; } };
function t(key) { return key; }
function _worktreeSessionCount() { return 0; }
function _sessionSnapshotById(sid) { return sessionsById.get(sid) || null; }
function showConfirmDialog() { events.push('confirm'); return Promise.resolve(true); }
function exitSessionSelectMode() { events.push('exit'); }
function showToast(msg) { events.push('toast:' + msg); }
function renderSessionList() {}
function _sessionPrefersReducedMotion() { return false; }
function _sessionResponseRetainsWorktree() { return false; }
function _profileMatchesActiveProfile(profile, active) { return profile === active; }
let switchCalls = 0;
async function _switchProfileForSessionLoad(profile) {
  switchCalls++; S.activeProfile = profile; S.activeProfileIsDefault = false;
}
async function api(path, opts) { events.push('api:' + path); return { worktree_retained: false }; }

%(owners)s
%(owner_row)s
%(match)s
%(render)s

_renderBatchActionBar();
(async () => {
  const button = bar.children.find(n => n.textContent === 'session_batch_archive');
  if (!button) throw new Error('no archive button');
  await button.onclick();
  console.log(JSON.stringify({ events, switchCalls }));
})();
""" % bodies
    out = _run_js(driver, {}, _SESSIONS_JS)
    toasts = [e[len("toast:"):] for e in out["events"] if e.startswith("toast:")]
    assert toasts == ["session_batch_archive_foreign_owner"], (
        f"a foreign-owner preflight failure must show the foreign-owner copy, "
        f"got {toasts!r}"
    )
    assert "session_batch_archive_mixed_profiles" not in out["events"], (
        "the foreign-owner case still shows the mixed_profiles copy, which "
        "suggests archiving one at a time — that 409s too"
    )
    assert "api:/api/session/archive" not in out["events"], (
        "a rejected batch must send no archive request"
    )
    assert out["switchCalls"] == 0, "the batch handler must never switch profiles"


def test_unknown_owner_preflight_failure_keeps_the_original_copy():
    """Counterpart: a row with no derivable owner still gets the original
    mixed_profiles copy (archiving one at a time is the right advice there).
    """
    src = _sessions_source()
    bodies = {
        "owners": _extract_function(src, "_archiveBatchOwners"),
        "owner_row": _extract_function(src, "_archiveBatchOwnerForRow"),
        "match": _extract_function(src, "_archiveBatchOwnersMatch"),
        "render": _extract_function(src, "_renderBatchActionBar"),
    }
    driver = r"""
const events = [];
const rows = [
  { session_id: 'ok', profile: 'default', session_source: 'webui' },
  { session_id: 'ghost', profile: null, session_source: 'cli' },
];
const sessionsById = new Map(rows.map(r => [r.session_id, r]));
const _allSessions = rows;
const _selectedSessions = new Set(['ok', 'ghost']);
const S = { session: { session_id: 'root-chat' }, activeProfile: 'default',
            activeProfileIsDefault: true };
const _showArchived = false;
const _sessionSwipeReturnOffsets = { set() {} };
let _pendingSessionReflowPositions = null;
const bar = { style: {}, children: [], appendChild(n) { this.children.push(n); } };
function $(id) { return id === 'batchActionBar' ? bar : null; }
const document = { createElement(t) { return { tag: t, style: {}, className: '',
  textContent: '', appendChild() {}, onclick: null }; } };
function t(key) { return key; }
function _worktreeSessionCount() { return 0; }
function _sessionSnapshotById(sid) { return sessionsById.get(sid) || null; }
function showConfirmDialog() { events.push('confirm'); return Promise.resolve(true); }
function exitSessionSelectMode() { events.push('exit'); }
function showToast(msg) { events.push('toast:' + msg); }
function renderSessionList() {}
function _sessionPrefersReducedMotion() { return false; }
function _sessionResponseRetainsWorktree() { return false; }
function _profileMatchesActiveProfile(profile, active) { return profile === active; }
let switchCalls = 0;
async function _switchProfileForSessionLoad(profile) { switchCalls++; }
async function api(path, opts) { events.push('api:' + path); return { worktree_retained: false }; }

%(owners)s
%(owner_row)s
%(match)s
%(render)s

_renderBatchActionBar();
(async () => {
  const button = bar.children.find(n => n.textContent === 'session_batch_archive');
  if (!button) throw new Error('no archive button');
  await button.onclick();
  console.log(JSON.stringify({ events, switchCalls }));
})();
""" % bodies
    out = _run_js(driver, {}, _SESSIONS_JS)
    toasts = [e[len("toast:"):] for e in out["events"] if e.startswith("toast:")]
    assert toasts == ["session_batch_archive_mixed_profiles"], (
        f"an unknown-owner rejection must keep the original copy, got {toasts!r}"
    )
    assert out["switchCalls"] == 0, "the batch handler must never switch profiles"
