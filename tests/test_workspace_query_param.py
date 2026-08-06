"""Tests for the ?workspace= boot query param (one-shot workspace routing).

Mirrors tests/test_5682_profile_query_switch.py: the JS functions are
extracted from the static sources and executed in node, so the tests fail
if the functions disappear or change contract — no browser needed.
"""
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


def test_valid_absolute_path_intent():
    out = _run_node(_node_prelude() + """
applyUrl('/?workspace=/Users/someone/Projects/demo&q=hello');
const intent = _workspaceQueryIntentFromLocation();
console.log(JSON.stringify(intent));
""")
    import json
    intent = json.loads(out)
    assert intent == {
        "hasParam": True,
        "valid": True,
        "path": "/Users/someone/Projects/demo",
    }


def test_missing_param_is_empty_intent():
    out = _run_node(_node_prelude() + """
applyUrl('/?q=hello');
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    import json
    intent = json.loads(out)
    assert intent["hasParam"] is False


@pytest.mark.parametrize(
    "bad",
    [
        "relative/path",
        "../escape",
        "/ok/../../etc",
        "",
    ],
)
def test_invalid_paths_are_flagged_invalid(bad):
    out = _run_node(_node_prelude() + f"""
applyUrl('/?workspace=' + encodeURIComponent({bad!r}));
console.log(JSON.stringify(_workspaceQueryIntentFromLocation()));
""")
    import json
    intent = json.loads(out)
    assert intent["valid"] is False


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
    import json
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
    import json
    assert json.loads(out)["calls"] == 0


def test_boot_routes_workspace_through_one_shot_contract():
    """The boot block must consume the param, set the one-shot
    S._profileSwitchWorkspace cue and start a fresh session (worktree:false),
    exactly like the profile-switch workspace path newSession() already knows.
    """
    assert "_workspaceQueryIntentFromLocation" in BOOT_JS
    boot_idx = BOOT_JS.find("_workspaceQueryIntentFromLocation")
    block = BOOT_JS[boot_idx : boot_idx + 1600]
    assert "_consumeWorkspaceQueryParamFromLocation" in block
    assert "S._profileSwitchWorkspace=workspaceIntent.path" in block
    assert "newSession(true,{worktree:false})" in block
    # failure path must clear the one-shot cue so a later manual newSession()
    # does not inherit a workspace the server already rejected
    assert "S._profileSwitchWorkspace=null" in block
