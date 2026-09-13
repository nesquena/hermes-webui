"""#6224 runtime regression: `/sessions` and `/resume` must open the session browser.

Before the fix the WebUI composer sent the literal `/sessions` text to the agent
instead of opening the session browser, and a failure inside the browser opener
or `renderSessionList()` leaked the composer value and the command dropdown
(composer stuck holding the command, dropdown stuck open).

These tests execute the *real* branch from `static/messages.js` under node with
the surrounding `send()` context stubbed, so they assert runtime behaviour:

* success -> composer cleared, dropdown hidden, `autoResize()` called and
  `send()` returns BEFORE the agent-command lookup (the command is never
  forwarded to the agent),
* the opener throwing / `renderSessionList()` rejecting -> the same cleanup
  still runs AND the rejection keeps propagating out of `send()` (send() still
  rejects, the error is not swallowed),
* a missing mobile-aware opener falls back to `expandSidebar()`.

Source-text assertions are deliberately avoided here: slicing the source cannot
prove any of the above. Same node-harness style as
tests/test_5306_subagent_sidebar_flicker.py.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_BRANCH_MARKER = "if(_parsedCmd.name==='sessions'"

# Node program: the extracted branch is spliced in place of the /*__BRANCH__*/
# placeholder, so the code under test is the shipped expression, not a copy.
_HARNESS = r"""
'use strict';
const scenario = JSON.parse(process.env.SCENARIO_6224);

const state = {opened: 0, expanded: 0, rendered: 0, resized: 0, dropdownHidden: 0, agentLookup: 0};
let composerValue = 'unsent draft /sessions';

function $(id){ return { get value(){ return composerValue; }, set value(v){ composerValue = v; } }; }
function autoResize(){ state.resized += 1; }
function hideCmdDropdown(){ state.dropdownHidden += 1; }

let _openProfileSwitchSessionBrowser = function(){
  state.opened += 1;
  if (scenario.opener === 'throw') throw new Error('opener failed');
};
if (scenario.opener === 'missing') _openProfileSwitchSessionBrowser = undefined;

let expandSidebar = function(){ state.expanded += 1; };
if (scenario.sidebar === 'missing') expandSidebar = undefined;

let renderSessionList = async function(){
  state.rendered += 1;
  if (scenario.render === 'reject') throw new Error('render failed');
};
if (scenario.render === 'missing') renderSessionList = undefined;

async function getAgentCommandMetadata(name){
  state.agentLookup += 1;
  return {name: name};
}

const _parsedCmd = {name: scenario.cmd};

async function send(){
  const text = scenario.cmd;
  /*__BRANCH__*/
  // Reaching this point means the slash command was NOT short-circuited: the
  // pre-existing send() path continues into the agent-command lookup.
  const _agentCmd = await getAgentCommandMetadata(_parsedCmd.name);
  return {outcome: 'fell-through-to-agent-command-lookup', agentMeta: _agentCmd && _agentCmd.name};
}

(async () => {
  let result;
  try {
    result = await send();
  } catch (e) {
    result = {outcome: 'rejected', error: String((e && e.message) || e)};
  }
  if (result === undefined) result = {outcome: 'returned-early'};
  console.log(JSON.stringify(Object.assign({}, result, state, {composerValue: composerValue})));
})();
"""


def _extract_branch(source: str) -> str:
    """Return the complete `/sessions` + `/resume` statement from messages.js."""
    start = source.find(_BRANCH_MARKER)
    assert start >= 0, "sessions/resume branch not found in static/messages.js"
    brace = source.find("{", start)
    assert brace >= 0, "sessions/resume branch opening brace not found"
    depth = 0
    i = brace
    while i < len(source):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
        i += 1
    raise AssertionError("sessions/resume branch braces unbalanced")


def _run(cmd="sessions", opener="ok", render="ok", sidebar="ok") -> dict:
    branch = _extract_branch(MESSAGES_JS.read_text(encoding="utf-8"))
    assert branch.rstrip().endswith("}"), "extracted branch looks truncated"
    source = _HARNESS.replace("/*__BRANCH__*/", branch)
    env = dict(os.environ)
    env["SCENARIO_6224"] = json.dumps(
        {"cmd": cmd, "opener": opener, "render": render, "sidebar": sidebar}
    )
    proc = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_cleanup_ran(out: dict) -> None:
    assert out["composerValue"] == "", (
        "composer value must be cleared in the finally block, got %r" % out["composerValue"]
    )
    assert out["dropdownHidden"] == 1, "hideCmdDropdown() must run in the finally block"
    assert out["resized"] == 1, "autoResize() must run in the finally block"
    assert out["agentLookup"] == 0, (
        "the slash command must never reach the agent-command lookup"
    )


@pytest.mark.parametrize("cmd", ["sessions", "resume"])
def test_sessions_and_resume_short_circuit_before_agent_lookup(cmd):
    out = _run(cmd=cmd)

    assert out["outcome"] == "returned-early", out
    assert out["opened"] == 1, "the session browser opener must be invoked"
    assert out["rendered"] == 1, "renderSessionList() must be awaited"
    _assert_cleanup_ran(out)


def test_sessions_runs_cleanup_and_still_rejects_when_opener_throws():
    out = _run(cmd="sessions", opener="throw")

    assert out["outcome"] == "rejected", out
    assert "opener failed" in out["error"], out
    assert out["opened"] == 1, "the opener was reached before it threw"
    _assert_cleanup_ran(out)


def test_sessions_runs_cleanup_and_still_rejects_when_render_session_list_rejects():
    out = _run(cmd="sessions", render="reject")

    assert out["outcome"] == "rejected", out
    assert "render failed" in out["error"], out
    assert out["opened"] == 1, "the browser still opens before the list refresh fails"
    _assert_cleanup_ran(out)


def test_sessions_falls_back_to_expand_sidebar_when_mobile_opener_missing():
    out = _run(cmd="sessions", opener="missing")

    assert out["outcome"] == "returned-early", out
    assert out["expanded"] == 1, "expandSidebar() must be used when the mobile opener is absent"
    assert out["opened"] == 0
    assert out["rendered"] == 1
    _assert_cleanup_ran(out)


def test_sessions_short_circuits_when_render_session_list_is_unavailable():
    out = _run(cmd="sessions", render="missing")

    assert out["outcome"] == "returned-early", out
    assert out["rendered"] == 0, "no list refresh is possible without renderSessionList()"
    assert out["opened"] == 1
    _assert_cleanup_ran(out)
