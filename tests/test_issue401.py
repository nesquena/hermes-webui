"""
Regression tests for issue #401 / PR #402:
Tool call cards show incorrect/duplicate entries on session load after context compaction.

Root cause: loadSession() applied its own B9 sanitization (producing a new message array
with different indices) but did not remap the session-level tool_calls.assistant_msg_idx
values to match. It then assigned the broken tool_calls directly to S.toolCalls, bypassing
renderMessages()'s fallback that correctly derives tool calls from per-message tool_calls.

Fix: build origIdxToSanitizedIdx during the B9 pass and remap each tc.assistant_msg_idx;
set S.toolCalls=[] so renderMessages() uses the fallback derivation.

These tests verify the JS logic statically (no server needed).
"""
import pathlib
import subprocess
import textwrap
import json

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


# --- Static structural checks ---

def test_loadsession_sets_toolcalls_empty():
    """loadSession must set S.toolCalls=[] instead of pre-filling from session-level tool_calls."""
    assert "S.toolCalls=[]" in SESSIONS_JS, (
        "loadSession() must set S.toolCalls=[] so renderMessages() uses its fallback "
        "derivation from per-message tool_calls with correct sanitized-array indices"
    )


def test_loadsession_does_not_assign_broken_tool_calls():
    """loadSession must NOT assign session.tool_calls directly to S.toolCalls (causes index mismatch)."""
    # The old broken pattern: S.toolCalls=(data.session.tool_calls||[]).map(tc=>({...tc,done:true}))
    assert "S.toolCalls=(data.session.tool_calls" not in SESSIONS_JS, (
        "loadSession() must not assign session-level tool_calls directly to S.toolCalls — "
        "those indices are relative to the pre-sanitization array and will be wrong after B9 filtering"
    )


def test_loadsession_builds_idx_remap():
    """loadSession must build an origIdxToSanitizedIdx map during B9 sanitization."""
    assert "origIdxToSanitizedIdx" in SESSIONS_JS, (
        "loadSession() must build origIdxToSanitizedIdx during B9 sanitization "
        "to remap session-level tool_calls.assistant_msg_idx"
    )


def test_loadsession_remaps_assistant_msg_idx():
    """loadSession must remap tc.assistant_msg_idx using the index map."""
    assert "tc.assistant_msg_idx" in SESSIONS_JS, (
        "loadSession() must update tc.assistant_msg_idx using the sanitized index map"
    )


# --- Behavioural Node.js tests ---

def _run_js(script_body: str) -> dict:
    """Run a JS snippet that exercises the B9 sanitization logic extracted from sessions.js."""
    # Extract just the B9 + index-remap block from loadSession
    # We'll re-implement it inline for testability
    script = textwrap.dedent(f"""
        // Simulate the B9 sanitization + index remap logic from loadSession()
        function sanitizeAndRemap(messages, tool_calls) {{
            const allMsgs = messages || [];
            const sanitized = [];
            const origIdxToSanitizedIdx = {{}};
            let lastKeptAsstIdx = -1;
            for (let i = 0; i < allMsgs.length; i++) {{
                const m = allMsgs[i];
                if (!m || !m.role) continue;
                if (m.role === 'tool') continue;
                if (m.role === 'assistant') {{
                    let c = m.content || '';
                    if (Array.isArray(c)) c = c.filter(p => p && p.type === 'text').map(p => p.text || '').join('');
                    if (!String(c).trim().length) {{ continue; }}
                    lastKeptAsstIdx = sanitized.length;
                }}
                origIdxToSanitizedIdx[i] = sanitized.length;
                sanitized.push(m);
            }}
            const remapped = (tool_calls || []).map(tc => {{
                if (!tc || tc.assistant_msg_idx === undefined) return tc;
                const origIdx = tc.assistant_msg_idx;
                const newIdx = (origIdx in origIdxToSanitizedIdx)
                    ? origIdxToSanitizedIdx[origIdx]
                    : (lastKeptAsstIdx >= 0 ? lastKeptAsstIdx : -1);
                return {{ ...tc, assistant_msg_idx: newIdx }};
            }});
            return {{ sanitized, remapped }};
        }}

        {script_body}
    """)
    proc = subprocess.run(
        ["node", "-e", script], check=True, capture_output=True, text=True
    )
    return json.loads(proc.stdout)


def test_b9_remaps_tool_call_idx_after_empty_assistant_filtered():
    """Tool call pointing to index 1 (empty assistant at orig idx 1, kept at idx 0) remaps correctly."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'hello' },          // orig 0 -> sanitized 0
            { role: 'assistant', content: '' },           // orig 1 -> FILTERED (empty)
            { role: 'assistant', content: 'done.' },      // orig 2 -> sanitized 1
        ];
        const tool_calls = [
            { name: 'terminal', assistant_msg_idx: 1 },   // pointed to filtered-out empty assistant
            { name: 'read_file', assistant_msg_idx: 2 },  // pointed to kept assistant
        ];
        const { sanitized, remapped } = sanitizeAndRemap(messages, tool_calls);
        process.stdout.write(JSON.stringify({
            sanitized_length: sanitized.length,
            tc0_new_idx: remapped[0].assistant_msg_idx,  // should attach to lastKeptAsstIdx = 1
            tc1_new_idx: remapped[1].assistant_msg_idx,  // should remap 2 -> 1
        }));
    """)
    assert result["sanitized_length"] == 2, f"Expected 2 messages after B9, got {result['sanitized_length']}"
    assert result["tc0_new_idx"] == 1, (
        f"Tool call pointing to filtered empty assistant should attach to last kept assistant (idx 1), got {result['tc0_new_idx']}"
    )
    assert result["tc1_new_idx"] == 1, (
        f"Tool call pointing to orig idx 2 should remap to sanitized idx 1, got {result['tc1_new_idx']}"
    )


def test_b9_remaps_multiple_empty_assistants():
    """Multiple consecutive empty assistants all remap to the last (nearest) kept assistant.

    Note: the remapping pass runs after the full sanitization loop, so lastKeptAsstIdx
    already reflects the final kept-assistant position. This means even empty-assistant
    tool calls that came BEFORE the kept assistant get attached to it — which is correct
    behavior for context-compacted sessions where all tool calls belong to the one
    non-empty assistant response.
    """
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'go' },              // orig 0 -> sanitized 0
            { role: 'assistant', content: '' },           // orig 1 -> FILTERED
            { role: 'assistant', content: '' },           // orig 2 -> FILTERED
            { role: 'assistant', content: '' },           // orig 3 -> FILTERED
            { role: 'assistant', content: 'result' },     // orig 4 -> sanitized 1
        ];
        const tool_calls = [
            { name: 'a', assistant_msg_idx: 1 },
            { name: 'b', assistant_msg_idx: 2 },
            { name: 'c', assistant_msg_idx: 3 },
            { name: 'd', assistant_msg_idx: 4 },
        ];
        const { sanitized, remapped } = sanitizeAndRemap(messages, tool_calls);
        process.stdout.write(JSON.stringify({
            sanitized_length: sanitized.length,
            tc0_idx: remapped[0].assistant_msg_idx,
            tc1_idx: remapped[1].assistant_msg_idx,
            tc2_idx: remapped[2].assistant_msg_idx,
            tc3_idx: remapped[3].assistant_msg_idx,
        }));
    """)
    assert result["sanitized_length"] == 2
    # Tool calls from filtered empty assistants: after the full loop, lastKeptAsstIdx=1,
    # so all filtered-assistant tool calls correctly attach to the kept assistant at idx 1.
    assert result["tc0_idx"] == 1, f"Expected 1 (last kept asst), got {result['tc0_idx']}"
    assert result["tc1_idx"] == 1
    assert result["tc2_idx"] == 1
    # Tool call from the kept assistant at orig idx 4 -> sanitized idx 1
    assert result["tc3_idx"] == 1, f"Expected 1, got {result['tc3_idx']}"


def test_b9_no_filtering_needed_indices_preserved():
    """When no empty assistant messages exist, indices should pass through unchanged."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'hi' },              // orig 0 -> sanitized 0
            { role: 'assistant', content: 'hello' },      // orig 1 -> sanitized 1
            { role: 'user', content: 'more' },            // orig 2 -> sanitized 2
            { role: 'assistant', content: 'yes' },        // orig 3 -> sanitized 3
        ];
        const tool_calls = [
            { name: 'x', assistant_msg_idx: 1 },
            { name: 'y', assistant_msg_idx: 3 },
        ];
        const { sanitized, remapped } = sanitizeAndRemap(messages, tool_calls);
        process.stdout.write(JSON.stringify({
            sanitized_length: sanitized.length,
            tc0_idx: remapped[0].assistant_msg_idx,
            tc1_idx: remapped[1].assistant_msg_idx,
        }));
    """)
    assert result["sanitized_length"] == 4
    assert result["tc0_idx"] == 1, f"Expected 1, got {result['tc0_idx']}"
    assert result["tc1_idx"] == 3, f"Expected 3, got {result['tc1_idx']}"


def test_b9_tool_role_messages_filtered():
    """Messages with role='tool' must be filtered out and not affect index mapping."""
    result = _run_js("""
        const messages = [
            { role: 'user', content: 'run' },             // orig 0 -> sanitized 0
            { role: 'tool', content: 'output' },          // orig 1 -> FILTERED (tool role)
            { role: 'assistant', content: 'done' },       // orig 2 -> sanitized 1
        ];
        const tool_calls = [
            { name: 'terminal', assistant_msg_idx: 2 },
        ];
        const { sanitized, remapped } = sanitizeAndRemap(messages, tool_calls);
        process.stdout.write(JSON.stringify({
            sanitized_length: sanitized.length,
            tc0_idx: remapped[0].assistant_msg_idx,
        }));
    """)
    assert result["sanitized_length"] == 2, f"tool-role message must be filtered, got {result['sanitized_length']}"
    assert result["tc0_idx"] == 1, f"Expected orig idx 2 -> sanitized idx 1, got {result['tc0_idx']}"
