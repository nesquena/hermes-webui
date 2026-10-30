"""Behavior/work oracle for the residual semantic parser, independent of DOM scheduling."""
import json
import subprocess
from pathlib import Path

from tests.test_issue3455_think_block_extraction import _extract_block

ROOT = Path(__file__).resolve().parents[1]


def test_spaced_publication_does_not_rescan_growing_prose():
    source = (ROOT / 'static/messages.js').read_text()
    names = ['_thinkingFenceMarkerAt', '_nextThinkingOpener', '_textTailIsPartialOpener',
             '_lineIsIndentedCode', '_mergeInlineThinkingReasoning',
             '_extractInlineThinkingFromContent', '_splitThinkFromContent',
             'syncInflightAssistantMessage', '_stripXmlToolCalls', '_parseStreamState']
    script = _extract_block(source, 'const _thinkPairs=') + ';\n'
    script += '\n'.join(_extract_block(source, 'function '+name+'(') for name in names)
    # Existing master is a supported subject, not an absent-symbol failure.
    incremental = 'function _createIncrementalSemanticState(' in source
    if incremental:
        for name in ['_createIncrementalSemanticState', '_consumeSemanticProse',
                     '_resyncSemanticProse', '_semanticSnapshot', '_parseStreamStateBatch',
                     '_durableSemanticSnapshot']:
            script += '\n' + _extract_block(source, 'function '+name+'(')
    script += r'''
const assert=require('node:assert/strict');
let assistantText='',reasoningText='',liveReasoningText='',segmentStart=0;
const activeSid='s',INFLIGHT={s:{messages:[]}},_throttledPersist=()=>{};
let _semanticState=INCREMENTAL?_createIncrementalSemanticState():null;
let _durableSemanticState=INCREMENTAL?_createIncrementalSemanticState(undefined,true):null;
let _segmentSemanticState=null;
let _semanticCursor=0,_semanticSegmentStart=0,_semanticRaw='',_semanticFallback=null,_semanticFallbackCursor=-1;
const _semanticMetrics={fullParses:0,fullBytes:0,resyncReasons:{}};
const batch=_extractInlineThinkingFromContent;
let bytes=0,calls=0;
_extractInlineThinkingFromContent=(text,...args)=>{calls++;bytes+=text.length;return batch(text,...args);};
for(let i=0;i<4000;i++){
 const delta=i===2000?' << literal ':'0123456789';
 assistantText+=delta;
 if(INCREMENTAL)_consumeSemanticProse(delta);
 syncInflightAssistantMessage();
 const painted=_parseStreamState();
 assert.equal(painted.displayText,assistantText);
 assert.equal(INFLIGHT.s.messages[0].content,assistantText);
}
console.log(JSON.stringify({calls,bytes,size:assistantText.length}));
assert.ok(bytes<=assistantText.length*4,`growing-history scan budget exceeded: ${bytes}`);
'''.replace('INCREMENTAL', json.dumps(incremental))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
