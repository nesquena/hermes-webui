"""Execute the live title listener and delayed done rebind in Node."""
import json
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('delayed', [False, True])
def test_rotated_title_reaches_continuation_after_done(delayed):
    source = (Path(__file__).parents[1] / 'static/messages.js').read_text()
    start = source.index("    source.addEventListener('title',")
    end = source.index("    source.addEventListener('title_status',", start)
    listener = source[start:end]
    start = source.index('function applySessionTitleUpdate(')
    end = source.index('\n}\n', start) + 3
    apply = source[start:end]
    # Execute the actual pending-title drain from the done rebind, not a
    # duplicate implementation. It runs after S.session becomes done.session.
    marker = '          if(_pendingTitleUpdates.has(completedSid))'
    drain = ''
    if marker in source:
        start = source.index(marker)
        end = source.index('\n          }', start) + len('\n          }')
        drain = source[start:end]
    script = '''
const assert=require('assert');
const activeSid='A';
const S={session:{session_id:'A',title:'New Chat'}};
const _allSessions=[{session_id:'B',title:'New Chat'}];
const _sessionTitleProvisionalBySid=new Map();
const _pendingTitleUpdates=new Map();
const _firstUserMessageTitleCandidate=()=>'';
const _sessionTitleLooksDefaultOrProvisional=t=>!t||t==='New Chat';
let onTitle;
const source={addEventListener:(_,fn)=>onTitle=fn};
''' + apply + listener + '''
const completedSid='B';
function finishDone(){
 S.session={session_id:'B',title:'New Chat'};
''' + drain + '''
}
if(!DELAYED) finishDone();
onTitle({data:JSON.stringify({session_id:'A',target_session_id:'B',title:'Recovered title'})});
if(DELAYED) finishDone();
assert.equal(S.session.session_id,'B');
assert.equal(S.session.title,'Recovered title');
assert.equal(_allSessions[0].title,'Recovered title');
onTitle({data:JSON.stringify({session_id:'unrelated',target_session_id:'B',title:'Wrong'})});
assert.equal(S.session.title,'Recovered title');
S.session.title='Manual title';
onTitle({data:JSON.stringify({session_id:'A',target_session_id:'B',title:'Overwrite'})});
assert.equal(S.session.title,'Manual title');
'''
    result = subprocess.run(['node', '-e', script.replace('DELAYED', json.dumps(delayed))], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
