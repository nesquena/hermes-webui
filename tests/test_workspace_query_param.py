"""Tests for the ?workspace= boot query param (one-shot workspace routing).

Mirrors tests/test_5682_profile_query_switch.py: the JS functions are
extracted from the static sources and executed in node, so the tests fail
if the functions disappear or change contract — no browser needed.

The boot-block tests execute the actual profile-switch and workspace routing
blocks extracted verbatim from static/boot.js against stubbed collaborators
(S, newSession, switchToProfile), giving deterministic behavioral coverage of:
- an encoded Windows absolute path reaching the session-create request;
- a server-acceptable path whose basename contains "..";
- length boundaries (1023 / 1024 / above): every nonblank value reaches
  newSession() — server rejection is the authority, there is no client cap;
- compound ?profile=&workspace= launches where switchToProfile() actually
  returns false and where it actually throws: newSession() must not be called
  and the workspace parameter must not be consumed;
- the successful compound case: creation occurs under the switched profile.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"
BOOT_JS_PATH = REPO_ROOT / "static" / "boot.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
BOOT_JS = BOOT_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

PROFILE_BLOCK_START = "let _profileSwitchCompleted=false;"
PROFILE_BLOCK_END = "if(typeof fetchReasoningChip"
WS_BLOCK_START = "// ?workspace=<path> (one-shot, symmetric to ?profile=)"
WS_BLOCK_END = "const _profileQueryBlocksSavedLocal"


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _node_prelude() -> str:
    return f"""
const sessionsSrc = {SESSIONS_JS!r};
function extractFunc(src, name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
function evalSession(name) {{
  globalThis[name] = (0, eval)('(' + extractFunc(sessionsSrc, name) + ')');
}}
function applyUrl(rel) {{
  const next = new URL(rel, 'https://example.test');
  window.location.href = next.href;
  window.location.pathname = next.pathname;
  window.location.search = next.search;
  window.location.hash = next.hash;
}}
global.window = {{
  location: {{}},
  history: {{
    state: {{ from: 'test' }},
    calls: [],
    replaceState(state, title, url) {{
      this.calls.push({{ state, title, url }});
      this.state = state;
      applyUrl(url);
    }}
  }}
}};
global.localStorage = {{
  _s: {{}},
  getItem(k) {{ return Object.prototype.hasOwnProperty.call(this._s, k) ? this._s[k] : null; }},
  setItem(k, v) {{ this._s[k] = String(v); }},
  removeItem(k) {{ delete this._s[k]; }}
}};
evalSession('_workspaceQueryIntentFromLocation');
evalSession('_consumeWorkspaceQueryParamFromLocation');
evalSession('_consumeProfileQueryParamFromLocation');
"""


def _node_boot_runner() -> str:
    """Run the real profile-switch block, then the real workspace routing
    block, both extracted verbatim from static/boot.js, in a shared scope.

    switchToProfile is stubbed per scenario (true / false / throw), so the
    completion flag is computed by the shipped code, not preset by the test.
    """
    return _node_prelude() + f"""
const bootSrc = {BOOT_JS!r};
function slice(start, end) {{
  const s = bootSrc.indexOf(start);
  const e = bootSrc.indexOf(end);
  if (s < 0 || e < 0 || e <= s) throw new Error('boot block not found: ' + start.slice(0, 40));
  return bootSrc.slice(s, e);
}}
const profileBlock = slice({PROFILE_BLOCK_START!r}, {PROFILE_BLOCK_END!r});
const wsBlock = slice({WS_BLOCK_START!r}, {WS_BLOCK_END!r});
async function runBootBlocks(ctx) {{
  const S = ctx.S;
  const profileIntent = ctx.profileIntent;
  const prefillIntent = null;
  const calls = ctx.calls;
  const _profileSwitchProfileBefore = S.activeProfile || 'default';
  const _profileSwitchIsDefaultBefore = !!S.activeProfileIsDefault;
  async function switchToProfile(name) {{
    ctx.switchCalls.push(name);
    if (ctx.switchOutcome === 'throws') throw new Error('switch failed');
    if (ctx.switchOutcome === 'returns-false') return false;
    S.activeProfile = name;
    return true;
  }}
  async function newSession(fresh, opts) {{
    calls.push({{
      fresh, opts,
      workspaceAtCall: S._profileSwitchWorkspace,
      profileAtCall: S.activeProfile
    }});
    if (ctx.newSessionThrows) throw new Error('session-create rejected');
    S.session = {{ session_id: 'test' }};
  }}
  const syncTopbar = () => {{}};
  const syncWorkspacePanelState = () => {{}};
  const renderSessionList = async () => {{}};
  const _finalizeComposerPrefillOnBoot = async () => {{}};
  const _startBootModelDropdown = () => {{}};
  return await eval('(async () => {{' + profileBlock + ';' + wsBlock + '; return "fell-through";}})()');
}}
"""


# ---------------------------------------------------------------------------
# Intent parsing — after trimming, any nonempty value is a routing candidate
# ---------------------------------------------------------------------------

def test_valid_absolute_unix_path_intent():
    out = _run_node(_node_prelude() + """
applyUrl('/?workspace=/Users/someone/Projects/demo&q=hello');
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    intent = json.loads(out)
    assert intent == {
        "hasParam": True,
        "valid": True,
        "path": "/Users/someone/Projects/demo",
    }


def test_windows_absolute_path_is_a_routing_candidate():
    """C:\\Users\\name\\project is valid on a Windows host; trust decisions
    belong to resolve_trusted_workspace() server-side."""
    out = _run_node(_node_prelude() + r"""
applyUrl('/?workspace=' + encodeURIComponent('C:\\Users\\name\\project'));
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    intent = json.loads(out)
    assert intent["hasParam"] is True
    assert intent["valid"] is True
    assert intent["path"] == "C:\\Users\\name\\project"


def test_dotdot_in_basename_is_a_routing_candidate():
    """A legitimate directory name containing '..' must not be rejected by a
    blanket substring test — server canonicalization decides."""
    out = _run_node(_node_prelude() + """
applyUrl('/?workspace=' + encodeURIComponent('/Users/x/notes..archive'));
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    intent = json.loads(out)
    assert intent["valid"] is True
    assert intent["path"] == "/Users/x/notes..archive"


@pytest.mark.parametrize("length", [1023, 1024, 4096])
def test_long_paths_are_routing_candidates(length):
    """No client-side length cap: a deeply nested but valid host path is
    accepted by resolve_trusted_workspace(), so the browser must not
    classify it as invalid before the server sees it."""
    out = _run_node(_node_prelude() + f"""
const seg = 'a'.repeat(63);
let p = '/base';
while (p.length < {length}) p += '/' + seg;
p = p.slice(0, {length});
applyUrl('/?workspace=' + encodeURIComponent(p));
const intent = _workspaceQueryIntentFromLocation();
console.log(JSON.stringify({{valid: intent.valid, len: intent.path.length}}));
""")
    state = json.loads(out)
    assert state["valid"] is True
    assert state["len"] == length


def test_missing_param_is_empty_intent():
    out = _run_node(_node_prelude() + """
applyUrl('/?q=hello');
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    intent = json.loads(out)
    assert intent["hasParam"] is False


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_values_are_flagged_invalid(blank):
    out = _run_node(_node_prelude() + f"""
applyUrl('/?workspace=' + encodeURIComponent({blank!r}));
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    intent = json.loads(out)
    assert intent["hasParam"] is True
    assert intent["valid"] is False


# ---------------------------------------------------------------------------
# URL cleanup
# ---------------------------------------------------------------------------

def test_consume_removes_only_workspace_param():
    out = _run_node(_node_prelude() + """
applyUrl('/app/?workspace=/Users/x/proj&q=hello&keep=1#frag');
_consumeWorkspaceQueryParamFromLocation();
console.log(JSON.stringify({
  search: window.location.search,
  hash: window.location.hash,
  pathname: window.location.pathname,
  calls: window.history.calls.length
}));
""")
    state = json.loads(out)
    assert "workspace=" not in state["search"]
    assert "q=hello" in state["search"]
    assert "keep=1" in state["search"]
    assert state["hash"] == "#frag"
    assert state["pathname"] == "/app/"
    assert state["calls"] == 1


def test_consume_is_noop_without_param():
    out = _run_node(_node_prelude() + """
applyUrl('/app/?q=hello');
_consumeWorkspaceQueryParamFromLocation();
console.log(JSON.stringify({calls: window.history.calls.length}));
""")
    assert json.loads(out)["calls"] == 0


# ---------------------------------------------------------------------------
# Boot behavior — profile-switch and workspace blocks executed verbatim from
# static/boot.js; switchToProfile is stubbed per scenario
# ---------------------------------------------------------------------------

def _boot_scenario(url: str, *, profile_intent: str, switch_outcome: str,
                   new_session_throws: bool = False,
                   extra_js: str = "") -> str:
    return _node_boot_runner() + f"""
(async () => {{
  {extra_js}
  const ctx = {{
    S: {{ activeProfile: 'default', activeProfileIsDefault: true,
          _profileSwitchWorkspace: null, session: null }},
    profileIntent: {profile_intent},
    switchOutcome: {switch_outcome!r},
    newSessionThrows: {'true' if new_session_throws else 'false'},
    calls: [],
    switchCalls: []
  }};
  const routed = await runBootBlocks(ctx);
  console.log(JSON.stringify({{
    routed: routed === undefined ? 'routed' : routed,
    calls: ctx.calls,
    switchCalls: ctx.switchCalls,
    search: window.location.search,
    cueAfter: ctx.S._profileSwitchWorkspace
  }}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""


def _apply(url: str) -> str:
    return f"applyUrl({url!r});"


def test_windows_path_reaches_session_create():
    """An encoded Windows absolute path must flow through the one-shot cue
    into the session-create request."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        extra_js=_apply("/?workspace=C%3A%5CUsers%5Cname%5Cproject")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert len(state["calls"]) == 1
    assert state["calls"][0]["workspaceAtCall"] == "C:\\Users\\name\\project"
    assert state["calls"][0]["fresh"] is True
    assert state["calls"][0]["opts"] == {"worktree": False}
    assert "workspace=" not in state["search"]


def test_dotdot_basename_reaches_session_create():
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fnotes..archive")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert state["calls"][0]["workspaceAtCall"] == "/Users/x/notes..archive"


@pytest.mark.parametrize("length", [1023, 1024, 2048])
def test_boundary_length_paths_reach_session_create(length):
    """Every nonblank value reaches newSession(); server rejection remains
    the authority (no parser-only cap)."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        extra_js=f"""
  const seg = 'a'.repeat(63);
  let p = '/base';
  while (p.length < {length}) p += '/' + seg;
  p = p.slice(0, {length});
  applyUrl('/?workspace=' + encodeURIComponent(p));
"""))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert len(state["calls"]) == 1
    assert len(state["calls"][0]["workspaceAtCall"]) == length


@pytest.mark.parametrize("outcome", ["returns-false", "throws"])
def test_incomplete_profile_switch_defers_workspace(outcome):
    """Compound ?profile=&workspace= where switchToProfile() actually returns
    false or actually throws (the stub exercises both outcomes through the
    real boot.js block): newSession() must not be called and the workspace
    parameter must survive in the URL for a retry."""
    out = _run_node(_boot_scenario(
        "", profile_intent="{hasParam:true,valid:true,name:'work'}",
        switch_outcome=outcome,
        extra_js=_apply("/?profile=work&workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert state["switchCalls"] == ["work"]      # the switch was attempted
    assert state["calls"] == []                  # but no session was created
    assert "workspace=" in state["search"]       # and the intent survives
    assert state["routed"] == "fell-through"


def test_completed_profile_switch_routes_workspace_under_new_profile():
    """Successful compound case: the session is created under the switched
    profile, and both parameters are consumed."""
    out = _run_node(_boot_scenario(
        "", profile_intent="{hasParam:true,valid:true,name:'work'}",
        switch_outcome="returns-true",
        extra_js=_apply("/?profile=work&workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert state["switchCalls"] == ["work"]
    assert len(state["calls"]) == 1
    assert state["calls"][0]["profileAtCall"] == "work"
    assert state["calls"][0]["workspaceAtCall"] == "/Users/x/proj"
    assert "workspace=" not in state["search"]
    assert "profile=" not in state["search"]


def test_failed_session_create_clears_cue_and_falls_through():
    """A server-rejected path must clear the one-shot cue (so a later manual
    newSession() does not inherit it) and fall back to normal restore."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_throws=True,
        extra_js=_apply("/?workspace=%2Fnot%2Fallowed")))
    state = json.loads(out)
    assert state["routed"] == "fell-through"
    assert state["cueAfter"] is None
    assert len(state["calls"]) == 1
