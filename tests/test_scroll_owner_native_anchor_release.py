"""Execute the window transaction, including its error and overlapping owners."""
import json
import pytest
from tests.test_issue500_message_list_virtualization import _run_node, _extract_func_script, UI_JS_PATH


@pytest.mark.parametrize('prior', ['', 'auto', 'none'])
@pytest.mark.parametrize('failure', ['', 'insert', 'restore', 'ownership'])
def test_commit_releases_only_its_inline_override(prior, failure):
    source = _extract_func_script(UI_JS_PATH.read_text()) + r"""
const assert=require('assert');
const container={style:{overflowAnchor:PRIOR},get offsetHeight(){return 700;}};
const $=()=>container, S={session:{session_id:'one'}};
const target={children:[],dataset:{},querySelectorAll:()=>[]};
const staged={children:FAILURE==='insert'?[{outerHTML:'<div></div>'}]:[]};
target.insertBefore=()=>{assert.equal(container.style.overflowAnchor,'none');throw Error('insert');};
function _restoreMessageWindowReader(){
 assert.equal(container.style.overflowAnchor,'none');
 if(FAILURE==='restore') throw Error('restore');
}
function _initializeMessageWindowOwnership(){if(FAILURE==='ownership') throw Error('ownership');}
function _messageWindowNodeKey(){return '';}
function _messageRawIdxForSessionIndex(i){return i;}
eval(extractFunc('_commitMessageWindow'));
let error='';
try{_commitMessageWindow(target,staged,null,false);}catch(e){error=e.message;}
assert.equal(error,FAILURE);
assert.equal(container.style.overflowAnchor,PRIOR);
console.log('ok');
"""
    source = source.replace('PRIOR', json.dumps(prior)).replace('FAILURE', json.dumps(failure))
    assert _run_node(source) == 'ok'


def test_nested_commit_retains_outer_suppression():
    source = _extract_func_script(UI_JS_PATH.read_text()) + r"""
const assert=require('assert');
const container={style:{overflowAnchor:''},get offsetHeight(){return 700;}};
const $=()=>container, S={session:{session_id:'one'}};
const target={children:[],dataset:{},querySelectorAll:()=>[]};
const staged={children:[]};
let inside=false, calls=0;
function _restoreMessageWindowReader(){
 assert.equal(container.style.overflowAnchor,'none');
 if(!inside){
   inside=true;
   _commitMessageWindow(target,staged,null,false);
   assert.equal(container.style.overflowAnchor,'none');
 }
 calls++;
}
function _initializeMessageWindowOwnership(){}
function _messageWindowNodeKey(){return '';}
function _messageRawIdxForSessionIndex(i){return i;}
eval(extractFunc('_commitMessageWindow'));
_commitMessageWindow(target,staged,null,false);
assert.equal(container.style.overflowAnchor,'');
assert.equal(calls,2);
console.log('ok');
"""
    assert _run_node(source) == 'ok'


@pytest.mark.timeout(60)
def test_browser_native_anchor_release():
    import subprocess
    import sys
    from pathlib import Path

    pytest.importorskip('playwright.sync_api')
    script = Path(__file__).with_name('browser_native_anchor_release.py')
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=50,
    )
    assert result.returncode == 0, result.stdout + result.stderr

