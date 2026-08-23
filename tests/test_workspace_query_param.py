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
import urllib.error
import urllib.request
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
evalSession('_consumeLaunchActionParamFromLocation');
evalSession('_consumeProfileQueryParamFromLocation');
"""


def _node_boot_runner() -> str:
    """Run the real profile-switch block, then the real workspace-routing +
    action=new-chat launch region, all extracted verbatim from static/boot.js
    in their shipped production order, in a shared scope.

    The second slice deliberately spans from the workspace block through the
    `action=new-chat` branch (it ends at the saved-session restore), so the
    ordering between the two launch intents is exercised by the shipped code
    rather than asserted on source shape. `_shouldStartFreshPwaChat` is also
    extracted verbatim instead of stubbed.

    switchToProfile is stubbed per scenario (true / false / throw) and
    newSession can reject with a real HTTP status, so completion flags and
    the reject/consume policy are computed by the shipped code, not preset by
    the test.
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
const launchBlock = slice({WS_BLOCK_START!r}, {WS_BLOCK_END!r});
globalThis._shouldStartFreshPwaChat = (0, eval)('(' + extractFunc(bootSrc, '_shouldStartFreshPwaChat') + ')');
async function runBootBlocks(ctx) {{
  const S = ctx.S;
  const profileIntent = ctx.profileIntent;
  const prefillIntent = null;
  const calls = ctx.calls;
  const pwaLaunchAction = ctx.pwaLaunchAction;
  const urlSession = ctx.urlSession;
  const _shouldStartFreshPwaChat = globalThis._shouldStartFreshPwaChat;
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
      profileAtCall: S.activeProfile,
      searchAtCall: window.location.search
    }});
    if (ctx.newSessionRejectStatus) {{
      // Mirror the error shape api() throws for a non-ok response, body
      // included: the boot block discriminates a workspace verdict from any
      // other 400 by parsing `code` out of it. When rejectOnlyWorkspaceCue is
      // set, only the workspace-cued attempt fails — that is what a workspace
      // 400 means: the verdict is on the path, so the same request without it
      // succeeds.
      if (!ctx.rejectOnlyWorkspaceCue || S._profileSwitchWorkspace !== null) {{
        const err = new Error('session-create rejected');
        err.status = ctx.newSessionRejectStatus;
        err.body = JSON.stringify(
          ctx.newSessionRejectCode
            ? {{ error: 'rejected', code: ctx.newSessionRejectCode }}
            : {{ error: 'rejected' }}
        );
        throw err;
      }}
    }}
    if (ctx.newSessionThrows) throw new Error('session-create rejected');
    S.session = {{ session_id: 'test' }};
    if (ctx.newSessionThrowsAfterCreate) {{
      // The real newSession() keeps initializing client state after the server
      // accepted and S.session is set; any of those steps can throw.
      throw new Error('post-accept init failed');
    }}
  }}
  const syncTopbar = () => {{}};
  const syncWorkspacePanelState = () => {{}};
  const lockComposerForClarify = (placeholder) => {{ ctx.composerLocked = placeholder || true; }};
  const renderSessionList = async () => {{
    // A post-create rendering step: the session already exists server-side when
    // this runs, which is exactly the case the boot block must not replay.
    if (ctx.renderThrows) throw new Error('render failed');
  }};
  const _finalizeComposerPrefillOnBoot = async () => {{ ctx.prefillFinalized = true; }};
  const _startBootModelDropdown = () => {{}};
  return await eval('(async () => {{' + profileBlock + ';' + launchBlock + '; return "fell-through";}})()');
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
                   new_session_throws_after_create: bool = False,
                   render_throws: bool = False,
                   new_session_reject_status: int | None = None,
                   new_session_reject_code: str | None = "invalid_workspace",
                   reject_only_workspace_cue: bool = False,
                   pwa_launch_action: str | None = None,
                   url_session: str | None = None,
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
    newSessionThrowsAfterCreate: {'true' if new_session_throws_after_create else 'false'},
    renderThrows: {'true' if render_throws else 'false'},
    newSessionRejectStatus: {new_session_reject_status if new_session_reject_status else 'null'},
    newSessionRejectCode: {json.dumps(new_session_reject_code)},
    rejectOnlyWorkspaceCue: {'true' if reject_only_workspace_cue else 'false'},
    pwaLaunchAction: {json.dumps(pwa_launch_action)},
    urlSession: {json.dumps(url_session)},
    calls: [],
    switchCalls: []
  }};
  const routed = await runBootBlocks(ctx);
  console.log(JSON.stringify({{
    routed: routed === undefined ? 'routed' : routed,
    // The boot either created/restored a session (S.session set), stopped on
    // purpose to protect an outstanding workspace launch (returned early with
    // no session), or fell through to the normal restore path below the block.
    held: routed === undefined && ctx.S.session === null,
    calls: ctx.calls,
    switchCalls: ctx.switchCalls,
    search: window.location.search,
    prefillFinalized: ctx.prefillFinalized === true,
    composerLocked: ctx.composerLocked || false,
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
    assert state["held"] is True                 # boot held instead of restoring


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
        new_session_reject_status=400,
        extra_js=_apply("/?workspace=%2Fnot%2Fallowed")))
    state = json.loads(out)
    assert state["routed"] == "fell-through"
    assert state["cueAfter"] is None
    assert len(state["calls"]) == 1


# ---------------------------------------------------------------------------
# Significant surrounding whitespace — trimming is a blank predicate only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/home/u/project ", " /home/u/project", "/home/u/pro ject"])
def test_significant_whitespace_is_preserved_verbatim(path):
    """A Unix directory name may legitimately carry leading/trailing spaces.
    Trimming the value would silently route to a DIFFERENT directory, so the
    decoded string must reach the parser output untouched."""
    out = _run_node(_node_prelude() + f"""
applyUrl('/?workspace=' + encodeURIComponent({path!r}));
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    intent = json.loads(out)
    assert intent["valid"] is True
    assert intent["path"] == path


def test_trailing_space_path_reaches_session_create_unmodified():
    """End-to-end through the real boot block: the exact decoded path, spaces
    included, is what the session-create request receives."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        extra_js=_apply("/?workspace=%2Fhome%2Fu%2Fproject%20")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert state["calls"][0]["workspaceAtCall"] == "/home/u/project "


# ---------------------------------------------------------------------------
# Composed launch: ?action=new-chat + ?workspace= must create ONE session
# ---------------------------------------------------------------------------

def test_new_chat_launch_with_workspace_creates_one_session_with_workspace():
    """`?action=new-chat&workspace=…&q=…` must not create a workspace-less
    session first. The workspace block owns the single creation, and both
    launch intents are consumed so a reload cannot mint a second session."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        pwa_launch_action="new-chat",
        extra_js=_apply("/?action=new-chat&workspace=%2FUsers%2Fx%2Fproj&q=hello")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert len(state["calls"]) == 1                       # exactly one session
    assert state["calls"][0]["workspaceAtCall"] == "/Users/x/proj"
    assert state["calls"][0]["opts"] == {"worktree": False}
    assert "workspace=" not in state["search"]            # both launch intents
    assert "action=" not in state["search"]               # are consumed
    assert "q=hello" in state["search"]                   # prefill still pending


def test_new_chat_launch_with_workspace_survives_hard_reload_as_one_session():
    """Simulate the hard reload: replay the boot against the URL left behind
    by the first launch. No launch intent remains, so no second session is
    created — the whole launch produced exactly one."""
    first = json.loads(_run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        pwa_launch_action="new-chat",
        extra_js=_apply("/?action=new-chat&workspace=%2FUsers%2Fx%2Fproj&q=hello"))))
    assert len(first["calls"]) == 1
    reload_url = "/" + (first["search"] or "")
    second = json.loads(_run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        pwa_launch_action=None,
        extra_js=_apply(reload_url))))
    assert second["calls"] == []
    assert second["routed"] == "fell-through"


def test_new_chat_launch_without_workspace_still_creates_session():
    """The plain PWA shortcut path is untouched by the reordering."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        pwa_launch_action="new-chat",
        extra_js=_apply("/?action=new-chat")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert len(state["calls"]) == 1
    assert state["calls"][0]["fresh"] is True
    # newSession(true) is called with no options argument on this path, so the
    # key is absent from the serialized call record (undefined, not {worktree:false}).
    assert "opts" not in state["calls"][0]


def test_new_chat_with_url_session_does_not_create_a_session():
    """_shouldStartFreshPwaChat() is false when the URL names a session; the
    reordering must not change that."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        pwa_launch_action="new-chat", url_session="abc123",
        extra_js=_apply("/?action=new-chat&session=abc123")))
    state = json.loads(out)
    assert state["calls"] == []
    assert state["routed"] == "fell-through"


def test_rejected_workspace_falls_back_to_the_new_chat_launch():
    """When the server rejects the path on an `?action=new-chat&workspace=…`
    launch, the workspace intent is consumed and the shortcut still honours
    its own contract: one workspace-less new chat, not a silent restore."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=400,
        reject_only_workspace_cue=True,
        pwa_launch_action="new-chat",
        extra_js=_apply("/?action=new-chat&workspace=%2Fetc")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert len(state["calls"]) == 2          # rejected attempt, then the shortcut
    assert state["calls"][1]["workspaceAtCall"] is None
    assert "workspace=" not in state["search"]


# ---------------------------------------------------------------------------
# Failure policy: the parameter survives transport failures, and is consumed
# only on an objective server verdict
# ---------------------------------------------------------------------------

def test_deferred_profile_switch_suppresses_the_new_chat_shortcut():
    """`?profile=&workspace=&action=new-chat` where the profile switch fails:
    the workspace launch is deferred, so the new-chat shortcut must NOT create
    a workspace-less session — otherwise the later successful retry produces a
    second session from the same launch."""
    out = _run_node(_boot_scenario(
        "", profile_intent="{hasParam:true,valid:true,name:'work'}",
        switch_outcome="returns-false",
        pwa_launch_action="new-chat",
        extra_js=_apply("/?profile=work&action=new-chat&workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert state["switchCalls"] == ["work"]
    assert state["calls"] == []                  # no session at all this boot
    assert "workspace=" in state["search"]       # intent preserved for retry
    assert state["held"] is True                 # boot held instead of restoring


def test_transport_failure_suppresses_the_new_chat_shortcut():
    """Same invariant for a transport failure: the workspace parameter
    survives, so the shortcut must not mint a session the retry would
    duplicate."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=401,
        pwa_launch_action="new-chat",
        extra_js=_apply("/?action=new-chat&workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert len(state["calls"]) == 1              # the failed attempt only
    assert "workspace=" in state["search"]
    assert state["held"] is True                 # boot held instead of restoring


@pytest.mark.parametrize("code", [None, "invalid_toolsets"])
def test_unrelated_400_preserves_workspace_param(code):
    """A 400 raised by another field of the same request (an invalid
    enabled_toolsets payload, say) is not a verdict on the path. Only the
    server's `code:"invalid_workspace"` marks a permanent rejection, so an
    unrelated 400 must leave the intent retryable."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=400,
        new_session_reject_code=code,
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert state["held"] is True                 # boot held instead of restoring
    assert "workspace=" in state["search"]


@pytest.mark.parametrize("status", [401, 500, 503])
def test_transport_failure_preserves_workspace_param(status):
    """A 401 between profile bootstrap and session creation redirects to
    `login?next=<pathname+search>`. If the parameter had already been
    consumed, that snapshot would drop the requested project silently. Same
    reasoning for 5xx and network errors: they are not verdicts on the path."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=status,
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj&q=hello")))
    state = json.loads(out)
    assert state["held"] is True                 # boot held instead of restoring
    assert "workspace=%2FUsers%2Fx%2Fproj" in state["search"] or \
           "workspace=/Users/x/proj" in state["search"]
    assert state["cueAfter"] is None


def test_network_error_without_status_preserves_workspace_param():
    """A fetch-level TypeError carries no .status; it must be treated as
    transport, not as a server verdict."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_throws=True,
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert state["held"] is True                 # boot held instead of restoring
    assert "workspace=" in state["search"]


def test_server_rejected_path_consumes_param_before_fallback():
    """400 is resolve_trusted_workspace()'s objective verdict on the path:
    retrying the same URL can only fail again, so the parameter is consumed
    and the boot falls back to the documented restore."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=400,
        extra_js=_apply("/?workspace=%2Fetc&q=hello")))
    state = json.loads(out)
    assert state["routed"] == "fell-through"
    assert "workspace=" not in state["search"]
    assert "q=hello" in state["search"]


def test_param_is_not_consumed_before_the_create_attempt():
    """The URL still carries the parameter at the moment newSession() is
    called — consumption happens strictly after the server accepts."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert "workspace=" in state["calls"][0]["searchAtCall"]
    assert "workspace=" not in state["search"]


def test_blank_workspace_param_is_consumed_and_falls_through():
    """A blank value is an objectively unusable intent: consume it so it
    cannot loop, and fall through to the normal restore."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        extra_js=_apply("/?workspace=%20%20&q=hello")))
    state = json.loads(out)
    assert state["calls"] == []
    assert state["routed"] == "fell-through"
    assert "workspace=" not in state["search"]
    assert "q=hello" in state["search"]


# ---------------------------------------------------------------------------
# An outstanding launch must survive the fallback restore path too
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("saved_session", [None, "sess-abc"])
def test_held_launch_does_not_reach_the_restore_path(saved_session):
    """Suppressing only the new-chat shortcut is not enough. Below it,
    loadSession() calls _setActiveSessionUrl(), which rewrites the entire query
    string and would drop the preserved `workspace`; and the no-saved-session
    path can auto-bind a fresh default-workspace session. Boot must stop before
    either, whether or not a saved session exists."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=503,
        extra_js=(
            (f"localStorage.setItem('hermes-webui-session', {saved_session!r});\n  "
             if saved_session else "")
            + _apply("/?workspace=%2FUsers%2Fx%2Fproj&q=hello")
        )))
    state = json.loads(out)
    assert state["held"] is True                 # returned before the restore
    assert len(state["calls"]) == 1              # only the failed attempt
    assert "workspace=" in state["search"]       # intent intact for the reload
    assert "q=hello" in state["search"]          # prefill intact too


def test_held_launch_leaves_the_url_byte_identical():
    """The whole point of holding is that the next load replays the same URL.
    Nothing in the held path may touch it."""
    out = _run_node(_boot_scenario(
        "", profile_intent="{hasParam:true,valid:true,name:'work'}",
        switch_outcome="throws",
        extra_js=_apply("/app/?profile=work&workspace=%2FUsers%2Fx%2Fproj&q=hi#frag")))
    state = json.loads(out)
    assert state["held"] is True
    assert state["calls"] == []
    assert "profile=work" in state["search"]     # not consumed either: the
    assert "workspace=" in state["search"]       # retry needs the whole launch
    assert "q=hi" in state["search"]


def test_held_launch_does_not_finalize_the_composer_prefill():
    """`_finalizeComposerPrefillOnBoot()` consumes `q=` and fills the composer.
    Both are wrong while a launch is outstanding: consuming strips the prefill
    from the URL the retry depends on, and a filled composer invites a Send
    that routes through plain newSession() with no workspace cue — creating a
    default-workspace session while the requested one is still pending, i.e.
    the very duplicate the held branch exists to prevent."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=503,
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj&q=hello")))
    state = json.loads(out)
    assert state["held"] is True
    assert state["prefillFinalized"] is False    # composer left untouched
    assert "q=hello" in state["search"]          # prefill still in the URL


def test_held_launch_locks_the_composer():
    """An empty-but-interactive composer is still an escape hatch: a manually
    typed Send routes through plain newSession() and binds to the
    profile-default workspace while the requested one is pending. The held
    state must lock the composer (with an explanatory placeholder) so the only
    action is the reload that retries the launch."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_reject_status=503,
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert state["held"] is True
    assert state["composerLocked"]               # locked, not merely empty
    assert "reload" in str(state["composerLocked"]).lower()


def test_successful_launch_does_not_lock_the_composer():
    """The lock is strictly a held-state affordance."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj")))
    state = json.loads(out)
    assert state["routed"] == "routed"
    assert state["composerLocked"] is False


def test_post_create_render_failure_keeps_the_session_and_spends_the_intent():
    """When /api/session/new succeeded but a later rendering step throws, the
    launch already produced its one session. Replaying it on reload would
    create a second workspace session and orphan the first, so the parameter
    must be consumed and the boot must not hold."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        render_throws=True,
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj&q=hello")))
    state = json.loads(out)
    assert len(state["calls"]) == 1              # the session was created
    assert "workspace=" not in state["search"]   # intent spent, not replayable
    assert state["held"] is False                # and the boot did not hold
    assert state["routed"] == "fell-through"     # normal path finishes the UI


def test_throw_inside_new_session_after_accept_still_spends_the_intent():
    """newSession() keeps initializing client state after the server accepted
    and S.session is set (todo hydration, stream start, dropdown sync). A
    throw from inside that tail must be classified like any other post-create
    failure: the session exists, so the launch is spent — creation is detected
    by observing S.session, not by a flag set only after newSession()
    returns."""
    out = _run_node(_boot_scenario(
        "", profile_intent="null", switch_outcome="returns-true",
        new_session_throws_after_create=True,
        extra_js=_apply("/?workspace=%2FUsers%2Fx%2Fproj&q=hello")))
    state = json.loads(out)
    assert len(state["calls"]) == 1              # the POST happened once
    assert "workspace=" not in state["search"]   # intent spent, not replayable
    assert state["routed"] == "fell-through"     # boot finishes normally
    # And the session survives for the normal path to render:
    # held would have cleared it.
    assert state["held"] is False


# ---------------------------------------------------------------------------
# Server side of the same contract: the discriminator the client branches on
# ---------------------------------------------------------------------------
def test_server_tags_workspace_rejection_with_a_code():
    """The client can only tell a path verdict from an unrelated 400 because
    POST /api/session/new tags the former. Pin that contract server-side so
    the two halves cannot drift apart."""
    from tests._pytest_port import BASE

    body = json.dumps({"workspace": "/definitely/not/a/trusted/workspace"}).encode()
    req = urllib.request.Request(
        BASE + "/api/session/new", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            pytest.fail(f"expected a 400 rejection, got {r.status}")
    except urllib.error.HTTPError as e:
        assert e.code == 400
        payload = json.loads(e.read())
    assert payload.get("code") == "invalid_workspace"
    assert payload.get("error")          # human-readable message still present


def test_inaccessible_workspace_is_not_tagged_as_a_verdict(tmp_path):
    """A path the server cannot INSPECT (permission denied) is recoverable:
    the user grants access and the same request succeeds unchanged. It must
    not carry the permanent-rejection code, or the boot would discard a launch
    that a reload would have completed."""
    from tests._pytest_port import BASE

    denied_parent = tmp_path / "denied"
    denied_parent.mkdir()
    (denied_parent / "project").mkdir()
    denied_parent.chmod(0o000)
    try:
        body = json.dumps({"workspace": str(denied_parent / "project")}).encode()
        req = urllib.request.Request(
            BASE + "/api/session/new", data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                pytest.skip(f"path was not access-denied for the server (status {r.status})")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            payload = json.loads(e.read())
    finally:
        denied_parent.chmod(0o755)

    if "Cannot access path" not in (payload.get("error") or ""):
        pytest.skip("server resolved the path without an access error (running as root?)")
    assert payload.get("code") != "invalid_workspace", (
        "a recoverable access failure must not be tagged as a permanent path verdict"
    )


def test_workspace_access_error_is_a_valueerror_subclass():
    """Existing callers catch ValueError; the new distinction must not change
    who catches what."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from api.workspace import WorkspaceAccessError

    assert issubclass(WorkspaceAccessError, ValueError)


def test_malformed_path_is_classified_permanent_not_recoverable():
    """An embedded NUL makes `Path.stat()` raise ValueError, and the message
    carries the same "Cannot access path:" prefix as a permission failure. It
    is nonetheless permanent -- no user action makes that value resolvable --
    so it must not be classified recoverable, or boot would preserve and
    replay an intrinsically invalid intent on every reload.

    This is why the classification travels as a flag from the probe rather
    than being re-derived from the message text."""
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(REPO_ROOT))
    from api.workspace import _workspace_access_failure

    failure = _workspace_access_failure(_Path("/tmp/bad\x00path"))
    assert failure is not None
    message, recoverable = failure
    assert "Cannot access path" in message      # reads like an access failure
    assert recoverable is False                 # but is emphatically not one


@pytest.mark.parametrize("case,recoverable", [
    ("missing", False),
    ("not-a-directory", False),
])
def test_permanent_workspace_failures_are_not_recoverable(tmp_path, case, recoverable):
    """The other permanent verdicts keep their classification."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from api.workspace import _workspace_access_failure

    if case == "missing":
        target = tmp_path / "nope"
    else:
        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")

    failure = _workspace_access_failure(target)
    assert failure is not None
    assert failure[1] is recoverable
