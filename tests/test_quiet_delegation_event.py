"""Execute the live completion handler: child events stay quiet, process toasts survive."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_delegation_completion_suppresses_toast_but_still_acks():
    script = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[1],'utf8');
const start=src.indexOf('function _handleBgTaskCompleteEvent(');
if(start<0)throw new Error('completion handler missing');
const brace=src.indexOf('{',start);
let depth=0,end=0;
for(let i=brace;i<src.length;i++){
  if(src[i]==='{')depth++;
  else if(src[i]==='}'&&!--depth){end=i+1;break;}
}
let toast=[],ack=[],marked=[],cleared=[];
const S={session:{session_id:'sid',message_count:9},messages:[]};
const seen=new Set();
function _bgTaskCompleteRingBufferAdd(sid,id){
  if(seen.has(sid+id))return true;
  seen.add(sid+id);return false;
}
function _isSessionActivelyViewed(){return false;}
function _markSessionViewed(...args){marked.push(args);}
function _clearSessionCompletionUnread(...args){cleared.push(args);}
function _apiUrl(path){return path;}
function showToast(...args){toast.push(args);}
function fetch(path,opts){ack.push([path,JSON.parse(opts.body)]);return Promise.resolve();}
eval(src.slice(start,end));
function event(id,kind){return {data:JSON.stringify({session_id:'sid',event_id:id,task_id:'task-123',summary:'done',...(kind?{kind}:{})})};}
_handleBgTaskCompleteEvent(event('child','async_delegation'),'sid',{});
_handleBgTaskCompleteEvent(event('child','async_delegation'),'sid',{});
_handleBgTaskCompleteEvent(event('ordinary'),'sid',{});
_handleBgTaskCompleteEvent(event('wrong','async_delegation'),'other',{});
console.log(JSON.stringify({toast,ack,marked,cleared}));
"""
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script, str(ROOT / "static" / "messages.js")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    assert outcome["toast"] == [["Task task-123 done: done", 2600]]
    assert [item[1]["event_id"] for item in outcome["ack"]] == ["child", "ordinary"]
    assert all(item[0] == "api/bg-task-complete-ack" for item in outcome["ack"])
    assert outcome["marked"] == []
    assert outcome["cleared"] == []
