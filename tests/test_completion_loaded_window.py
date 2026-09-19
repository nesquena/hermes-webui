"""Loaded pagination boundaries survive unsolicited full-session payloads."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_loaded_window_projection():
    source = (ROOT / 'static/sessions.js').read_text()
    start = source.find('function _captureLoadedMessageWindow(')
    assert start >= 0, 'missing loaded-window preservation'
    end = source.index('function _captureSameSessionForceReloadHint(', start)
    script = """
const assert=require('node:assert/strict');
let S={session:{session_id:'a',message_count:6},messages:[{role:'user',content:'q4'},{role:'assistant',content:'a5'}]};
let _messagesTruncated=true,_oldestIdx=4;
""" + source[start:end] + """
const full={session_id:'a',message_count:8,messages:Array.from({length:8},(_,i)=>({role:i%2?'assistant':'user',content:(i%2?'a':'q')+i}))};
const hint=_captureLoadedMessageWindow('a');
full.todo_state={items:[{id:'older-task',content:'Keep historical task',status:'pending'}]};
const saved=JSON.stringify(full);
const projected=_preserveLoadedMessageWindow(full,hint);
assert.deepEqual(projected.messages,full.messages.slice(4));
assert.equal(projected._messages_offset,4);
assert.equal(projected._messages_truncated,true);
assert.equal(projected.message_count,8);
assert.equal(projected.todo_state,full.todo_state,'authoritative metadata lost');
assert.equal(JSON.stringify(full),saved,'transport payload mutated');
assert.equal(_preserveLoadedMessageWindow(full,null),full);
assert.equal(_preserveLoadedMessageWindow({...full,session_id:'b'},hint).messages.length,8);
assert.equal(_preserveLoadedMessageWindow({...full,regeneration_revision:1},hint).messages.length,8);
const changed={...full,messages:full.messages.map(m=>({...m}))};changed.messages[4].content='edited';
assert.equal(_preserveLoadedMessageWindow(changed,hint),changed);
const shrunk={...full,message_count:5,messages:full.messages.slice(0,5)};
assert.equal(_preserveLoadedMessageWindow(shrunk,hint),shrunk);
const paged={...full,_messages_offset:2,_messages_truncated:true,messages:full.messages.slice(2)};
assert.deepEqual(_preserveLoadedMessageWindow(paged,hint).messages,full.messages.slice(4));
const narrower={...full,_messages_offset:6,_messages_truncated:true,messages:full.messages.slice(6)};
assert.equal(_preserveLoadedMessageWindow(narrower,hint),narrower);
for(const bad of [-1,NaN,Infinity,1.5,'4']) { _oldestIdx=bad;assert.equal(_captureLoadedMessageWindow('a'),null); }
_oldestIdx=0;assert.equal(_captureLoadedMessageWindow('a'),null);
_oldestIdx=4;_messagesTruncated=false;assert.equal(_captureLoadedMessageWindow('a'),null);
_messagesTruncated=true;assert.equal(_captureLoadedMessageWindow('b'),null);
console.log('loaded-window boundaries passed');
"""
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
