"""Tests for the ?workspace= boot query param (one-shot workspace routing).

Mirrors tests/test_5682_profile_query_switch.py: the JS functions are
extracted from the static sources and executed in node, so the tests fail
if the functions disappear or change contract — no browser needed.

The boot-block tests execute the actual routing block from static/boot.js
against stubbed collaborators (S, newSession, profile intent flags), giving
deterministic behavioral coverage of:
- an encoded Windows absolute path reaching the session-create request;
- a server-acceptable path whose basename contains "..";
- compound ?profile=&workspace= launches where the profile switch did not
  complete (returned false / threw): newSession() must not be called and the
  workspace parameter must not be consumed;
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

BLOCK_START = "// ?workspace=<path> (one-shot, symmetric to ?profile=)"
BLOCK_END = "const _profileQueryBlocksSavedLocal"


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
evalSession('_workspaceQueryIntentFromLocation');
evalSession('_consumeWorkspaceQueryParamFromLocation');
"""


def _node_boot_runner() -> str:
    """Wrap the real boot routing block into a callable that takes stubs.

    The block is executed verbatim (extracted between its comment marker and
    the statement that follows it), so these tests exercise the shipped code
    path, not a re-implementation.
    """
    return _node_prelude() + f"""
const bootSrc = {BOOT_JS!r};
const _bs = bootSrc.indexOf({BLOCK_START!r});
const _be = bootSrc.indexOf({BLOCK_END!r});
if (_bs < 0 || _be < 0 || _be <= _bs) throw new Error('workspace boot block not found');
const bootBlock = bootSrc.slice(_bs, _be);
async function runBootBlock(ctx) {{
  const S = ctx.S;
  const profileIntent = ctx.profileIntent;
  const _profileSwitchCompleted = ctx.profileSwitchCompleted;
  const prefillIntent = ctx.prefillIntent || null;
  const calls = ctx.calls;
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
  const routed = await (async () => {{ {'{'}
    // eval keeps the block's own `return` semantics: undefined => routed+returned
    return await eval('(async () => {{' + bootBlock + '; return "fell-through";}})()');
  {'}'} }})();
  return routed;
}}
"""


# ---------------------------------------------------------------------------
# Intent parsing — the client must not narrow the server's path language
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
# Boot block behavior — executed verbatim from static/boot.js with stubs
# ---------------------------------------------------------------------------

def _boot_scenario(url: str, *, profile_intent: str, switch_completed: bool,
                   new_session_throws: bool = False) -> str:
    return _node_boot_runner() + f"""
(async () => {{
  applyUrl({url!r});
  const ctx = {{
    S: {{ activeProfile: 'default', _profileSwitchWorkspace: null, session: null }},
    profileIntent: {profile_intent},
    profileSwitchCompleted: {'true' if switch_completed else 'false'},
    newSessionThrows: {'true' if new_session_throws else 'false'},
    calls: []
  }};
  if (ctx.profileSwitchCompleted) ctx.S.activeProfile = 'work';
  const routed = await runBootBlock(ctx);
  console.log(JSON.stringify({{
    routed: routed === undefined ? 'routed' : routed,
    calls: ctx.calls,
    search: window.location.search,
    cueAfter: ctx.S._profileSwitchWorkspace
  }}));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""


def test_windows_path_reaches_session_create():
    """An encoded Windows absolute path must flow through the one-shot cue
    into the session-create request."""
    out = _run_node(_boot_scenario(
        "/?workspace=" + "C%3A%5CUsers%5Cname%5Cproject",
        profile_intent="null", switch_completed=False))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert len(state["calls"]) == 1
    assert state["calls"][0]["workspaceAtCall"] == "C:\\Users\\name\\project"
    assert state["calls"][0]["fresh"] is True
    assert state["calls"][0]["opts"] == {"worktree": False}
    assert "workspace=" not in state["search"]


def test_dotdot_basename_reaches_session_create():
    out = _run_node(_boot_scenario(
        "/?workspace=%2FUsers%2Fx%2Fnotes..archive",
        profile_intent="null", switch_completed=False))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert state["calls"][0]["workspaceAtCall"] == "/Users/x/notes..archive"


@pytest.mark.parametrize("reason", ["returned-false", "threw"])
def test_incomplete_profile_switch_defers_workspace(reason):
    """Compound ?profile=&workspace= where the switch did not complete
    (switchToProfile returned false or threw — both leave the completion flag
    unset in boot.js): newSession() must not be called and the workspace
    parameter must survive in the URL for a retry."""
    out = _run_node(_boot_scenario(
        "/?profile=work&workspace=%2FUsers%2Fx%2Fproj",
        profile_intent="{hasParam:true,valid:true,name:'work'}",
        switch_completed=False))
    state = json.loads(out)
    assert state["calls"] == []
    assert "workspace=" in state["search"]
    assert state["routed"] == "fell-through"


def test_completed_profile_switch_routes_workspace_under_new_profile():
    """Successful compound case: the session is created under the switched
    profile, and the workspace parameter is consumed."""
    out = _run_node(_boot_scenario(
        "/?profile=work&workspace=%2FUsers%2Fx%2Fproj",
        profile_intent="{hasParam:true,valid:true,name:'work'}",
        switch_completed=True))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert len(state["calls"]) == 1
    assert state["calls"][0]["profileAtCall"] == "work"
    assert state["calls"][0]["workspaceAtCall"] == "/Users/x/proj"
    assert "workspace=" not in state["search"]


def test_failed_session_create_clears_cue_and_falls_through():
    """A server-rejected path must clear the one-shot cue (so a later manual
    newSession() does not inherit it) and fall back to normal restore."""
    out = _run_node(_boot_scenario(
        "/?workspace=%2Fnot%2Fallowed",
        profile_intent="null", switch_completed=False,
        new_session_throws=True))
    state = json.loads(out)
    assert state["routed"] == "fell-through"
    assert state["cueAfter"] is None
    assert len(state["calls"]) == 1


def test_profile_switch_failure_does_not_mark_completed_in_source():
    """Source-level guard: in boot.js, a switchToProfile() that throws is
    caught without setting the completion flag, so the behavioral gate above
    covers both the returned-false and the threw cases."""
    idx = BOOT_JS.find("_profileSwitchCompleted=await switchToProfile")
    assert idx > 0
    tail = BOOT_JS[idx : idx + 800]
    assert "catch(e)" in tail
    catch_body = tail[tail.find("catch(e)"):]
    assert "_profileSwitchCompleted=true" not in catch_body
