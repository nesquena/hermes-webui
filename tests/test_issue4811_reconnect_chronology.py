"""Regression coverage for #4811: reconnect session scrambles message chronology.

_anchorSceneRowsByMessageIndex must emit rows in chronological order
(thinking first, then tools sorted by started_at, then prose) regardless of
whether tools come from the message object or from S.toolCalls.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Driver: extracts all helper functions in the _anchorScene* family by scanning
# the source for `function _anchorScene` blocks, then evaluates them with the
# required closure variables (activeSid, streamId, S) in scope.  Accepts a JSON
# payload on stdin and writes the byIdx map (as an object keyed by index) on stdout.
_DRIVER_SRC = r"""
'use strict';
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Extract every function whose name starts with _anchorScene
// by brace-matching from the `function` keyword.
function extractFunc(src, name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found in source');
  let i = src.indexOf('{', start);
  if (i < 0) throw new Error('No opening brace for ' + name);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}

const funcNames = [
  '_anchorSceneMessageText',
  '_anchorSceneCleanText',
  '_anchorSceneTextKey',
  '_anchorSceneSafePayload',
  '_anchorSceneToolId',
  '_anchorSceneToolName',
  '_anchorSceneToolArgs',
  '_anchorSceneStringPayload',
  '_anchorSceneRowBase',
  '_anchorSceneProseRow',
  '_anchorSceneThinkingRow',
  '_anchorSceneToolRowFromCall',
  '_anchorSceneMessageReasoningText',
  '_anchorSceneRowsByMessageIndex',
];

// Build a self-contained module: closure variables + all functions + runner
let code = '(function() {\n';
// Closure variables that the functions reference
code += 'var activeSid = "test-session";\n';
code += 'var streamId = "test-stream";\n';
// S is passed in from the test payload
code += 'var S;\n';
for (const name of funcNames) {
  code += extractFunc(src, name) + '\n';
}
code += `
var buf = '';
process.stdin.on('data', function(c) { buf += c; });
process.stdin.on('end', function() {
  var payload = JSON.parse(buf || '{}');
  S = payload.S || { toolCalls: [] };
  var messages = payload.messages || [];
  var turnStart = payload.turnStart !== undefined ? payload.turnStart : 0;
  var lastAsstIndex = payload.lastAsstIndex !== undefined ? payload.lastAsstIndex : messages.length - 1;
  var result = _anchorSceneRowsByMessageIndex(messages, turnStart, lastAsstIndex);
  // Convert Map to plain object keyed by index
  var out = {};
  result.forEach(function(rows, idx) { out[idx] = rows; });
  process.stdout.write(JSON.stringify(out));
});
})();
`;

process.stdout.write(code);
"""

# Generator driver: write the driver script to a temp file and return its path
@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    gen_path = tmp_path_factory.mktemp("gen") / "gen.js"
    gen_path.write_text(_DRIVER_SRC, encoding="utf-8")
    # Run the generator to produce the actual driver
    result = subprocess.run(
        [NODE, str(gen_path), str(MESSAGES_JS)],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        pytest.fail(f"Driver generation failed:\n{result.stderr}")
    driver = tmp_path_factory.mktemp("driver") / "driver.js"
    driver.write_text(result.stdout, encoding="utf-8")
    return str(driver)


def _run(driver_path: str, payload: dict) -> dict:
    """Run the driver with the given payload; return parsed stdout as dict."""
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Node error:\n{result.stderr}")
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------

def _tool(tool_id: str, name: str = "terminal", started_at=None, args=None):
    t = {"id": tool_id, "name": name, "done": True}
    if started_at is not None:
        t["started_at"] = started_at
    if args is not None:
        t["args"] = args
    return t


def _s_tool(tool_id: str, assistant_msg_idx: int, name: str = "terminal", started_at=None):
    t = {"id": tool_id, "name": name, "done": True, "assistant_msg_idx": assistant_msg_idx}
    if started_at is not None:
        t["started_at"] = started_at
    return t


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_thinking_before_tools_before_prose(driver_path):
    """Phase ordering: thinking (0) < tools (1) < prose (2)."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "I did it",
            "reasoning": "thinking first",
            "tool_calls": [_tool("t1", started_at=100)],
        },
        {"role": "assistant", "content": "final answer"},
    ]
    payload = {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}}
    out = _run(driver_path, payload)
    assert "1" in out
    rows = out["1"]
    roles = [r["role"] for r in rows]
    assert roles == ["thinking", "tool", "prose"], f"Expected thinking/tool/prose, got {roles}"


def test_reconnect_tools_interleave_by_started_at(driver_path):
    """S.toolCalls entries interleave chronologically with message-local tools."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "progress",
            "tool_calls": [
                _tool("t-100", started_at=100),
                _tool("t-300", started_at=300),
            ],
        },
        {"role": "assistant", "content": "final"},
    ]
    # reconnect tool at started_at=150 should land between t-100 and t-300
    s_tool_calls = [_s_tool("t-150", assistant_msg_idx=1, started_at=150)]
    payload = {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": s_tool_calls}}
    out = _run(driver_path, payload)
    rows = out["1"]
    tool_rows = [r for r in rows if r["role"] == "tool"]
    tool_ids = [r["tool_call_id"] for r in tool_rows]
    assert tool_ids == ["t-100", "t-150", "t-300"], f"Expected t-100/t-150/t-300 order, got {tool_ids}"


def test_no_duplicate_when_s_toolcalls_repeats_message_tool(driver_path):
    """Dedup: S.toolCalls must not add a second row for a tool already in message.tool_calls."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "progress",
            "tool_calls": [_tool("dup-id", started_at=100)],
        },
        {"role": "assistant", "content": "final"},
    ]
    s_tool_calls = [_s_tool("dup-id", assistant_msg_idx=1, started_at=100)]
    payload = {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": s_tool_calls}}
    out = _run(driver_path, payload)
    rows = out["1"]
    tool_rows = [r for r in rows if r["role"] == "tool"]
    assert len(tool_rows) == 1, f"Expected 1 tool row (deduped), got {len(tool_rows)}"
    assert tool_rows[0]["tool_call_id"] == "dup-id"


def test_deterministic_order_without_started_at(driver_path):
    """When started_at is missing, encounter order (insertion order) is the tiebreaker."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "progress",
            "tool_calls": [
                _tool("first"),
                _tool("second"),
                _tool("third"),
            ],
        },
        {"role": "assistant", "content": "final"},
    ]
    payload = {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}}
    out = _run(driver_path, payload)
    rows = out["1"]
    tool_rows = [r for r in rows if r["role"] == "tool"]
    tool_ids = [r["tool_call_id"] for r in tool_rows]
    assert tool_ids == ["first", "second", "third"], f"Expected insertion order, got {tool_ids}"


def test_cross_message_index_ordering_preserved(driver_path):
    """Tools from different message indices stay in their own buckets."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "step 1",
            "tool_calls": [_tool("t-idx1", started_at=100)],
        },
        {
            "role": "assistant",
            "content": "step 2",
            "tool_calls": [_tool("t-idx2", started_at=50)],
        },
        {"role": "assistant", "content": "final"},
    ]
    payload = {"messages": messages, "turnStart": 0, "lastAsstIndex": 3, "S": {"toolCalls": []}}
    out = _run(driver_path, payload)
    assert "1" in out and "2" in out
    idx1_tools = [r["tool_call_id"] for r in out["1"] if r["role"] == "tool"]
    idx2_tools = [r["tool_call_id"] for r in out["2"] if r["role"] == "tool"]
    assert idx1_tools == ["t-idx1"]
    assert idx2_tools == ["t-idx2"]


def test_s_toolcalls_outside_turn_range_ignored(driver_path):
    """S.toolCalls entries with assistant_msg_idx outside [turnStart+1, lastAsstIndex) are dropped."""
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "step"},
        {"role": "assistant", "content": "final"},
    ]
    s_tool_calls = [
        # idx 0 = turnStart, must be excluded
        _s_tool("t-0", assistant_msg_idx=0, started_at=10),
        # idx 2 = lastAsstIndex, must be excluded
        _s_tool("t-2", assistant_msg_idx=2, started_at=10),
        # valid
        _s_tool("t-1", assistant_msg_idx=1, started_at=10),
    ]
    payload = {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": s_tool_calls}}
    out = _run(driver_path, payload)
    all_tool_ids = [r["tool_call_id"] for rows in out.values() for r in rows if r["role"] == "tool"]
    assert "t-0" not in all_tool_ids
    assert "t-2" not in all_tool_ids
    assert "t-1" in all_tool_ids


def test_order_index_sequential_within_bucket(driver_path):
    """order_index is 0-based sequential within each message index bucket."""
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "prose",
            "reasoning": "think",
            "tool_calls": [_tool("t1", started_at=100)],
        },
        {"role": "assistant", "content": "final"},
    ]
    payload = {"messages": messages, "turnStart": 0, "lastAsstIndex": 2, "S": {"toolCalls": []}}
    out = _run(driver_path, payload)
    rows = out["1"]
    indices = [r["order_index"] for r in rows]
    assert indices == list(range(len(rows))), f"order_index not sequential: {indices}"
