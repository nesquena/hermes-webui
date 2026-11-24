"""#8004: sending the first message with no conversation open does not await a
second session-list render before the message is posted.

newSession() refreshes the sidebar itself (forced, see #7936). The send path's
nine ``if(!S.session){...}`` branches also awaited renderSessionList(), so the
first message waited for a full /api/sessions + /api/projects read before
POST /api/chat/start. tests/browser_new_chat_focus.py sends a first message with
the list response held; these pins cover the command branches it does not drive.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

# An awaited list render that follows the awaited newSession(): on the same line,
# on the next one, after the guard's closing brace, or behind a typeof check.
AWAITED_RENDER_AFTER_NEW_SESSION = re.compile(
    r"await\s+newSession\s*\([^)]*\)\s*;?\s*\}?\s*"
    r"(?:if\s*\([^)]*\)\s*)?await\s+renderSessionList\s*\("
)
NO_SESSION_GUARD = re.compile(r"if\s*\(\s*!\s*S\.session\s*\)\s*\{\s*await\s+newSession\s*\([^)]*\)")


def _send_body():
    start = MESSAGES_JS.index("async function send(){")
    return MESSAGES_JS[start : MESSAGES_JS.index("\n}\n", start) + 3]


def test_the_send_path_creates_the_session_without_awaiting_a_list_render():
    body = _send_body()
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(body)
    # The composer ownership helper also rejects failed/superseded creates.
    assert body.count("await _ensureSessionForComposerAction(") >= 9
    start = MESSAGES_JS.index("async function _ensureSessionForComposerAction(")
    helper = MESSAGES_JS[start:MESSAGES_JS.index("\n}\n", start)]
    # The helper mints through the canonical newSession() path (passing options
    # only when the caller supplied them, keeping master's zero-argument shape)
    # and never renders the list itself.
    assert "await newSession(" in helper
    assert "_newSessionActionWasSuperseded(result)" in helper
    assert "renderSessionList" not in helper


@pytest.mark.parametrize("source", [
    "if(!S.session){await newSession();await renderSessionList();}",
    "if(!S.session){await newSession();}\n        await renderSessionList();",
    "if (!S.session) {\n  await newSession();\n  await renderSessionList();\n}",
    "if(!S.session){await newSession();}\nif(typeof renderSessionList==='function') await renderSessionList();",
])
def test_the_pin_sees_an_awaited_render_in_any_spelling(source):
    assert AWAITED_RENDER_AFTER_NEW_SESSION.search(source)


def test_the_pin_allows_the_background_refresh_and_unrelated_renders():
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(
        "if(!S.session){await newSession();}\nvoid renderSessionList();"
    )
    assert not AWAITED_RENDER_AFTER_NEW_SESSION.search(
        "if(!S.session){await newSession();}\nconst activeSid=S.session.session_id;"
    )


def test_no_send_branch_mints_its_own_session_outside_the_ownership_helper():
    body = _send_body()
    assert not NO_SESSION_GUARD.search(body), (
        "every implicit create in send() must go through "
        "_ensureSessionForComposerAction() so a superseded create stops the branch"
    )
    assert body.count("await _ensureSessionForComposerAction(") >= 11


def _function_source(name: str) -> str:
    start = MESSAGES_JS.index(f"function {name}(")
    if MESSAGES_JS[max(0, start - 6) : start] == "async ":
        start -= 6
    return MESSAGES_JS[start : MESSAGES_JS.index("\n}\n", start) + 3]


@pytest.mark.parametrize(
    ("case", "metadata", "dispatchable"),
    [
        (
            "non-dispatchable",
            "{available: true, command: {name: 'agents', cli_only: false}}",
            "() => false",
        ),
        ("registry-unavailable", "{available: false}", None),
    ],
)
def test_abandoned_command_stops_when_its_implicit_create_is_superseded(
    case, metadata, dispatchable
):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the command-branch harness")
    assert node is not None

    script = textwrap.dedent(
        f"""
        let _sendInProgress=false;
        let _sendInProgressSid=null;
        let _pendingSelections=[];
        let _projectPickerOpenEpoch=1;
        const msg={{value:'/agents'}};
        const S={{session:null,pendingFiles:[],busy:false,messages:[],activeProfile:'default'}};
        const document={{querySelector(){{return null;}}}};
        const COMMANDS=[];
        const _AGENT_COMMANDS_RUN_ON_WEBUI=new Set();
        let renders=0;
        let createCalls=0;
        function $(id){{return id==='msg'?msg:{{value:'',focus(){{}}}};}}
        function _composerTextWithPendingSelections(){{return msg.value;}}
        function _flushSelectionBlocksToComposer(){{}}
        function _clearStaleBusyStateBeforeSend(){{}}
        function parseCommand(){{return {{name:'agents',args:''}};}}
        async function getAgentCommandMetadata(){{
          return {metadata};
        }}
        {'const _isWebuiDispatchableAgentCommand=' + dispatchable + ';' if dispatchable else ''}
        async function newSession(_flash, options){{
          createCalls+=1;
          // A newer navigation takes the pane while creation is in flight.
          S.session={{session_id:'session-b',profile:'default'}};
          return {{status:'superseded',session:S.session}};
        }}
        function cliOnlyCommandResponse(){{return 'cli-only explainer';}}
        function _setComposerAfterCliOnlyExplainer(){{}}
        function renderMessages(){{renders+=1;}}
        function autoResize(){{}}
        function hideCmdDropdown(){{}}
        function renderTray(){{}}
        function showToast(){{}}
        function _normalizeGoalContinuationId(id){{return String(id||'').trim();}}
        function _takeRestoredDraftGoalContinuationId(){{return '';}}
        {_function_source('_newSessionActionWasSuperseded')}
        {_function_source('_ensureSessionForComposerAction')}
        {_send_body()}

        (async()=>{{
          await send();
          process.stdout.write(JSON.stringify({{
            case:{case!r},
            createCalls,
            messages:S.messages.map(message=>({{role:message.role,content:message.content}})),
            text:msg.value,
            sid:S.session&&S.session.session_id,
            renders,
            sendInProgress:_sendInProgress,
          }}));
        }})().catch(error=>{{console.error(error&&error.stack||error);process.exit(1);}});
        """
    )
    handle = tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8")
    try:
        handle.write(script)
        handle.close()
        result = subprocess.run(
            [node, handle.name], cwd=ROOT, capture_output=True, text=True, timeout=30
        )
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)

    assert observed["createCalls"] == 1
    assert observed["messages"] == [], (
        "a superseded command must not append rows to the pane a newer "
        "navigation now owns"
    )
    assert observed["text"] == "/agents", (
        "the abandoned command stays recoverable in the composer"
    )
    assert observed["sid"] == "session-b"
    assert observed["renders"] == 0
    assert observed["sendInProgress"] is False
