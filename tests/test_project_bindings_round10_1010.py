"""Round-10 re-review of head ``5be3d7cc`` (maintainer re-gate review
5479915135 @ 2026-10-10T16:43:57Z, CORE finding ``static/sessions.js:2031``).

CORE — "New conversation in worktree" uses the project's default workspace
instead of the displayed workspace.  Verified in Chromium with real HTTP: a chat
in git workspace B, with its active project bound to non-git workspace A,
submits A and receives HTTP 400; master submits B and succeeds.  Exact fix at
``static/panels.js:5996``::

    await newSession(false,{worktree:true,workspace:currentWs});

``newSession()`` merges the ACTIVE project's bound workspace/model into the
request options *unless the caller already set one* (``hasOwnProperty`` check).
That merge is deliberate for the top-level New Chat button (selecting a project
then pressing "+" must open in the project's pinned workspace), and an explicit
``workspace`` option is the designed opt-out — the sibling
``switchToWorkspace()`` already uses exactly that form
(``newSession(false,{workspace:path})``) after the same class of finding.  The
worktree action was the one call site that never opted out, so whenever the
displayed workspace differed from the project's default the project default won
and the worktree was minted in the wrong place.

The probe below slices newSession()'s own workspace-resolution preamble out of
``static/sessions.js`` and evaluates it in node, so the semantics under test are
the shipped ones: the pre-fix call shape ``{worktree:true}`` resolves the
PROJECT default, the fixed shape resolves the DISPLAYED workspace.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# Unique anchors inside newSession() (static/sessions.js).
_START = "    if(!Object.prototype.hasOwnProperty.call(options,'project_id')"
_END = "const inheritWs="


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _resolution_preamble() -> str:
    """The shipped options-merge + workspace-resolution block of newSession()."""
    src = _read("static/sessions.js")
    assert src.count(_START) == 1, "the active-project merge marker moved"
    i = src.index(_START)
    assert src.count(_END) == 1, "the inheritWs marker moved"
    j = src.index(_END, i)
    end = src.index("\n", j)
    return src[i:end]


def _run_node(tmp_path: Path, script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is required for the frontend behavior probe")
    script_path = tmp_path / "round10_resolution.js"
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


_PROBE = """
const fs = require('fs');
const PREAMBLE = %s;

const ENV = {
  // The displayed workspace is B; the project's default workspace is A
  // (non-git, so worktree:true there is a 400 — the maintainer's repro).
  S: {session: {workspace: '/ws/B'}, _profileSwitchWorkspace: null,
      _profileDefaultWorkspace: '/ws/D', activeProfile: 'default'},
  activeProject: 'proj-1',
  NO_PROJECT_FILTER: '__none__',
  allProjects: [{project_id: 'proj-1', profile: 'default', workspaces: ['/ws/A'],
                 model: 'proj-model', model_provider: 'proj-provider'}],
};

function resolve(optionsIn, env) {
  const body = new Function(
    'optionsIn',
    'env',
    `
    let options = optionsIn;
    const _activeProject = env.activeProject;
    const NO_PROJECT_FILTER = env.NO_PROJECT_FILTER;
    const _allProjects = env.allProjects;
    const S = env.S;
    const _profileMatchesActiveProfile = () => true;
    const _projectBindingsForNewSession = (p) => ({
      workspace: (p.workspaces || [])[0], model: p.model,
      model_provider: p.model_provider,
    });
    ${PREAMBLE}
    return {workspace: inheritWs, options};
    `
  );
  return body(optionsIn, env);
}

// The options object AS SHIPPED by the worktree action in static/panels.js.
function shippedWorktreeOptions(env) {
  const panels = fs.readFileSync('static/panels.js', 'utf8');
  const wi = panels.indexOf('workspace_new_worktree_conversation');
  if (wi < 0) throw new Error('the worktree action moved');
  const seg = panels.slice(wi, panels.indexOf('workspace_choose_path', wi));
  const m = seg.match(/newSession\\(false,\\{([^}]*)\\}\\)/);
  if (!m) throw new Error('the worktree action no longer calls newSession');
  const options = {};
  for (const part of m[1].split(',')) {
    const t = part.trim();
    if (!t) continue;
    const c = t.indexOf(':');
    const key = (c < 0 ? t : t.slice(0, c)).trim();
    const raw = c < 0 ? 'true' : t.slice(c + 1).trim();
    // currentWs is the dropdown's displayed workspace (fed by the shown
    // session's workspace in both renderWorkspaceDropdownInto() callers).
    options[key] = raw === 'currentWs'
      ? env.S.session.workspace
      : (raw === 'true' ? true : (raw === 'false' ? false : raw));
  }
  return options;
}

const withProject = ENV;
const withoutProject = Object.assign({}, ENV, {activeProject: null});

const bug = resolve({worktree: true}, withProject);              // the pre-fix call shape
const shipped = resolve(shippedWorktreeOptions(withProject), withProject);
const noProject = resolve({worktree: true}, withoutProject);

console.log(JSON.stringify({
  bug_workspace: bug.workspace,
  bug_saw: (bug.options || {}).workspace,
  shipped_options: shippedWorktreeOptions(withProject),
  shipped_workspace: shipped.workspace,
  no_project_workspace: noProject.workspace,
}));
""" % json.dumps(_resolution_preamble())


def test_a_pre_fix_worktree_call_submits_the_projects_default_workspace(tmp_path):
    """The empty-options call shape really does submit A instead of the shown B."""
    out = json.loads(_run_node(tmp_path, _PROBE))
    # Symptom as reported: the project's bound workspace (A) is what gets posted
    # even though the conversation is displayed in B.
    assert out["bug_workspace"] == "/ws/A", out
    assert out["bug_saw"] == "/ws/A", out


def test_b_the_shipped_worktree_call_resolves_the_displayed_workspace(tmp_path):
    """The call site as shipped opts out of the project merge (the fix)."""
    out = json.loads(_run_node(tmp_path, _PROBE))
    assert out["shipped_options"].get("workspace") == "/ws/B", out
    assert out["shipped_workspace"] == "/ws/B", out


def test_c_without_an_active_project_the_displayed_workspace_is_used(tmp_path):
    """Control: the regression is specific to the active-project merge."""
    out = json.loads(_run_node(tmp_path, _PROBE))
    assert out["no_project_workspace"] == "/ws/B", out


# ---------------------------------------------------------------------------
# Source guards: the call site passes the displayed workspace, and the
# now-required opt-out semantics are still what newSession() implements.
# ---------------------------------------------------------------------------


def _worktree_action_source() -> str:
    src = _read("static/panels.js")
    i = src.index("function renderWorkspaceDropdownInto(")
    j = src.index("workspace_new_worktree_conversation", i)
    return src[j : src.index("workspace_choose_path", j)]


def test_worktree_action_passes_the_displayed_workspace():
    seg = _worktree_action_source()
    assert "newSession(false,{worktree:true,workspace:currentWs})" in seg, (
        "the worktree action must pass the displayed workspace or the active "
        "project's default binding silently overrides it"
    )
    assert "worktree:true" in seg


def test_currentws_is_the_displayed_workspace_of_the_dropdown():
    """``currentWs`` is the dropdown's own active path, fed by the shown session."""
    src = _read("static/panels.js")
    assert "function renderWorkspaceDropdownInto(dd, workspaces, currentWs)" in src
    # ...and both callers compute it from the displayed session's workspace.
    assert src.count("renderWorkspaceDropdownInto(dd, data.workspaces, S.session?.workspace") == 2


def test_the_explicit_workspace_opt_out_is_still_shipped_in_newSession():
    src = _read("static/sessions.js")
    assert (
        "if(_pb.workspace&&!Object.prototype.hasOwnProperty.call(_merged,'workspace'))"
        in src
    ), "the merge must keep deferring to a caller-supplied workspace"
    assert "const boundWs=(options&&options.workspace)||null;" in src


def test_the_sibling_switch_to_workspace_opt_out_is_unchanged():
    src = _read("static/panels.js")
    assert "await newSession(false,{workspace:path});" in src


def test_panels_js_parses():
    if shutil.which("node") is None:
        pytest.skip("node is required")
    result = subprocess.run(
        ["node", "--check", str(REPO_ROOT / "static" / "panels.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
