"""Regression coverage for anchor live prose sealing + row-level parser finalization."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
NODE = shutil.which("node")

EXTRACT_FN_JS = """
function hasFunction(src, name){
  return src.indexOf('function ' + name) !== -1;
}

function extractFn(src, name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const paramsStart = src.indexOf('(', start);
  let depth = 0;
  let paramsEnd = -1;
  for(let i = paramsStart; i < src.length; i++){
    if(src[i] === '(') depth += 1;
    else if(src[i] === ')'){
      depth -= 1;
      if(depth === 0){ paramsEnd = i; break; }
    }
  }
  const braceStart = src.indexOf('{', paramsEnd);
  if(braceStart === -1) throw new Error(name + ' body not found');
  depth = 0;
  for(let i = braceStart; i < src.length; i++){
    if(src[i] === '{') depth += 1;
    else if(src[i] === '}'){
      depth -= 1;
      if(depth === 0){
        return src.slice(start, i + 1);
      }
    }
  }
  throw new Error(name + ' body did not close');
}
"""

HARNESS_JS_TEMPLATE = r"""
const fs = require('fs');
const msgSrc = fs.readFileSync(__MSG_SRC__, 'utf8');
const uiSrc = fs.readFileSync(__UI_SRC__, 'utf8');
let _extractSource = msgSrc;
let _anchorProseSmdCache;
let _anchorProseIncrementalNode;
let _finalizeAnchorProseIncrementalNode;
let _anchorSceneNodeForRow;

function extractFunction(name){
  return extractFn(_extractSource, name);
}
__EXTRACT_FN_JS__

function createHarness(options={}){
  const calls = {written:0, ended:0, failedEnds:0, created:0, tailFlush:0, tailClear:0, sanitize:0, enhance:0, identityClear:0};
  const cache = new Map();
  const failParserEnd = !!(options && options.failParserEnd);
  class FakeElement{
    constructor(tagName='div'){
      this.tagName = String(tagName || 'div').toUpperCase();
      this.children = [];
      this.attributes = Object.create(null);
      this.dataset = Object.create(null);
      this.parentNode = null;
      this.style = {};
      this._textContent = '';
      this._classSet = new Set();
      this.classList = {
        add: (name)=>this._classSet.add(String(name)),
        remove: (name)=>this._classSet.delete(String(name)),
        toggle: (name, on)=>{
          const key = String(name);
          if(on === false) this._classSet.delete(key);
          else this._classSet.add(key);
        },
        contains: (name)=>this._classSet.has(String(name)),
      };
    }
    appendChild(child){
      if(!child) return null;
      child.parentNode = this;
      this.children.push(child);
      return child;
    }
    get className(){
      return Array.from(this._classSet).join(' ');
    }
    set className(value){
      this._classSet.clear();
      String(value || '').split(/\s+/).filter(Boolean).forEach(v=>this._classSet.add(v));
    }
    setAttribute(name, value){
      const key = String(name);
      const val = String(value);
      this.attributes[key] = val;
      if(key === 'class'){
        this.className = val;
      }
      if(key.startsWith('data-')){
        const ds = key.slice(5).replace(/-([a-z])/g, (_, c)=>c.toUpperCase());
        this.dataset[ds] = val;
      }
    }
    getAttribute(name){
      return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    }
    querySelector(selector){
      if(selector === '.msg-body'){
        const direct = this.children.find((c)=>c.className && c.className.split(' ').includes('msg-body')) || null;
        if(direct) return direct;
        if(typeof this._innerHTML === 'string'){
          const html = this._innerHTML || '';
          const open = '<div class=\"msg-body\">';
          const close = '</div>';
          if(html.startsWith(open) && html.endsWith(close)){
            return {
              textContent: html.slice(open.length, html.length - close.length),
            };
          }
        }
        return null;
      }
      return null;
    }
    get innerHTML(){
      return this._innerHTML || '';
    }
    set innerHTML(value){
      this._innerHTML = String(value);
      this.textContent = this._innerHTML;
    }
    get textContent(){
      return this._textContent || '';
    }
    set textContent(value){
      this._textContent = String(value);
    }
  }

  global.document = {
    createElement(tag){
      return new FakeElement(tag);
    },
    createTextNode(value){
      const node = new FakeElement('#text');
      node.textContent = String(value || '');
      return node;
    },
  };

  function bindParser(renderer, parser, el){
    if(!parser || !el) return;
    parser._body = el;
    el.__smdParser = parser;
  }

  global.window = {
    _fadeTextEffect: false,
    _shouldUseLiveProseFade: ()=>false,
  };
  window.__anchorProseIncrementalNode = null;
  window._anchorProseSmdCache = cache;
  window.smd = {
    HREF: 'href',
    SRC: 'src',
    parser(){
      calls.created += 1;
      return {id: calls.created, ended: false, pending: ''};
    },
    parser_write(parser, text){
      calls.written += 1;
      if(!parser || parser.ended || !parser._body) return;
      const chunk = String(text || '');
      if(!chunk) return;
      const flushNow = chunk.length > 1 ? chunk.slice(0, -1) : '';
      if(flushNow) parser._body.textContent += flushNow;
      parser.pending += chunk.slice(-1);
    },
    parser_end(parser){
      calls.ended += 1;
      if(failParserEnd){
        calls.failedEnds += 1;
        throw new Error('parser_end failure');
      }
      if(!parser || parser.ended || !parser._body) return;
      parser._body.textContent += parser.pending || '';
      parser.pending = '';
      parser.ended = true;
    },
  };

  global._safeSmdRenderer = ()=>({set_attr(){},add_text(){},add_token(){},end_token(){}});
  global._smdRendererWithoutUnderscoreEmphasis = (renderer)=>renderer;
  global._streamFadeRenderer = (body)=>({body,set_attr(){},add_text(){},add_token(){},end_token(){}});
  global._smdBindParserIdentity = bindParser;
  global._smdClearParserIdentity = (el, parser)=>{
    if(!el || !parser || el.__smdParser !== parser) return;
    calls.identityClear += 1;
    delete el.__smdParser;
  };
  global._smdMediaTailFlush = ()=>{ calls.tailFlush += 1; };
  global._smdMediaTailClear = ()=>{ calls.tailClear += 1; };
  global._sanitizeSmdLinks = ()=>{ calls.sanitize += 1; };
  global.enhanceMarkdownTables = ()=>{ calls.enhance += 1; };
  global.renderMd = (text)=>String(text || '');
  global.esc = (value)=>String(value || '');

  _anchorProseSmdCache = cache;
  window._anchorProseSmdCache = cache;
  _extractSource = msgSrc;
  eval(extractFunction('_anchorProseIncrementalNode'));
  eval(extractFunction('_finalizeAnchorProseIncrementalNode'));
  _extractSource = uiSrc;
  eval(extractFunction('_anchorSceneNodeForRow'));
  window.__anchorProseIncrementalNode = _anchorProseIncrementalNode;

  return {
    calls,
    cache,
    renderRow(row){
      return _anchorSceneNodeForRow(row, {settled:false});
    },
  };
}

function runCase(name){
  const harness = createHarness();
  const row = (status, text)=>({role:'prose', local_id:'live-prose:1', status, text});

  if(name === 'running_row_does_not_finalize'){
    const node = harness.renderRow(row('running', 'running text.'));
    return {
      finalizeCount: harness.calls.ended,
      hasNode: !!node,
    };
  }

  if(name === 'completed_row_finalizes_once'){
    harness.renderRow(row('running', 'streamed final'));
    const node = harness.renderRow(row('completed', 'streamed final.'));
    const body = node && node.querySelector('.msg-body');
    return {
      endCount: harness.calls.ended,
      bodyText: body ? body.textContent : '',
      sanitizeCalls: harness.calls.sanitize,
      enhanceCalls: harness.calls.enhance,
      tailFlush: harness.calls.tailFlush,
      tailClear: harness.calls.tailClear,
    };
  }

  if(name === 'completed_row_rerender_is_idempotent'){
    harness.renderRow(row('completed', 'idempotent.'));
    const node = harness.renderRow(row('completed', 'idempotent.'));
    const body = node && node.querySelector('.msg-body');
    return {
      endCount: harness.calls.ended,
      bodyText: body ? body.textContent : '',
      createdParsers: harness.calls.created,
      writeCalls: harness.calls.written,
    };
  }

  if(name === 'finalized_row_rebuilds_on_changed_text'){
    harness.renderRow(row('completed', 'first.'));
    const node = harness.renderRow(row('completed', 'first!!'));
    const body = node && node.querySelector('.msg-body');
    return {
      endCount: harness.calls.ended,
      createdParsers: harness.calls.created,
      bodyText: body ? body.textContent : '',
      writeCalls: harness.calls.written,
    };
  }

  if(name === 'tool_boundary_completes_row'){
    harness.renderRow(row('running', 'pre tool'));
    const node = harness.renderRow(row('completed', 'pre tool.'));
    const body = node && node.querySelector('.msg-body');
    return {
      finalizeCount: harness.calls.ended,
      bodyText: body ? body.textContent : '',
      writeCalls: harness.calls.written,
      createdParsers: harness.calls.created,
    };
  }

  if(name === 'completed_row_end_failure_falls_back_to_render_md'){
    const failingHarness = createHarness({failParserEnd:true});
    const node = failingHarness.renderRow({
      role:'prose',
      local_id:'live-prose:1',
      status:'completed',
      text:'streamed final.',
    });
    const body = node && node.querySelector('.msg-body');
    return {
      finalizeCount: failingHarness.calls.ended,
      failedEndCount: failingHarness.calls.failedEnds,
      bodyText: body ? body.textContent : '',
      renderCount: failingHarness.calls.created,
      writeCalls: failingHarness.calls.written,
      cacheSize: failingHarness.cache.size,
    };
  }

  throw new Error('unknown case: ' + name);
}

const output = runCase("__CASE__");
console.log(JSON.stringify(output));
"""


def _run_scenario(case_name: str) -> dict:
    assert NODE is not None, "node is required for anchor incremental harness tests"
    case = str(case_name)
    script = HARNESS_JS_TEMPLATE
    script = script.replace("__MSG_SRC__", json.dumps(str(ROOT / 'static' / 'messages.js')))
    script = script.replace("__UI_SRC__", json.dumps(str(ROOT / 'static' / 'ui.js')))
    script = script.replace("__EXTRACT_FN_JS__", EXTRACT_FN_JS)
    script = script.replace("__CASE__", case)
    result = subprocess.run([NODE, "-e", script], cwd=ROOT, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def _messages_js_source() -> str:
    return (ROOT / "static" / "messages.js").read_text(encoding="utf-8")


@pytest.mark.skipif(NODE is None, reason="node is required for anchor prose incremental harness tests")
def test_running_anchor_prose_does_not_finalize_parser():
    data = _run_scenario("running_row_does_not_finalize")
    assert data["finalizeCount"] == 0


@pytest.mark.skipif(NODE is None, reason="node is required for anchor prose incremental harness tests")
def test_completed_anchor_row_finalizes_once_and_runs_post_end_cleanup():
    data = _run_scenario("completed_row_finalizes_once")
    assert data["endCount"] == 1
    assert data["bodyText"] == "streamed final."
    assert data["sanitizeCalls"] >= 1
    assert data["enhanceCalls"] >= 1
    assert data["tailFlush"] >= 1
    assert data["tailClear"] >= 1


@pytest.mark.skipif(NODE is None, reason="node is required for anchor prose incremental harness tests")
def test_completed_row_rerender_is_idempotent_after_end():
    data = _run_scenario("completed_row_rerender_is_idempotent")
    assert data["endCount"] == 1
    assert data["bodyText"] == "idempotent."
    assert data["createdParsers"] == 1
    assert data["writeCalls"] >= 1


@pytest.mark.skipif(NODE is None, reason="node is required for anchor prose incremental harness tests")
def test_changed_text_after_finalized_row_rebuilds_from_full_authoritative_text():
    data = _run_scenario("finalized_row_rebuilds_on_changed_text")
    assert data["endCount"] == 2
    assert data["createdParsers"] == 2
    assert data["bodyText"] == "first!!"
    assert data["writeCalls"] >= 2


@pytest.mark.skipif(NODE is None, reason="node is required for anchor prose incremental harness tests")
def test_tool_boundary_sealing_updates_row_to_completed_for_finalize():
    messages_source = _messages_js_source()
    assert "_upsertAnchorProcessProse(pendingDisplayTextBeforeTool,{sealed:true})" in messages_source
    assert "_upsertAnchorProcessProse(pendingDisplayTextBeforeComplete,{sealed:true})" in messages_source
    assert "status:options.sealed?'completed':'running'" in messages_source
    data = _run_scenario("tool_boundary_completes_row")
    assert data["finalizeCount"] == 1
    assert data["bodyText"] == "pre tool."
    assert data["createdParsers"] == 1
    assert data["writeCalls"] >= 1


@pytest.mark.skipif(NODE is None, reason="node is required for anchor prose incremental harness tests")
def test_anchor_prose_parser_end_failure_falls_back_to_full_renderMd():
    data = _run_scenario("completed_row_end_failure_falls_back_to_render_md")
    assert data["finalizeCount"] == 1
    assert data["failedEndCount"] == 1
    assert data["renderCount"] == 1
    assert data["bodyText"] == "streamed final."
    assert data["cacheSize"] == 0
