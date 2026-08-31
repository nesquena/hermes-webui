import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
TERMINAL_JS = (REPO_ROOT / "static" / "terminal.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _function_source(source: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
    assert match, f"missing JavaScript function {name}"
    brace = source.find("{", match.end())
    assert brace >= 0
    depth = 1
    quote = None
    escaped = False
    line_comment = False
    block_comment = False
    index = brace + 1
    while index < len(source) and depth:
        char = source[index]
        nxt = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if char == "\n":
                line_comment = False
        elif block_comment:
            if char == "*" and nxt == "/":
                block_comment = False
                index += 1
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char == "/" and nxt == "/":
            line_comment = True
            index += 1
        elif char == "/" and nxt == "*":
            block_comment = True
            index += 1
        elif char in "'\"`":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"unterminated JavaScript function {name}"
    return source[match.start() : index]


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_read_only_claude_menu_exposes_one_authoritative_resume_action():
    functions = "\n".join(
        _function_source(SESSIONS_JS, name)
        for name in (
            "_buildSessionAction",
            "_claudeResumeActionLabel",
            "_setClaudeResumeActionState",
            "_appendClaudeResumeAction",
            "_openSessionActionMenu",
        )
    )
    script = f"""
const assert=require('assert');
const calls=[];
function makeClassList(){{
  const values=new Set();
  return {{
    add(...names){{names.forEach(name=>values.add(name));}},
    remove(...names){{names.forEach(name=>values.delete(name));}},
    contains(name){{return values.has(name);}},
    toggle(name,on){{if(on)values.add(name);else values.delete(name);}},
  }};
}}
function makeElement(tag){{
  const el={{tagName:tag.toUpperCase(),children:[],className:'',classList:makeClassList(),style:{{}},_html:'',_name:null}};
  Object.defineProperty(el,'innerHTML',{{
    get(){{return el._html;}},
    set(value){{
      el._html=String(value);
      const match=el._html.match(/<span class="ws-opt-name">([\\s\\S]*?)<\\/span>/);
      el._name={{textContent:match?match[1]:''}};
    }},
  }});
  el.appendChild=child=>{{el.children.push(child);return child;}};
  el.setAttribute=(name,value)=>{{el[name]=String(value);}};
  el.querySelector=selector=>selector==='.ws-opt-name'?el._name:null;
  return el;
}}
global.document={{createElement:makeElement}};
global.window={{innerHeight:800}};
global.ICONS={{link:'',download:'',edit:'',terminal:''}};
global.esc=value=>String(value);
global.t=(key)=>({{
  claude_resume_qwen:'Resume with Claude Qwen',
  claude_resume_ornith:'Resume with Claude Local · Ornith',
  claude_active_elsewhere:'Active in another Claude process',
  claude_resume_unavailable:'Remote resume unavailable',
}}[key]||key);
global.api=async(path)=>{{calls.push(path);return {{kind:'claude_code',profile:'qwen',label:'Claude Qwen',can_remote_resume:true,coarse_status:'inactive',workspace_label:'Project'}};}};
global.resumeClaudeSession=session=>{{calls.push('resume:'+session.session_id);}};
global._sessionActionMenu=null;
global._sessionActionSessionId=null;
global._sessionActionAnchor=null;
global._sessionActionMenuId=0;
global._isReadOnlySession=()=>true;
global._isMessagingSession=()=>false;
global._isCliSession=()=>true;
global.closeSessionActionMenu=()=>{{}};
global._appendSessionCopyLinkAction=(menu)=>menu.appendChild(Object.assign(makeElement('button'),{{label:'copy'}}));
global._appendSessionExportHtmlAction=(menu)=>menu.appendChild(Object.assign(makeElement('button'),{{label:'export'}}));
global._mountSessionActionMenu=(menu)=>{{global.mounted=menu;}};
{functions}
(async()=>{{
  const session={{session_id:'opaque-row',kind:'claude_code',profile:'qwen',label:'Claude Qwen',can_remote_resume:true,read_only:true}};
  _openSessionActionMenu(session,{{}});
  await new Promise(resolve=>setImmediate(resolve));
  const actions=global.mounted.children.filter(child=>child._name);
  assert.strictEqual(actions.length,1);
  assert.strictEqual(actions[0]._name.textContent,'Resume with Claude Qwen');
  assert.strictEqual(actions[0].disabled,false);
  await actions[0].onclick({{preventDefault(){{}},stopPropagation(){{}}}});
  assert(calls.includes('/api/claude-code/status?session_id=opaque-row'));
  assert(calls.includes('resume:opaque-row'));
  assert(!global.mounted.children.some(child=>child._name&&child._name.textContent==='session_rename'));
  console.log(JSON.stringify({{labels:actions.map(action=>action._name.textContent),calls}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = _run_node(script)
    assert result["labels"] == ["Resume with Claude Qwen"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_read_only_claude_menu_fails_closed_for_live_and_unknown_owners():
    functions = "\n".join(
        _function_source(SESSIONS_JS, name)
        for name in (
            "_buildSessionAction",
            "_claudeResumeActionLabel",
            "_setClaudeResumeActionState",
            "_appendClaudeResumeAction",
        )
    )
    script = f"""
const assert=require('assert');
function makeClassList(){{const values=new Set();return {{add(...n){{n.forEach(x=>values.add(x));}},remove(...n){{n.forEach(x=>values.delete(x));}},contains(n){{return values.has(n);}},toggle(n,on){{if(on)values.add(n);else values.delete(n);}}}};}}
function makeElement(){{
  const el={{children:[],className:'',classList:makeClassList(),_name:null}};
  Object.defineProperty(el,'innerHTML',{{set(value){{const m=String(value).match(/<span class="ws-opt-name">([\\s\\S]*?)<\\/span>/);el._name={{textContent:m?m[1]:''}};}}}});
  el.appendChild=child=>{{el.children.push(child);return child;}};
  el.setAttribute=(name,value)=>{{el[name]=String(value);}};
  el.querySelector=selector=>selector==='.ws-opt-name'?el._name:null;
  return el;
}}
global.document={{createElement:makeElement}};
global.ICONS={{terminal:''}};
global.esc=value=>String(value);
global.t=(key)=>({{
  claude_resume_qwen:'Resume with Claude Qwen',
  claude_resume_ornith:'Resume with Claude Local · Ornith',
  claude_active_elsewhere:'Active in another Claude process',
  claude_resume_unavailable:'Remote resume unavailable',
}}[key]||key);
global.resumeClaudeSession=()=>{{throw new Error('disabled action ran');}};
{functions}
(async()=>{{
  const liveMenu=makeElement();
  global.api=async()=>({{kind:'claude_code',profile:'ornith',label:'Claude Local · Ornith',can_remote_resume:true,coarse_status:'active_elsewhere',workspace_label:'Project'}});
  _appendClaudeResumeAction(liveMenu,{{session_id:'live',kind:'claude_code',profile:'ornith',can_remote_resume:true}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(liveMenu.children[0]._name.textContent,'Active in another Claude process');
  assert.strictEqual(liveMenu.children[0].disabled,true);

  let statusCalls=0;
  global.api=async()=>{{statusCalls+=1;throw new Error('must not probe an unmapped row');}};
  const unknownMenu=makeElement();
  _appendClaudeResumeAction(unknownMenu,{{session_id:'unknown',kind:'claude_code',profile:null,can_remote_resume:false}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(unknownMenu.children[0]._name.textContent,'Remote resume unavailable');
  assert.strictEqual(unknownMenu.children[0].disabled,true);
  assert.strictEqual(statusCalls,0);
  console.log(JSON.stringify({{live:liveMenu.children[0]._name.textContent,unknown:unknownMenu.children[0]._name.textContent}}));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = _run_node(script)
    assert result == {
        "live": "Active in another Claude process",
        "unknown": "Remote resume unavailable",
    }


def _terminal_harness(scenario: str) -> str:
    return f"""
const assert=require('assert');
const calls=[];
const listeners={{}};
const visualListeners={{}};
const storageWrites=[];
let focusCalls=0;
let clearCalls=0;
let resetCalls=0;
let dataHandler=null;
let confirmResult=true;

function classList(){{
  const values=new Set();
  return {{add(...n){{n.forEach(x=>values.add(x));}},remove(...n){{n.forEach(x=>values.delete(x));}},toggle(n,on){{if(on)values.add(n);else values.delete(n);}},contains(n){{return values.has(n);}}}};
}}
function style(){{const values={{}};return {{setProperty(k,v){{values[k]=v;}},removeProperty(k){{delete values[k];}},values}};}}
function element(id){{return {{id,hidden:false,textContent:'',classList:classList(),style:style(),children:[],setAttribute(k,v){{this[k]=String(v);}},getBoundingClientRect(){{return {{height:240}};}},querySelector(){{return null;}},addEventListener(){{}},focus(){{focusCalls+=1;}},scrollIntoView(){{calls.push('scrollIntoView');}}}};}}
const nodes={{
  composerTerminalPanel:element('composerTerminalPanel'),
  composerTerminalDock:element('composerTerminalDock'),
  terminalViewport:element('terminalViewport'),
  terminalSurface:element('terminalSurface'),
  terminalWorkspaceLabel:element('terminalWorkspaceLabel'),
  terminalDockWorkspaceLabel:element('terminalDockWorkspaceLabel'),
  terminalResizeHandle:element('terminalResizeHandle'),
  btnTerminalStopClaude:element('btnTerminalStopClaude'),
  btnTerminalRestart:element('btnTerminalRestart'),
  claudeTerminalKeys:element('claudeTerminalKeys'),
  composerWrap:element('composerWrap'),
  messages:element('messages'),
}};
nodes.composerTerminalPanel.querySelector=selector=>selector==='.composer-terminal-inner'?nodes.terminalViewport:null;
nodes.messages.scrollHeight=0;nodes.messages.scrollTop=0;nodes.messages.clientHeight=0;

class FakeTerminal{{
  constructor(options){{this.options=options;this.cols=80;this.rows=24;this.buffer={{active:{{length:0}}}};}}
  loadAddon(){{}}
  open(surface){{calls.push('open:'+surface.id);}}
  onData(handler){{dataHandler=handler;return {{dispose(){{}}}};}}
  focus(){{focusCalls+=1;calls.push('focus');}}
  write(text){{calls.push('write:'+text);}}
  writeln(text){{calls.push('writeln:'+text);}}
  clear(){{clearCalls+=1;calls.push('clear');}}
  reset(){{resetCalls+=1;}}
  dispose(){{calls.push('dispose');}}
  getSelection(){{return '';}}
}}
class FakeEventSource{{
  static CLOSED=2;
  static instances=[];
  constructor(url){{this.url=url;this.readyState=1;this.handlers={{}};this.closed=false;FakeEventSource.instances.push(this);calls.push('eventsource:'+url);}}
  addEventListener(name,handler){{(this.handlers[name]||(this.handlers[name]=[])).push(handler);}}
  emit(name,data='{{}}'){{(this.handlers[name]||[]).forEach(handler=>handler({{data}}));}}
  close(){{this.closed=true;this.readyState=2;calls.push('source-close');}}
}}
const visualViewport={{height:500,offsetTop:0,addEventListener(name,handler){{visualListeners[name]=handler;}}}};
const windowObject={{
  Terminal:FakeTerminal,
  FitAddon:{{FitAddon:class{{fit(){{calls.push('fit');}}}}}},
  WebLinksAddon:null,
  ResizeObserver:null,
  MutationObserver:null,
  visualViewport,
  innerHeight:800,
  matchMedia(){{return {{matches:true}};}},
  addEventListener(name,handler){{listeners[name]=handler;}},
  setTimeout,
}};
global.window=windowObject;
global.document={{
  baseURI:'https://example.test/',
  hidden:false,
  documentElement:{{classList:classList(),style:style()}},
  fonts:null,
  head:null,
  addEventListener(name,handler){{listeners['document:'+name]=handler;}},
  getElementById(id){{return nodes[id]||null;}},
}};
global.location={{href:'https://example.test/'}};
global.URL=URL;
global.EventSource=FakeEventSource;
global.ResizeObserver=undefined;
global.MutationObserver=undefined;
global.Blob=Blob;
global.navigator={{
  clipboard:{{writeText:async()=>{{}}}},
  sendBeacon(){{calls.push('beacon');return true;}},
}};
global.localStorage={{setItem(k,v){{storageWrites.push(['local',k,v]);}},getItem(){{return null;}}}};
global.sessionStorage={{setItem(k,v){{storageWrites.push(['session',k,v]);}},getItem(){{return null;}}}};
global.getComputedStyle=()=>({{getPropertyValue:()=>''}});
global.requestAnimationFrame=callback=>{{callback();return 1;}};
global.cancelAnimationFrame=()=>{{}};
global.scrollToBottom=()=>{{}};
global.recordClientSSEError=()=>{{}};
global.showToast=(message)=>calls.push('toast:'+message);
global.showConfirmDialog=async()=>confirmResult;
global.t=key=>({{
  terminal_title:'Terminal',terminal_clear:'Clear',terminal_copy_output:'Copy output',terminal_restart:'Restart',terminal_reconnect:'Reconnect',terminal_collapse:'Collapse',terminal_expand:'Expand',terminal_close:'Close',terminal_error:'Terminal error',terminal_input_failed:'Input failed: ',terminal_start_failed:'Start failed: ',terminal_no_workspace_title:'No workspace',terminal_remote_backend_unsupported:'Unsupported',claude_terminal_stop_title:'Stop Claude?',claude_terminal_stop_message:'This stops the managed Claude process.',claude_terminal_stop:'Stop Claude',claude_terminal_stopped:'Claude stopped',
}}[key]||key);
global.$=id=>nodes[id]||null;
global.S={{session:{{session_id:'hermes-session',workspace:'/workspace'}},terminalRemoteBackend:false}};
global.api=async(path,options={{}})=>{{
  const body=options.body?JSON.parse(options.body):null;
  calls.push({{path,body,focusAtCall:focusCalls}});
  if(path==='/api/claude-code/resume')return {{ok:true,attached:false,handle:'safe-handle',generation:'generation-1'}};
  if(path==='/api/terminal/start')return {{ok:true}};
  return {{ok:true}};
}};
{TERMINAL_JS}
(async()=>{{
{scenario}
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_claude_resume_focuses_before_network_and_passes_exit_words_through():
    result = _run_node(
        _terminal_harness(
            """
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  const pending=resumeClaudeSession(session);
  assert.strictEqual(focusCalls,1);
  const resumeCall=calls.find(call=>call.path==='/api/claude-code/resume');
  assert(resumeCall);
  assert.strictEqual(resumeCall.focusAtCall,1);
  await pending;
  S.session=null;
  dataHandler('exit');
  dataHandler('\\r');
  await TERMINAL_UI.inputQueue;
  const inputCalls=calls.filter(call=>call.path==='/api/claude-code/terminal/input');
  assert.deepStrictEqual(inputCalls.map(call=>call.body.data),['exit','\\r']);
  assert(!calls.some(call=>call.path==='/api/terminal/close'));
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  assert.strictEqual(storageWrites.length,0);
  assert(!FakeEventSource.instances[0].url.includes('capability'));
  console.log(JSON.stringify({focusCalls,input:inputCalls.map(call=>call.body.data),storageWrites,sourceUrl:FakeEventSource.instances[0].url}));
"""
        )
    )
    assert result["focusCalls"] == 1
    assert result["input"] == ["exit", "\r"]
    assert result["storageWrites"] == []
    assert "capability" not in result["sourceUrl"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_claude_detach_reset_mobile_keys_and_confirmed_stop_are_safe():
    result = _run_node(
        _terminal_harness(
            """
  const session={session_id:'opaque-session',kind:'claude_code',profile:'ornith',label:'Claude Local · Ornith',workspace_label:'Project',can_remote_resume:true};
  await resumeClaudeSession(session);
  const source=FakeEventSource.instances[0];
  source.emit('terminal_reset',JSON.stringify({generation:'generation-1'}));
  assert.strictEqual(clearCalls,1);
  assert.strictEqual(resetCalls,0);
  sendClaudeTerminalKey('ctrl-c');
  sendClaudeTerminalKey('up');
  await TERMINAL_UI.inputQueue;
  assert.deepStrictEqual(calls.filter(call=>call.path==='/api/claude-code/terminal/input').map(call=>call.body.data),['\\u0003','\\u001b[A']);
  assert(visualListeners.resize&&visualListeners.scroll);
  visualListeners.resize();
  document.hidden=true;
  listeners['document:visibilitychange']();
  assert(source.closed);
  document.hidden=false;
  listeners['document:visibilitychange']();
  await new Promise(resolve=>setImmediate(resolve));
  listeners.beforeunload();
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  await closeComposerTerminal();
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));

  await resumeClaudeSession(session);

  confirmResult=false;
  await stopClaudeTerminal();
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  confirmResult=true;
  await stopClaudeTerminal();
  const stops=calls.filter(call=>call.path==='/api/claude-code/stop');
  assert.strictEqual(stops.length,1);
  assert.deepStrictEqual(stops[0].body,{handle:'safe-handle',generation:'generation-1'});
  console.log(JSON.stringify({clearCalls,stops:stops.length,keys:calls.filter(call=>call.path==='/api/claude-code/terminal/input').map(call=>call.body.data),visual:Object.keys(visualListeners)}));
"""
        )
    )
    assert result["clearCalls"] == 1
    assert result["stops"] == 1
    assert result["keys"] == ["\x03", "\x1b[A"]
    assert result["visual"] == ["resize", "scroll"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_generic_terminal_close_command_interception_is_unchanged():
    result = _run_node(
        _terminal_harness(
            """
  await toggleComposerTerminal(true);
  dataHandler('exit');
  dataHandler('\\r');
  await new Promise(resolve=>setImmediate(resolve));
  assert(calls.some(call=>call.path==='/api/terminal/close'));
  assert(!calls.some(call=>call.path==='/api/terminal/input'&&call.body.data==='\\r'));
  assert(!calls.some(call=>call.path==='/api/claude-code/terminal/input'));
  console.log(JSON.stringify({closed:calls.filter(call=>call.path==='/api/terminal/close').length}));
"""
        )
    )
    assert result["closed"] == 1


def test_claude_terminal_mobile_controls_have_accessible_touch_targets():
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    for key in ("esc", "tab", "ctrl-c", "up", "down", "left", "right"):
        assert f'data-terminal-key="{key}"' in html
    assert 'id="btnTerminalStopClaude"' in html
    assert 'onclick="stopClaudeTerminal()"' in html
    assert ".claude-terminal-key{min-width:44px;min-height:44px" in css
