"""Focused Workstream D coverage for client recovery and multi-session UX."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function_decl(source: str, name: str) -> str:
    marker = f"function {name}"
    start = source.find(marker)
    assert start >= 0, f"{name} not found"
    params = source.find("(", start)
    assert params >= 0, f"{name} parameter list not found"
    paren_depth = 0
    close = -1
    for idx in range(params, len(source)):
        if source[idx] == "(":
            paren_depth += 1
        elif source[idx] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                close = idx
                break
    assert close >= 0, f"{name} parameter list did not close"
    brace = source.find("{", close)
    assert brace >= 0, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(source)):
        if source[idx] == "{":
            depth += 1
        elif source[idx] == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"{name} body did not close")


def _run_node(script: str) -> dict:
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _ui_recovery_source() -> str:
    start = UI_JS.index("const INFLIGHT_KEY")
    end = UI_JS.index("// ─── Todo state:", start)
    return UI_JS[start:end]


def test_recovery_storage_is_tail_and_cursor_bounded_for_three_active_sessions():
    """A long live answer must not make the localStorage blob grow per token."""
    source = _ui_recovery_source()
    script = """
const vm = require('vm');
const source = SOURCE;
const storage = {
  values: Object.create(null),
  getItem(key) { return Object.prototype.hasOwnProperty.call(this.values, key) ? this.values[key] : null; },
  setItem(key, value) { this.values[key] = String(value); },
  removeItem(key) { delete this.values[key]; },
};
const context = {
  localStorage: storage,
  window: { _inflightStateLimits: {
    maxSessions: 25,
    messages: 100,
    toolCalls: 200,
    stringChars: 500000,
    jsonChars: 4000000,
  }},
  Date,
  console,
};
vm.createContext(context);
vm.runInContext(source, context);
const answer = 'A'.repeat(180000) + 'ANSWER-TAIL';
const reasoning = 'R'.repeat(90000) + 'REASONING-TAIL';
const state = {
  streamId: 'stream-a',
  messages: Array.from({length: 160}, (_, index) => ({
    role: index % 2 ? 'assistant' : 'user',
    content: index === 159 ? answer : 'history-' + index + '-'.repeat(12000),
    _live: index === 159,
  })),
  lastAssistantText: answer,
  lastReasoningText: reasoning,
  lastRunJournalSeq: 812,
  lastRunJournalEventId: 'stream-a:812',
  toolCalls: Array.from({length: 120}, (_, index) => ({
    tid: 'tool-' + index,
    name: 'terminal',
    args: {command: 'x'.repeat(12000)},
    snippet: 'output-' + 'y'.repeat(12000),
    done: index < 119,
  })),
  activityBurstAnchors: Array.from({length: 200}, (_, index) => ({id: index + 1, textEnd: index * 1000})),
};
for (const sid of ['session-a', 'session-b', 'session-c']) {
  context.saveInflightState(sid, {...state, streamId: 'stream-' + sid});
}
const raw = storage.values['hermes-webui-inflight-state'] || '';
const all = JSON.parse(raw || '{}');
const entries = Object.values(all);
const sample = entries[0] || {};
const live = Array.isArray(sample.messages) ? sample.messages.find(message => message && message._live) : null;
const tool = Array.isArray(sample.toolCalls) ? sample.toolCalls[0] : null;
process.stdout.write(JSON.stringify({
  bytes: raw.length,
  sessionCount: entries.length,
  recoveryVersion: sample.recoveryVersion,
  recoveryMode: sample.recoveryMode,
  assistantLength: sample.assistantTextLength,
  assistantTail: sample.assistantTextTail,
  reasoningLength: sample.reasoningTextLength,
  reasoningTail: sample.reasoningTextTail,
  assistantTruncated: sample.assistantTextTruncated,
  messageCount: Array.isArray(sample.messages) ? sample.messages.length : -1,
  liveContentLength: live && typeof live.content === 'string' ? live.content.length : -1,
  liveContentTail: live && typeof live.content === 'string' ? live.content.slice(-32) : '',
  toolCount: Array.isArray(sample.toolCalls) ? sample.toolCalls.length : -1,
  toolArgsLength: tool && tool.args && typeof tool.args.command === 'string' ? tool.args.command.length : -1,
  hasToolOutput: !!(tool && (tool.output || tool.result || tool.preview)),
  cursor: sample.lastRunJournalSeq,
}));
""".replace("SOURCE", json.dumps(source))
    result = _run_node(script)

    assert result["sessionCount"] == 3
    assert result["bytes"] <= 262144
    assert result["recoveryVersion"] == 2
    assert result["recoveryMode"] in {"journal-tail", "tail-journal"}
    assert result["assistantLength"] == 180000 + len("ANSWER-TAIL")
    assert result["assistantTail"].endswith("ANSWER-TAIL")
    assert len(result["assistantTail"]) <= 8192
    assert result["reasoningLength"] == 90000 + len("REASONING-TAIL")
    assert result["reasoningTail"].endswith("REASONING-TAIL")
    assert len(result["reasoningTail"]) <= 8192
    assert result["assistantTruncated"] is True
    assert result["messageCount"] <= 12
    assert result["liveContentLength"] <= 8192
    assert result["liveContentTail"].endswith("ANSWER-TAIL")
    assert result["toolCount"] <= 24
    assert result["toolArgsLength"] <= 2048
    assert result["hasToolOutput"] is False
    assert result["cursor"] == 812


def test_background_projection_is_metadata_only_and_keeps_three_sessions_distinct():
    """Background activity can update the sidebar without carrying token text."""
    assert "function _recordSessionActivityProjection" in SESSIONS_JS
    assert "function _getSessionActivityProjection" in SESSIONS_JS
    assert "session-activity-status" in SESSIONS_JS
    assert "data-session-activity-status" in SESSIONS_JS

    declarations = "\n".join(
        [
            "const SESSION_ACTIVITY_PROJECTION_MAX_ENTRIES = 32;",
            "const SESSION_ACTIVITY_PROJECTION_TTL_MS = 10 * 60 * 1000;",
            "const SESSION_ACTIVITY_ALLOWED_STATUSES = Object.freeze({running:true, waiting:true, completed:true, cancelled:true, error:true});",
            "const SESSION_ACTIVITY_ALLOWED_PHASES = Object.freeze({queued:true, thinking:true, tool:true, answer:true, approval:true, clarify:true, done:true, cancelled:true, error:true, working:true});",
            "const _sessionActivityById = new Map();",
            "let _sessionActivityRenderTimer = 0;",
            _function_decl(SESSIONS_JS, "_scheduleSessionActivityProjectionRender"),
            _function_decl(SESSIONS_JS, "_recordSessionActivityProjection"),
            _function_decl(SESSIONS_JS, "_getSessionActivityProjection"),
            _function_decl(SESSIONS_JS, "_formatSessionActivityProjection"),
        ]
    )
    script = declarations + """
_recordSessionActivityProjection('session-a', {
  streamId: 'stream-a', status: 'running', phase: 'answer',
  assistantChars: 900000, reasoningChars: 30, toolCount: 2,
  eventCount: 22, tokenText: 'must never be persisted or projected',
});
_recordSessionActivityProjection('session-b', {
  streamId: 'stream-b', status: 'waiting', phase: 'approval', toolCount: 7,
});
_recordSessionActivityProjection('session-c', {
  streamId: 'stream-c', status: 'completed', phase: 'done', toolCount: 1,
});
const rows = ['session-a', 'session-b', 'session-c'].map(sid => {
  const projection = _getSessionActivityProjection(sid);
  return {
    sid,
    projection,
    label: _formatSessionActivityProjection(projection),
  };
});
process.stdout.write(JSON.stringify(rows));
"""
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)

    assert [row["sid"] for row in rows] == ["session-a", "session-b", "session-c"]
    assert [row["projection"]["status"] for row in rows] == [
        "running",
        "waiting",
        "completed",
    ]
    assert rows[0]["projection"]["phase"] == "answer"
    assert rows[1]["projection"]["phase"] == "approval"
    assert rows[0]["projection"]["toolCount"] == 2
    assert rows[1]["projection"]["toolCount"] == 7
    assert rows[0]["projection"]["assistantChars"] == 900000
    assert "must never" not in json.dumps(rows)
    assert all("tokenText" not in row["projection"] for row in rows)
    assert all(row["label"] for row in rows)


def test_selected_renderer_and_ownership_guards_remain_separate_from_projection():
    """Projection updates must not weaken the existing selected-pane gates."""
    token = MESSAGES_JS[MESSAGES_JS.index("source.addEventListener('token'"):]
    token = token[: token.index("source.addEventListener('interim_assistant'")]
    assert "assistantText+=d.text" in token
    assert "syncInflightAssistantMessage();" in token
    assert "_scheduleRender" in token
    sync = _function_decl(MESSAGES_JS, "syncInflightAssistantMessage")
    assert "_recordLiveActivityProjection" in sync

    attach = _function_decl(MESSAGES_JS, "attachLiveStream")
    assert "closeOtherLiveStreams(activeSid)" in attach
    assert "existingLive.streamId===streamId" in attach
    assert "source.readyState===EventSource.OPEN" in attach

    close = _function_decl(MESSAGES_JS, "closeLiveStream")
    assert "INFLIGHT[sessionId].reattach=true" in close
    assert "journalReplayFromStart=true" in close

    cancel = MESSAGES_JS[MESSAGES_JS.index("source.addEventListener('cancel'"):]
    cancel = cancel[: cancel.index("for(const _runJournalEventName")]
    assert "_clearOwnerInflightState" in cancel
    assert "_clearApprovalForOwner" in cancel
    assert "_clearClarifyForOwner('cancelled')" in cancel

    # The background projection is intentionally not allowed to invoke the full
    # transcript renderer. rAF/incremental rendering stays in messages.js.
    projection = _function_decl(SESSIONS_JS, "_recordSessionActivityProjection")
    assert "renderMessages" not in projection


def test_background_row_has_compact_status_hook_without_rendering_a_transcript():
    """The browser-facing row contract exposes status as a small data node."""
    render_start = SESSIONS_JS.index("function _renderOneSession")
    render_end = SESSIONS_JS.index("function _showProjectPicker", render_start)
    row_source = SESSIONS_JS[render_start:render_end]
    compact = re.sub(r"\s+", "", row_source)
    assert "session-activity-status" in row_source
    assert "dataset.sessionActivityStatus" in row_source
    assert "data-session-activity-status" in row_source
    assert "renderMessages" not in row_source
    assert "activity" in compact
