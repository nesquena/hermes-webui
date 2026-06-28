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
		  '_anchorSceneContentText','_anchorSceneContentVisibleText','_anchorSceneMessageHasContentToolUse',
          '_anchorSceneFinalAnswerText',
		  '_anchorSceneSafePayload','_anchorSceneToolId','_anchorSceneToolName',
	  '_anchorSceneToolArgs','_anchorSceneContentTool','_anchorSceneStringPayload','_anchorSceneRowBase',
	  '_anchorSceneProseRow','_anchorSceneThinkingRow','_anchorSceneToolRowFromCall',
	  '_anchorSceneToolRowName','_anchorSceneToolRowId',
	  '_anchorSceneToolRowsHaveNonConflictingIds','_anchorSceneToolRowsHaveDifferentExplicitIds',
	  '_anchorSceneToolRowStartedAt','_anchorSceneToolRowsHaveSameStartedAt',
	  '_anchorSceneToolRowBodyText','_anchorSceneToolRowsHaveCompatibleBody',
	  '_anchorSceneToolRowsHaveCompatibleNames',
	  '_anchorSceneToolRowArgs','_anchorSceneObjectContainsSubset',
	  '_anchorSceneToolRowsHaveCompatibleInvocation',
	  '_anchorSceneToolRowHasInvocationEvidence','_anchorSceneToolRowsCanNameMatch',
	  '_anchorSceneMatchingContentToolRow',
	  '_anchorSceneMessageReasoningText','_anchorSceneRowsFromContentParts',
	  '_enrichSettledToolRowBodyFromLive',
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
	  var byIdx=_anchorSceneRowsByMessageIndex(messages,turnStart,lastAsstIndex,p.includeFinal?{includeFinal:true}:undefined);
      var finalAnswer=_anchorSceneFinalAnswerText(messages[lastAsstIndex]||{});
	  var cards=[];
	  var rows=[];
  byIdx.forEach(function(bucket,idx){
    bucket.forEach(function(row){
      rows.push({
        idx:idx, role:row.role||'', text:row.text||'',
        tool_call_id:row.tool_call_id||'', kind:row.kind||''
      });
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
	  if(p.returnRows) process.stdout.write(JSON.stringify({cards:cards,rows:rows,finalAnswer:finalAnswer}));
	  else process.stdout.write(JSON.stringify(cards));
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


def test_non_final_post_tool_text_survives_fresh_settlement(driver_path):
    """A non-final assistant message may contain process text after a tool_use.
    That text is activity, not the final answer tail, and must survive fresh
    settlement in the anchor rows."""
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": [
                "I will inspect first.",
                {"type": "tool_use", "tool_use_id": "term-content", "tool_name": "terminal"},
                {"type": "text", "text": "I found the relevant file."},
                {"type": "thinking", "text": "Need one more check."},
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    result = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {"toolCalls": []},
            "returnRows": True,
        },
    )

    rows = [row for row in result["rows"] if row["idx"] == 1]
    assert [(row["role"], row["text"] or row["tool_call_id"]) for row in rows] == [
        ("prose", "I will inspect first."),
        ("tool", "term-content"),
        ("prose", "I found the relevant file."),
        ("thinking", "Need one more check."),
    ]


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


def test_content_tool_use_enriched_from_matching_message_tool_call(driver_path):
    """Fresh settlement keeps content[] ordering while preserving richer tool body."""
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": [
                "I will inspect first.",
                {"type": "tool_use", "tool_use_id": "term-content", "tool_name": "terminal"},
                "Done.",
            ],
            "tool_calls": [
                {
                    "id": "term-message",
                    "function": {"name": "terminal", "arguments": '{"cmd":"ls -la"}'},
                    "snippet": _TERM_OUTPUT,
                    "started_at": 100,
                }
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "term-live",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "ls -la"},
                        "snippet": _TERM_OUTPUT,
                        "started_at": 100,
                    }
                ]
            },
        },
    )

    matching = [card for card in cards if card["tid"] == "term-content"]
    assert len(matching) == 1
    assert not [card for card in cards if card["tid"] in ("term-message", "term-live")]
    assert matching[0]["snippet"] == _TERM_OUTPUT
    assert matching[0]["args"] == {"cmd": "ls -la"}
    assert matching[0]["rendersOutputBody"] is True


def test_final_content_tail_thinking_stays_activity_and_content_order_wins_started_at(driver_path):
    """Fresh settlement keeps final content order and does not fold thinking into the answer."""
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check first."},
                {
                    "type": "tool_use",
                    "tool_use_id": "term-content",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                {"type": "thinking", "text": "Tail thinking must stay activity."},
                {"type": "reasoning", "reasoning": "Tail reasoning key must stay activity."},
                {"type": "text", "text": "Final visible answer."},
            ],
            "tool_calls": [
                {
                    "id": "term-message",
                    "name": "terminal",
                    "args": {"cmd": "ls"},
                    "snippet": "OUTPUT",
                    "started_at": 100,
                }
            ],
        },
    ]

    result = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 1,
            "includeFinal": True,
            "returnRows": True,
            "S": {"toolCalls": []},
        },
    )

    rows = [row for row in result["rows"] if row["idx"] == 1]
    assert result["finalAnswer"] == "Final visible answer."
    assert [(row["role"], row["text"] or row["tool_call_id"]) for row in rows] == [
        ("prose", "Let me check first."),
        ("tool", "term-content"),
        ("thinking", "Tail thinking must stay activity."),
        ("thinking", "Tail reasoning key must stay activity."),
    ]


def test_final_output_text_tail_content_becomes_answer_not_activity(driver_path):
    """Fresh settlement treats output_text.content after the last tool as final answer text."""
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check first."},
                {
                    "type": "tool_use",
                    "tool_use_id": "term-content",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                {"type": "output_text", "content": "Final answer from content field."},
            ],
            "tool_calls": [
                {
                    "id": "term-message",
                    "name": "terminal",
                    "args": {"cmd": "ls"},
                    "snippet": "OUTPUT",
                    "started_at": 100,
                }
            ],
        },
    ]

    result = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 1,
            "includeFinal": True,
            "returnRows": True,
            "S": {"toolCalls": []},
        },
    )

    rows = [row for row in result["rows"] if row["idx"] == 1]
    assert result["finalAnswer"] == "Final answer from content field."
    assert [(row["role"], row["text"] or row["tool_call_id"]) for row in rows] == [
        ("prose", "Let me check first."),
        ("tool", "term-content"),
    ]


def test_ordered_content_bucket_ignores_unmatched_live_started_at(driver_path):
    """When content[] owns row order, unmatched live rows keep encounter order."""
    messages = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "First content row."},
                {
                    "type": "tool_use",
                    "tool_use_id": "content-tool",
                    "tool_name": "terminal",
                    "args": {"cmd": "first"},
                },
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    result = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "returnRows": True,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-late",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "late"},
                        "snippet": "late",
                        "started_at": 300,
                    },
                    {
                        "id": "live-early",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "early"},
                        "snippet": "early",
                        "started_at": 100,
                    },
                ]
            },
        },
    )

    rows = [row for row in result["rows"] if row["idx"] == 1]
    assert [(row["role"], row["text"] or row["tool_call_id"]) for row in rows] == [
        ("prose", "First content row."),
        ("tool", "content-tool"),
        ("tool", "live-late"),
        ("tool", "live-early"),
    ]


def test_content_tool_use_partial_args_enriched_from_matching_live_tool_call(driver_path):
    """Matched live tool args fill missing invocation details without clobbering content args."""
    messages = [
        {"role": "user", "content": "patch"},
        {
            "role": "assistant",
            "content": [
                "I will patch the file.",
                {
                    "type": "tool_use",
                    "tool_use_id": "patch-content",
                    "tool_name": "edit_file",
                    "args": {"path": "x.py"},
                },
                "Done.",
            ],
            "tool_calls": [
                {"id": "patch-message", "name": "edit_file", "snippet": _DIFF, "started_at": 100}
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "patch-live",
                        "name": "edit_file",
                        "assistant_msg_idx": 1,
                        "args": {
                            "path": "x.py",
                            "old_string": "old",
                            "new_string": "new",
                        },
                        "snippet": _DIFF,
                        "started_at": 100,
                    }
                ]
            },
        },
    )

    matching = [card for card in cards if card["tid"] == "patch-content"]
    assert len(matching) == 1
    assert matching[0]["args"] == {
        "path": "x.py",
        "old_string": "old",
        "new_string": "new",
    }
    assert matching[0]["rendersDiff"] is True


def test_id_flexible_content_tool_does_not_absorb_third_same_command_id(driver_path):
    """A content row matched to one alternate id must not hide a later repeated call."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "message-a",
                    "name": "terminal",
                    "args": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                    "started_at": 100,
                }
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-b",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "ls"},
                        "snippet": "OUTPUT B",
                        "started_at": 200,
                    }
                ]
            },
        },
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "OUTPUT A"
    assert "message-a" not in by_tid
    assert by_tid["live-b"]["snippet"] == "OUTPUT B"
    assert len([card for card in cards if card["name"] == "terminal"]) == 2


def test_id_flexible_content_tool_keeps_same_started_at_repeat_distinct(driver_path):
    """Same started_at is not enough to reuse a consumed different-id content row."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "message-a",
                    "name": "terminal",
                    "args": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                    "started_at": 100,
                }
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-b",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "ls"},
                        "snippet": "OUTPUT B",
                        "started_at": 100,
                    }
                ]
            },
        },
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "OUTPUT A"
    assert "message-a" not in by_tid
    assert by_tid["live-b"]["snippet"] == "OUTPUT B"
    assert len([card for card in cards if card["name"] == "terminal"]) == 2


def test_id_flexible_content_tool_keeps_identical_output_repeat_distinct(driver_path):
    """Identical output is not identity proof for repeated same-command calls."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "message-a",
                    "name": "terminal",
                    "args": {"cmd": "ls"},
                    "snippet": "SAME OUTPUT",
                }
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-b",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "ls"},
                        "snippet": "SAME OUTPUT",
                    }
                ]
            },
        },
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "SAME OUTPUT"
    assert "message-a" not in by_tid
    assert by_tid["live-b"]["snippet"] == "SAME OUTPUT"
    assert len([card for card in cards if card["name"] == "terminal"]) == 2


def test_ambiguous_different_id_content_tools_do_not_merge_by_position(driver_path):
    """Multiple same-name tools with different ids must not be paired by position."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {"type": "tool_use", "tool_use_id": "content-a", "tool_name": "terminal"},
                "Second check.",
                {"type": "tool_use", "tool_use_id": "content-b", "tool_name": "terminal"},
                "Done.",
            ],
            "tool_calls": [
                {"id": "message-b", "name": "terminal", "snippet": "OUTPUT B"},
                {"id": "message-a", "name": "terminal", "snippet": "OUTPUT A"},
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}},
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == ""
    assert by_tid["content-b"]["snippet"] == ""
    assert by_tid["message-a"]["snippet"] == "OUTPUT A"
    assert by_tid["message-b"]["snippet"] == "OUTPUT B"


def test_remaining_same_name_content_tool_does_not_name_merge_after_exact_match(driver_path):
    """A prior exact-id match must not make the remaining same-name row look safe."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {"type": "tool_use", "tool_use_id": "content-a", "tool_name": "terminal"},
                "Second check.",
                {"type": "tool_use", "tool_use_id": "content-b", "tool_name": "terminal"},
                "Done.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "snippet": "OUTPUT A"},
                {"id": "message-b", "name": "terminal", "snippet": "OUTPUT B"},
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}},
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "OUTPUT A"
    assert by_tid["content-b"]["snippet"] == ""
    assert by_tid["message-b"]["snippet"] == "OUTPUT B"


def test_remaining_matching_content_tool_merges_after_exact_match(driver_path):
    """After an exact match, one remaining row may merge with matching invocation evidence."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-b",
                    "tool_name": "terminal",
                    "args": {"cmd": "pwd"},
                },
                "Done.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "args": {"cmd": "ls"}, "snippet": "OUTPUT A"},
                {"id": "message-b", "name": "terminal", "args": {"cmd": "pwd"}, "snippet": "OUTPUT B"},
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}},
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "OUTPUT A"
    assert by_tid["content-b"]["snippet"] == "OUTPUT B"
    assert "message-b" not in by_tid
    assert len([card for card in cards if card["name"] == "terminal"]) == 2


def test_nested_args_with_different_key_order_merge_without_duplicate(driver_path):
    """Nested args compare by value, not object insertion order."""
    messages = [
        {"role": "user", "content": "patch twice"},
        {
            "role": "assistant",
            "content": [
                "First patch.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "edit_file",
                    "args": {"patch": {"path": "a.py", "body": "x"}},
                },
                "Second patch.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-b",
                    "tool_name": "edit_file",
                    "args": {"patch": {"path": "b.py", "body": "y"}},
                },
                "Done.",
            ],
            "tool_calls": [
                {
                    "id": "content-a",
                    "name": "edit_file",
                    "args": {"patch": {"path": "a.py", "body": "x"}},
                    "snippet": "PATCH A",
                },
                {
                    "id": "message-b",
                    "name": "edit_file",
                    "args": {"patch": {"body": "y", "path": "b.py"}},
                    "snippet": "PATCH B",
                },
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}},
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "PATCH A"
    assert by_tid["content-b"]["snippet"] == "PATCH B"
    assert "message-b" not in by_tid
    assert len([card for card in cards if card["name"] == "edit_file"]) == 2


def test_consumed_singleton_content_tool_keeps_distinct_live_invocation(driver_path):
    """A row consumed by message.tool_calls must not hide a later distinct live call."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "args": {"cmd": "ls"}, "snippet": "OUTPUT A"}
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-b",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "pwd"},
                        "snippet": "OUTPUT B",
                    }
                ]
            },
        },
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "OUTPUT A"
    assert by_tid["live-b"]["snippet"] == "OUTPUT B"
    assert len([card for card in cards if card["name"] == "terminal"]) == 2


def test_consumed_singleton_same_command_live_tool_stays_distinct(driver_path):
    """A repeated same-command invocation with a new id must keep its own card."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "args": {"cmd": "ls"}, "snippet": "OUTPUT A"}
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-b",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "ls"},
                        "snippet": "OUTPUT B",
                    }
                ]
            },
        },
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "OUTPUT A"
    assert by_tid["live-b"]["snippet"] == "OUTPUT B"
    assert len([card for card in cards if card["name"] == "terminal"]) == 2


def test_consumed_singleton_anonymous_live_tool_stays_distinct(driver_path):
    """A consumed content row must not absorb a later anonymous live call."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "args": {"cmd": "ls"}, "snippet": "OUTPUT A"}
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"cmd": "ls"},
                        "snippet": "OUTPUT B",
                    }
                ]
            },
        },
    )

    terminal_cards = [card for card in cards if card["name"] == "terminal"]
    snippets = sorted(card["snippet"] for card in terminal_cards)

    assert len(terminal_cards) == 2
    assert snippets == ["OUTPUT A", "OUTPUT B"]


def test_consumed_singleton_body_only_live_tool_stays_distinct(driver_path):
    """A body-only live row has no proof that it is the consumed content tool."""
    messages = [
        {"role": "user", "content": "inspect twice"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "args": {"cmd": "ls"}, "snippet": "OUTPUT A"}
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-b",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "snippet": "OUTPUT B",
                    }
                ]
            },
        },
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["snippet"] == "OUTPUT A"
    assert by_tid["live-b"]["snippet"] == "OUTPUT B"
    assert len([card for card in cards if card["name"] == "terminal"]) == 2


def test_consumed_singleton_different_name_live_tool_stays_distinct(driver_path):
    """A consumed content row must not be reused for a different tool name."""
    messages = [
        {"role": "user", "content": "edit then inspect"},
        {
            "role": "assistant",
            "content": [
                "Edit the file.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "edit_file",
                    "args": {"path": "x.py"},
                },
                "Inspect it.",
            ],
            "tool_calls": [
                {
                    "id": "content-a",
                    "name": "edit_file",
                    "args": {"path": "x.py"},
                    "snippet": "EDITED",
                }
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {
            "messages": messages,
            "turnStart": 0,
            "lastAsstIndex": 2,
            "S": {
                "toolCalls": [
                    {
                        "id": "live-b",
                        "name": "terminal",
                        "assistant_msg_idx": 1,
                        "args": {"path": "x.py"},
                        "snippet": "TERMINAL OUTPUT",
                    }
                ]
            },
        },
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["name"] == "edit_file"
    assert by_tid["content-a"]["snippet"] == "EDITED"
    assert by_tid["live-b"]["name"] == "terminal"
    assert by_tid["live-b"]["snippet"] == "TERMINAL OUTPUT"


def test_singleton_content_tool_does_not_name_merge_conflicting_args(driver_path):
    """A one-row content pool still must not merge explicit different invocations."""
    messages = [
        {"role": "user", "content": "patch"},
        {
            "role": "assistant",
            "content": [
                "Patch a.py.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "edit_file",
                    "args": {"path": "a.py"},
                },
            ],
            "tool_calls": [
                {"id": "message-b", "name": "edit_file", "args": {"path": "b.py"}, "snippet": "PATCH B"}
            ],
        },
        {"role": "assistant", "content": "final answer"},
    ]

    cards = _run(
        driver_path,
        {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}},
    )
    by_tid = {card["tid"]: card for card in cards}

    assert by_tid["content-a"]["args"] == {"path": "a.py"}
    assert by_tid["content-a"]["snippet"] == ""
    assert by_tid["message-b"]["args"] == {"path": "b.py"}
    assert by_tid["message-b"]["snippet"] == "PATCH B"
    assert len([card for card in cards if card["name"] == "edit_file"]) == 2


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
