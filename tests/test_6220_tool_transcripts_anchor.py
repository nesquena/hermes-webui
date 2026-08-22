"""
Regression tests for #6220: hydrate ID-linked historical tool transcripts into Anchors.

The new `_hydrateHistoricalToolTranscriptAnchorScenes()` function scans
settled messages for eligible assistant turns with ID-linked `tool_calls[]`
and matching `role: tool` result messages, then builds deterministic
`activity_scene_v1` scenes and attaches them as `_anchor_activity_scene`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"


def _ui_js() -> str:
    assert UI_JS_PATH.exists(), "static/ui.js not found"
    return UI_JS_PATH.read_text(encoding="utf-8")


def _run_node_script(script: str) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node executable is required for JavaScript behavior checks")
    try:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".js", encoding="utf-8", delete=False
        ) as handle:
            handle.write(script)
            script_path = handle.name
        try:
            result = subprocess.run(
                [node, script_path],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=15,
            )
        finally:
            Path(script_path).unlink(missing_ok=True)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            "node behavior check timed out"
            f"\nstdout:\n{exc.stdout or '<empty>'}"
            f"\nstderr:\n{exc.stderr or '<empty>'}"
        )
    if result.returncode:
        pytest.fail(
            "node behavior check failed"
            f"\nexit code: {result.returncode}"
            f"\nstdout:\n{result.stdout or '<empty>'}"
            f"\nstderr:\n{result.stderr or '<empty>'}"
        )
    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    return stdout_lines[-1] if stdout_lines else ""


# ---------------------------------------------------------------------------
# Source-level assertions
# ---------------------------------------------------------------------------

def test_6220_hydration_function_exists():
    """The _hydrateHistoricalToolTranscriptAnchorScenes function must exist in ui.js."""
    src = _ui_js()
    assert (
        "function _hydrateHistoricalToolTranscriptAnchorScenes()" in src
    ), "Hydration function not found"
    assert (
        "activity_scene_v1" in src
    ), "scene version marker not found in function body"


def test_6220_hydration_called_in_render_messages():
    """renderMessages must call the hydration function early, before the gate."""
    src = _ui_js()

    # The call site must appear in renderMessages.
    idx = src.index("function renderMessages(options)")
    fn_body_start = src.index("{", idx)
    # Find the matching closing brace of renderMessages
    depth = 1
    pos = fn_body_start + 1
    while pos < len(src) and depth > 0:
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
        pos += 1
    render_body = src[fn_body_start + 1 : pos - 1]

    assert (
        "_hydrateHistoricalToolTranscriptAnchorScenes" in render_body
    ), "Hydration function not called inside renderMessages"


# ---------------------------------------------------------------------------
# Behavioral tests via node
# ---------------------------------------------------------------------------

_HYDRATION_FN_START = "function _hydrateHistoricalToolTranscriptAnchorScenes()"


def _extract_hydration_fn() -> str:
    """Extract the hydration function body from ui.js."""
    src = _ui_js()
    idx = src.index(_HYDRATION_FN_START)
    paren = src.index("(", idx)
    brace = src.index("{", paren)
    depth = 1
    pos = brace + 1
    while pos < len(src) and depth > 0:
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
        pos += 1
    return src[idx:pos]


def test_6220_eligible_transcript_hydrates_anchor_scene():
    """Eligible assistant with fully-linked tool_calls + role:tool gets a scene."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        // Stubs for helpers used by the hydration function.
        function _cliToolResultSnippet(raw) {{
            return String(raw || '').slice(0, 4000);
        }}
        function _cliPatchSnippetFromArgs(name, args) {{
            return '';
        }}
        function _cliToolCardSnippet(result, patch) {{
            return result || patch || '';
        }}
        function _toolArgsSnapshot(args) {{
            if (!args || typeof args !== 'object' || Array.isArray(args)) return {{}};
            const keys = Object.keys(args).slice(0, 6);
            const out = {{}};
            keys.forEach(k => {{ out[k] = String(args[k]); }});
            return out;
        }}
        function msgContent(m) {{
            return typeof m.content === 'string' ? m.content : '';
        }}

        // Global S with messages simulating an OpenAI-compatible transcript.
        var S = {{
            messages: [
                {{ role: 'user', content: 'Run git status' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_abc123',
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"git status"}}' }}
                        }}
                    ]
                }},
                {{
                    role: 'tool',
                    tool_call_id: 'call_abc123',
                    content: 'On branch main\\\\nnothing to commit'
                }},
                {{
                    role: 'assistant',
                    content: 'Your repo is clean.'
                }}
            ]
        }};

        {fn_source}

        _hydrateHistoricalToolTranscriptAnchorScenes();

        // The turn-final assistant (index 3) should now have a scene.
        var result = {{
            index0_scene: S.messages[0]._anchor_activity_scene || null,
            index1_scene: S.messages[1]._anchor_activity_scene || null,
            index2_scene: S.messages[2]._anchor_activity_scene || null,
            index3_scene: S.messages[3]._anchor_activity_scene || null,
        }};
        console.log(JSON.stringify(result));
        """
    )

    result = json.loads(_run_node_script(script))

    assert result["index0_scene"] is None, "user message should not get a scene"
    assert result["index1_scene"] is None, "tool-call-bearing assistant should NOT own the scene"
    assert result["index2_scene"] is None, "tool result should not get a scene"
    assert result["index3_scene"] is not None, "turn-final assistant should own the scene"

    scene = result["index3_scene"]
    assert scene["version"] == "activity_scene_v1"
    assert len(scene["activity_rows"]) == 1
    row = scene["activity_rows"][0]
    assert row["role"] == "tool"
    assert row["tool_call_id"] == "call_abc123"
    assert row["tool"]["name"] == "terminal"
    assert row["tool"]["done"] is True
    assert "git status" in json.dumps(row["tool"]["args"])


def test_6220_missing_tool_call_id_fails_closed():
    """A tool_call without an id must NOT hydrate a scene."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        // Message without tool_call id.
        var S = {{
            messages: [
                {{ role: 'user', content: 'hello' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            // no id field
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"ls"}}' }}
                        }}
                    ]
                }},
                {{ role: 'tool', tool_call_id: 'call_xyz', content: 'result' }},
                {{ role: 'assistant', content: 'done' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();
        console.log(JSON.stringify({{ scene: S.messages[3]._anchor_activity_scene || null }}));
        """
    )

    result = json.loads(_run_node_script(script))
    assert result["scene"] is None, "missing tool_call id must fail closed — no scene"


def test_6220_unmatched_tool_result_fails_closed():
    """A tool_call with an id that does not match any role:tool must NOT hydrate."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        var S = {{
            messages: [
                {{ role: 'user', content: 'hello' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_abc',
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"ls"}}' }}
                        }}
                    ]
                }},
                // No matching role:tool for call_abc
                {{ role: 'assistant', content: 'done' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();
        console.log(JSON.stringify({{ scene: S.messages[2]._anchor_activity_scene || null }}));
        """
    )

    result = json.loads(_run_node_script(script))
    assert result["scene"] is None, "unmatched result must fail closed — no scene"


def test_6220_cross_turn_boundary_fails_closed():
    """Tool result after next user message must NOT hydrate."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        var S = {{
            messages: [
                {{ role: 'user', content: 'Run ls' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_abc',
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"ls"}}' }}
                        }}
                    ]
                }},
                {{ role: 'user', content: 'Now run pwd' }},
                {{ role: 'tool', tool_call_id: 'call_abc', content: 'result here' }},
                {{ role: 'assistant', content: 'done' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();
        console.log(JSON.stringify({{ scene: S.messages[4]._anchor_activity_scene || null }}));
        """
    )

    result = json.loads(_run_node_script(script))
    assert result["scene"] is None, "cross-turn result must fail closed — no scene"


def test_6220_duplicate_tool_call_id_fails_closed():
    """Duplicate tool_call ids within a message must NOT hydrate."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        var S = {{
            messages: [
                {{ role: 'user', content: 'hello' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_dup',
                            type: 'function',
                            function: {{ name: 'tool_a', arguments: '{{}}' }}
                        }},
                        {{
                            id: 'call_dup',
                            type: 'function',
                            function: {{ name: 'tool_b', arguments: '{{}}' }}
                        }}
                    ]
                }},
                {{ role: 'tool', tool_call_id: 'call_dup', content: 'result' }},
                {{ role: 'assistant', content: 'done' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();
        console.log(JSON.stringify({{ scene: S.messages[3]._anchor_activity_scene || null }}));
        """
    )

    result = json.loads(_run_node_script(script))
    assert result["scene"] is None, "duplicate ids must fail closed — no scene"


def test_6220_existing_anchor_scene_not_overwritten():
    """A message with an existing _anchor_activity_scene must remain untouched."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        var existingScene = Object.freeze({{
            version: 'activity_scene_v1',
            mode: 'compact_worklog',
            activity_rows: Object.freeze([]),
            final_answer: 'existing answer',
        }});

        var S = {{
            messages: [
                {{ role: 'user', content: 'hello' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_abc',
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"ls"}}' }}
                        }}
                    ],
                    _anchor_activity_scene: existingScene
                }},
                {{ role: 'tool', tool_call_id: 'call_abc', content: 'result' }},
                {{ role: 'assistant', content: 'done' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();
        console.log(JSON.stringify({{
            index1_scene: S.messages[1]._anchor_activity_scene || null,
            index3_scene: S.messages[3]._anchor_activity_scene || null,
        }}));
        """
    )

    result = json.loads(_run_node_script(script))

    assert result["index1_scene"] is not None
    assert result["index1_scene"]["final_answer"] == "existing answer", (
        "existing scene must not be overwritten"
    )
    # The turn-final message should NOT get a scene either — the turn was
    # already anchor-owned (the hydration checks the first eligible assistant
    # and skips it because it already has a scene).
    assert result["index3_scene"] is None, (
        "turn that already has an anchor scene should not be re-hydrated"
    )


def test_6220_multiple_tool_calls_hydrate_in_order():
    """Multiple linked tool_calls produce multiple activity rows in order."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        var S = {{
            messages: [
                {{ role: 'user', content: 'search and ls' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_A',
                            type: 'function',
                            function: {{ name: 'web_search', arguments: '{{"query":"test"}}' }}
                        }},
                        {{
                            id: 'call_B',
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"ls -la"}}' }}
                        }}
                    ]
                }},
                {{ role: 'tool', tool_call_id: 'call_A', content: 'search results here' }},
                {{ role: 'tool', tool_call_id: 'call_B', content: 'total 42\\\\ndrwxr-xr-x ...' }},
                {{ role: 'assistant', content: 'Found results and listed files.' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();
        var scene = S.messages[4]._anchor_activity_scene || null;
        console.log(JSON.stringify({{ scene: scene }}));
        """
    )

    result = json.loads(_run_node_script(script))

    assert result["scene"] is not None
    rows = result["scene"]["activity_rows"]
    assert len(rows) == 2
    assert rows[0]["tool"]["name"] == "web_search"
    assert rows[0]["tool_call_id"] == "call_A"
    assert rows[1]["tool"]["name"] == "terminal"
    assert rows[1]["tool_call_id"] == "call_B"
    assert result["scene"]["final_answer"] == "Found results and listed files."


def test_6220_hydration_idempotent():
    """Calling the function twice must not produce different results."""
    fn_source = _extract_hydration_fn()

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        var S = {{
            messages: [
                {{ role: 'user', content: 'run ls' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_abc',
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"ls"}}' }}
                        }}
                    ]
                }},
                {{ role: 'tool', tool_call_id: 'call_abc', content: 'file1 file2' }},
                {{ role: 'assistant', content: 'Files listed.' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();
        var scene1 = JSON.stringify(S.messages[3]._anchor_activity_scene || null);
        // Rotate the tool result index artificially (same data, different position)
        // and call again — should be a no-op because scene is already attached.
        _hydrateHistoricalToolTranscriptAnchorScenes();
        var scene2 = JSON.stringify(S.messages[3]._anchor_activity_scene || null);
        console.log(JSON.stringify({{ same: scene1 === scene2, scene1: scene1, scene2: scene2 }}));
        """
    )

    result = json.loads(_run_node_script(script))
    assert result["same"] is True, "second call must not change the scene"


def test_6220_legacy_fallback_skips_anchor_owned_turns():
    """
    _legacySettledFallbackHasToolMetadata must return false when a message
    has _anchor_activity_scene (including one hydrated by our new function).
    """
    fn_source = _extract_hydration_fn()
    ui_js = _ui_js()

    # Extract _legacySettledFallbackHasToolMetadata
    legacy_start = ui_js.index("function _legacySettledFallbackHasToolMetadata(")
    legacy_paren = ui_js.index("(", legacy_start)
    legacy_brace = ui_js.index("{", legacy_paren)
    depth = 1
    pos = legacy_brace + 1
    while pos < len(ui_js) and depth > 0:
        if ui_js[pos] == "{":
            depth += 1
        elif ui_js[pos] == "}":
            depth -= 1
        pos += 1
    legacy_fn = ui_js[legacy_start:pos]

    script = textwrap.dedent(
        f"""
        function _cliToolResultSnippet(raw) {{ return String(raw || '').slice(0, 4000); }}
        function _cliPatchSnippetFromArgs(name, args) {{ return ''; }}
        function _cliToolCardSnippet(result, patch) {{ return result || patch || ''; }}
        function _toolArgsSnapshot(args) {{ return args || {{}}; }}
        function msgContent(m) {{ return typeof m.content === 'string' ? m.content : ''; }}

        var S = {{
            messages: [
                {{ role: 'user', content: 'run ls' }},
                {{
                    role: 'assistant',
                    content: null,
                    tool_calls: [
                        {{
                            id: 'call_abc',
                            type: 'function',
                            function: {{ name: 'terminal', arguments: '{{"command":"ls"}}' }}
                        }}
                    ]
                }},
                {{ role: 'tool', tool_call_id: 'call_abc', content: 'result' }},
                {{ role: 'assistant', content: 'done' }}
            ]
        }};

        {fn_source}
        _hydrateHistoricalToolTranscriptAnchorScenes();

        {legacy_fn}

        // Before hydration, the first assistant should have been eligible.
        // After hydration, the turn-final assistant has a scene, making the
        // legacy check return false.
        var finalMsg = S.messages[3];
        var hasToolMetadata = _legacySettledFallbackHasToolMetadata(finalMsg);
        console.log(JSON.stringify({{
            hasToolMetadata: hasToolMetadata,
            hasScene: !!finalMsg._anchor_activity_scene,
            finalAnswer: finalMsg._anchor_activity_scene ? finalMsg._anchor_activity_scene.final_answer : null,
        }}));
        """
    )

    result = json.loads(_run_node_script(script))
    assert result["hasScene"] is True, "turn-final message should have a scene"
    assert result["finalAnswer"] == "done", "final answer should match content"
    assert result["hasToolMetadata"] is False, (
        "_legacySettledFallbackHasToolMetadata must return false for anchor-owned turn"
    )
