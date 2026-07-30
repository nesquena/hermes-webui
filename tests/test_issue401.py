"""
Regression tests for tool-card persistence on session reload.

The older loadSession() path rewrote message history on the client:
- dropped role='tool' rows
- dropped empty assistant rows even when they carried tool_calls
- then ignored session.tool_calls on reload

That broke both durable logging and page refresh for valid tool runs.
"""
import json
import pathlib
import subprocess
import textwrap

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
_SYNC_TOOL_CALLS_FN_NAME = (
    "function _syncToolCallsForLoadedMessages(messages, sessionToolCalls){"
)
_SYNC_TOOL_CALLS_FN_END_MARKER = "async function _ensureMessagesLoaded"
_SYNC_TOOL_CALLS_FN_START = SESSIONS_JS.find(_SYNC_TOOL_CALLS_FN_NAME)
_SYNC_TOOL_CALLS_FN_END = SESSIONS_JS.find(
    _SYNC_TOOL_CALLS_FN_END_MARKER,
    _SYNC_TOOL_CALLS_FN_START,
)
assert _SYNC_TOOL_CALLS_FN_START >= 0
assert _SYNC_TOOL_CALLS_FN_END > _SYNC_TOOL_CALLS_FN_START
_SYNC_TOOL_CALLS_FN = SESSIONS_JS[_SYNC_TOOL_CALLS_FN_START:_SYNC_TOOL_CALLS_FN_END]


def _run_sync_tool_calls(messages: list, session_tool_calls: list) -> list:
    assert _SYNC_TOOL_CALLS_FN_NAME in SESSIONS_JS
    assert _SYNC_TOOL_CALLS_FN.strip()
    script = textwrap.dedent(
        f"""
        const S = {{}};
        S.toolCalls = [];
        {_SYNC_TOOL_CALLS_FN}
        const messages = {json.dumps(messages)};
        const sessionToolCalls = {json.dumps(session_tool_calls)};
        _syncToolCallsForLoadedMessages(messages, sessionToolCalls);
        process.stdout.write(JSON.stringify(S.toolCalls));
        """
    )
    proc = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def test_loadsession_preserves_tool_rows():
    """Reload must keep tool rows in S.messages so snippets can be reconstructed."""
    assert "if (m.role === 'tool') continue;" not in SESSIONS_JS, (
        "loadSession() must not drop role='tool' messages; renderMessages() hides them "
        "visually, but it still needs them for snippet reconstruction"
    )


def test_loadsession_uses_session_toolcalls_only_as_fallback():
    """Session summaries are the fallback, not the primary reload source."""
    assert "function _syncToolCallsForLoadedMessages(messages, sessionToolCalls)" in SESSIONS_JS
    assert "if(!hasMessageToolMetadata&&Array.isArray(sessionToolCalls)&&sessionToolCalls.length)" in SESSIONS_JS
    assert "windowOffset" not in SESSIONS_JS
    assert "copy.assistant_msg_idx=idx-offset;" not in SESSIONS_JS
    assert "S.toolCalls=[];" in SESSIONS_JS


def test_rendermessages_treats_openai_toolcall_assistants_as_visible():
    """OpenAI assistant rows with empty content but tool_calls must stay anchorable."""
    assert "function _messageIsRenderable(m)" in UI_JS
    assert "const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;" in UI_JS
    assert "hasTc||hasTu||hasPartialTc||_messageHasReasoningPayload(m)||_assistantMessageHasVisibleContent(m)" in UI_JS


def test_rendermessages_treats_partial_toolcall_assistants_as_visible():
    """Assistant rows carrying `_partial_tool_calls` must stay anchorable."""
    assert "function _messageIsRenderable(m)" in UI_JS
    assert "const hasPartialTc=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;" in UI_JS
    assert "hasTc||hasTu||hasPartialTc||_messageHasReasoningPayload(m)||_assistantMessageHasVisibleContent(m)" in UI_JS


def test_rendermessages_rebuilds_tool_cards_from_partial_tool_calls():
    """Fallback reconstruction should include private `_partial_tool_calls` rows."""
    assert "function _legacySettledFallbackHasToolMetadata(message)" in UI_JS
    assert "Array.isArray(message._partial_tool_calls)&&message._partial_tool_calls.length>0" in UI_JS
    assert "if(_legacySettledFallbackHasToolMetadata(m)) fallbackToolSources.push({m,rawIdx});" in UI_JS
    assert "if(Array.isArray(m._partial_tool_calls)){" in UI_JS
    assert "tc.snippet||tc.result||tc.output||tc.preview" in UI_JS
    assert "done:true" in UI_JS


def test_reload_keeps_empty_assistant_toolcall_anchor():
    """OpenAI-style assistant {content:'', tool_calls:[...]} must survive reload."""
    messages = [
        {"role": "user", "content": "list files"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": '{"output":"ok"}'},
        {"role": "assistant", "content": "Done."},
    ]
    filtered = [m for m in messages if m and m.get("role")]
    tool_calls = _run_sync_tool_calls(
        messages,
        [{"name": "terminal", "assistant_msg_idx": 1}],
    )
    assert len(filtered) == 4
    assert tool_calls == []
    assert filtered[1]["role"] == "assistant" and filtered[1].get("tool_calls")
    assert filtered[2]["role"] == "tool"


def test_reload_keeps_empty_assistant_partial_toolcall_anchor():
    """Partial tool-call rows with empty content must survive reload."""
    messages = [
        {"role": "user", "content": "open log"},
        {
            "role": "assistant",
            "content": "",
            "_partial_tool_calls": [{"name": "read_file", "args": {"path": "README.md"}}],
        },
        {"role": "assistant", "content": "Done."},
    ]
    filtered = [m for m in messages if m and m.get("role")]
    tool_calls = _run_sync_tool_calls(
        messages,
        [{"name": "write_file", "assistant_msg_idx": 1}],
    )
    assert len(filtered) == 3
    assert tool_calls == []
    assert filtered[1]["role"] == "assistant"
    assert isinstance(filtered[1].get("_partial_tool_calls"), list)
    assert filtered[1]["_partial_tool_calls"][0]["name"] == "read_file"


def test_reload_uses_session_summary_when_messages_have_no_tool_metadata():
    """Older sessions should still render from session.tool_calls on reload."""
    messages = [
        {"role": "user", "content": "build site"},
        {"role": "assistant", "content": "Starting."},
        {"role": "tool", "content": '{"bytes_written": 4955}'},
        {"role": "assistant", "content": ""},
    ]
    tool_calls = _run_sync_tool_calls(
        messages,
        [{"name": "write_file", "assistant_msg_idx": 1, "snippet": "bytes_written", "tid": ""}],
    )
    assert len(tool_calls) == 1
    assert tool_calls[0]["done"] is True


def test_rebased_legacy_toolcalls_stay_distinct_in_paged_window():
    """Backend-rebased legacy tool calls must not be rebased a second time."""
    messages = [
        {"role": "assistant", "content": "first legacy page row"},
        {"role": "assistant", "content": "second legacy page row"},
    ]
    session_tool_calls = [
        {"name": "first_tool", "assistant_msg_idx": 0, "snippet": "first"},
        {"name": "second_tool", "assistant_msg_idx": 1, "snippet": "second"},
    ]
    tool_calls = _run_sync_tool_calls(messages, session_tool_calls)
    assert [tc["assistant_msg_idx"] for tc in tool_calls] == [0, 1]
    assert len({tc["assistant_msg_idx"] for tc in tool_calls}) == 2
