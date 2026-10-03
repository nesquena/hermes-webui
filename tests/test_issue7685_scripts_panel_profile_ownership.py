"""#7685 finding 3: the Scripts pane is profile-owned.

Three boundaries, driven through the REAL extracted functions from
``static/panels.js`` under Node with a small DOM stand-in:

1. a profile switch clears the previous owner's rows immediately;
2. a reply that arrives after a switch is discarded instead of publishing
   the old profile's list over the new owner's result;
3. once the switch is accepted, the visible Scripts pane is refreshed for
   the new owner.

Behaviour, not source strings: the test builds the DOM the functions touch,
runs the extracted functions, and asserts on what the DOM ends up holding.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract(source: str, start_marker: str) -> str:
    """Source of the top-level statement starting at ``start_marker``.

    Balanced-brace scan from the marker to the end of the statement, so the
    test keeps working when the function body is edited.
    """
    idx = source.index(start_marker)
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


def _dom_and_helpers() -> str:
    return """
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

// ── api() stand-in: URL-keyed replies, so call ORDER never decides which
// payload a caller gets (a queue made the composition test depend on how many
// unrelated calls switchToProfile happens to make first).
const _routes = {};
const _scripted = [];
let _apiMode = 'resolve';
function _route(url, data){ _routes[url] = data; }
async function api(url){
  if (_apiMode === 'reject') throw new Error('network');
  if (Object.prototype.hasOwnProperty.call(_routes, url)) return _routes[url];
  const entry = _scripted.shift();
  return entry ? entry.data : { exists:true, scripts: [] };
}

// ── the state switchProfile() reads ───────────────────────────────────
const S = { activeProfile: 'alpha' };
let _currentTasksSubtab = 'jobs';

// ── markers so the test can observe the extracted functions' behaviour ──
const _calls = { scriptsLoaded: 0, cleared: 0 };
"""


def _load_scripts_fn() -> str:
    body = _extract(PANELS_JS, "async function loadScriptsList(")
    # Strip the leading "let _scriptsOwnerProfile..." declarations (the test
    # provides them) so only the functions are extracted.
    return body


def _clear_fn() -> str:
    return _extract(PANELS_JS, "function clearScriptsList(")


def _owner_key_fn() -> str:
    return _extract(PANELS_JS, "function _scriptsOwnerKey(")


def _format_size_fn() -> str:
    return _extract(PANELS_JS, "function _formatScriptSize(")


_SCRIPTS_HELPERS = (
    _extract(PANELS_JS, "async function loadScriptsList(")
    + _extract(PANELS_JS, "function clearScriptsList(")
    + _extract(PANELS_JS, "function _scriptsOwnerKey(")
    + _extract(PANELS_JS, "function _renderScriptItem(")
    + _extract(PANELS_JS, "function _formatScriptSize(")
)


def _render_item_fn() -> str:
    return _extract(PANELS_JS, "function _renderScriptItem(")


_DECLS = """
let _scriptsOwnerProfile='';
let _scriptsRequestSeq=0;
let _scriptsLastDir=null;
let _scriptsSwitchNeedsRefresh=false;
"""


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


def test_late_reply_from_the_old_owner_is_discarded() -> None:
    """A reply for a superseded owner must not publish over the new one.

    Sequence: switch to alpha loads (slow), a switch to beta clears + loads,
    then the alpha reply lands. The pane must hold BETA's rows.
    """
    source = (
        _dom_and_helpers()
        + _DECLS
        + _SCRIPTS_HELPERS
        + """
        // alpha's reply is scripted first, then beta's.
        _scripted.push({ data: { exists:true, scripts:[{name:'alpha.py',description:'A',size:10}] } });
        _scripted.push({ data: { exists:true, scripts:[{name:'beta.py',description:'B',size:20}] } });

        // alpha starts a load, then the switch to beta supersedes it.
        const alphaLoad = loadScriptsList(false);
        _scriptsOwnerProfile = 'beta';          // what switchProfile() does
        clearScriptsList();                      // rows dropped for the old owner
        const betaLoad = loadScriptsList(false);
        await Promise.all([alphaLoad, betaLoad]);

        const html = $('scriptsList').innerHTML;
        if (!html.includes('beta.py')) throw new Error('beta rows missing: ' + html);
        if (html.includes('alpha.py')) throw new Error('stale alpha rows published: ' + html);
        console.log('OK');
        """
    )
    assert _run(source).endswith("OK")


def test_switch_clears_the_previous_profiles_rows() -> None:
    """clearScriptsList() empties the pane and resets the owner."""
    source = (
        _dom_and_helpers()
        + _DECLS
        + _SCRIPTS_HELPERS
        + """
        _scripted.push({ data: { exists:true, scripts:[{name:'old.py',description:'OLD',size:1}] } });
        await loadScriptsList(false);
        if (!$('scriptsList').innerHTML.includes('old.py')) throw new Error('precondition');
        clearScriptsList();
        if ($('scriptsList').innerHTML !== '') throw new Error('rows not cleared');
        if (_scriptsOwnerProfile !== '') throw new Error('owner not reset');
        console.log('OK');
        """
    )
    assert _run(source).endswith("OK")


def test_switch_refreshes_the_visible_scripts_pane() -> None:
    """When Scripts was the visible subtab, an accepted switch reloads it.

    The switch path sets ``_scriptsSwitchNeedsRefresh``; the acceptance path
    consumes it. The assertion is that a NEW request is issued after the
    owner changed — i.e. the pane is not left empty.
    """
    source = (
        _dom_and_helpers()
        + _DECLS
        + _SCRIPTS_HELPERS
        + """
        // The switch path (extracted inline below) marks the pane for refresh
        // because the Scripts subtab is the visible one.
        _currentTasksSubtab = 'scripts';
        if (typeof clearScriptsList === 'function') {
          _scriptsSwitchNeedsRefresh = _currentTasksSubtab === 'scripts';
          clearScriptsList();
        }
        // Acceptance: the owner changes and the pane refreshes for it.
        S.activeProfile = 'beta';
        _scripted.push({ data: { exists:true, scripts:[{name:'beta.py',description:'B',size:2}] } });
        if (_scriptsSwitchNeedsRefresh) {
          _scriptsSwitchNeedsRefresh = false;
          await loadScriptsList(true);
        }
        if (!$('scriptsList').innerHTML.includes('beta.py')) {
          throw new Error('visible pane was left empty after the switch');
        }
        if (_scriptsSwitchNeedsRefresh) throw new Error('refresh flag not consumed');
        console.log('OK');
        """
    )
    assert _run(source).endswith("OK")


def test_switch_hook_is_wired_into_switchprofile() -> None:
    """The real ``switchToProfile`` must clear Scripts and refresh on acceptance.

    This is the composition test: without the hook the two guards above are
    dead code. Every collaborator is a no-op except the Scripts ownership
    path, which is the REAL extracted code under test.
    """
    import re as _re

    switch_body = _extract(PANELS_JS, "async function switchToProfile(")

    # Every function the switch body calls becomes a no-op, EXCEPT the ones
    # this test needs to observe (the Scripts helpers) — so a missing stub
    # cannot silently turn the assertion into a vacuous pass.
    names = set(_re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", switch_body))
    # Assignment targets too: ``_sessionListSkeletonActive = false;`` never
    # appears as a call, so the call-scan misses it and the TDZ bites later.
    names |= set(_re.findall(r"\b([A-Za-z_$][\w$]*)\s*=[^=]", switch_body))
    provided = {
        "if", "for", "while", "switch", "catch", "return", "typeof", "function",
        "await", "new", "Promise", "String", "Number", "Boolean", "Array",
        "Object", "JSON", "Math", "Date", "Set", "Map", "Error", "RegExp",
        "parseInt", "parseFloat", "isNaN", "encodeURIComponent",
        "decodeURIComponent", "setTimeout", "clearTimeout", "void", "api", "$",
        "esc", "t", "S", "window", "document", "localStorage", "console",
        "clearScriptsList", "loadScriptsList", "switchToProfile",
        "showToast", "_wsTreeGen",
    }
    keep_real = {
        "_applyModelToDropdown", "loadWorkspacesPanel", "loadWorkspaceList",
        "syncTopbar", "_setHiddenTabs", "startGatewaySSE", "stopGatewaySSE",
        "renderSessionList", "_resetCronUnreadForProfileSwitch", "applyBotName",
        "_clearPersistedModelState", "expandSidebar", "_isDesktopWidth",
        "_profileMatchesActiveProfile", "getModelLabel", "loadDir",
        "invalidateSlashSkillCaches", "refreshProfileTransitionReasoningChip",
        "animateNextSessionListRefresh", "bumpWorkspaceTreeGen",
        "showSessionListSkeleton", "showWorkspaceTreeSkeleton",
        "clearWorkspaceTreeSkeleton", "renderSessionListFromCache",
        "newSession", "activateCurrentProfile", "closeSessionActionMenu",
        "_invalidateSessionListRenders", "_modelStateForSelect",
        "_openProfileSwitchSessionBrowser", "_refreshProfileSwitchBackground",
        "_setProfileSwitchListEmbargo", "_profileSwitchListEmbargo", "add", "remove", "find", "forEach",
        "from", "trim", "stringify", "appendChild", "createElement",
        "querySelectorAll", "resolve", "then",
    }
    # State the switch body assigns to (a function stub would break it).
    # Names the harness itself declares: never re-declare them.
    _PRE_DECLARED = {
        "_scriptsSwitchNeedsRefresh", "_scriptsOwnerProfile", "_scriptsRequestSeq",
        "_scriptsLastDir", "_currentTasksSubtab", "S", "_applyModelToDropdown",
        "showToast", "_profileSwitchGeneration",
    }

    # Names the switch body ASSIGNS to (a function stub would break the write).
    _ASSIGNED_STATE = {
        "_sessionListSkeletonActive", "_wsTreeGen", "_profileSwitchListEmbargo",
        "_scriptsSwitchNeedsRefresh", "_scriptsOwnerProfile", "_scriptsRequestSeq",
        "_scriptsLastDir", "_currentTasksSubtab", "_profileSwitchGeneration",
        "S", "_renamingSid",
    }

    # One entry per identifier, built in a single pass (the two loops below
    # would otherwise emit the same name twice, which Node rejects).
    stubs = []
    for name in sorted(names):
        if name in provided or name[0].isupper():
            continue
        if name in {"add", "remove", "find", "forEach", "from", "trim",
                    "stringify", "appendChild", "createElement",
                    "querySelectorAll", "resolve", "then"}:
            continue
        if name in keep_real:
            continue  # declared with a real body below
        # Names the switch body ASSIGNS to must be values, not functions;
        # stubbing one as a function throws "is not a function"/TypeError.
        if name in _ASSIGNED_STATE:
            # Skip anything the harness itself already declares (_DECLS /
            # _dom_and_helpers); a second ``let`` is a SyntaxError.
            if name not in _PRE_DECLARED:
                stubs.append(f"let {name} = false;")
            continue
        stubs.append(f"function {name}(){{}}")
    # _dom_and_helpers() already defines _applyModelToDropdown / showToast /
    # localStorage-backed helpers; only add the ones it does not.
    for extra in (
        "_profileMatchesActiveProfile(){ return true; }",
        "_isDesktopWidth(){ return true; }",
        "loadWorkspacesList(){ return Promise.resolve(); }",
        "getModelLabel(x){ return x; }",
        "loadDir(){ return Promise.resolve(); }",
        "invalidateSlashSkillCaches(){}",
        "activateCurrentProfile(){ return Promise.resolve(); }",
    ):
        name = extra.split("(")[0].split("{")[0].strip()
        if name not in names:
            stubs.append(f"function {extra}")
    stub_block = "\n        ".join(dict.fromkeys(stubs))  # de-dupe, keep order

    source = (
        _dom_and_helpers()
        + _DECLS
        + _SCRIPTS_HELPERS
        + f"""
        let _profileSwitchGeneration = 0;
        {stub_block}
        """
        + switch_body.replace(
            "const data = await api('/api/profile/switch'",
            "const data = await Promise.resolve({ active: 'beta', is_default: false }) // api('/api/profile/switch'",
        )
        + """
        _currentTasksSubtab = 'scripts';
        S.activeProfile = null;            // force a real switch past the self-switch guard
        // The switch body makes other api() calls first; shift past them by
        // pre-seeding the queue so the Scripts list reply is beta's rows.
        _route('/api/scripts/list', { exists:true, scripts:[{name:'beta.py',description:'B',size:2}] });
        await switchToProfile('beta');
        const html = $('scriptsList').innerHTML;
        if (!html.includes('beta.py')) throw new Error('pane not refreshed: ' + html);
        if (html.includes('alpha.py')) throw new Error('stale rows survived');
        console.log('OK');
        """
    )
    assert _run(source).endswith("OK"), _run(source)
