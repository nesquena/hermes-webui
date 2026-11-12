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
ISSUE5749_CAPTURED_SESSION = json.loads(
    (ROOT / "tests" / "fixtures" / "issue5749_captured_session_prefix.json").read_text(encoding="utf-8")
)


def _run_node(src, script, tmp_path):
    assert NODE, "node is required for issue #5749 regression tests"
    script_path = tmp_path / "issue5749_node_script.js"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run([NODE, str(script_path)], text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _normalized_text(text):
    return " ".join(str(text or "").split()).lower()


def _issue5749_captured_scene():
    scenes = ISSUE5749_CAPTURED_SESSION["anchor_activity_scenes"]
    scene = next(iter(scenes.values()))["scene"]
    rows = scene["activity_rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["role"] == "prose"
    assert row["kind"] == "process_prose"
    assert row["source_event_type"] == "token"
    assert row["local_id"].startswith("live-prose:")
    assert not any(
        other is not row and other.get("text") == row["text"] and not str(other.get("local_id", "")).startswith("live-prose:")
        for other in rows
    )
    row_key = _normalized_text(row["text"])
    final_key = _normalized_text(scene["final_answer"])
    assert row_key and final_key.startswith(row_key)
    assert 0.40 <= len(row_key) / len(final_key) <= 0.45
    return scene, row


@pytest.mark.reproduction
@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_reproduction_fixture_matches_lone_live_prefix_shape():
    scene, row = _issue5749_captured_scene()
    final_key = _normalized_text(scene["final_answer"])
    row_key = _normalized_text(row["text"])

    assert row_key != final_key
    assert final_key.startswith(row_key)
    assert scene["final_answer"] == ISSUE5749_CAPTURED_SESSION["messages"][0]["content"]


@pytest.mark.reproduction
@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_settlement_preserves_pre_tool_live_token_prefix_rows(tmp_path):
    fixture_scene, fixture_row = _issue5749_captured_scene()
    final_answer = fixture_scene["final_answer"]
    prefix_text = fixture_row["text"]
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
      local_id: {json.dumps(fixture_row["local_id"])},
      text: {json.dumps(prefix_text)},
      status: 'running',
      attachments: [{{ id: 'attachment-1' }}],
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
    assert [row["local_id"] for row in data["rows"]] == [fixture_row["local_id"], "tool-row-1"]
    assert data["rows"][0]["text"] == prefix_text
    assert data["rows"][0]["role"] == "prose"
    assert data["rows"][1]["role"] == "tool"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_distinct_live_token_prefix_rows_survive_settlement(tmp_path):
    final_answer = "The solution is to preserve short legitimate live-token prefixes while still suppressing longer duplicated final-answer spans."
    prefix_text = "Checking context"
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


@pytest.mark.reproduction
@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_long_live_progress_prefix_is_suppressed_without_settled_duplicate(tmp_path):
    fixture_scene, fixture_row = _issue5749_captured_scene()
    final_answer = fixture_scene["final_answer"]
    prefix_text = fixture_row["text"]
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
      local_id: {json.dumps(fixture_row["local_id"])},
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
    assert data == []


@pytest.mark.reproduction
@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_render_fallback_suppresses_persisted_live_progress_prefix_rows(tmp_path):
    fixture_scene, fixture_row = _issue5749_captured_scene()
    final_answer = fixture_scene["final_answer"]
    prefix_text = fixture_row["text"]
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
eval(extractFunc('_anchorSceneLiveTokenFinalPrefix'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
const liveRow = {{
  role: 'prose',
  kind: 'process_prose',
  source_event_type: 'token',
  local_id: {json.dumps(fixture_row["local_id"])},
  text: {json.dumps(prefix_text)},
}};
const liveResult = _anchorSceneTransparentNodeForRow(liveRow, {{
  settled: true,
  finalAnswer: {json.dumps(final_answer)},
  liveTokenFinalPrefixEligible: true,
}});
process.stdout.write(JSON.stringify({{
  liveResult: liveResult === null,
}}));
"""
    data = _run_node(UI_JS, script, tmp_path)
    assert data["liveResult"] is True


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
eval(extractFunc('_anchorSceneLiveTokenFinalPrefix'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
const liveRow = {{
  role: 'prose',
  kind: 'process_prose',
  source_event_type: 'token',
  local_id: 'live-prose:stream-1:near',
  text: {json.dumps(prefix_text)},
}};
const liveResult = _anchorSceneTransparentNodeForRow(liveRow, {{
  settled: true,
  finalAnswer: {json.dumps(final_answer)},
  liveTokenFinalPrefixEligible: true,
}});
process.stdout.write(JSON.stringify(liveResult === null));
"""
    data = _run_node(UI_JS, script, tmp_path)
    assert data is True


@pytest.mark.reproduction
@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_render_fallback_preserves_pre_tool_live_token_prefix_rows(tmp_path):
    fixture_scene, fixture_row = _issue5749_captured_scene()
    final_answer = fixture_scene["final_answer"]
    prefix_text = fixture_row["text"]
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
eval(extractFunc('_anchorSceneLiveTokenFinalPrefix'));
eval(extractFunc('_anchorSceneTransparentNodeForRow'));
const liveRow = {{
  role: 'prose',
  kind: 'process_prose',
  source_event_type: 'token',
  local_id: {json.dumps(fixture_row["local_id"])},
  text: {json.dumps(prefix_text)},
}};
const liveResult = _anchorSceneTransparentNodeForRow(liveRow, {{
  settled: true,
  finalAnswer: {json.dumps(final_answer)},
  liveTokenFinalPrefixEligible: false,
}});
process.stdout.write(JSON.stringify({{
  visible: !!liveResult,
  rowId: liveResult && liveResult.attributes && liveResult.attributes['data-anchor-row-id'] || '',
}}));
"""
    data = _run_node(UI_JS, script, tmp_path)
    assert data == {"visible": True, "rowId": fixture_row["local_id"]}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_non_live_prefix_rows_survive_mode_and_attachment_variants(tmp_path):
    fixture_scene, fixture_row = _issue5749_captured_scene()
    final_answer = fixture_scene["final_answer"]
    prefix_text = fixture_row["text"]
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
eval(extractFunc('_anchorSceneLiveTokenFinalPrefix'));
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


@pytest.mark.reproduction
@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_issue5749_multi_segment_settled_tool_rows_do_not_shield_final_accumulator(tmp_path):
    """#5758 gap: in a multi-segment turn the settled per-message tool rows are
    appended AFTER the projected live rows, so a combined-list "after the last
    tool row" boundary never marked the final segment's live-prose accumulator
    as final — its stale prefix snapshot survived settlement and rendered as a
    duplicate of the answer's beginning above the settled segment. The boundary
    must come from the live projection's own chronology: the accumulator (after
    the last PROJECTED tool row) is dropped, while pre-tool narration and every
    tool row survive."""
    final_answer = (
        "## Verdict\n\nThe report is mostly right but oversimplifies: curated skill "
        "bundles help strongly across all tested harness combinations, while "
        "one-shot self-generated bundles without quality control regress the "
        "baseline on every configuration we measured. The correct takeaway is to "
        "keep a curated, versioned library instead of disabling skills entirely."
    )
    accumulator_prefix = final_answer[:70]
    narration_text = "Checking the local skill inventory before judging the claim."
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
// The settled per-message rows re-list the turn's tool as a completed copy;
// it lands AFTER the projected live rows in the combined ordering.
function _anchorSceneRowsByMessageIndex() {{
  return new Map([[2, [{{
    role: 'tool',
    kind: 'tool_result',
    source_event_type: 'tool_complete',
    local_id: 'settled-tool-row-1',
    tool_call_id: 'call-1',
    text: '',
    status: 'completed',
  }}]]]);
}}
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
  {{ role: 'assistant', content: {json.dumps(narration_text)}, id: 'assistant-1' }},
  {{ role: 'assistant', content: {json.dumps(final_answer)}, id: 'assistant-2' }},
];
const scene = _completeSettledAnchorSceneForTurn(messages, 2, {{
  mode: 'transparent_stream',
  final_answer: {json.dumps(final_answer)},
  lifecycle: {{ terminal_state: 'done' }},
  identity: {{ source_message_refs: ['legacy'] }},
  activity_rows: [
    {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'token',
      local_id: 'live-prose:stream-multi:1',
      text: {json.dumps(narration_text)},
      status: 'running',
    }},
    {{
      role: 'tool',
      kind: 'tool_started',
      source_event_type: 'tool',
      local_id: 'live-tool-row-1',
      tool_call_id: 'call-1',
      text: '',
      status: 'running',
    }},
    {{
      role: 'prose',
      kind: 'process_prose',
      source_event_type: 'token',
      local_id: 'live-prose:stream-multi:2',
      text: {json.dumps(accumulator_prefix)},
      status: 'running',
    }},
  ],
}});
process.stdout.write(JSON.stringify(scene.activity_rows.map(row => ({{
  role: row.role,
  local_id: row.local_id,
  text: row.text || '',
}}))));
"""
    data = _run_node(MESSAGES_JS, script, tmp_path)
    local_ids = [row["local_id"] for row in data]
    # The stale accumulator snapshot (strict prefix of the final answer, well
    # under the 0.9 near-match ratio) is dropped; narration and tools survive.
    assert "live-prose:stream-multi:2" not in local_ids
    assert "live-prose:stream-multi:1" in local_ids
    assert data[0]["text"] == narration_text
    assert any(row["role"] == "tool" for row in data)
