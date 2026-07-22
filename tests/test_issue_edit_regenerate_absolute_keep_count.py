"""Regression: edit/regenerate use absolute keep_count (#2184 pattern)."""

import re
import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    needle_async = f"async function {name}"
    needle_sync = f"function {name}"
    start = src.find(needle_async)
    if start < 0:
        start = src.index(needle_sync)
    brace = src.index("{", start)
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"function {name!r} body not found")


def test_submit_edit_uses_absolute_keep_count():
    body = _function_body(UI_JS, "submitEdit")
    assert re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body)
    assert "keep_count: absoluteKeepCount" in body


def test_regenerate_uses_absolute_keep_count():
    body = _function_body(UI_JS, "regenerateResponse")
    assert re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*assistantIdx", body)
    assert "keep_count: absoluteKeepCount" in body


def test_submit_edit_captures_absolute_before_await():
    body = _function_body(UI_JS, "submitEdit")
    cap = re.search(r"absoluteKeepCount\s*=\s*_oldestIdx\s*\+\s*msgIdx", body)
    assert cap
    first_await = re.search(r"\bawait\b", body)
    assert first_await and cap.start() < first_await.start()


def test_truncated_edit_and_regenerate_do_not_force_full_reload():
    """A visible paginated target can be truncated without loading all history."""
    for name in ("submitEdit", "regenerateResponse"):
        body = "".join(_function_body(UI_JS, name).split())
        assert "if(!initialWindowTruncated&&typeof_ensureAllMessagesLoaded==='function')" in body
        assert "_loadedMessageSliceEndForKeepCount(absoluteKeepCount,currentWindowOffset,currentWindowTruncated)" in body


def test_loaded_window_slice_end_preserves_absolute_keep_count_semantics():
    """Translate only the local array slice; the API keep_count stays absolute."""
    helper = _function_body(UI_JS, "_loadedMessageSliceEndForKeepCount")
    script = f"""
const assert = require('assert');
{helper}
assert.strictEqual(_loadedMessageSliceEndForKeepCount(103, 70, true), 33);
assert.strictEqual(_loadedMessageSliceEndForKeepCount(103, 0, false), 103);
assert.strictEqual(_loadedMessageSliceEndForKeepCount(0, 70, true), 0);
"""
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def _run_pagination_race(function_name: str) -> dict:
    helper = _function_body(UI_JS, "_loadedMessageSliceEndForKeepCount")
    mutation = _function_body(UI_JS, function_name)
    invocation = (
        "submitEdit(20, 'replacement')"
        if function_name == "submitEdit"
        else "regenerateResponse({closest:()=>({dataset:{msgIdx:'20'}})})"
    )
    script = f"""
const makeMessages=(start,end)=>Array.from({{length:end-start}},(_,i)=>{{
  const absolute=start+i;
  return {{role:absolute%2===0?'assistant':'user',content:'abs-'+absolute}};
}});
const S={{session:{{session_id:'sid-race'}},busy:false,messages:makeMessages(70,100)}};
let _oldestIdx=70;
let _messagesTruncated=true;
let resolveTruncate;
const truncatePending=new Promise(resolve=>{{resolveTruncate=resolve;}});
const calls=[];
async function api(url,opts){{calls.push({{url,opts}});return truncatePending;}}
function _ensureAllMessagesLoaded(){{throw new Error('truncated window must not full-load');}}
function _deliberateSessionModelPick(){{return null;}}
function _reArmRecoveryPick(){{}}
function renderMessages(){{}}
function msgContent(message){{return String(message&&message.content||'');}}
function setStatus(message){{throw new Error(message);}}
function send(){{return Promise.resolve();}}
const input={{value:''}};
function $(id){{return id==='msg'?input:null;}}
{helper}
{mutation}
(async()=>{{
  const pending={invocation};
  await Promise.resolve();
  S.messages=makeMessages(40,100);
  _oldestIdx=40;
  _messagesTruncated=true;
  resolveTruncate({{ok:true}});
  await pending;
  process.stdout.write(JSON.stringify({{
    keepCount:JSON.parse(calls[0].opts.body).keep_count,
    rows:S.messages.map(message=>message.content),
    input:input.value,
  }}));
}})().catch(error=>{{console.error(error.stack||error);process.exit(1);}});
"""
    completed = subprocess.run(
        ["node", "-e", script], check=False, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_edit_and_regenerate_slice_from_current_window_after_truncate_await():
    for function_name in ("submitEdit", "regenerateResponse"):
        result = _run_pagination_race(function_name)
        assert result["keepCount"] == 90
        assert result["rows"] == [f"abs-{absolute}" for absolute in range(40, 90)]
