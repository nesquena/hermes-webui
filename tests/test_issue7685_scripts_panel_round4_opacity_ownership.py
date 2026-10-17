"""#7685 review round 4: the newest request owns the pane's dim.

Driven against the REAL ``loadScriptsList`` with the same park/deliver
harness as round 2, per the reviewer's request:

> Keep feedback owned by the newest request: set its initial opacity
> unconditionally as above, and let stale replies return without clearing a
> newer request's dim. Please add a regression for the ordering above, plus a
> late stale reply arriving while a newer animated refresh is still pending.

1. **[MUST-FIX] a superseded request's late reply cleared the newer
   request's dim.** The dispatch line set the dim only on the ``animate``
   arm (``if(animate&&box) box.style.opacity='0.5'``), and both stale-return
   arms cleared opacity unconditionally. So a silent (non-animated) refresh
   — the accepted-profile refresh ``loadScriptsList(false,false)`` — never
   dimmed, yet a late reply from an OLDER request cleared whatever dim the
   newest request had set. The fix sets the initial opacity unconditionally
   (``box.style.opacity = animate ? '0.5' : ''``) and lets stale replies
   return without touching opacity at all.

2. **A held request left the pane stuck at half opacity.** With the old
   code the dim could only be cleared by a reply that was never going to
   arrive (the obsolete request held on the wire forever). The newest
   request's own initial set is now authoritative.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = __import__("pytest").mark.skipif(NODE is None, reason="node not on PATH")


# ── extraction ──────────────────────────────────────────────────────────


def _extract(source: str, start_marker: str) -> str:
    start = source.find(start_marker)
    assert start >= 0, f"marker not found: {start_marker!r}"
    i = source.find("{", start)
    assert i >= 0
    depth = 0
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces from {start_marker!r}")


def _run(source: str) -> str:
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(source)
        path = Path(fh.name)
    try:
        proc = subprocess.run(
            ["node", str(path)], capture_output=True, text=True, timeout=60
        )
    finally:
        path.unlink(missing_ok=True)
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    return proc.stdout.strip()


# ── harness (same shape as round 2) ─────────────────────────────────────

_DOM_AND_HELPERS = """
function _mkEl(id){
  const cls = new Set();
  return { id, innerHTML:'', style:{}, hidden:false, textContent:'',
    dataset:{}, value:'',
    classList:{ add:(c)=>cls.add(c), remove:(c)=>cls.delete(c),
      toggle:(c)=>{ cls.has(c)?cls.delete(c):cls.add(c); },
      contains:(c)=>cls.has(c) },
    addEventListener(){}, removeEventListener(){}, setAttribute(){},
    getAttribute(){ return null; }, remove(){}, appendChild(){},
    querySelector(){ return null; }, querySelectorAll(){ return []; } };
}
function $(id){ if(!_els[id]) _els[id]=_mkEl(id); return _els[id]; }
function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;'); }
const _els = {};
const document = { querySelector(){ return null; }, querySelectorAll(){ return []; },
  createElement(){ return _mkEl('new'); }, addEventListener(){} };
const window = {};
const localStorage = { removeItem(){}, getItem(){ return null; }, setItem(){} };
function t(k){ return k; }

const _routes = {};
const _parked = [];
const _parkUrls = new Set();
function _route(url, data){ _routes[url] = data; }
function _park(url){ _parkUrls.add(url); }
function _unpark(url){ _parkUrls.delete(url); }
function _deliver(url){
  // FIFO: release exactly ONE parked promise for this url (the oldest).
  for(let i=0; i<_parked.length; i++){
    if(_parked[i].url!==url) continue;
    const [p]=_parked.splice(i,1);
    p.resolve(_routes[p.url]);
    return;
  }
}
function _failUrl(url, err){
  for(let i=0; i<_parked.length; i++){
    if(_parked[i].url!==url) continue;
    const [p]=_parked.splice(i,1);
    p.reject(err);
    return;
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

const S = { activeProfile: 'alpha' };
let _currentTasksSubtab = 'jobs';
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
    """The real Scripts helpers, including the one under test."""
    return "\n".join(
        _extract(PANELS_JS, marker)
        for marker in (
            "function _scriptsOwnerKey(",
            "function _invalidateScriptsForProfileSwitch(",
            "function _refreshScriptsAfterProfileSwitch(",
            "function clearScriptsList(",
            "async function loadScriptsList(",
            "function _scriptsReplyIsCurrent(",
            "function _renderScriptItem(",
            "function _formatScriptSize(",
        )
    )


# ── the reviewer's ordering scenario ────────────────────────────────────


def test_a_newer_non_animated_refresh_keeps_its_undimmed_pane_when_an_older_held_reply_lands() -> None:
    """The exact ordering the reviewer replayed.

    1. An animated list request starts and is HELD (parked).
    2. The non-animated accepted-profile refresh ``loadScriptsList(false,false)``
       starts and resolves first.
    3. The obsolete (older) request's reply finally arrives.

    The rows are the newer request's, and the pane must be at FULL opacity —
    the newest request never dimmed, and the obsolete reply must not dim it
    either. (Before the fix the obsolete reply's stale arm cleared opacity
    only when it ran; combined with the animate-only dim this is the shape
    that left the pane's ownership ambiguous.)
    """
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';

// 1. The animated request is held on the wire.
_park('/api/scripts/list');
const held = loadScriptsList(true);
if (_parked.length !== 1) throw new Error('precondition: first reply should be parked');
if ($('scriptsList').style.opacity !== '0.5') {
  throw new Error('the animated request did not dim the pane: ' + $('scriptsList').style.opacity);
}

// 2. The newer NON-animated refresh starts and resolves first.
_unpark('/api/scripts/list');
_route('/api/scripts/list', { exists:true, scripts:[{name:'beta.py',description:'B',size:2}] });
const fresh = loadScriptsList(false, false);
await fresh;
if (!$('scriptsList').innerHTML.includes('beta.py')) {
  throw new Error('the newer refresh did not publish: ' + $('scriptsList').innerHTML);
}
if ($('scriptsList').style.opacity !== '') {
  throw new Error('the newest (non-animated) request left the pane dimmed: ' + $('scriptsList').style.opacity);
}

// 3. The obsolete request's reply finally lands with the OLD payload.
_park('/api/scripts/list');
_deliver('/api/scripts/list');
await held;
if ($('scriptsList').innerHTML.includes('alpha.py')) {
  throw new Error('the obsolete reply published its rows');
}
if (!$('scriptsList').innerHTML.includes('beta.py')) {
  throw new Error('the obsolete reply clobbered the newest rows');
}
// The newest request owns the dim: full opacity, whatever the stale reply does.
if ($('scriptsList').style.opacity !== '') {
  throw new Error('the stale reply touched the newest request opacity: ' + $('scriptsList').style.opacity);
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_a_late_stale_reply_does_not_dim_a_pending_animated_refresh() -> None:
    """The second ordering the reviewer asked for.

    A newer ANIMATED refresh is still pending (its dim must survive), and a
    late stale reply from an older request arrives. The pane must stay at the
    newer request's ``0.5`` — not cleared by the stale reply, and not left
    stuck once the newer request resolves.

    Both requests animate, so the pane's dim is continuously owned: the older
    one dims it, the newer one re-dims it, and the stale reply from the older
    one must not clear the newer request's value.
    """
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';

// An older ANIMATED request is held on the wire (it owns the dim).
_park('/api/scripts/list');
const older = loadScriptsList(true);
if (_parked.length !== 1) throw new Error('precondition: first reply should be parked');
if ($('scriptsList').style.opacity !== '0.5') {
  throw new Error('precondition: the older animated request should have dimmed the pane');
}

// A newer ANIMATED refresh starts and is ALSO held — its dim must survive.
_park('/api/scripts/list');
const newer = loadScriptsList(true);
if (_parked.length !== 2) throw new Error('precondition: both replies should be parked');
if ($('scriptsList').style.opacity !== '0.5') {
  throw new Error('the newer animated request did not dim the pane: ' + $('scriptsList').style.opacity);
}

// The late stale reply arrives while the newer refresh is still pending.
_deliver('/api/scripts/list');
await older;
if ($('scriptsList').style.opacity !== '0.5') {
  throw new Error('the stale reply cleared the newer request dim: ' + $('scriptsList').style.opacity);
}

// Now the newer animated refresh resolves: the dim must clear, not stick.
_route('/api/scripts/list', { exists:true, scripts:[{name:'gamma.py',description:'G',size:3}] });
_deliver('/api/scripts/list');
await newer;
if (!$('scriptsList').innerHTML.includes('gamma.py')) {
  throw new Error('the newer animated refresh did not publish: ' + $('scriptsList').innerHTML);
}
if ($('scriptsList').style.opacity !== '') {
  throw new Error('the dim stuck after the newest reply: ' + $('scriptsList').style.opacity);
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_a_stale_error_reply_does_not_clear_the_newest_dim() -> None:
    """The error arm of the same ownership rule.

    A held request whose reply FAILS after a newer request took ownership
    must not clear the newer request's dim either. Both requests animate so
    the dim stays continuously owned until the newest one resolves.
    """
    source = (
        _DOM_AND_HELPERS
        + _DECLS
        + _scripts_helpers()
        + """
_currentTasksSubtab = 'scripts';
S.activeProfile = 'alpha';

// Older ANIMATED request held, then a newer animated request held on top.
_park('/api/scripts/list');
const older = loadScriptsList(true);
if ($('scriptsList').style.opacity !== '0.5') {
  throw new Error('precondition: older request should be dimmed');
}
_park('/api/scripts/list');
const newer = loadScriptsList(true);
if ($('scriptsList').style.opacity !== '0.5') {
  throw new Error('precondition: newer request should be dimmed');
}

// The OLDER request's reply fails. The newer request's dim must survive.
_failUrl('/api/scripts/list', new Error('boom'));
await older;
if ($('scriptsList').style.opacity !== '0.5') {
  throw new Error('a stale error cleared the newest request dim: ' + $('scriptsList').style.opacity);
}
if ($('scriptsList').innerHTML.includes('Could not load')) {
  throw new Error('a stale error painted an error state over the newest request');
}
console.log('OK');
"""
    )
    assert _run(source).endswith("OK"), _run(source)


def test_the_initial_dim_is_set_unconditionally_in_the_source() -> None:
    """Source contract: the dispatch line owns the dim for BOTH arms.

    The animate-only form (``if(animate&&box) box.style.opacity='0.5';``) is
    the regression: a non-animated request never claimed the dim, so a stale
    reply's clear was acting on a value it did not own. The fix sets it
    unconditionally with a ternary.
    """
    load = _extract(PANELS_JS, "async function loadScriptsList(")
    assert "box.style.opacity=animate?'0.5':''" in load, (
        "the dispatch line must set the initial opacity unconditionally as "
        "'box.style.opacity=animate?\\'0.5\\':\\'\\'' so the newest request "
        "always owns the dim, whichever arm it took"
    )
    assert "if(animate&&box) box.style.opacity='0.5';" not in load, (
        "the animate-only dim is still present"
    )
    # Both stale-return arms must NOT touch opacity any more.
    stale_returns = [
        line
        for line in load.splitlines()
        if "_scriptsReplyIsCurrent(owner,gen,seq)" in line and "return" in line
    ]
    assert stale_returns, "the stale guards were not found"
    for line in stale_returns:
        assert "opacity" not in line, (
            f"a stale-return arm still touches opacity: {line.strip()!r}"
        )
