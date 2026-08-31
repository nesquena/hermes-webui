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
  terminalTitleLabel:element('terminalTitleLabel'),
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
  static instances=[];
  constructor(options){{this.options=options;this.cols=80;this.rows=24;this.buffer={{active:{{length:0}}}};FakeTerminal.instances.push(this);}}
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
  emit(name,data='{{}}',lastEventId=''){{(this.handlers[name]||[]).forEach(handler=>handler({{data,lastEventId}}));}}
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


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_spa_session_navigation_detaches_claude_viewer_without_stopping_it():
    result = _run_node(
        _terminal_harness(
            """
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  await resumeClaudeSession(session);
  const source=FakeEventSource.instances[0];
  syncClaudeTerminalNavigation('different-hermes-session');
  assert(source.closed);
  assert.strictEqual(TERMINAL_UI.term,null);
  assert.strictEqual(TERMINAL_UI.mode,'shell');
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  console.log(JSON.stringify({closed:source.closed,disposed:calls.includes('dispose'),stops:calls.filter(call=>call.path==='/api/claude-code/stop').length}));
"""
        )
    )
    assert result == {"closed": True, "disposed": True, "stops": 0}
    assert "syncClaudeTerminalNavigation(sid)" in _function_source(
        SESSIONS_JS, "loadSession"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("stale_result", ["failure", "success"])
def test_same_session_stale_resume_completion_cannot_replace_or_detach_new_viewer(
    stale_result,
):
    result = _run_node(
        _terminal_harness(
            f"""
  function deferred(){{let resolve,reject;const promise=new Promise((ok,no)=>{{resolve=ok;reject=no;}});return {{promise,resolve,reject}};}}
  const resumes=[deferred(),deferred()];
  let resumeIndex=0;
  global.api=async(path,options={{}})=>{{
    const body=options.body?JSON.parse(options.body):null;
    calls.push({{path,body,focusAtCall:focusCalls}});
    if(path==='/api/claude-code/resume')return resumes[resumeIndex++].promise;
    return {{ok:true}};
  }};
  const session={{session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true}};
  const stale=resumeClaudeSession(session);
  const current=resumeClaudeSession(session);
  resumes[1].resolve({{handle:'new-handle',generation:'new-generation'}});
  assert.strictEqual(await current,true);
  if('{stale_result}'==='failure')resumes[0].reject(Object.assign(new Error('owned elsewhere'),{{status:409}}));
  else resumes[0].resolve({{handle:'old-handle',generation:'old-generation'}});
  assert.strictEqual(await stale,false);
  assert.strictEqual(TERMINAL_UI.handle,'new-handle');
  assert.strictEqual(TERMINAL_UI.generation,'new-generation');
  assert.strictEqual(TERMINAL_UI.mode,'claude_code');
  assert.strictEqual(FakeEventSource.instances.length,1);
  assert(FakeEventSource.instances[0].url.includes('new-handle'));
  console.log(JSON.stringify({{handle:TERMINAL_UI.handle,sources:FakeEventSource.instances.length,mode:TERMINAL_UI.mode}}));
"""
        )
    )
    assert result == {"handle": "new-handle", "sources": 1, "mode": "claude_code"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_same_session_stale_failure_before_new_success_keeps_new_request_in_charge():
    result = _run_node(
        _terminal_harness(
            """
  function deferred(){let resolve,reject;const promise=new Promise((ok,no)=>{resolve=ok;reject=no;});return {promise,resolve,reject};}
  const resumes=[deferred(),deferred()];
  let resumeIndex=0;
  global.api=async(path,options={})=>{
    const body=options.body?JSON.parse(options.body):null;
    calls.push({path,body,focusAtCall:focusCalls});
    if(path==='/api/claude-code/resume')return resumes[resumeIndex++].promise;
    return {ok:true};
  };
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  const stale=resumeClaudeSession(session);
  const current=resumeClaudeSession(session);
  resumes[0].reject(Object.assign(new Error('owned elsewhere'),{status:409}));
  assert.strictEqual(await stale,false);
  assert.strictEqual(TERMINAL_UI.mode,'claude_code');
  resumes[1].resolve({handle:'new-handle',generation:'new-generation'});
  assert.strictEqual(await current,true);
  assert.strictEqual(TERMINAL_UI.handle,'new-handle');
  assert.strictEqual(FakeEventSource.instances.length,1);
  console.log(JSON.stringify({handle:TERMINAL_UI.handle,sources:FakeEventSource.instances.length,mode:TERMINAL_UI.mode}));
"""
        )
    )
    assert result == {"handle": "new-handle", "sources": 1, "mode": "claude_code"}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("operation", ["input", "resize", "reconnect"])
def test_detach_during_token_mint_cannot_retry_or_reattach_claude_terminal(operation):
    result = _run_node(
        _terminal_harness(
            f"""
  function deferred(){{let resolve;const promise=new Promise(ok=>{{resolve=ok;}});return {{promise,resolve}};}}
  const session={{session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true}};
  await resumeClaudeSession(session);
  const mint=deferred();
  const beforeSources=FakeEventSource.instances.length;
  let operationCalls=0;
  global.api=async(path,options={{}})=>{{
    const body=options.body?JSON.parse(options.body):null;
    calls.push({{path,body,focusAtCall:focusCalls}});
    if(path==='/api/claude-code/terminal-token')return mint.promise;
    if(path==='/api/claude-code/terminal/input'||path==='/api/claude-code/terminal/resize'){{
      operationCalls+=1;
      throw Object.assign(new Error('expired'),{{status:404}});
    }}
    return {{ok:true}};
  }};
  let pending;
  if('{operation}'==='input'){{dataHandler('x');pending=TERMINAL_UI.inputQueue;}}
  else if('{operation}'==='resize')pending=_resizeClaudeTerminal();
  else pending=_reconnectClaudeTerminal();
  await new Promise(resolve=>setImmediate(resolve));
  detachClaudeTerminal();
  mint.resolve({{ok:true}});
  await pending;
  assert.strictEqual(operationCalls,'{operation}'==='reconnect'?0:1);
  assert.strictEqual(FakeEventSource.instances.length,beforeSources);
  assert.strictEqual(TERMINAL_UI.mode,'shell');
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  console.log(JSON.stringify({{operationCalls,sources:FakeEventSource.instances.length,beforeSources,mode:TERMINAL_UI.mode}}));
"""
        )
    )
    assert result["operationCalls"] == (0 if operation == "reconnect" else 1)
    assert result["sources"] == result["beforeSources"]
    assert result["mode"] == "shell"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_claude_reconnect_uses_last_sse_cursor_and_reset_clears_before_redraw():
    result = _run_node(
        _terminal_harness(
            """
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  await resumeClaudeSession(session);
  const first=FakeEventSource.instances[0];
  first.emit('output',JSON.stringify({text:'first'}),'7');
  document.hidden=true;
  listeners['document:visibilitychange']();
  document.hidden=false;
  listeners['document:visibilitychange']();
  await new Promise(resolve=>setImmediate(resolve));
  const second=FakeEventSource.instances[1];
  assert(second.url.includes('cursor=7'));
  assert(!second.url.includes('capability'));
  second.emit('terminal_reset',JSON.stringify({generation:'generation-1'}),'8');
  await new Promise(resolve=>setImmediate(resolve));
  const third=FakeEventSource.instances[2];
  assert(second.closed);
  assert(third.url.includes('cursor=8'));
  second.emit('output',JSON.stringify({text:'stranded'}),'9');
  third.emit('output',JSON.stringify({text:'redraw'}),'9');
  assert.strictEqual(clearCalls,1);
  assert.strictEqual(calls.filter(value=>value==='write:first').length,1);
  assert.strictEqual(calls.filter(value=>value==='write:redraw').length,1);
  assert.strictEqual(calls.filter(value=>value==='write:stranded').length,0);
  console.log(JSON.stringify({urls:FakeEventSource.instances.map(source=>source.url),clearCalls,writes:calls.filter(value=>typeof value==='string'&&value.startsWith('write:'))}));
"""
        )
    )
    assert result["clearCalls"] == 1
    assert result["writes"] == ["write:first", "write:redraw"]
    assert "cursor=7" in result["urls"][1]
    assert "cursor=8" in result["urls"][2]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("event,state", [("terminal_closed", "closed"), ("terminal_error", "error")])
def test_terminal_retirement_clears_authority_and_reconnect_resumes_public_session(
    event, state
):
    result = _run_node(
        _terminal_harness(
            f"""
  const session={{session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true}};
  await resumeClaudeSession(session);
  const retired=FakeEventSource.instances[0];
  retired.emit('{event}',JSON.stringify({{generation:'generation-1'}}),'11');
  assert(retired.closed);
  assert.strictEqual(TERMINAL_UI.claudeState,'{state}');
  assert.strictEqual(TERMINAL_UI.handle,null);
  assert.strictEqual(TERMINAL_UI.generation,null);
  assert.strictEqual(TERMINAL_UI.claudePublicSessionId,'opaque-session');
  const resumesBefore=calls.filter(call=>call.path==='/api/claude-code/resume').length;
  await restartComposerTerminal();
  assert.strictEqual(calls.filter(call=>call.path==='/api/claude-code/resume').length,resumesBefore+1);
  assert.strictEqual(TERMINAL_UI.claudeState,'live');
  assert.strictEqual(TERMINAL_UI.handle,'safe-handle');
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  console.log(JSON.stringify({{state:'{state}',resumed:calls.filter(call=>call.path==='/api/claude-code/resume').length,handle:TERMINAL_UI.handle}}));
"""
        )
    )
    assert result["state"] == state
    assert result["resumed"] == 2
    assert result["handle"] == "safe-handle"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_reconnect_404_retires_authority_and_next_reconnect_resumes_session():
    result = _run_node(
        _terminal_harness(
            """
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  await resumeClaudeSession(session);
  let failStream=true;
  const originalApi=global.api;
  global.api=async(path,options={})=>{
    if(path==='/api/claude-code/terminal-token'&&JSON.parse(options.body).operation==='stream'&&failStream){
      calls.push({path,body:JSON.parse(options.body)});
      throw Object.assign(new Error('retired'),{status:404});
    }
    return originalApi(path,options);
  };
  await restartComposerTerminal();
  assert.strictEqual(TERMINAL_UI.claudeState,'error');
  assert.strictEqual(TERMINAL_UI.handle,null);
  failStream=false;
  await restartComposerTerminal();
  assert.strictEqual(calls.filter(call=>call.path==='/api/claude-code/resume').length,2);
  assert.strictEqual(TERMINAL_UI.claudeState,'live');
  console.log(JSON.stringify({state:TERMINAL_UI.claudeState,resumes:calls.filter(call=>call.path==='/api/claude-code/resume').length}));
"""
        )
    )
    assert result == {"state": "live", "resumes": 2}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_short_visual_viewport_clamps_claude_terminal_without_overshoot():
    result = _run_node(
        _terminal_harness(
            """
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  await resumeClaudeSession(session);
  visualViewport.height=130;
  visualListeners.resize();
  const values=nodes.terminalViewport.style.values;
  const height=parseInt(values['--claude-terminal-height'],10);
  const min=parseInt(values['--claude-terminal-min-height'],10);
  const max=parseInt(values['--claude-terminal-max-height'],10);
  assert(height>0&&height<=34);
  assert(min>0&&min<=34);
  assert(max>0&&max<=34);
  assert(min<=height&&height<=max);
  console.log(JSON.stringify({height,min,max}));
"""
        )
    )
    assert 0 < result["min"] <= result["height"] <= result["max"] <= 34
    css = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert "--claude-terminal-min-height" in css
    assert "--claude-terminal-max-height" in css


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_claude_cursor_accepts_only_canonical_int64_event_ids():
    result = _run_node(
        _terminal_harness(
            """
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  await resumeClaudeSession(session);
  const source=FakeEventSource.instances[0];
  source.emit('output',JSON.stringify({text:'zero'}),'0');
  assert.strictEqual(TERMINAL_UI.claudeCursor,'0');
  source.emit('output',JSON.stringify({text:'max'}),'9223372036854775807');
  assert.strictEqual(TERMINAL_UI.claudeCursor,'9223372036854775807');
  for(const invalid of ['9223372036854775808','0001',' 9 ','1.5','x','9'.repeat(100000)]){
    source.emit('output',JSON.stringify({text:'ignored'}),invalid);
    assert.strictEqual(TERMINAL_UI.claudeCursor,'9223372036854775807');
  }
  _disconnectTerminalSource();
  await _reconnectClaudeTerminal();
  const boundaryUrl=FakeEventSource.instances[1].url;
  assert.strictEqual(new URL(boundaryUrl).searchParams.get('cursor'),'9223372036854775807');
  _disconnectTerminalSource();
  TERMINAL_UI.claudeCursor='9'.repeat(100000);
  await _reconnectClaudeTerminal();
  const safeUrl=FakeEventSource.instances[2].url;
  assert.strictEqual(new URL(safeUrl).searchParams.has('cursor'),false);
  assert(!safeUrl.includes('capability'));
  console.log(JSON.stringify({boundary:new URL(boundaryUrl).searchParams.get('cursor'),safeHasCursor:new URL(safeUrl).searchParams.has('cursor')}));
"""
        )
    )
    assert result == {
        "boundary": "9223372036854775807",
        "safeHasCursor": False,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_hiding_during_resume_prevents_attach_until_visible_without_stop():
    result = _run_node(
        _terminal_harness(
            """
  function deferred(){let resolve;const promise=new Promise(ok=>{resolve=ok;});return {promise,resolve};}
  const resumeGate=deferred();
  let resumeCalls=0;
  global.api=async(path,options={})=>{
    const body=options.body?JSON.parse(options.body):null;
    calls.push({path,body,focusAtCall:focusCalls});
    if(path==='/api/claude-code/resume'){
      resumeCalls+=1;
      if(resumeCalls===1)return resumeGate.promise;
      return {ok:true,attached:true,handle:'safe-handle',generation:'generation-1'};
    }
    return {ok:true};
  };
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  const pending=resumeClaudeSession(session);
  assert.strictEqual(focusCalls,1);
  document.hidden=true;
  listeners['document:visibilitychange']();
  resumeGate.resolve({ok:true,attached:false,handle:'safe-handle',generation:'generation-1'});
  assert.strictEqual(await pending,false);
  assert.strictEqual(FakeEventSource.instances.length,0);
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  document.hidden=false;
  listeners['document:visibilitychange']();
  await new Promise(resolve=>setImmediate(resolve));
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(FakeEventSource.instances.length,1);
  assert.strictEqual(resumeCalls,2);
  console.log(JSON.stringify({hiddenSources:0,visibleSources:FakeEventSource.instances.length,resumeCalls,stops:calls.filter(call=>call.path==='/api/claude-code/stop').length}));
"""
        )
    )
    assert result == {
        "hiddenSources": 0,
        "visibleSources": 1,
        "resumeCalls": 2,
        "stops": 0,
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_hiding_during_reconnect_token_mint_prevents_hidden_eventsource():
    result = _run_node(
        _terminal_harness(
            """
  function deferred(){let resolve;const promise=new Promise(ok=>{resolve=ok;});return {promise,resolve};}
  const session={session_id:'opaque-session',kind:'claude_code',profile:'qwen',label:'Claude Qwen',workspace_label:'Project',can_remote_resume:true};
  await resumeClaudeSession(session);
  const streamGate=deferred();
  let streamMints=0;
  const originalApi=global.api;
  global.api=async(path,options={})=>{
    if(path==='/api/claude-code/terminal-token'&&JSON.parse(options.body).operation==='stream'){
      calls.push({path,body:JSON.parse(options.body)});
      streamMints+=1;
      if(streamMints===1)return streamGate.promise;
      return {ok:true};
    }
    return originalApi(path,options);
  };
  const pending=_reconnectClaudeTerminal();
  await new Promise(resolve=>setImmediate(resolve));
  document.hidden=true;
  listeners['document:visibilitychange']();
  streamGate.resolve({ok:true});
  assert.strictEqual(await pending,false);
  assert.strictEqual(FakeEventSource.instances.length,1);
  assert(FakeEventSource.instances[0].closed);
  assert(!calls.some(call=>call.path==='/api/claude-code/stop'));
  document.hidden=false;
  listeners['document:visibilitychange']();
  await new Promise(resolve=>setImmediate(resolve));
  await new Promise(resolve=>setImmediate(resolve));
  assert.strictEqual(FakeEventSource.instances.length,2);
  console.log(JSON.stringify({hiddenSources:1,visibleSources:FakeEventSource.instances.length,streamMints,stops:calls.filter(call=>call.path==='/api/claude-code/stop').length}));
"""
        )
    )
    assert result == {
        "hiddenSources": 1,
        "visibleSources": 2,
        "streamMints": 2,
        "stops": 0,
    }
