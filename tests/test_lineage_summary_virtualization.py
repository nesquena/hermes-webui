"""Measured-height windows and rendered-list scroll ownership."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')


def run_js(body):
    if NODE is None:
        pytest.skip('node not on PATH')
    source = (ROOT / 'static/sessions.js').read_text()
    names = ['_sessionVirtualWindow', '_scheduleSessionVirtualizedRender']
    import re
    functions = []
    for name in names:
        match = re.search(r'^function ' + name + r'\(.*?^\}', source, re.M | re.S)
        assert match, name
        functions.append(match.group())
    prelude = """
const SESSION_VIRTUAL_THRESHOLD_ROWS=80,SESSION_VIRTUAL_ROW_HEIGHT=52,SESSION_VIRTUAL_BUFFER_ROWS=12;
let _sessionListLastScrollAt=0,_sessionListSkeletonActive=false,_renamingSid=null,_sessionVirtualScrollRaf=0;
let _sessionVirtualScrollList={dataset:{sessionVirtualEnabled:'false',sessionVirtualTotal:'120'},scrollTop:1000,clientHeight:520};
let renders=0,callbacks=[];
function renderSessionListFromCache(){renders++;}
function requestAnimationFrame(cb){callbacks.push(cb);return callbacks.length;}
"""
    result = subprocess.run([NODE, '-e', prelude + '\n'.join(functions) + body], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_variable_height_rows_use_prefix_sum_spacers():
    result = run_js("""
const offsets=[0];
for(let i=0;i<120;i++)offsets.push(offsets.at(-1)+(i%3===0?96:52));
const variable=_sessionVirtualWindow({total:120,scrollTop:1000,viewportHeight:520,offsets,variableHeight:true});
const compact=_sessionVirtualWindow({total:120,scrollTop:1000,viewportHeight:520});
console.log(JSON.stringify({variable,compact}));
""")
    variable = result['variable']
    assert variable['virtualized'] is True, 'Detailed lineage summaries must stay windowed'
    assert variable['end'] - variable['start'] < 40
    offsets = [0]
    for i in range(120):
        offsets.append(offsets[-1] + (96 if i % 3 == 0 else 52))
    assert variable['topPad'] == offsets[variable['start']]
    assert variable['bottomPad'] == offsets[120] - offsets[variable['end']]
    assert result['compact']['virtualized'] is True
    assert result['compact']['end'] - result['compact']['start'] < 120


def test_scroll_scheduler_uses_rendered_window_mode_even_for_queued_callback():
    result = run_js("""
_scheduleSessionVirtualizedRender();
const bypassCallbacks=callbacks.length;
_sessionVirtualScrollList.dataset.sessionVirtualEnabled='true';
_scheduleSessionVirtualizedRender();
// A density/metadata render can change mode while a compact RAF is pending.
_sessionVirtualScrollList.dataset.sessionVirtualEnabled='false';
for(const cb of callbacks)cb();
console.log(JSON.stringify({bypassCallbacks,renders}));
""")
    assert result['bypassCallbacks'] == 0, 'Variable-height scrolling must not queue DOM rebuilding'
    assert result['renders'] == 0, 'Pending compact callback must respect new Detailed mode'
