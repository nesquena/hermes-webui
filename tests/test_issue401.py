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


def test_loadsession_preserves_tool_rows():
    """Reload must keep tool rows in S.messages so snippets can be reconstructed."""
    assert "if (m.role === 'tool') continue;" not in SESSIONS_JS, (
        "loadSession() must not drop role='tool' messages; renderMessages() hides them "
        "visually, but it still needs them for snippet reconstruction"
    )


def test_loadsession_uses_session_toolcalls_as_fallback_but_keeps_recovered_journal_cards():
    """Session summaries are the fallback, but journal-recovered cards must survive mixed histories.

    If a long session already has earlier assistant/tool metadata, reload still needs
    the session-level `_recovered_from_run_journal` tool summaries for a later
    interrupted turn whose recovered assistant anchor has no inline tool_calls.
    """
    assert "_recovered_from_run_journal" in SESSIONS_JS
    assert "data.session.tool_calls" in SESSIONS_JS
    assert "recoveredSessionToolCalls" in SESSIONS_JS
    assert "const toolCalls = (!hasMessageToolMetadata && normalized.length) ? normalized : recoveredSessionToolCalls;" in SESSIONS_JS or "const toolCalls=(!hasMessageToolMetadata&&normalized.length)?normalized:recoveredSessionToolCalls;" in SESSIONS_JS


def test_rendermessages_treats_openai_toolcall_assistants_as_visible():
    """OpenAI assistant rows with empty content but tool_calls must stay anchorable."""
    assert "const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;" in UI_JS
    assert "if(hasTc||hasTu||_messageHasReasoningPayload(m)) return true;" in UI_JS


def _run_js(script_body: str) -> dict:
    script = textwrap.dedent(f"""
        function loadSessionShape(messages, sessionToolCalls) {{
            const filtered = (messages || []).filter(m => m && m.role);
            const hasMessageToolMetadata = filtered.some(m => {{
                if (!m || m.role !== 'assistant') return false;
                const hasTc = Array.isArray(m.tool_calls) && m.tool_calls.length > 0;
                const hasPartialTc = Array.isArray(m._partial_tool_calls) && m._partial_tool_calls.length > 0;
                const hasTu = Array.isArray(m.content) && m.content.some(p => p && p.type === 'tool_use');
                return hasTc || hasPartialTc || hasTu;
            }});
            const normalized = (sessionToolCalls || []).map(tc => ({{ ...tc, done: true }}));
            const recoveredSessionToolCalls = normalized.filter(tc => tc && tc._recovered_from_run_journal);
            const toolCalls = (!hasMessageToolMetadata && normalized.length)
                ? normalized
                : recoveredSessionToolCalls;
            return {{ filtered, hasMessageToolMetadata, recoveredSessionToolCalls, toolCalls }};
        }}

        {script_body}
    """)
    proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    return json.loads(proc.stdout)


def test_reload_keeps_empty_assistant_toolcall_anchor():
    """OpenAI-style assistant {content:'', tool_calls:[...]} must survive reload."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'list files' },
            {
                role: 'assistant',
                content: '',
                tool_calls: [{ id: 'call-1', function: { name: 'terminal', arguments: '{}' } }]
            },
            { role: 'tool', tool_call_id: 'call-1', content: '{"output":"ok"}' },
            { role: 'assistant', content: 'Done.' }
        ];
        const loaded = loadSessionShape(messages, [{ name: 'terminal', assistant_msg_idx: 1 }]);
        process.stdout.write(JSON.stringify({
            filtered_len: loaded.filtered.length,
            has_metadata: loaded.hasMessageToolMetadata,
            fallback_len: loaded.toolCalls.length,
            assistant_tool_idx: loaded.filtered.findIndex(m => m.role === 'assistant' && m.tool_calls),
            tool_idx: loaded.filtered.findIndex(m => m.role === 'tool')
        }));
    """)
    assert result["filtered_len"] == 4
    assert result["has_metadata"] is True
    assert result["fallback_len"] == 0
    assert result["assistant_tool_idx"] == 1
    assert result["tool_idx"] == 2


def test_reload_uses_session_summary_when_messages_have_no_tool_metadata():
    """Older sessions should still render from session.tool_calls on reload."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'build site' },
            { role: 'assistant', content: 'Starting.' },
            { role: 'tool', content: '{"bytes_written": 4955}' },
            { role: 'assistant', content: '' }
        ];
        const sessionToolCalls = [
            { name: 'write_file', assistant_msg_idx: 1, snippet: 'bytes_written', tid: '' }
        ];
        const loaded = loadSessionShape(messages, sessionToolCalls);
        process.stdout.write(JSON.stringify({
            has_metadata: loaded.hasMessageToolMetadata,
            fallback_len: loaded.toolCalls.length,
            done_flag: loaded.toolCalls[0] && loaded.toolCalls[0].done === true
        }));
    """)
    assert result["has_metadata"] is False
    assert result["fallback_len"] == 1
    assert result["done_flag"] is True


def test_reload_keeps_recovered_journal_toolcalls_even_when_older_messages_have_tool_metadata():
    """Mixed sessions must not drop recovered journal cards for the interrupted tail turn."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'old request' },
            { role: 'assistant', content: '', tool_calls: [{ id: 'call-1', function: { name: 'terminal', arguments: '{}' } }] },
            { role: 'tool', tool_call_id: 'call-1', content: '{"output":"ok"}' },
            { role: 'assistant', content: 'old complete answer' },
            { role: 'user', content: 'new request after restart' },
            { role: 'assistant', content: 'Recovered partial text only', _recovered_from_run_journal: true }
        ];
        const sessionToolCalls = [
            {
                name: 'terminal',
                assistant_msg_idx: 5,
                snippet: 'git status --short',
                tid: 'journal-72',
                _recovered_from_run_journal: true,
            }
        ];
        const loaded = loadSessionShape(messages, sessionToolCalls);
        process.stdout.write(JSON.stringify({
            has_metadata: loaded.hasMessageToolMetadata,
            tool_len: loaded.toolCalls.length,
            recovered_only: loaded.toolCalls.length === 1 && loaded.toolCalls[0]._recovered_from_run_journal === true,
            recovered_anchor: loaded.toolCalls[0] && loaded.toolCalls[0].assistant_msg_idx
        }));
    """)
    assert result["has_metadata"] is True
    assert result["tool_len"] == 1
    assert result["recovered_only"] is True
    assert result["recovered_anchor"] == 5


def test_rendermessages_fallback_derives_cards_from_partial_tool_calls():
    """Cancelled/recovered turns with only _partial_tool_calls still need settled tool cards."""
    assert "const hasPartialToolCalls=Array.isArray(m._partial_tool_calls)&&m._partial_tool_calls.length>0;" in UI_JS
    assert "if(hasTopLevelToolCalls||hasContentToolUse||hasPartialToolCalls)" in UI_JS
    assert "(m._partial_tool_calls||[]).forEach(tc=>{" in UI_JS
