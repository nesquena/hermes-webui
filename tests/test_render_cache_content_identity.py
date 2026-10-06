"""Render caching must preserve complete content identity across long messages."""
import json
import os
from pathlib import Path

import pytest

from tests.test_issue500_message_list_virtualization import _run_node, _extract_func_script, UI_JS_PATH


@pytest.mark.parametrize('mode', ['assistant', 'user-markdown', 'user-plain'])
def test_equal_length_prefix_suffix_messages_do_not_share_cached_body(mode):
    source = Path(os.environ.get('SCROLL_CACHE_SOURCE', UI_JS_PATH)).read_text()
    script = _extract_func_script(source) + r"""
const _renderCache=new Map(),_renderCacheMax=300;
let calls=0;
const renderMd=text=>{calls++;return 'markdown:'+text;};
const _renderUserFencedBlocks=text=>{calls++;return 'plain:'+text;};
const _stripXmlToolCallsDisplay=text=>text;
const window={_renderUserMarkdown:MODE==='user-markdown'};
eval(extractFunc('_renderCacheKey'));
eval(extractFunc('_getCachedRender'));
const first='shared prefix '.repeat(10)+'ANSWER ZERO'+' shared suffix'.repeat(40);
const second=first.replace('ANSWER ZERO','ANSWER NINE');
const user=MODE!=='assistant';
const a=_getCachedRender(first,user),b=_getCachedRender(second,user);
const again=_getCachedRender(first,user);
console.log(JSON.stringify({different:a!==b,first:a.includes('ANSWER ZERO'),
 second:b.includes('ANSWER NINE'),reused:again===a,calls}));
"""
    result = json.loads(_run_node('const MODE='+json.dumps(mode)+';\n'+script))
    assert result == dict(different=True, first=True, second=True, reused=True, calls=2)
