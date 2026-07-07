"""Regression tests for issue #5749 Transparent Stream prefix dedupe."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _run_node(src, script, tmp_path):
    assert NODE, "node is required for issue #5749 regression tests"
    script_path = tmp_path / "issue5749_node_script.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run([NODE, str(script_path)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_settlement_suppresses_live_token_prefix_rows(tmp_path):
    final_answer = "I found the issue and I am fixing it by deduping live-token prefixes during settlement and render fallback so Transparent Stream does not repeat the same prose row."
    prefix_text = "I found the issue and I am fixing it by deduping live-token prefixes during settlement and render fallback"
    script = f"""
const src = {json.dumps(MESSAGES_JS)};
function extractFunc(name) {{
  const start = src.indexOf('function ' + name);
  if (start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for (let i = params; i < src.length; i++) {{
    if (src[i] === '(') depth++;
    else if (src[i] === ')') {{
      depth--;
      if (depth === 0) {{ close = i; break; }}
    }}
  }}
  const brace = src.indexOf('{{', close);
  depth = 0;
  for (let i = brace; i < src.length; i++) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') {{
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }}
  }}
  throw new Error(name + ' body did not close');
}}
global.window = {{
  chatActivityMode() {{ return 'transparent_stream'; }},
  _chatActivityDisplayMode: 'transparent_stream',
  _transparentStream: true,
}};
global.S = {{ session: {{}} }};
eval(extractFunc('_anchorSceneCleanText'));
eval(extractFunc('_anchorSceneTextKey'));
eval(extractFunc('_anchorSceneExistingRowKey'));
eval(extractFunc('_anchorSceneRowHasLiveIdentity'));
eval(extractFunc('_anchorSceneSettleLiveRunningRow'));
eval(extractFunc('_anchorSceneRowLooksLikeFinalAnswer'));
eval(extractFunc('_anchorSceneRowTextOverlapsExisting'));
eval(extractFunc('_anchorSceneMessageRowsHaveThinking'));
eval(extractFunc('_completeSettledAnchorSceneForTurn'));
function _anchorSceneActiveMode() {{ return 'transparent_stream'; }}
function _anchorSceneFinalAnswerText(message) {{ return message && (message.final_answer || message.content || ''); }}
function _anchorSceneRowsByMessageIndex() {{ return new Map(); }}
function _anchorSceneMessageRef(message) {{ return String(message && message.id || ''); }}
function _anchorSceneTurnDurationForSettlement() {{ return 0; }}
function _anchorSceneRowDisplayHintForMode(row, sceneMode) {{
  const hints = row && typeof row === 'object' && row.display_hints && typeof row.display_hints === 'object' ? row.display_hints : null;
  if (sceneMode === 'transparent_stream') return (hints && hints.transparent_stream) || 'chronological_activity';
  if (sceneMode === 'compact_worklog') return (hints && hints.compact_worklog) || row && row.display_hint || 'activity_row';
  return row && row.display_hint || 'activity_row';
}}
const messages = [
  {{ role: 'user', content: 'Prompt', id: 'user-1' }},
  {{ role: 'assistant', content: {json.dumps(final_answer)}, id: 'assistant-1' }},
];
const scene = _completeSettledAnchorSceneForTurn(messages, 1, {{
  mode: 'transparent_stream',
  final_answer: {json.dumps(final_answer)},
  lifecycle: {{ terminal_state: 'done' }},
  identity: {{ source_message_refs: ['legacy'] }},
  activity_rows: [
    {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'token',
      local_id: 'live-prose:stream-1:1',
      text: {json.dumps(prefix_text)},
      status: 'running',
      attachments: [{{ id: 'attachment-1' }}],
    }},
    {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'manual',
      local_id: 'session-prose:stream-1:2',
      text: {json.dumps(prefix_text)},
      status: 'running',
      attachments: [{{ id: 'attachment-2' }}],
    }},
    {{
      role: 'tool',
      kind: 'tool_result',
      source_event_type: 'tool',
      local_id: 'tool-row-1',
      text: 'Fetched docs',
      status: 'running',
    }},
  ],
}});
process.stdout.write(JSON.stringify({{
  final_answer: scene.final_answer,
  rows: scene.activity_rows.map(row => ({{
    role: row.role,
    kind: row.kind,
    source_event_type: row.source_event_type,
    local_id: row.local_id,
    text: row.text,
    status: row.status,
    attachments: row.attachments || null,
  }})),
}}));
"""
    data = _run_node(MESSAGES_JS, script, tmp_path)
    assert data["final_answer"] == final_answer
    assert [row["local_id"] for row in data["rows"]] == ["session-prose:stream-1:2", "tool-row-1"]
    assert data["rows"][0]["text"] == prefix_text
    assert data["rows"][0]["attachments"] == [{"id": "attachment-2"}]
    assert data["rows"][1]["role"] == "tool"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_short_live_token_prefix_rows_survive_settlement(tmp_path):
    final_answer = "The solution is to preserve short legitimate live-token prefixes while still suppressing longer duplicated final-answer spans."
    prefix_text = "The"
    script = f"""
const src = {json.dumps(MESSAGES_JS)};
function extractFunc(name) {{
  const start = src.indexOf('function ' + name);
  if (start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for (let i = params; i < src.length; i++) {{
    if (src[i] === '(') depth++;
    else if (src[i] === ')') {{
      depth--;
      if (depth === 0) {{ close = i; break; }}
    }}
  }}
  const brace = src.indexOf('{{', close);
  depth = 0;
  for (let i = brace; i < src.length; i++) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') {{
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }}
  }}
  throw new Error(name + ' body did not close');
}}
global.window = {{
  chatActivityMode() {{ return 'transparent_stream'; }},
  _chatActivityDisplayMode: 'transparent_stream',
  _transparentStream: true,
}};
global.S = {{ session: {{}} }};
eval(extractFunc('_anchorSceneCleanText'));
eval(extractFunc('_anchorSceneTextKey'));
eval(extractFunc('_anchorSceneExistingRowKey'));
eval(extractFunc('_anchorSceneRowHasLiveIdentity'));
eval(extractFunc('_anchorSceneSettleLiveRunningRow'));
eval(extractFunc('_anchorSceneRowLooksLikeFinalAnswer'));
eval(extractFunc('_anchorSceneRowTextOverlapsExisting'));
eval(extractFunc('_anchorSceneMessageRowsHaveThinking'));
eval(extractFunc('_completeSettledAnchorSceneForTurn'));
function _anchorSceneActiveMode() {{ return 'transparent_stream'; }}
function _anchorSceneFinalAnswerText(message) {{ return message && (message.final_answer || message.content || ''); }}
function _anchorSceneRowsByMessageIndex() {{ return new Map(); }}
function _anchorSceneMessageRef(message) {{ return String(message && message.id || ''); }}
function _anchorSceneTurnDurationForSettlement() {{ return 0; }}
function _anchorSceneRowDisplayHintForMode(row, sceneMode) {{
  const hints = row && typeof row === 'object' && row.display_hints && typeof row.display_hints === 'object' ? row.display_hints : null;
  if (sceneMode === 'transparent_stream') return (hints && hints.transparent_stream) || 'chronological_activity';
  if (sceneMode === 'compact_worklog') return (hints && hints.compact_worklog) || row && row.display_hint || 'activity_row';
  return row && row.display_hint || 'activity_row';
}}
const messages = [
  {{ role: 'user', content: 'Prompt', id: 'user-1' }},
  {{ role: 'assistant', content: {json.dumps(final_answer)}, id: 'assistant-1' }},
];
const scene = _completeSettledAnchorSceneForTurn(messages, 1, {{
  mode: 'transparent_stream',
  final_answer: {json.dumps(final_answer)},
  lifecycle: {{ terminal_state: 'done' }},
  identity: {{ source_message_refs: ['legacy'] }},
  activity_rows: [
    {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'token',
      local_id: 'live-prose:stream-short:1',
      text: {json.dumps(prefix_text)},
      status: 'running',
    }},
  ],
}});
process.stdout.write(JSON.stringify(scene.activity_rows.map(row => row.text)));
"""
    data = _run_node(MESSAGES_JS, script, tmp_path)
    assert data == [prefix_text]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_long_live_progress_prefix_survives_without_settled_duplicate(tmp_path):
    final_answer = "I checked the logs, identified the failing deterministic shard, and now I am applying the narrow render-path fix before rerunning the exact test file. The final answer continues with validation details and the pushed commit."
    prefix_text = "I checked the logs, identified the failing deterministic shard, and now I am applying the narrow render-path fix before rerunning the exact test file."
    script = f"""
const src = {json.dumps(MESSAGES_JS)};
function extractFunc(name) {{
  const start = src.indexOf('function ' + name);
  if (start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for (let i = params; i < src.length; i++) {{
    if (src[i] === '(') depth++;
    else if (src[i] === ')') {{
      depth--;
      if (depth === 0) {{ close = i; break; }}
    }}
  }}
  const brace = src.indexOf('{{', close);
  depth = 0;
  for (let i = brace; i < src.length; i++) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') {{
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }}
  }}
  throw new Error(name + ' body did not close');
}}
global.window = {{
  chatActivityMode() {{ return 'transparent_stream'; }},
  _chatActivityDisplayMode: 'transparent_stream',
  _transparentStream: true,
}};
global.S = {{ session: {{}} }};
eval(extractFunc('_anchorSceneCleanText'));
eval(extractFunc('_anchorSceneTextKey'));
eval(extractFunc('_anchorSceneExistingRowKey'));
eval(extractFunc('_anchorSceneRowHasLiveIdentity'));
eval(extractFunc('_anchorSceneSettleLiveRunningRow'));
eval(extractFunc('_anchorSceneRowLooksLikeFinalAnswer'));
eval(extractFunc('_anchorSceneRowTextOverlapsExisting'));
eval(extractFunc('_anchorSceneMessageRowsHaveThinking'));
eval(extractFunc('_completeSettledAnchorSceneForTurn'));
function _anchorSceneActiveMode() {{ return 'transparent_stream'; }}
function _anchorSceneFinalAnswerText(message) {{ return message && (message.final_answer || message.content || ''); }}
function _anchorSceneRowsByMessageIndex() {{ return new Map(); }}
function _anchorSceneMessageRef(message) {{ return String(message && message.id || ''); }}
function _anchorSceneTurnDurationForSettlement() {{ return 0; }}
function _anchorSceneRowDisplayHintForMode(row, sceneMode) {{
  const hints = row && typeof row === 'object' && row.display_hints && typeof row.display_hints === 'object' ? row.display_hints : null;
  if (sceneMode === 'transparent_stream') return (hints && hints.transparent_stream) || 'chronological_activity';
  if (sceneMode === 'compact_worklog') return (hints && hints.compact_worklog) || row && row.display_hint || 'activity_row';
  return row && row.display_hint || 'activity_row';
}}
const messages = [
  {{ role: 'user', content: 'Prompt', id: 'user-1' }},
  {{ role: 'assistant', content: {json.dumps(final_answer)}, id: 'assistant-1' }},
];
const scene = _completeSettledAnchorSceneForTurn(messages, 1, {{
  mode: 'transparent_stream',
  final_answer: {json.dumps(final_answer)},
  lifecycle: {{ terminal_state: 'done' }},
  identity: {{ source_message_refs: ['legacy'] }},
  activity_rows: [
    {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'token',
      local_id: 'live-prose:stream-long:1',
      text: {json.dumps(prefix_text)},
      status: 'running',
    }},
  ],
}});
process.stdout.write(JSON.stringify(scene.activity_rows.map(row => ({{
  local_id: row.local_id,
  text: row.text,
  status: row.status,
}}))));
"""
    data = _run_node(MESSAGES_JS, script, tmp_path)
    assert data == [{"local_id": "live-prose:stream-long:1", "text": prefix_text, "status": "completed"}]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_render_fallback_preserves_persisted_live_progress_prefix_rows(tmp_path):
    final_answer = "I found the issue and I am fixing it by deduping live-token prefixes during settlement and render fallback so Transparent Stream does not repeat the same prose row."
    prefix_text = "I found the issue and I am fixing it by deduping live-token prefixes during settlement and render fallback"
    script = f"""
const src = {json.dumps(UI_JS)};
function extractFunc(name) {{
  const start = src.indexOf('function ' + name);
  if (start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for (let i = params; i < src.length; i++) {{
    if (src[i] === '(') depth++;
    else if (src[i] === ')') {{
      depth--;
      if (depth === 0) {{ close = i; break; }}
    }}
  }}
  const brace = src.indexOf('{{', close);
  depth = 0;
  for (let i = brace; i < src.length; i++) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') {{
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }}
  }}
  throw new Error(name + ' body did not close');
}}
class FakeElement {{
  constructor(tag) {{
    this.tagName = String(tag || 'div').toUpperCase();
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
  }}
  setAttribute(name, value) {{
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) {{
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }}
  }}
}}
global.window = {{}};
global.document = {{ createElement(tag) {{ return new FakeElement(tag); }} }};
global._anchorSceneNodeForRow = () => new FakeElement('div');
global._decorateTransparentEventRow = node => node;
global._anchorSceneToolCallFromRow = () => ({{}});
global.buildToolCard = () => new FakeElement('div');
global._thinkingActivityNode = () => new FakeElement('div');
eval(extractFunc('_anchorSceneProseMatchesFinalAnswer'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
const liveRow = {{
  role: 'prose',
  kind: 'process_prose',
  source_event_type: 'token',
  local_id: 'live-prose:stream-1:1',
  text: {json.dumps(prefix_text)},
}};
const boundaryRow = {{
  role: 'prose',
  kind: 'process_prose',
  source_event_type: 'manual',
  local_id: 'session-prose:stream-1:2',
  text: {json.dumps(prefix_text)},
  attachments: [{{ id: 'attachment-2' }}],
}};
const liveResult = _anchorSceneTransparentNodeForRow(liveRow, {{ settled: true, finalAnswer: {json.dumps(final_answer)} }});
const boundaryResult = _anchorSceneTransparentNodeForRow(boundaryRow, {{ settled: true, finalAnswer: {json.dumps(final_answer)} }});
process.stdout.write(JSON.stringify({{
  liveResult: liveResult === null,
  boundaryResult: !!boundaryResult,
  boundaryRowId: boundaryResult && boundaryResult.attributes && boundaryResult.attributes['data-anchor-row-id'] || '',
}}));
"""
    data = _run_node(UI_JS, script, tmp_path)
    assert data["liveResult"] is False
    assert data["boundaryResult"] is True
    assert data["boundaryRowId"] == "session-prose:stream-1:2"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_render_fallback_suppresses_near_complete_live_prefix_rows(tmp_path):
    final_answer = "I found the issue and I am fixing it by deduping live-token prefixes during settlement and render fallback now."
    prefix_text = "I found the issue and I am fixing it by deduping live-token prefixes during settlement and render fallback"
    script = f"""
const src = {json.dumps(UI_JS)};
function extractFunc(name) {{
  const start = src.indexOf('function ' + name);
  if (start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for (let i = params; i < src.length; i++) {{
    if (src[i] === '(') depth++;
    else if (src[i] === ')') {{
      depth--;
      if (depth === 0) {{ close = i; break; }}
    }}
  }}
  const brace = src.indexOf('{{', close);
  depth = 0;
  for (let i = brace; i < src.length; i++) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') {{
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }}
  }}
  throw new Error(name + ' body did not close');
}}
class FakeElement {{
  constructor(tag) {{
    this.tagName = String(tag || 'div').toUpperCase();
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
  }}
  setAttribute(name, value) {{
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) {{
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }}
  }}
}}
global.window = {{}};
global.document = {{ createElement(tag) {{ return new FakeElement(tag); }} }};
global._anchorSceneNodeForRow = () => new FakeElement('div');
global._decorateTransparentEventRow = node => node;
global._anchorSceneToolCallFromRow = () => ({{}});
global.buildToolCard = () => new FakeElement('div');
global._thinkingActivityNode = () => new FakeElement('div');
eval(extractFunc('_anchorSceneProseMatchesFinalAnswer'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
const liveRow = {{
  role: 'prose',
  kind: 'process_prose',
  source_event_type: 'token',
  local_id: 'live-prose:stream-1:near',
  text: {json.dumps(prefix_text)},
}};
const liveResult = _anchorSceneTransparentNodeForRow(liveRow, {{ settled: true, finalAnswer: {json.dumps(final_answer)} }});
process.stdout.write(JSON.stringify(liveResult === null));
"""
    data = _run_node(UI_JS, script, tmp_path)
    assert data is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_non_live_prefix_rows_survive_mode_and_attachment_variants(tmp_path):
    final_answer = "I found the issue and I am fixing it by deduping live-token prefixes during settlement and render fallback so Transparent Stream does not repeat the same prose row."
    prefix_text = "I found the issue and I am fixing it by deduping live-token prefixes"
    script = f"""
const src = {json.dumps(UI_JS)};
function extractFunc(name) {{
  const start = src.indexOf('function ' + name);
  if (start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for (let i = params; i < src.length; i++) {{
    if (src[i] === '(') depth++;
    else if (src[i] === ')') {{
      depth--;
      if (depth === 0) {{ close = i; break; }}
    }}
  }}
  const brace = src.indexOf('{{', close);
  depth = 0;
  for (let i = brace; i < src.length; i++) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') {{
      depth--;
      if (depth === 0) return src.slice(start, i + 1);
    }}
  }}
  throw new Error(name + ' body did not close');
}}
class FakeElement {{
  constructor(tag) {{
    this.tagName = String(tag || 'div').toUpperCase();
    this.attributes = Object.create(null);
    this.dataset = Object.create(null);
  }}
  setAttribute(name, value) {{
    this.attributes[name] = String(value);
    if (name.startsWith('data-')) {{
      const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      this.dataset[key] = String(value);
    }}
  }}
}}
global.window = {{}};
global.document = {{ createElement(tag) {{ return new FakeElement(tag); }} }};
global._anchorSceneNodeForRow = () => new FakeElement('div');
global._decorateTransparentEventRow = node => node;
global._anchorSceneToolCallFromRow = () => ({{}});
global.buildToolCard = () => new FakeElement('div');
global._thinkingActivityNode = () => new FakeElement('div');
eval(extractFunc('_anchorSceneProseMatchesFinalAnswer'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
const variants = [
  {{
    label: 'full_dom',
    row: {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'manual',
      local_id: 'session-prose:stream-1:3',
      text: {json.dumps(prefix_text)},
      attachments: [{{ id: 'attachment-1' }}],
    }},
    opts: {{ settled: true, finalAnswer: {json.dumps(final_answer)} }},
  }},
  {{
    label: 'virtualized',
    row: {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'manual',
      local_id: 'session-prose:stream-1:4',
      text: {json.dumps(prefix_text)},
    }},
    opts: {{ settled: false, finalAnswer: {json.dumps(final_answer)} }},
  }},
  {{
    label: 'attachments',
    row: {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'manual',
      local_id: 'session-prose:stream-1:5',
      text: {json.dumps(prefix_text)},
      attachments: [{{ id: 'attachment-2' }}],
    }},
    opts: {{ settled: true, finalAnswer: {json.dumps(final_answer)} }},
  }},
];
const rendered = variants.map(({{
  label,
  row,
  opts,
}}) => {{
  const node = _anchorSceneTransparentNodeForRow(row, opts);
  return {{
    label,
    visible: !!node,
    rowId: node && node.attributes && node.attributes['data-anchor-row-id'] || '',
    attachmentCount: Array.isArray(row.attachments) ? row.attachments.length : 0,
  }};
}});
process.stdout.write(JSON.stringify(rendered));
"""
    data = _run_node(UI_JS, script, tmp_path)
    assert data == [
        {"label": "full_dom", "visible": True, "rowId": "session-prose:stream-1:3", "attachmentCount": 1},
        {"label": "virtualized", "visible": True, "rowId": "session-prose:stream-1:4", "attachmentCount": 0},
        {"label": "attachments", "visible": True, "rowId": "session-prose:stream-1:5", "attachmentCount": 1},
    ]
