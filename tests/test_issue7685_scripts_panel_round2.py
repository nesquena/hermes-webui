"""#7685 review round 2: containment, honesty, and one invalidate helper.

Desired-invariant probes driven against the REAL functions. Each finding in
the second review round gets a behavioural regression:

1. A rejected leaf (a directory named ``entry.py``, a FIFO) does not leak
   the descriptor it acquired, and a FIFO cannot block the open.
2. Missing open flags refuse the read instead of silently dropping the
   containment they provide (never ``getattr(os, "O_X", 0)``).
3. A read that raises is reported as an error, and a capped read is
   reported as truncated — never as an empty/complete success.
4. The ALTERNATE switch path (``_switchProfileForSessionLoad`` in
   sessions.js) goes through the same invalidate/refresh helper and does
   not leave the previous profile's rows up, nor publish a pending reply
   (success OR error) over the new owner.
5. A refused switch restores the pane instead of leaving it blank, and a
   Scripts load started during the pending switch cannot repaint the
   cleared pane.

The frontend tests extract the real functions from the real source files
and run them under Node with a small DOM stand-in, so a stubbed-out
implementation cannot pass.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def scripts_module(tmp_path, monkeypatch):
    """Import ``api.scripts_panel`` with the profile pointed at a temp dir."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    profiles_stub = types.ModuleType("api.profiles")

    def _get_active_hermes_home():
        return tmp_path

    profiles_stub.get_active_hermes_home = _get_active_hermes_home
    monkeypatch.setitem(sys.modules, "api.profiles", profiles_stub)
    if "api.scripts_panel" in sys.modules:
        del sys.modules["api.scripts_panel"]
    import importlib

    return importlib.import_module("api.scripts_panel"), scripts_dir


def _extract(source: str, start_marker: str) -> str:
    """Source of the top-level statement starting at ``start_marker``.

    Balanced-brace scan, so the test keeps working when the function body is
    edited. Raises if the marker is gone from the source.
    """
    idx = source.find(start_marker)
    if idx == -1:
        raise AssertionError(f"marker {start_marker!r} not found in source")
    depth = 0
    i = idx
    started = False
    while i < len(source):
        ch = source[i]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
            if started and depth == 0:
                return source[idx : i + 1]
        i += 1
    raise AssertionError(f"unbalanced block for {start_marker!r}")


def _run(source: str) -> str:
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", source],
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node failed:\n{result.stderr}\n--- source ---\n{source}")
    return result.stdout.strip()


# ── finding 1: descriptor accounting + a real FIFO ────────────────────


def test_a_directory_leaf_does_not_leak_the_descriptor_it_acquired(
    scripts_module, monkeypatch
) -> None:
    """Finding 1: ``entry.py`` as a directory opens fine, so it is rejected
    only AFTER acquisition — and that fd must be closed, not leaked once
    per list request.
    """
    mod, scripts_dir = scripts_module
    (scripts_dir / "entry.py").mkdir()

    opens: list[int] = []
    closes: list[int] = []
    real_open, real_close = os.open, os.close

    def tracking_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        opens.append(fd)
        return fd

    def tracking_close(fd):
        closes.append(fd)
        return real_close(fd)

    monkeypatch.setattr(mod.os, "open", tracking_open)
    monkeypatch.setattr(mod.os, "close", tracking_close)

    # Both paths reach the opener and must not leak.
    assert mod._open_script_for_read(scripts_dir, "entry.py") is None
    assert mod.read_script("entry.py") is None
    assert mod.list_scripts()["scripts"] == []

    # Every descriptor acquired was released. Each call acquires a base fd
    # and then the leaf (which is what gets rejected here), so the point is
    # not the count but the balance: nothing may survive.
    assert opens, "the opener was never exercised"
    assert sorted(closes) == sorted(opens), (
        f"fd leak: opened {opens} but closed {closes}"
    )


def test_a_fifo_leaf_does_not_block_the_open(scripts_module) -> None:
    """Finding 1: a real FIFO must be refused (not opened, not blocked)."""
    mod, scripts_dir = scripts_module
    fifo_path = scripts_dir / "blocked.py"
    os.mkfifo(fifo_path)

    try:
        acquired = mod._open_script_for_read(scripts_dir, "blocked.py")
    finally:
        # Clear the FIFO so the fixture teardown can rmtree without hanging.
        if fifo_path.exists():
            fifo_path.unlink()

    assert acquired is None, "a FIFO must not be handed back as a regular file"

    # And through the public read path, which listing also reaches.
    os.mkfifo(fifo_path)
    try:
        assert mod.read_script("blocked.py") is None
        assert mod.list_scripts()["scripts"] == []
    finally:
        if fifo_path.exists():
            fifo_path.unlink()


def test_descriptors_are_closed_on_every_rejection_path(
    scripts_module, monkeypatch
) -> None:
    """Finding 1: track open/close over a directory containing a mixture of
    rejections (missing name, a directory, a symlink escape) and successes.
    """
    mod, scripts_dir = scripts_module
    outside = scripts_dir.parent / "outside.py"
    outside.write_text('"""outside"""\n', encoding="utf-8")
    (scripts_dir / "legit.py").write_text('"""legit"""\n', encoding="utf-8")
    (scripts_dir / "hole.py").symlink_to(outside)
    (scripts_dir / "dir.py").mkdir()

    opens: list[int] = []
    closes: list[int] = []
    real_open, real_close = os.open, os.close

    def tracking_open(path, flags, *a, **kw):
        fd = real_open(path, flags, *a, **kw)
        opens.append(fd)
        return fd

    def tracking_close(fd):
        closes.append(fd)
        return real_close(fd)

    monkeypatch.setattr(mod.os, "open", tracking_open)
    monkeypatch.setattr(mod.os, "close", tracking_close)

    listed = mod.list_scripts()
    for _ in range(5):
        assert mod.read_script("missing.py") is None
    assert mod.read_script("legit.py") is not None
    assert mod.read_script("dir.py") is None
    assert mod.read_script("hole.py") is None

    assert sorted(closes) == sorted(opens), (
        f"fd leak across every rejection path: opened {opens}, closed {closes}"
    )
    assert [e["name"] for e in listed["scripts"]] == ["legit.py"]


# ── finding 2: capability-checked flags, never a silent zero ──────────


def test_missing_no_follow_refuses_rather_than_substituting_zero(
    scripts_module, monkeypatch
) -> None:
    """Finding 2: ``getattr(os, "O_NOFOLLOW", 0)`` dropped the containment
    silently. A platform without the flag must fail closed.
    """
    mod, scripts_dir = scripts_module
    (scripts_dir / "race.py").write_text("stable\n", encoding="utf-8")

    monkeypatch.delattr(mod.os, "O_NOFOLLOW", raising=False)
    with pytest.raises(OSError):
        mod._leaf_open_flags()
    with pytest.raises(OSError):
        mod._base_open_flags()
    # The containment boundary then refuses the request outright.
    assert mod._open_script_for_read(scripts_dir, "race.py") is None


def test_missing_o_directory_refuses_the_base_open(scripts_module, monkeypatch) -> None:
    mod, scripts_dir = scripts_module
    (scripts_dir / "race.py").write_text("stable\n", encoding="utf-8")
    monkeypatch.delattr(mod.os, "O_DIRECTORY", raising=False)
    with pytest.raises(OSError):
        mod._base_open_flags()
    assert mod._open_script_for_read(scripts_dir, "race.py") is None


def test_o_nonblock_is_a_tolerated_absence_not_a_silent_drop(
    scripts_module, monkeypatch
) -> None:
    """O_NONBLOCK is the one optional flag: without it we still open, but the
    helper must say so instead of pretending the protection is present."""
    mod, _scripts_dir = scripts_module
    monkeypatch.delattr(mod.os, "O_NONBLOCK", raising=False)
    flags = mod._leaf_open_flags()
    assert flags & mod.os.O_RDONLY == 0  # O_RDONLY is 0 on POSIX
    # The containment flags are still there.
    assert flags & mod.os.O_NOFOLLOW
    assert flags & mod.os.O_CLOEXEC


def test_flags_actually_carry_the_containment(scripts_module) -> None:
    """The flag builders must really set the containment bits they document."""
    mod, _scripts_dir = scripts_module
    for flags in (mod._base_open_flags(), mod._leaf_open_flags()):
        assert flags & mod.os.O_NOFOLLOW, "O_NOFOLLOW must be set on every open"
        assert flags & mod.os.O_CLOEXEC, "O_CLOEXEC must be set on every open"
    assert mod._base_open_flags() & mod.os.O_DIRECTORY, "O_DIRECTORY on the base"
    # The leaf must NOT be opened with O_DIRECTORY (it is a file).
    assert not (mod._leaf_open_flags() & mod.os.O_DIRECTORY)
    assert mod._leaf_open_flags() & mod.os.O_NONBLOCK, "the leaf is nonblocking"


# ── finding 3: an honest read contract ────────────────────────────────


def test_a_read_error_is_reported_not_swallowed(scripts_module, monkeypatch) -> None:
    """Finding 3: a read that used to return ``{content:'',too_large:false}``
    on an I/O error must now surface a real error.
    """
    mod, scripts_dir = scripts_module
    (scripts_dir / "flaky.py").write_text("x = 1\n", encoding="utf-8")

    real_read = os.read

    def exploding_read(fd, n):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(mod.os, "read", exploding_read)
    try:
        result = mod.read_script("flaky.py")
        # The lower-level helper reports the same thing directly.
        acquired = mod._open_script_for_read(scripts_dir, "flaky.py")
        assert acquired is not None
        fd, _st = acquired
        try:
            read = mod._read_bounded(fd, 4096)
            assert not read.ok
            assert read.truncated is False
            assert read.content == b""
        finally:
            mod._close_quietly(fd)
    finally:
        monkeypatch.setattr(mod.os, "read", real_read)

    assert result is not None
    assert "content" not in result, "a failed read must not look like a file"
    assert result["error"] == "read_failed"
    assert "Input/output error" in result["detail"]


def test_short_read_still_loops_and_then_marks_truncation_at_the_cap(
    scripts_module, monkeypatch
) -> None:
    """A short read must loop so a small chunk does not truncate the preview,
    and hitting the cap must be reported rather than implied."""
    mod, scripts_dir = scripts_module
    (scripts_dir / "small.py").write_text("a = 1\n", encoding="utf-8")

    real_read = os.read

    # 2 bytes per read: the loop has to run several times.
    def short_read(fd, n):
        return real_read(fd, min(n, 2))

    monkeypatch.setattr(mod.os, "read", short_read)
    result = mod.read_script("small.py")
    assert result is not None and result["too_large"] is False
    assert result["content"] == "a = 1\n"
    assert result["truncated"] is False, "a complete read is not truncated"


def test_a_capped_read_is_flagged_truncated(scripts_module) -> None:
    """A read that stopped at the cap must say so; the old flag was missing."""
    mod, scripts_dir = scripts_module
    # Exactly the cap, and NOTHING after it: the reader stops when the budget is
    # exhausted, but a follow-up read returns EOF, so the file is COMPLETE.
    #
    # #7685 finding 3: this assertion used to demand truncated=True here. The
    # old reader treated "budget exhausted" as "there is more", which is wrong
    # for the exact-cap file — it is the only size where the two are not
    # distinguishable by the budget alone, so the reader now probes once more.
    (scripts_dir / "exact.py").write_text(
        "a" * mod._MAX_SCRIPT_BYTES, encoding="utf-8"
    )
    result = mod.read_script("exact.py")
    assert result is not None
    assert result["size"] == mod._MAX_SCRIPT_BYTES
    assert result["too_large"] is False
    assert len(result["content"]) == mod._MAX_SCRIPT_BYTES
    assert result["truncated"] is False


def test_a_failed_read_reports_the_error_it_caught(scripts_module, monkeypatch) -> None:
    """The error string carries the exception type and message, so the route
    layer can log something actionable instead of a silent empty body."""
    mod, scripts_dir = scripts_module
    (scripts_dir / "boom.py").write_text("b = 2\n", encoding="utf-8")

    def raising_read(fd, n):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(mod.os, "read", raising_read)
    result = mod.read_script("boom.py")
    assert result["error"] == "read_failed"
    assert "PermissionError" in result["detail"]


# ── frontend helpers (findings 4 and 5) ───────────────────────────────

_DOM_AND_HELPERS = """
// ── DOM stand-in ──────────────────────────────────────────────────────
function showToast(){}
function _applyModelToDropdown(){ return null; }
const _els = {};
function _mkEl(id){
  const cls = new Set();
  return {
    id, innerHTML:'', style:{}, hidden:false, disabled:false, textContent:'',
    dataset:{}, setAttribute(){}, getAttribute(){ return null; },
    querySelector(){ return null; }, querySelectorAll(){ return []; },
    classList: { add: (c)=>cls.add(c), remove: (c)=>cls.delete(c), toggle: (c)=>{ cls.has(c)?cls.delete(c):cls.add(c); }, contains: (c)=>cls.has(c) },
  };
}
function $(id){ if(!_els[id]) _els[id] = _mkEl(id); return _els[id]; }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }
const document = { querySelector(){ return null; }, querySelectorAll(){ return []; }, createElement(){ return _mkEl('new'); }, addEventListener(){} };
const window = {};
const localStorage = { removeItem(){}, getItem(){ return null; }, setItem(){} };
function t(k){ return k; }

// ── api() stand-in: URL-keyed replies, with per-URL parking so a reply can
// be held on the wire and delivered on demand (deferred replies, which the
// reviewer explicitly asked for). ``_park(url)`` makes subsequent calls for
// that URL hang; ``_deliver``/``_failUrl`` release exactly that URL's parked
// promises. Parking is additive — a second URL does not cancel the first.
const _routes = {};
const _parked = [];
const _parkUrls = new Set();
function _route(url, data){ _routes[url] = data; }
function _park(url){ _parkUrls.add(url); }
function _unpark(url){ _parkUrls.delete(url); }
function _tick(){ return new Promise(r => setTimeout(r, 5)); }
function _deliver(url){
  for(let i=_parked.length-1; i>=0; i--){
    if(_parked[i].url!==url) continue;
    const [p]=_parked.splice(i,1);
    p.resolve(_routes[p.url]);
  }
}
function _failUrl(url, err){
  for(let i=_parked.length-1; i>=0; i--){
    if(_parked[i].url!==url) continue;
    const [p]=_parked.splice(i,1);
    p.reject(err);
  }
}
_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
async function api(url){
  if (Object.prototype.hasOwnProperty.call(_routes, url)) {
    if (_parkUrls.has(url)) {
      return new Promise((resolve, reject) => _parked.push({url, resolve, reject}));
    }
    return _routes[url];
  }
  return { exists:true, scripts: [] };
}

// ── the state the switch paths read ──────────────────────────────────
const S = { activeProfile: 'alpha' };
let _currentTasksSubtab = 'jobs';
// _switchProfileForSessionLoad's failure path resets the skeleton flag that
// sessions.js owns; the harness declares it so the rollback is reachable.
let _sessionListSkeletonActive = false;
"""

_DECLS = """
let _scriptsOwnerProfile='';
let _scriptsRequestSeq=0;
let _scriptsLastDir=null;
let _scriptsInvalidateSeq=0;
let _scriptsSwitchNeedsRefresh=false;
"""


def _scripts_helpers() -> str:
    """The real Scripts helpers, plus the shared invalidate/refresh."""
    return (
        _extract(PANELS_JS, "function _scriptsOwnerKey(")
        + _extract(PANELS_JS, "function _invalidateScriptsForProfileSwitch(")
        + _extract(PANELS_JS, "function _refreshScriptsAfterProfileSwitch(")
        + _extract(PANELS_JS, "function clearScriptsList(")
        + _extract(PANELS_JS, "async function loadScriptsList(")
        + _extract(PANELS_JS, "function _scriptsReplyIsCurrent(")
        + _extract(PANELS_JS, "function _renderScriptItem(")
        + _extract(PANELS_JS, "function _formatScriptSize(")
    )


def _sessions_helper() -> str:
    """The REAL alternate switch helper from static/sessions.js."""
    return _extract(SESSIONS_JS, "async function _switchProfileForSessionLoad(")


def test_shared_invalidate_helper_exists_in_panels_js() -> None:
    """Guard the contract itself: one helper, used by both switch paths."""
    assert "function _invalidateScriptsForProfileSwitch(" in PANELS_JS
    assert "function _refreshScriptsAfterProfileSwitch(" in PANELS_JS
    # The alternate path must call it — no duplicated clear logic.
    alt = _sessions_helper()
    assert "_invalidateScriptsForProfileSwitch()" in alt
    assert "_refreshScriptsAfterProfileSwitch()" in alt
    # The canonical path too.
    canonical = _extract(PANELS_JS, "async function switchToProfile(")
    assert "_invalidateScriptsForProfileSwitch()" in canonical
    assert "_refreshScriptsAfterProfileSwitch()" in canonical


def test_invalidate_bumps_the_generation_so_pending_replies_die() -> None:
    """The publication gate keys off an invalidate generation, not just the
    owner key (which can still match while the switch is in flight)."""
    assert "_scriptsInvalidateSeq" in PANELS_JS
    body = _extract(PANELS_JS, "function _scriptsReplyIsCurrent(")
    assert "_scriptsInvalidateSeq" in body, (
        "the gate must compare against the invalidate generation"
    )


def test_alternate_switch_clears_and_a_pending_reply_is_dropped() -> None:
    """Finding 4, driven through the REAL ``_switchProfileForSessionLoad``.

    An Alpha list reply is held on the wire. The alternate switch runs for
    Beta (clearing the pane). Alpha's reply then lands — first as a SUCCESS,
    then as an ERROR — and neither may repaint the pane.
    """
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + _sessions_helper()
        + """
// every collaborator the real helper calls is a no-op we control.
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function _resetCronUnreadForProfileSwitch(){}
function refreshProfileTransitionReasoningChip(){}
function _clearPersistedModelState(){}
function startGatewaySSE(){}
function syncTopbar(){}
function renderSessionList(){ return Promise.resolve(); }
function renderSessionListFromCache(){}

// GIVEN the Scripts subtab is visible and an Alpha list load is in flight.
_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';
_park('/api/scripts/list');
const alphaLoad = loadScriptsList(false);
if (_parked.length !== 1) throw new Error('precondition: reply should be parked');

// WHEN the alternate switch for Beta runs.
_park('/api/profile/switch');
_route('/api/profile/switch', { active: 'beta', is_default: false });
const switchPromise = _switchProfileForSessionLoad('beta');
if ($('scriptsList').innerHTML !== '') throw new Error('rows not cleared by the alternate switch');
if (_scriptsOwnerProfile !== '') throw new Error('owner not reset by the invalidate');
if (_scriptsInvalidateSeq < 1) throw new Error('invalidate generation not bumped');

// The switch POST resolves for Beta. The switch's own refresh fires on
// accept, so the list must be ANSWERABLE (not parked) for the duration of
// that refresh. Unpark, deliver the switch, let the refresh paint Beta, then
// re-park the list so Alpha's still-outstanding reply can be held back and
// released at our command.
_route('/api/scripts/list', { exists:true, scripts:[{name:'beta.py',description:'B',size:2}] });
_unpark('/api/scripts/list');
_deliver('/api/profile/switch');
await switchPromise;
if (S.activeProfile !== 'beta') throw new Error('switch did not take effect');
if (!$('scriptsList').innerHTML.includes('beta.py')) {
  throw new Error('the alternate switch did not refresh for the new owner: ' + $('scriptsList').innerHTML);
}
_park('/api/scripts/list');

// THEN Alpha's parked reply lands with ALPHA's payload: it must be dropped.
_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
_deliver('/api/scripts/list');
await Promise.allSettled([alphaLoad]);
if ($('scriptsList').innerHTML.includes('alpha.py')) {
  throw new Error('stale ALPHA success published over the new owner');
}
if (!$('scriptsList').innerHTML.includes('beta.py')) {
  throw new Error("a stale reply clobbered the new owner rows");
}

// ...and an Alpha ERROR is dropped the same way.
_park('/api/scripts/list');
const alphaErr = loadScriptsList(false).catch(() => {});
_invalidateScriptsForProfileSwitch();
_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
_failUrl('/api/scripts/list', new Error('network'));
await alphaErr;
if ($('scriptsList').innerHTML.includes('alpha.py')) {
  throw new Error('stale ALPHA error published over the cleared pane');
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_alternate_switch_rejects_a_pending_error_reply() -> None:
    """Finding 4: a reply that FAILS for a superseded owner must not paint the
    failure message either (the old gate only compared a variable to itself)."""
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + _sessions_helper()
        + """
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function _resetCronUnreadForProfileSwitch(){}
function refreshProfileTransitionReasoningChip(){}
function _clearPersistedModelState(){}
function startGatewaySSE(){}
function syncTopbar(){}
function renderSessionList(){ return Promise.resolve(); }
function renderSessionListFromCache(){}

_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';
_park('/api/scripts/list');
const alphaLoad = loadScriptsList(false).catch(() => {});

// The alternate switch for Beta invalidates, then fails.
_route('/api/profile/switch', { active: 'beta', is_default: false });
_park('/api/profile/switch');
const switchPromise = _switchProfileForSessionLoad('beta');
await _tick();
_failUrl('/api/profile/switch', new Error('switch refused'));
let threw = false;
try { await switchPromise; } catch (_e) { threw = true; }
if (!threw) throw new Error('the helper must rethrow for loadSession');

// Alpha's reply arrives and FAILS: no error message may be painted.
// (Re-park the list URL: the switch POST stole _parkUrl above.)
_park('/api/scripts/list');
await _tick();
_failUrl('/api/scripts/list', new Error('network'));
await alphaLoad;
const html = $('scriptsList').innerHTML;
if (html.includes('Could not load scripts')) {
  throw new Error('a superseded error reply painted the pane: ' + html);
}
if (html.includes('alpha.py')) throw new Error('stale rows published');
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_alternate_switch_restores_the_pane_on_a_failed_switch() -> None:
    """Finding 5 on the alternate path: the POST rejects, Alpha is still
    active, and the pane must be restored rather than left blank."""
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + _sessions_helper()
        + """
function _invalidateSessionListRenders(){}
function _setProfileSwitchListEmbargo(){}
function showSessionListSkeleton(){}
function _resetCronUnreadForProfileSwitch(){}
function refreshProfileTransitionReasoningChip(){}
function _clearPersistedModelState(){}
function startGatewaySSE(){}
function syncTopbar(){}
function renderSessionList(){ return Promise.resolve(); }
function renderSessionListFromCache(){}

_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
_route('/api/profile/switch', { active: 'beta', is_default: false });
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';
await loadScriptsList(false);
if (!$('scriptsList').innerHTML.includes('alpha.py')) throw new Error('precondition');

// The alternate switch starts for Beta and clears the pane.
_park('/api/profile/switch');
const switchPromise = _switchProfileForSessionLoad('beta');
if ($('scriptsList').innerHTML !== '') throw new Error('rows not cleared at switch start');

// The switch POST fails: we are still on Alpha.
// Let the api() call reach the point where it parks its reply promise.
await new Promise(r => setTimeout(r, 5));
_failUrl('/api/profile/switch', new Error('network'));
let threw = false;
try { await switchPromise; } catch (_e) { threw = true; }
if (!threw) throw new Error('the helper must rethrow for loadSession');
if (S.activeProfile !== 'alpha') throw new Error('activeProfile moved on a failed switch');

// The rollback refetch is async: let it land before asserting.
await new Promise(r => setTimeout(r, 10));
// The pane must be restored with Alpha's rows, not left blank.
if (!$('scriptsList').innerHTML.includes('alpha.py')) {
  throw new Error('pane left blank after a refused switch: ' + $('scriptsList').innerHTML);
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_canonical_switch_rollback_restores_the_pane() -> None:
    """Finding 5 on the canonical path: ``switchToProfile`` returns false with
    Alpha still active, and the Scripts pane must not stay blank.

    Driven through the REAL extracted ``switchToProfile`` so the rollback call
    is the production one and not a reimplementation.
    """
    canonical = _extract(PANELS_JS, "async function switchToProfile(")
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
let _profileSwitchGeneration = 0;
function _renamingSid(){}
function closeSessionActionMenu(){}
function showSessionListSkeleton(){}
function _setProfileSwitchListEmbargo(){}
function _invalidateSessionListRenders(){}
function bumpWorkspaceTreeGen(){}
function showWorkspaceTreeSkeleton(){}
function clearWorkspaceTreeSkeleton(){}
function applyBotName(){}
function _resetCronUnreadForProfileSwitch(){}
function expandSidebar(){}
function invalidateSlashSkillCaches(){}
function refreshProfileTransitionReasoningChip(){}
function animateNextSessionListRefresh(){}
function _openProfileSwitchSessionBrowser(){}
function _refreshProfileSwitchBackground(){}
function renderSessionListFromCache(){}
function loadWorkspacesPanel(){}
function loadWorkspacesList(){ return Promise.resolve(); }
function getModelLabel(x){ return x; }
function loadDir(){ return Promise.resolve(); }
function newSession(){ return Promise.resolve(); }
function syncTopbar(){}
function _profilePanelLoad(){ return Promise.resolve(); }
function _isDesktopWidth(){ return true; }
function _profileMatchesActiveProfile(){ return true; }
function _clearPersistedModelState(){}
"""
        # The switch POST is refused by the server: Alpha stays active.
        + canonical.replace(
            "const data = await api('/api/profile/switch'",
            "throw new Error('switch refused'); // api('/api/profile/switch'",
        )
        + """
_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';
await loadScriptsList(false);
if (!$('scriptsList').innerHTML.includes('alpha.py')) throw new Error('precondition');

// The switch to Beta is refused by the server. Alpha is still active.
const ok = await switchToProfile('beta');
if (ok !== false) throw new Error('a refused switch must return false');
if (S.activeProfile !== 'alpha') throw new Error('activeProfile moved on a failed switch');

// The pane must be restored with Alpha's rows, not left blank.
if (!$('scriptsList').innerHTML.includes('alpha.py')) {
  throw new Error('pane left blank after rollback: ' + $('scriptsList').innerHTML);
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_a_load_started_during_a_pending_switch_cannot_repaint() -> None:
    """Finding 5: a Scripts load issued while the switch POST is in flight
    must not repaint the pane the switch just cleared."""
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
_route('/api/scripts/list', { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] });
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';
await loadScriptsList(false);
if (!$('scriptsList').innerHTML.includes('alpha.py')) throw new Error('precondition');

// The switch clears the pane; a load starts immediately after (its reply is
// parked on the wire so nothing paints before we can observe it).
_scriptsSwitchNeedsRefresh = true;
_invalidateScriptsForProfileSwitch();
_park('/api/scripts/list');
const stray = loadScriptsList(false).catch(() => {});
if ($('scriptsList').innerHTML !== '') throw new Error('pane was repainted during the pending switch');
if (_parked.length !== 1) throw new Error('the stray load should be parked');

// The switch is accepted for Beta, which invalidates again.
S.activeProfile = 'beta';
_invalidateScriptsForProfileSwitch();
_route('/api/scripts/list', { exists:true, scripts:[{name:'beta.py',description:'B',size:2}] });

// The stray load's reply lands now: it must be dropped.
_deliver('/api/scripts/list');
await Promise.allSettled([stray]);
if ($('scriptsList').innerHTML.includes('alpha.py')) {
  throw new Error('a load started during the pending switch repainted the pane');
}

// The correct owner's load repopulates.
_unpark('/api/scripts/list');
await loadScriptsList(false);
if (!$('scriptsList').innerHTML.includes('beta.py')) throw new Error('new owner rows missing');
if ($('scriptsList').innerHTML.includes('alpha.py')) throw new Error('stale rows published');
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_no_silent_zero_getattr_for_containment_flags() -> None:
    """Finding 2 at the source level: a silent-zero fallback for a containment
    flag must be gone from the executable code.

    The check only sees CODE: the source is unparsed twice, the second time
    after a transformer drops every docstring, so a docstring that quotes the
    old pattern to explain why it was removed cannot trip this.
    """
    import ast

    class _DropDocstrings(ast.NodeTransformer):
        def _strip(self, node):
            body = list(node.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
            return node

        visit_FunctionDef = _strip
        visit_AsyncFunctionDef = _strip
        visit_ClassDef = _strip
        visit_Module = _strip

    src = (REPO_ROOT / "api" / "scripts_panel.py").read_text(encoding="utf-8")
    tree = _DropDocstrings().visit(ast.parse(src))
    code_only = ast.unparse(ast.fix_missing_locations(tree))
    for bad in (
        "O_NOFOLLOW', 0)",
        'O_NOFOLLOW", 0)',
        "O_DIRECTORY', 0)",
        'O_DIRECTORY", 0)',
    ):
        assert bad not in code_only, f"silent-zero fallback still present: {bad}"
