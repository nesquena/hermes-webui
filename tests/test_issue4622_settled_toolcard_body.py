"""Regression coverage for #4622: settled tool cards lose their output body.

In the Transparent Stream / Worklog view, a terminal tool card stopped showing
its stdout and a patch/edit card stopped rendering its diff *after the stream
settled* (live cards were fine). The settled rebuild
(`_anchorSceneRowsByMessageIndex`) builds each tool row from
`messages[].tool_calls` (state.db / sidecar), which can carry only a short
preview — or, on a cold/paginated load, nothing — for the result body. The full
output lives on the live `S.toolCalls` entry at settle time, but the dedup loop
*skipped* the matching live entry instead of restoring the missing body onto the
surviving settled row.

This drives the REAL settled render chain end-to-end:
  _anchorSceneRowsByMessageIndex (messages.js)
    -> _anchorSceneToolRowFromCall (messages.js)        # settled row
    -> _enrichSettledToolRowBodyFromLive (messages.js)  # the fix
  -> _anchorSceneToolCallFromRow (ui.js, settled)       # the `tc` buildToolCard consumes

`tc.snippet` is what buildToolCard renders as the output body (full copy in the
Show-more `data-full`) and what the transparent "Output" tab shows; `tc.args` is
the detailed-input "Full" tab. Asserting on `tc` pins all three views without a
DOM.

The prior #4625 guard only exercised the LIVE `buildToolCard` path, which is why
it stayed green while the settled path regressed — these tests MUST drive the
settled rebuild.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Driver: extract the real settled-render functions from messages.js + ui.js by
# brace-matching, run them in node with the closure vars they reference, drive
# _anchorSceneRowsByMessageIndex -> _anchorSceneToolCallFromRow, and emit the
# `tc` object each settled tool card would be built from.
_DRIVER_SRC = r"""
'use strict';
const fs = require('fs');
const mSrc = fs.readFileSync(process.argv[2], 'utf8');
const uSrc = fs.readFileSync(process.argv[3], 'utf8');
function extractFunc(src, name) {
  const re = new RegExp('function\\s+' + name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++; else if (src[i] === '}') depth--; i++;
  }
  return src.slice(start, i);
}
const messagesFns = [
  '_anchorSceneMessageText','_anchorSceneCleanText','_anchorSceneTextKey',
  '_anchorSceneSafePayload','_anchorSceneToolId','_anchorSceneToolName',
  '_anchorSceneToolArgs','_anchorSceneStringPayload','_anchorSceneRowBase',
  '_anchorSceneProseRow','_anchorSceneThinkingRow','_anchorSceneToolRowFromCall',
  '_anchorSceneMessageReasoningText','_enrichSettledToolRowBodyFromLive',
  '_anchorSceneRowsByMessageIndex',
];
const uiFns = ['_anchorSceneToolCallFromRow'];
let code = '(function(){\n';
code += 'var activeSid="test-session"; var streamId="test-stream"; var S;\n';
for (const n of messagesFns) code += extractFunc(mSrc, n) + '\n';
for (const n of uiFns) code += extractFunc(uSrc, n) + '\n';
code += `
var buf='';
process.stdin.on('data',c=>buf+=c);
process.stdin.on('end',()=>{
  var p=JSON.parse(buf||'{}');
  S=p.S||{toolCalls:[]};
  var messages=p.messages||[];
  var turnStart=p.turnStart!==undefined?p.turnStart:0;
  var lastAsstIndex=p.lastAsstIndex!==undefined?p.lastAsstIndex:messages.length-1;
  var byIdx=_anchorSceneRowsByMessageIndex(messages,turnStart,lastAsstIndex);
  var cards=[];
  byIdx.forEach(function(rows,idx){
    rows.forEach(function(row){
      if(row.role!=='tool') return;
      var tc=_anchorSceneToolCallFromRow(row,{settled:true});
      var outputTab=String((tc.snippet||tc.preview||tc.result||tc.output)||'').trim();
      cards.push({
        idx:idx, name:tc.name, tid:tc.tid,
        snippet:tc.snippet||'', snippetLen:(tc.snippet||'').length,
        args:tc.args||{}, command:tc.command||'',
        rendersOutputBody:!!tc.snippet,
        rendersDiff:/^@@\\s/.test(tc.snippet||'') && (tc.snippet||'').split('\\n').filter(l=>l[0]==='+'||l[0]==='-').length>=2,
        transparentOutputTabNonEmpty:!!outputTab,
        transparentFullTabArgKeys:Object.keys((tc.args&&typeof tc.args==='object')?tc.args:{}),
      });
    });
  });
  process.stdout.write(JSON.stringify(cards));
});
})();
`;
process.stdout.write(code);
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    gen = tmp_path_factory.mktemp("gen") / "gen.js"
    gen.write_text(_DRIVER_SRC, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(gen), str(MESSAGES_JS), str(UI_JS)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        pytest.fail(f"Driver generation failed:\n{result.stderr}")
    driver = tmp_path_factory.mktemp("driver") / "driver.js"
    driver.write_text(result.stdout, encoding="utf-8")
    return str(driver)


def _run(driver_path: str, payload: dict) -> list:
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path], input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node error:\n{result.stderr}")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Builders for the reporter's scenario: a settled turn where the persisted
# state.db tool_calls lack the body, but the live S.toolCalls carries the full
# output + diff. This is the reconnect/settle reconciliation.
# ---------------------------------------------------------------------------

_TERM_OUTPUT = (
    "total 48\n"
    "drwxr-xr-x 6 user staff 192 .\n"
    "-rw-r--r-- 1 user staff 1024 app.py\n"
    "-rw-r--r-- 1 user staff 2048 README.md\n"
    "-rw-r--r-- 1 user staff  512 config.yaml"
)
_DIFF = "@@ -1,4 +1,5 @@\n def main():\n-    print('old')\n+    print('new')\n+    return 0\n     pass"


def _settled_then_live(term_persisted="", patch_persisted="", patch_args=None):
    """Turn with a terminal + edit_file call; persisted rows carry `*_persisted`
    bodies, live S.toolCalls carries the full output + diff."""
    messages = [
        {"role": "user", "content": "run ls and patch a file"},
        {
            "role": "assistant",
            "content": "Done.",
            "tool_calls": [
                {"id": "term-1", "name": "terminal", "started_at": 100,
                 "command": "ls -la", "snippet": term_persisted},
                {"id": "patch-1", "name": "edit_file", "started_at": 200,
                 "args": patch_args or {"path": "x.py"}, "snippet": patch_persisted},
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]
    S = {"toolCalls": [
        {"id": "term-1", "name": "terminal", "assistant_msg_idx": 1, "started_at": 100,
         "command": "ls -la", "snippet": _TERM_OUTPUT},
        {"id": "patch-1", "name": "edit_file", "assistant_msg_idx": 1, "started_at": 200,
         "args": {"path": "x.py"}, "snippet": _DIFF},
    ]}
    return {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": S}


def _card(cards, name):
    matches = [c for c in cards if c["name"] == name]
    assert matches, f"no settled card for {name}: {[c['name'] for c in cards]}"
    return matches[0]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_settled_terminal_output_restored_when_persisted_body_empty(driver_path):
    """Empty persisted snippet -> full terminal stdout restored from live S.toolCalls."""
    cards = _run(driver_path, _settled_then_live(term_persisted="", patch_persisted=""))
    term = _card(cards, "terminal")
    assert term["rendersOutputBody"] is True, "terminal output body must render after settle"
    assert term["snippet"] == _TERM_OUTPUT, "settled card must carry the FULL stdout (Show-more data-full)"
    assert term["transparentOutputTabNonEmpty"] is True, "transparent 'Output' tab must be non-empty"


def test_settled_patch_diff_restored_when_persisted_body_empty(driver_path):
    """Empty persisted snippet -> patch/edit diff body restored + renders as a diff."""
    cards = _run(driver_path, _settled_then_live(term_persisted="", patch_persisted=""))
    patch = _card(cards, "edit_file")
    assert patch["rendersOutputBody"] is True, "patch card body must render after settle"
    assert patch["rendersDiff"] is True, "patch/edit card must render the diff body after settle"
    assert patch["snippet"] == _DIFF
    # "Full" tab keeps the detailed input args.
    assert "path" in patch["transparentFullTabArgKeys"]


def test_settled_body_not_clobbered_when_persisted_value_genuine(driver_path):
    """A genuine persisted body must WIN over the live value (no clobber)."""
    payload = _settled_then_live(
        term_persisted="GENUINE PERSISTED OUTPUT",
        patch_persisted="@@ -1 +1 @@\n-persisted old\n+persisted new",
    )
    cards = _run(driver_path, payload)
    assert _card(cards, "terminal")["snippet"] == "GENUINE PERSISTED OUTPUT"
    assert _card(cards, "edit_file")["snippet"].startswith("@@ -1 +1 @@")


def test_settled_args_restored_when_persisted_args_empty(driver_path):
    """A patch card whose persisted args were dropped gets them back from live."""
    payload = _settled_then_live(patch_args={})  # persisted row has no args
    cards = _run(driver_path, payload)
    patch = _card(cards, "edit_file")
    # live S.toolCalls carried args={"path":"x.py"} -> restored onto the Full tab.
    assert "path" in patch["transparentFullTabArgKeys"]


def test_no_live_match_leaves_settled_row_unchanged(driver_path):
    """With no matching live entry, the settled row is untouched (graceful)."""
    payload = _settled_then_live(term_persisted="kept", patch_persisted="kept-diff")
    payload["S"] = {"toolCalls": []}  # nothing live to restore from
    cards = _run(driver_path, payload)
    assert _card(cards, "terminal")["snippet"] == "kept"
    assert _card(cards, "edit_file")["snippet"] == "kept-diff"


def test_single_row_no_duplicate_after_enrich(driver_path):
    """Enriching must not also add a second (live) row — exactly one row per tid."""
    cards = _run(driver_path, _settled_then_live())
    assert len([c for c in cards if c["tid"] == "term-1"]) == 1


def test_settled_capped_preview_restored_to_full_live_body(driver_path):
    """#4622's real symptom: the backend persists a 4000-char CAPPED PREVIEW
    (_TOOL_RESULT_SNIPPET_MAX), so a long output settles to a truncated prefix,
    not to empty. The enrich must detect the bounded preview and restore the
    full live body."""
    full = "X" * 9000  # live full body, well over the 4000 persistence cap
    preview = full[:4000]  # what the backend persisted (a prefix of full)
    payload = _settled_then_live(term_persisted=preview, patch_persisted="")
    # Make the live terminal snippet the full 9000-char body.
    for tc in payload["S"]["toolCalls"]:
        if tc["id"] == "term-1":
            tc["snippet"] = full
    cards = _run(driver_path, payload)
    term = _card(cards, "terminal")
    assert term["snippet"] == full, (
        "a 4000-char capped preview must be restored to the FULL live body (#4622)"
    )
    assert len(term["snippet"]) == 9000


def test_short_genuine_prefix_not_clobbered(driver_path):
    """A genuinely short persisted body that merely happens to be a prefix of the
    live one must NOT be clobbered — only a >=4000-char (cap-length) preview is
    treated as truncated."""
    full = "short output line\nwith more detail that came later"
    short = "short output line"  # a real prefix, but well under the 4000 cap
    payload = _settled_then_live(term_persisted=short, patch_persisted="kept")
    for tc in payload["S"]["toolCalls"]:
        if tc["id"] == "term-1":
            tc["snippet"] = full
    cards = _run(driver_path, payload)
    assert _card(cards, "terminal")["snippet"] == short, (
        "a genuinely short persisted body (not a cap-length preview) must win"
    )
