"""Regression tests for issue #3397 Transparent Stream settled prose segments."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const transparent = process.argv[3] === '1';
const message = JSON.parse(process.argv[4]);

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

function isTransparentStream() { return transparent; }
eval(extractFunc('_transparentStreamOrderedParts'));
process.stdout.write(JSON.stringify(_transparentStreamOrderedParts(message)));
"""


def _run_helper(message: dict, transparent: bool) -> object:
    if NODE is None:
        pytest.skip("node not on PATH")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(_DRIVER_SRC)
        script_path = Path(handle.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path), str(UI_JS_PATH), "1" if transparent else "0", json.dumps(message)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_transparent_stream_ordered_parts_preserve_text_tool_text_sequence():
    message = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Let me search first."},
            {"type": "tool_use", "id": "toolu_1", "name": "grep", "input": {"pattern": "TODO"}},
            {"type": "text", "text": "Found it, here's the fix."},
        ],
    }

    ordered = _run_helper(message, transparent=True)

    assert [part["kind"] for part in ordered] == ["text", "tool", "text"]
    assert ordered[0]["text"] == "Let me search first."
    assert ordered[1]["toolUseId"] == "toolu_1"
    assert ordered[2]["text"] == "Found it, here's the fix."


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_compact_mode_keeps_ordered_parts_helper_off():
    message = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Before"},
            {"type": "tool_use", "id": "toolu_1", "name": "grep", "input": {}},
            {"type": "text", "text": "After"},
        ],
    }

    assert _run_helper(message, transparent=False) is None


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_anchor_scene_messages_keep_dedicated_transparent_renderer():
    message = {
        "role": "assistant",
        "_anchor_activity_scene": {"version": "activity_scene_v1"},
        "content": [
            {"type": "text", "text": "Before"},
            {"type": "tool_use", "id": "toolu_1", "name": "grep", "input": {}},
            {"type": "text", "text": "After"},
        ],
    }

    assert _run_helper(message, transparent=True) is None


def test_render_messages_wires_ordered_parts_into_transparent_stream_and_skips_duplicate_tool_rows():
    assert "let orderedTransparentParts=_transparentStreamOrderedParts(m);" in UI_JS
    assert "if(message._anchor_activity_scene) return null;" in UI_JS
    assert "const partDisplayText=_transparentOrderedDisplayText(part.text);" in UI_JS
    assert "const partBodyHtml=_getCachedRender(partDisplayText,false);" in UI_JS
    assert "const transparentOrderedToolIds=new Set();" in UI_JS
    assert "const toolCall=_transparentOrderedToolCall(part, rawIdx, transparentOrderedToolCallsByTid, transparentToolResultsByTid, transparentPersistedSnippetByTid);" in UI_JS
    assert "if(part.toolUseId) transparentOrderedToolIds.add(part.toolUseId);" in UI_JS
    assert "if(tid&&transparentOrderedToolIds.has(tid)) continue;" in UI_JS


def test_ordered_tool_cards_refresh_from_settled_results_even_after_live_preview():
    assert "const liveSnip=(resultsByTid&&resultsByTid[tid])||(persistedByTid&&persistedByTid[tid])||'';" in UI_JS
    assert "(next.snippet===undefined||next.snippet===null||next.snippet==='')" not in UI_JS


def test_ordered_tool_card_falls_back_to_persisted_snippet_on_cold_load():
    # #4932 gate (#4927 parity): on a cold/paginated load the S.messages
    # tool_result join misses; the ordered inline card must still recover the
    # body from the persisted session.tool_calls snippet (persistedByTid),
    # otherwise its card renders empty AND suppresses the post-loop derived card
    # that would have recovered it.
    if NODE is None:
        import pytest
        pytest.skip("node not on PATH")
    driver = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
function grab(n){const re=new RegExp('function '+n+'\\([^]*?\\n}','m');const m=src.match(re);if(!m)throw new Error('not found '+n);return m[0];}
global._cliPatchSnippetFromArgs=()=> '';
global._cliToolCardSnippet=(a,b)=> a||b||'';
global._cliToolCardHasDiffSnippet=()=> false;
global._toolArgsSnapshot=(a)=> a||{};
eval(grab('_transparentOrderedToolCall'));
const part={kind:'tool', toolUseId:'call_x', name:'terminal', input:{command:'ls'}};
// resultsByTid MISSES (cold load), persistedByTid HAS the snippet.
const out=_transparentOrderedToolCall(part, 3, new Map(), {}, {call_x:'PERSISTED_OUTPUT_42'});
process.stdout.write(JSON.stringify({snippet: out.snippet}));
"""
    import tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(driver)
        path = fh.name
    try:
        result = subprocess.run([NODE, path, str(UI_JS_PATH)], capture_output=True, text=True, timeout=30)
    finally:
        os.unlink(path)
    assert result.returncode == 0, result.stderr
    import json as _json
    assert _json.loads(result.stdout)["snippet"] == "PERSISTED_OUTPUT_42", result.stdout
