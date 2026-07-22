import pathlib
import re
import json
import shutil
import subprocess

import pytest


STYLE_CSS = (pathlib.Path(__file__).parent.parent / "static" / "style.css").read_text(encoding="utf-8")
UI_JS = (pathlib.Path(__file__).parent.parent / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (pathlib.Path(__file__).parent.parent / "static" / "messages.js").read_text(encoding="utf-8")
COMPACT_CSS = re.sub(r"\s+", "", STYLE_CSS)


def test_tool_card_toggle_uses_transformable_layout_and_transition():
    assert ".tool-card-toggle,.tl-caret{" in COMPACT_CSS
    assert "display:inline-flex" in COMPACT_CSS
    assert "transition:transform.18sease" in COMPACT_CSS


def test_tool_card_detail_uses_transitionable_collapsed_state():
    assert ".tool-card-detail,.tl-detail{display:block;max-height:0;opacity:0;overflow:hidden;" in COMPACT_CSS
    assert re.search(
        r"\.tool-card\.open\s+\.tool-card-detail,\s*\.tl\.open\s+\.tl-detail\s*\{[^}]*max-height:\s*320px;[^}]*opacity:\s*1;",
        STYLE_CSS,
    )
    # Open state must set overflow to auto so the inner <pre> scroll is not clipped (#1170).
    assert re.search(
        r"\.tool-card\.open\s+\.tool-card-detail,\s*\.tl\.open\s+\.tl-detail\s*\{[^}]*overflow:\s*auto;",
        STYLE_CSS,
    )


def test_thinking_card_toggle_and_body_use_animation_friendly_state():
    assert ".thinking-card-btn-row{margin-left:auto;display:inline-flex;align-items:center;gap:6px;" in COMPACT_CSS
    assert ".thinking-card-toggle{font-size:10px;display:inline-flex;" in COMPACT_CSS
    assert ".thinking-card-header{display:flex;align-items:center;gap:8px;" in COMPACT_CSS
    # Body uses div default (display:block); canonical rule lives in the
    # consolidated block. Open state caps at 260px (intentional "quieter" sizing).
    assert ".thinking-card-body{max-height:0;opacity:0;overflow:hidden;" in COMPACT_CSS
    assert re.search(
        r"\.thinking-card\.open\s+\.thinking-card-body\s*\{[^}]*max-height:\s*260px;[^}]*opacity:\s*1;",
        STYLE_CSS,
    )


def test_tool_card_toggle_uses_same_chevron_icon_markup_as_thinking_card():
    assert "<span class=\"thinking-card-toggle\">${li('chevron-right',12)}</span>" in UI_JS
    assert "<span class=\"tool-card-toggle\">${li('chevron-right',12)}</span>" in UI_JS
    assert "<div class=\"${classes}\"${sourceAttr}><div class=\"thinking-card-header\" onclick=\"this.parentElement.classList.toggle('open')\"><span class=\"thinking-card-icon\">" in UI_JS


def test_thinking_card_header_includes_copy_button_that_does_not_toggle_card():
    assert "function _copyThinkingText(btn){" in UI_JS
    assert "const copyBtn=`<button class=\"thinking-copy-btn\"" in UI_JS
    assert "event.stopPropagation();_copyThinkingText(this)" in UI_JS
    assert "card.querySelector('.thinking-card-body')" in UI_JS
    assert "typeof card._thinkingSource==='string'" in UI_JS
    assert "card.getAttribute('data-thinking-source')" in UI_JS
    assert 'data-thinking-source="${esc(clean)}"' in UI_JS
    assert "_copyText(text).then(()=>{" in UI_JS
    assert "btn.innerHTML=li('check',12);" in UI_JS
    assert ".thinking-copy-btn{" in COMPACT_CSS
    assert ".thinking-copy-btn:hover,.thinking-copy-btn:focus-visible{" in COMPACT_CSS


def test_live_thinking_debounces_markdown_while_preserving_card_body():
    assert "function _renderThinkingInto(row,text='')" in UI_JS
    assert "row.querySelector('.thinking-card-body')" in UI_JS
    assert "function _flushThinkingMarkdown(row)" in UI_JS
    assert "function _scheduleThinkingMarkdownRender(row)" in UI_JS
    assert "clearTimeout(row._thinkingMarkdownTimer)" in UI_JS
    assert "_flushThinkingMarkdown(row);" in UI_JS
    assert "_renderThinkingInto(row,text);" in UI_JS


def test_thinking_cards_use_the_markdown_renderer_for_codex_reasoning():
    assert "function _thinkingBodyHtml(text='')" in UI_JS
    helper = UI_JS.split("function _thinkingBodyHtml(text='')", 1)[1].split("function ", 1)[0]
    assert "renderMd(clean)" in helper

    for function_name in ("_thinkingCardHtml", "_thinkingMarkup", "_flushThinkingMarkdown"):
        body = UI_JS.split(f"function {function_name}", 1)[1].split("function ", 1)[0]
        assert "_thinkingBodyHtml(" in body, (
            f"{function_name} must share the reasoning Markdown renderer"
        )

    assert '<div class="thinking-card-body">${bodyHtml}</div>' in UI_JS
    assert '<pre>${esc(clean)}</pre>' not in UI_JS


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_hundred_live_reasoning_updates_trigger_one_markdown_render_after_quiet_period(tmp_path):
    assert NODE is not None
    driver = tmp_path / "thinking-debounce.js"
    driver.write_text(
        r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
function extractFunc(name){
  const start=src.search(new RegExp('function\\s+'+name+'\\s*\\('));
  if(start<0) throw new Error(name+' not found');
  let i=src.indexOf('{',start)+1, depth=1;
  while(depth>0&&i<src.length){ if(src[i]==='{')depth++; else if(src[i]==='}')depth--; i++; }
  return src.slice(start,i);
}
let renderCount=0, timerId=0;
const timers=new Map();
global.setTimeout=(fn)=>{ const id=++timerId; timers.set(id,fn); return id; };
global.clearTimeout=(id)=>timers.delete(id);
function _sanitizeThinkingDisplayText(text){ return String(text||'').trim(); }
function _thinkingBodyHtml(text){ renderCount++; return `<div class="thinking-card-markdown"><p>${text}</p></div>`; }
function _thinkingMarkup(text){ return `<div class="thinking-card-body">${_thinkingBodyHtml(text)}</div>`; }
eval(extractFunc('_flushThinkingMarkdown'));
eval(extractFunc('_scheduleThinkingMarkdownRender'));
eval(extractFunc('_renderThinkingInto'));
const body={innerHTML:'<div class="thinking-card-markdown"><p>seed</p></div>'};
const row={
  _thinkingRenderedText:'seed',
  querySelector:(selector)=>selector==='.thinking-card-body'?body:null,
  innerHTML:'',
};
for(let i=1;i<=100;i++) _renderThinkingInto(row,'seed '+i);
const before=renderCount;
const pending=Array.from(timers.values());
timers.clear();
pending.forEach(fn=>fn());
process.stdout.write(JSON.stringify({
  before,
  after:renderCount,
  pendingCount:pending.length,
  rendered:row._thinkingRenderedText,
  sameBody:row.querySelector('.thinking-card-body')===body,
}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver), str(pathlib.Path(__file__).parent.parent / "static" / "ui.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "before": 0,
        "after": 1,
        "pendingCount": 1,
        "rendered": "seed 100",
        "sameBody": True,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_anchor_reasoning_streams_only_deltas_into_one_markdown_node(tmp_path):
    assert NODE is not None
    driver = tmp_path / "anchor-reasoning-incremental.js"
    driver.write_text(
        r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
function extractFunc(name){
  const start=src.search(new RegExp('function\\s+'+name+'\\s*\\('));
  if(start<0) throw new Error(name+' not found');
  let i=src.indexOf('{',start)+1, depth=1;
  while(depth>0&&i<src.length){ if(src[i]==='{')depth++; else if(src[i]==='}')depth--; i++; }
  return src.slice(start,i);
}
class FakeElement{
  constructor(){ this.children=[]; this.className=''; }
  appendChild(child){ this.children.push(child); return child; }
  querySelector(selector){
    if(selector==='.thinking-card-body') return this.body||null;
    if(selector==='.thinking-card') return this.card||null;
    return null;
  }
}
let nodeCreates=0;
const writes=[];
const _anchorThinkingSmdCache=new Map();
global.document={createElement:()=>new FakeElement()};
global.window={smd:{
  parser:(renderer)=>({renderer}),
  parser_write:(_parser,delta)=>writes.push(delta),
}};
function _safeSmdRenderer(root){ return {root}; }
function _smdRendererWithoutUnderscoreEmphasis(renderer){ return renderer; }
function _smdThinkingRenderer(renderer){ return renderer; }
function _smdBindParserIdentity(){}
function _thinkingActivityNode(){
  nodeCreates++;
  const node=new FakeElement();
  node.body=new FakeElement();
  node.card=new FakeElement();
  return node;
}
eval(extractFunc('_anchorThinkingIncrementalNode'));
let first=null, last=null;
for(let i=1;i<=100;i++){
  last=_anchorThinkingIncrementalNode('reason-1','x'.repeat(i));
  if(i===1) first=last;
}
process.stdout.write(JSON.stringify({
  sameNode:first===last,
  nodeCreates,
  writeCalls:writes.length,
  writtenChars:writes.join('').length,
  source:first.card._thinkingSource,
  cacheSize:_anchorThinkingSmdCache.size,
}));
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [NODE, str(driver), str(pathlib.Path(__file__).parent.parent / "static" / "messages.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "sameNode": True,
        "nodeCreates": 1,
        "writeCalls": 100,
        "writtenChars": 100,
        "source": "x" * 100,
        "cacheSize": 1,
    }


def test_anchor_reasoning_uses_persistent_streaming_markdown_node():
    assert "window.__anchorThinkingIncrementalNode=_anchorThinkingIncrementalNode" in MESSAGES_JS
    assert "window.__anchorThinkingIncrementalNode(thinkingKey,text)" in UI_JS


def test_thinking_card_uses_panel_chrome_with_gold_palette():
    # Canonical thinking-card rule lives in the consolidated block (border-radius
    # tightened from 10px → 8px as part of the "quieter card" design pass).
    assert re.search(
        r"\.thinking-card\s*\{[^}]*background:\s*var\(--accent-bg\);[^}]*border:\s*1px\s+solid\s+var\(--accent-bg-strong\);[^}]*border-radius:\s*8px;",
        STYLE_CSS,
    )
    assert "border-left: 2px solid rgba(201,168,76,.4);" not in STYLE_CSS
