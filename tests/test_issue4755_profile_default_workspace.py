"""Behavior coverage for #4755 profile default workspace precedence."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"
PANELS_JS = ROOT / "static" / "panels.js"
NODE = shutil.which("node")

node_test = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_async_function(source: str, name: str) -> str:
    marker = f"async function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name}() function must exist"
    brace = source.find("{", source.find(")", start))
    assert brace > start, f"{name}() function body must start"
    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    for idx in range(brace, len(source)):
        ch = source[idx]
        nxt = source[idx + 1] if idx + 1 < len(source) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"could not extract {name}()")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _new_session_driver(session_workspace: str, default_workspace: str, switch_workspace: str | None) -> str:
    new_session = _extract_async_function(SESSIONS_JS.read_text(encoding="utf-8"), "newSession")
    return textwrap.dedent(
        f"""
        let captured=null;
        var _newSessionInFlight=null;
        var _messagesTruncated=false;
        var _oldestIdx=0;
        var _activeProject=null;
        var NO_PROJECT_FILTER='__all__';
        var _sessionSourceFilter='webui';
        var S={{
          session:{{session_id:'previous-session',workspace:{json.dumps(session_workspace)}}},
          _profileDefaultWorkspace:{json.dumps(default_workspace)},
          _profileSwitchWorkspace:{json.dumps(switch_workspace)},
          activeProfile:'default',
          toolCalls:[],
        }};
        global.window={{}};
        global.document={{createElement:()=>({{dataset:{{}},appendChild:()=>{{}}}})}};
        global.localStorage={{setItem:()=>{{}}}};
        function $(id){{return null;}}
        function _newSessionPendingText(){{return 'Creating';}}
        function _setNewSessionPending(){{}}
        function updateQueueBadge(){{}}
        function clearLiveToolCards(){{}}
        function api(path,opts){{
          captured={{path,body:JSON.parse(opts.body)}};
          return Promise.resolve({{session:{{session_id:'new-session',messages:[],workspace:captured.body.workspace}}}});
        }}
        function _rememberNewChatDraftSession(){{}}
        function _setActiveSessionUrl(){{}}
        function _setSessionViewedCount(){{}}
        function updateSendBtn(){{}}
        function setStatus(){{}}
        function setComposerStatus(){{}}
        function syncTopbar(){{}}
        function renderMessages(){{}}
        function loadDir(){{return Promise.resolve();}}
        {new_session}
        newSession().then(()=>{{
          process.stdout.write(JSON.stringify({{captured,switchWorkspace:S._profileSwitchWorkspace}}));
        }}).catch(err=>{{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )


@node_test
def test_new_session_prefers_profile_default_over_current_session_workspace():
    payload = _run_node(_new_session_driver(
        session_workspace="/current-workspace",
        default_workspace="/profile-default",
        switch_workspace=None,
    ))

    assert payload["captured"]["path"] == "/api/session/new"
    assert payload["captured"]["body"]["workspace"] == "/profile-default"
    assert payload["captured"]["body"]["prev_session_id"] == "previous-session"


@node_test
def test_new_session_one_shot_switch_workspace_still_wins_and_clears():
    payload = _run_node(_new_session_driver(
        session_workspace="/current-workspace",
        default_workspace="/profile-default",
        switch_workspace="/explicit-switch",
    ))

    assert payload["captured"]["body"]["workspace"] == "/explicit-switch"
    assert payload["switchWorkspace"] is None


@node_test
def test_busy_workspace_switch_returns_before_session_update():
    switch_to_workspace = _extract_async_function(PANELS_JS.read_text(encoding="utf-8"), "switchToWorkspace")
    script = textwrap.dedent(
        f"""
        const calls=[];
        var S={{busy:true,session:{{session_id:'session-1',workspace:'/old',model:'gpt-5',model_provider:null}}}};
        function t(key){{return key;}}
        function showToast(message){{calls.push(['toast',message]);}}
        function api(path,opts){{calls.push(['api',path]);return Promise.resolve({{}});}}
        {switch_to_workspace}
        switchToWorkspace('/new','New').then(()=>{{
          process.stdout.write(JSON.stringify({{calls}}));
        }}).catch(err=>{{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    payload = _run_node(script)

    assert payload["calls"] == [["toast", "workspace_busy_switch"]]
