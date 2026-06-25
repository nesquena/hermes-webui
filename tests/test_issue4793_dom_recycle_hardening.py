"""Regression coverage for issue #4793 DOM recycle hardening."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> dict:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout.strip())


def _js_prefix() -> str:
    return f"""
const fs = require('fs');
const src = fs.readFileSync({json.dumps(str(UI_JS_PATH))}, 'utf8');
function extractBetween(startMarker, endMarker, startFrom = 0) {{
  const start = src.indexOf(startMarker, startFrom);
  if (start < 0) throw new Error(startMarker + ' not found');
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error(endMarker + ' not found');
  return src.slice(start, end);
}}
function extractBody(startMarker, endMarker, startFrom = 0) {{
  const start = src.indexOf(startMarker, startFrom);
  if (start < 0) throw new Error(startMarker + ' not found');
  const end = src.indexOf(endMarker, start);
  if (end < 0) throw new Error(endMarker + ' not found');
  return src.slice(start + startMarker.length, end);
}}
"""


def test_recycled_assistant_turn_reuse_clears_live_anchor_attrs():
    script = _js_prefix() + """
const recycleChunk = extractBetween("if(!currentAssistantTurn){", "const seg=document.createElement('div');");
const resetListChunk = extractBetween("const _recycleResetAttrs=[", "let _scrollbarDragActive=false;")
  .replace("const _recycleResetAttrs=", "var _recycleResetAttrs=");
const removedAttrs = [];
const role = {
  replacement: null,
  set outerHTML(value) { this.replacement = value; },
  get outerHTML() { return this.replacement; },
};
const recycled = {
  dataset: { recycleKey: 'old-key', role: 'old-role', sessionId: 'old-session' },
  classList: { contains(name) { return name === 'assistant-turn'; } },
  removeAttribute(name) { removedAttrs.push(name); },
  querySelector(selector) { return selector === '.msg-role.assistant' ? role : null; },
};
eval(resetListChunk);
const _recycleStash = new Map([[7, recycled]]);
const _msgNodeRecycleEnabled = true;
let currentAssistantTurn = null;
const rawIdx = 7;
const blocks = { innerHTML: 'keep me' };
const _assistantTurnBlocks = () => blocks;
const inner = { appendChild() {} };
const tsTitle = 'Turn title';
const m = { _turnTps: 42 };
const S = { session: { session_id: 'sess-1' } };
const _assistantRoleHtml = (title, tps) => `role:${title}:${tps}`;
const isTpsDisplayEnabled = () => true;
const _formatTurnTps = value => `tps:${value}`;
eval(recycleChunk);
console.log(JSON.stringify({
  removedAttrs,
  resetAttrs: _recycleResetAttrs,
  blocksInnerHTML: blocks.innerHTML,
  roleHtml: role.replacement,
  currentAssistantTurnIsRecycled: currentAssistantTurn === recycled,
  dataset: {
    role: currentAssistantTurn.dataset.role,
    sessionId: currentAssistantTurn.dataset.sessionId,
    recycleKey: String(currentAssistantTurn.dataset.recycleKey),
  },
}));
"""
    result = _run_node(script)

    expected_attrs = {
        "data-transparent-turn-collapsed",
        "data-transparent-turn-toggle-bound",
        "data-anchor-scene-live-owner",
        "data-anchor-stream-id",
        "data-live-assistant-turn",
    }
    assert set(result["resetAttrs"]) == expected_attrs
    assert len(result["resetAttrs"]) == len(expected_attrs)
    assert set(result["removedAttrs"]) == expected_attrs
    assert len(result["removedAttrs"]) == len(expected_attrs)
    assert result["currentAssistantTurnIsRecycled"] is True
    assert result["dataset"] == {
        "role": "assistant",
        "sessionId": "sess-1",
        "recycleKey": "7",
    }


def test_recycled_assistant_turn_reuse_keeps_block_clear_and_role_refresh():
    script = _js_prefix() + """
const recycleChunk = extractBetween("if(!currentAssistantTurn){", "const seg=document.createElement('div');");
const resetListChunk = extractBetween("const _recycleResetAttrs=[", "let _scrollbarDragActive=false;")
  .replace("const _recycleResetAttrs=", "var _recycleResetAttrs=");
const removedAttrs = [];
const role = {
  replacement: null,
  set outerHTML(value) { this.replacement = value; },
  get outerHTML() { return this.replacement; },
};
const recycled = {
  dataset: { recycleKey: 'old-key' },
  classList: { contains(name) { return name === 'assistant-turn'; } },
  removeAttribute(name) { removedAttrs.push(name); },
  querySelector(selector) { return selector === '.msg-role.assistant' ? role : null; },
};
eval(resetListChunk);
const _recycleStash = new Map([[7, recycled]]);
const _msgNodeRecycleEnabled = true;
let currentAssistantTurn = null;
const rawIdx = 7;
const blocks = { innerHTML: 'keep me' };
const _assistantTurnBlocks = () => blocks;
const inner = { appendChild() {} };
const tsTitle = 'Turn title';
const m = { _turnTps: 42 };
const S = { session: { session_id: 'sess-1' } };
const _assistantRoleHtml = (title, tps) => `role:${title}:${tps}`;
const isTpsDisplayEnabled = () => true;
const _formatTurnTps = value => `tps:${value}`;
eval(recycleChunk);
console.log(JSON.stringify({
  blocksInnerHTML: blocks.innerHTML,
  roleHtml: role.replacement,
  currentAssistantTurnIsRecycled: currentAssistantTurn === recycled,
  dataset: {
    role: currentAssistantTurn.dataset.role,
    sessionId: currentAssistantTurn.dataset.sessionId,
    recycleKey: String(currentAssistantTurn.dataset.recycleKey),
  },
}));
"""
    result = _run_node(script)

    assert result["blocksInnerHTML"] == ""
    assert result["roleHtml"] == "role:Turn title:tps:42"
    assert result["currentAssistantTurnIsRecycled"] is True
    assert result["dataset"] == {
        "role": "assistant",
        "sessionId": "sess-1",
        "recycleKey": "7",
    }


def test_messages_scrollbar_drag_ignores_bubbled_child_pointerdown():
    script = _js_prefix() + """
const pointerdownBody = extractBody("el.addEventListener('pointerdown',(e)=>{", "},{passive:true});");
let _scrollbarDragActive = false;
const el = { clientWidth: 120 };
eval(`function pointerdownHandler(e){${pointerdownBody}}`);
pointerdownHandler({ target: { clientWidth: 120 }, offsetX: 999 });
const bubbledArmed = _scrollbarDragActive;
_scrollbarDragActive = false;
pointerdownHandler({ target: el, offsetX: 999 });
console.log(JSON.stringify({
  bubbledArmed,
  directArmed: _scrollbarDragActive,
}));
"""
    result = _run_node(script)

    assert result["bubbledArmed"] is False
    assert result["directArmed"] is True


def test_messages_scrollbar_drag_still_arms_for_direct_messages_press_and_rerenders_on_pointerup():
    script = _js_prefix() + """
const pointerdownBody = extractBody("el.addEventListener('pointerdown',(e)=>{", "},{passive:true});");
const pointerupBody = extractBody("window.addEventListener('pointerup',()=>{", "},{passive:true});");
const rerenders = [];
let _scrollbarDragActive = false;
const el = { clientWidth: 120 };
function _scheduleMessageVirtualizedRender(force) {
  rerenders.push(force);
}
eval(`function pointerdownHandler(e){${pointerdownBody}}`);
eval(`function pointerupHandler(){${pointerupBody}}`);
pointerdownHandler({ target: el, offsetX: 999 });
const armed = _scrollbarDragActive;
pointerupHandler();
console.log(JSON.stringify({
  armed,
  afterPointerup: _scrollbarDragActive,
  rerenders,
}));
"""
    result = _run_node(script)

    assert result["armed"] is True
    assert result["afterPointerup"] is False
    assert result["rerenders"] == [True]
