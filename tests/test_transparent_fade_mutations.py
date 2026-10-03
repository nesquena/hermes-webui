"""Recency fading must update its visual state without rewriting old rows."""
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_transparent_fade_only_mutates_changed_attributes():
    source = (ROOT / 'static/ui.js').read_text()
    start = source.index('function _applyTransparentRowFading(turn){')
    end = source.index('\n}\n', start) + 2
    # These fakes implement the DOM mutation semantics we care about:
    # setAttribute records a mutation even when unchanged; removing an absent
    # attribute does not. Browser-level confirmation uses the real fixture.
    script = r'''
const assert = require('node:assert/strict');
let writes=0;
function row(){
  const attrs=new Map();
  return {
    getAttribute(name){return attrs.has(name)?attrs.get(name):null;},
    setAttribute(name,value){writes++;attrs.set(name,String(value));},
    removeAttribute(name){if(attrs.delete(name))writes++;},
  };
}
const rows=Array.from({length:6},row);
const turn={id:'liveAssistantTurn',getAttribute:()=>null};
const _assistantTurnBlocks=()=>({querySelectorAll:()=>rows});
const isTransparentStream=()=>true;
__FUNCTION__
const values=()=>rows.map(r=>r.getAttribute('data-transparent-fade'));
_applyTransparentRowFading(turn);
assert.deepEqual(values(),['5','4','3','2','1',null]);
assert.equal(writes,5);
writes=0;
_applyTransparentRowFading(turn);
assert.equal(writes,0,'unchanged rows must not mutate on a prose update');
rows.push(row());
_applyTransparentRowFading(turn);
assert.deepEqual(values(),['5','5','4','3','2','1',null]);
assert.equal(writes,5,'only the five rows whose fade changes should mutate');
rows.splice(2,1);
_applyTransparentRowFading(turn);
assert.deepEqual(values(),['5','4','3','2','1',null]);
turn.id='settled-turn';
_applyTransparentRowFading(turn);
assert.deepEqual(values(),[null,null,null,null,null,null]);
writes=0;
_applyTransparentRowFading(turn);
assert.equal(writes,0,'settled rows stay clear without repeated mutations');
turn.id='liveAssistantTurn';
_applyTransparentRowFading(turn);
assert.deepEqual(values(),['5','4','3','2','1',null]);
console.log(JSON.stringify({passed:true}));
'''.replace('__FUNCTION__', source[start:end])
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'passed': True}
