"""Static source assertions for issue #4346 virtual-scroll footer jitter fix."""
import pathlib
import re

ROOT = pathlib.Path(__file__).parent.parent

CSS = (ROOT / 'static' / 'style.css').read_text(encoding='utf-8')
JS  = (ROOT / 'static' / 'ui.js').read_text(encoding='utf-8')


def test_css_vscroll_measuring_guard():
    """style.css suppresses opacity transitions on .msg-foot and .msg-actions
    while .vscroll-measuring is present on the scroll container."""
    assert 'vscroll-measuring' in CSS
    assert re.search(
        r'\.vscroll-measuring\s+\.msg-foot.*transition\s*:\s*none\s*!important',
        CSS, re.DOTALL
    ), "missing transition:none !important for .vscroll-measuring .msg-foot"
    assert re.search(
        r'\.vscroll-measuring\s+\.msg-actions.*transition\s*:\s*none\s*!important',
        CSS, re.DOTALL
    ), "missing transition:none !important for .vscroll-measuring .msg-actions"
    assert re.search(
        r'\.vscroll-measuring\s+\.msg-time.*transition\s*:\s*none\s*!important',
        CSS, re.DOTALL
    ), "missing transition:none !important for .vscroll-measuring .msg-time"


def test_js_compensate_adds_vscroll_measuring():
    """_compensateScrollForMeasurementDelta adds and removes the vscroll-measuring
    class around the render callback."""
    fn_match = re.search(
        r'function _compensateScrollForMeasurementDelta\(renderFn\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match, "_compensateScrollForMeasurementDelta not found"
    body = fn_match.group(1)
    assert "classList.add('vscroll-measuring')" in body
    assert "classList.remove('vscroll-measuring')" in body


def test_js_try_finally_guards_class_removal():
    """The classList.remove is inside the finally{} block, not after it."""
    fn_match = re.search(
        r'function _compensateScrollForMeasurementDelta\(renderFn\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match
    body = fn_match.group(1)
    try_idx = body.find('try{')
    finally_idx = body.find('finally{')
    remove_idx = body.find("classList.remove('vscroll-measuring')")
    assert try_idx != -1, "no try block found in _compensateScrollForMeasurementDelta"
    assert finally_idx != -1, "no finally block found in _compensateScrollForMeasurementDelta"
    assert remove_idx != -1, "missing classList.remove('vscroll-measuring')"
    assert try_idx < finally_idx < remove_idx, \
        "classList.remove must remain in the finally{} cleanup path"


def test_js_recycle_flag_exists():
    """ui.js declares the _msgNodeRecycleEnabled flag."""
    assert '_msgNodeRecycleEnabled' in JS


def test_js_recycle_stash_exists():
    """ui.js declares the _recycleStash Map."""
    assert '_recycleStash' in JS


def test_js_recycle_flag_set_in_virtual_render():
    """_scheduleMessageVirtualizedRender sets _msgNodeRecycleEnabled=true
    before the compensate call and clears it in finally."""
    fn_match = re.search(
        r'function _scheduleMessageVirtualizedRender\(force\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match, "_scheduleMessageVirtualizedRender not found"
    body = fn_match.group(1)
    assert '_msgNodeRecycleEnabled=true' in body
    finally_match = re.search(r'finally\{([^}]*)\}', body)
    assert finally_match, "no finally block in _scheduleMessageVirtualizedRender"
    assert '_msgNodeRecycleEnabled=false' in finally_match.group(1)


def test_js_stash_populated_before_wipe():
    """The recycleStash population loop appears before innerHTML='' in renderMessages."""
    fn_match = re.search(
        r'function renderMessages\(options\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match, "renderMessages not found"
    body = fn_match.group(1)
    stash_pos = body.find('_recycleStash.set(')
    wipe_pos = body.find("inner.innerHTML='';")
    assert stash_pos != -1, "_recycleStash.set not found in renderMessages"
    assert wipe_pos != -1, "innerHTML wipe not found in renderMessages"
    assert stash_pos < wipe_pos, "_recycleStash.set must appear before innerHTML=''"


def test_js_user_row_checks_stash():
    """The user-row creation block checks _recycleStash before createElement."""
    fn_match = re.search(
        r'function renderMessages\(options\)\{(.+?)^(?=function )',
        JS, re.DOTALL | re.MULTILINE
    )
    assert fn_match, "renderMessages not found"
    body = fn_match.group(1)
    assert '_recycleStash.get(rawIdx)' in body, \
        "user row block must check _recycleStash.get(rawIdx)"
