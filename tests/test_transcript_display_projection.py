"""Executable coverage for bounded, non-mutating transcript display text."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractConst(name) {
  const match = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!match) throw new Error(name + ' not found');
  globalThis[name] = eval('(' + match[1] + ')');
}

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  let cursor = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth && cursor < src.length) {
    if (src[cursor] === '{') depth++;
    else if (src[cursor] === '}') depth--;
    cursor++;
  }
  return src.slice(start, cursor);
}

extractConst('_DATA_IMAGE_RE');
extractConst('_DATA_IMAGE_SVG_RE');
extractConst('_DATA_IMAGE_MAX_LEN');
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT');
extractConst('_TRANSCRIPT_DISPLAY_NOTICE');
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_RE');
eval(extractFunc('_isSafeDataImageUri'));
eval(extractFunc('_projectTranscriptTextForDisplay'));
eval(extractFunc('_stripXmlToolCallsDisplay'));
eval(extractFunc('_sanitizeThinkingDisplayText'));
eval(extractFunc('_renderThinkingInto'));

let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const payload = JSON.parse(input);
  const source = payload.value;
  if (payload.mode === 'thinking') {
    const pre = {textContent: ''};
    const row = {querySelector: () => pre};
    _renderThinkingInto(row, source);
    process.stdout.write(JSON.stringify({source, display: pre.textContent}));
    return;
  }
  const display = _projectTranscriptTextForDisplay(source, payload.options || {});
  process.stdout.write(JSON.stringify({source, display}));
});
"""

_TOOL_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  if (start < 0) throw new Error(name + ' not found');
  let cursor = src.indexOf('{', start);
  let depth = 1;
  cursor++;
  while (depth && cursor < src.length) {
    if (src[cursor] === '{') depth++;
    else if (src[cursor] === '}') depth--;
    cursor++;
  }
  return src.slice(start, cursor);
}

const esc = value => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const li = () => '';
const noop = () => '';
globalThis.esc = esc;
globalThis.li = li;
globalThis.t = key => key;
globalThis.toolIcon = noop;
globalThis._toolActionKind = () => 'shell';
globalThis._toolActionLabelText = () => 'terminal';
globalThis._toolDisplayName = () => 'terminal';
globalThis._toolDisclosureIdentity = () => 'tool-1';
globalThis._toolCardAllowsDetail = () => true;
globalThis._toolCardPreviewText = (tc, displaySnippet) => displaySnippet;
globalThis._DATA_IMAGE_RE = /^data:image\/(?:png|jpe?g|gif|webp|avif)(?:;base64)?,[a-z0-9+/=%._~:@!$&'()*+,;-]*$/i;
globalThis._DATA_IMAGE_SVG_RE = /^data:image\/svg\+xml;base64,[a-z0-9+/=]+$/i;
globalThis._DATA_IMAGE_MAX_LEN = 2 * 1024 * 1024;
globalThis._TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT = 60000;
globalThis._TRANSCRIPT_DISPLAY_NOTICE = '[opaque payload abbreviated for display]';
globalThis._TRANSCRIPT_DISPLAY_OPAQUE_RE = /data:(?:application|image)\/[a-z0-9.+-]+(?:;[a-z0-9=.+-]+)*;base64,[a-z0-9+/=\r\n]+|[a-z0-9+/=]{60001,}/ig;
globalThis._isSafeDataImageUri = value => String(value || '').length <= _DATA_IMAGE_MAX_LEN
  && (_DATA_IMAGE_RE.test(String(value || '')) || _DATA_IMAGE_SVG_RE.test(String(value || '')));
globalThis._projectTranscriptTextForDisplay = (value, options) => {
  const text = String(value || '');
  const surface = String(options && options.surface || 'message');
  _TRANSCRIPT_DISPLAY_OPAQUE_RE.lastIndex = 0;
  return text.replace(_TRANSCRIPT_DISPLAY_OPAQUE_RE, match => {
    if (_isSafeDataImageUri(match)) return match;
    if (match.length <= _TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT) return match;
    return `${match.slice(0, 2048)}\\n\\n${_TRANSCRIPT_DISPLAY_NOTICE} (${match.length} characters; ${surface})`;
  });
};
globalThis._formatToolArgPreview = () => '';
globalThis._toolTargetLabel = () => '';
globalThis._toolFullCommandLabel = () => '';
globalThis._toolDetailLeadLabel = () => 'Shell';
globalThis._redactToolTargetLabel = value => value;
globalThis._isMemorySave = () => false;
globalThis._isSkillUpdate = () => false;
globalThis._snippetLooksLikeDiff = () => false;
globalThis._colorDiffLines = esc;
globalThis._worklogDetailsExpandedDefault = () => false;
globalThis._worklogDetailHashKey = value => {
  const s = String(value || '');
  let hash = 2166136261;
  for (let i = 0; i < s.length; i++) {
    hash ^= s.charCodeAt(i);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash.toString(36);
};
globalThis._toolDisclosureIdentity = tc => {
  if (!tc) return '';
  const tid = tc.tid || tc.id || tc.tool_call_id || tc.tool_use_id || tc.call_id || '';
  if (tid) return `id:${tid}`;
  const stable = [
    tc.assistant_msg_idx !== undefined ? `a:${tc.assistant_msg_idx}` : '',
    tc.name || 'tool',
  ].join('\x1f');
  return stable.trim() ? `derived:${_worklogDetailHashKey(stable)}` : '';
};
globalThis.S = {toolCalls: [], messages: []};
eval(extractFunc('_toolCallByDisclosureKey'));
eval(extractFunc('_toggleToolDiff'));
globalThis.document = {
  createElement: () => {
    const attrs = {};
    return {
      dataset: {},
      _attrs: attrs,
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return Object.prototype.hasOwnProperty.call(attrs, k) ? attrs[k] : null; },
      removeAttribute(k) { delete attrs[k]; },
      querySelector() { return null; },
      classList: { add() {}, remove() {}, contains() { return false; } },
    };
  },
};
eval(extractFunc('buildToolCard'));
eval(extractFunc('_transparentToolDetailHtml'));

let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const payload = JSON.parse(input);
  if (payload.mode === 'transparent-detail') {
    process.stdout.write(JSON.stringify({html: _transparentToolDetailHtml(payload.tc, 'Completed')}));
    return;
  }
  if (payload.mode === 'recover') {
    // Simulate an HTML-cache round-trip: build the card, then drop the
    // _tcData expando exactly like innerHTML restore does.
    const row = buildToolCard(payload.tc);
    try { delete row._tcData; } catch (_) {}
    const key = row.getAttribute && row.getAttribute('data-tool-disclosure-key');
    // After a session switch the in-memory canonical tool calls survive.
    S.toolCalls = [payload.tc];
    const recovered = _toolCallByDisclosureKey(key);
    process.stdout.write(JSON.stringify({
      disclosureKey: key,
      recoveredSnippet: recovered && recovered.snippet ? recovered.snippet.length : 0,
      recoveredName: recovered && recovered.name,
    }));
    return;
  }
  const row = buildToolCard(payload.tc);
  process.stdout.write(JSON.stringify({
    htmlLength: row.innerHTML.length,
    hasFullPayloadAttribute: row.innerHTML.includes('data-full='),
  }));
});
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("transcript_projection") / "driver.js"
    path.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(path)


@pytest.fixture(scope="module")
def tool_driver_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("tool_projection") / "driver.js"
    path.write_text(_TOOL_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _project(driver_path: str, value: str, *, surface: str = "message") -> dict[str, str]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=json.dumps({"value": value, "options": {"surface": surface}}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def _project_thinking(driver_path: str, value: str) -> dict[str, str]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=json.dumps({"value": value, "mode": "thinking"}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def _tool_render(tool_driver_path: str, tc: dict) -> dict[str, object]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, tool_driver_path, str(UI_JS_PATH)],
        input=json.dumps({"tc": tc}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def _transparent_detail(tool_driver_path: str, tc: dict) -> str:
    assert NODE is not None
    result = subprocess.run(
        [NODE, tool_driver_path, str(UI_JS_PATH)],
        input=json.dumps({"tc": tc, "mode": "transparent-detail"}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)["html"]


def test_opaque_data_payload_is_bounded_without_mutating_source(driver_path: str) -> None:
    payload = "prefix data:application/octet-stream;base64," + ("A" * 200_000)

    result = _project(driver_path, payload, surface="tool")

    assert result["source"] == payload
    assert len(result["display"]) < 70_000
    assert "opaque payload abbreviated for display" in result["display"]


def test_long_unbroken_base64_run_is_bounded(driver_path: str) -> None:
    payload = '{"screenshot":"' + ("A" * 200_000) + '"}'

    result = _project(driver_path, payload, surface="tool")

    assert result["source"] == payload
    assert len(result["display"]) < 70_000
    assert "opaque payload abbreviated for display" in result["display"]


def test_ordinary_long_prose_is_unchanged(driver_path: str) -> None:
    prose = "A normal paragraph with normal wrapping. " * 5_000

    result = _project(driver_path, prose, surface="assistant")

    assert result == {"source": prose, "display": prose}


def test_supported_data_image_is_left_for_media_renderer(driver_path: str) -> None:
    image = "data:image/png;base64,iVBORw0KGgo="

    result = _project(driver_path, f"![screenshot]({image})", surface="assistant")

    assert result["display"] == f"![screenshot]({image})"


def test_repeated_projection_is_deterministic(driver_path: str) -> None:
    payload = "data:application/octet-stream;base64," + ("B" * 200_000)

    first = _project(driver_path, payload, surface="reasoning")
    second = _project(driver_path, payload, surface="reasoning")

    assert first == second
    assert first["source"] == payload


def test_live_thinking_render_is_bounded(driver_path: str) -> None:
    payload = "reasoning data:application/octet-stream;base64," + ("C" * 200_000)

    result = _project_thinking(driver_path, payload)

    assert result["source"] == payload
    assert len(result["display"]) < 70_000
    assert "opaque payload abbreviated for display" in result["display"]


def test_tool_card_does_not_embed_full_snippet_in_dom(tool_driver_path: str) -> None:
    result = _tool_render(
        tool_driver_path,
        {"name": "terminal", "done": True, "snippet": "D" * 200_000},
    )

    assert result["hasFullPayloadAttribute"] is False
    assert result["htmlLength"] < 10_000


def test_transparent_tool_detail_bounds_opaque_args_and_output(tool_driver_path: str) -> None:
    opaque = "A" * 200_000

    html = _transparent_detail(
        tool_driver_path,
        {"args": {"content": opaque}, "snippet": opaque},
    )

    assert len(html) < 20_000
    assert html.count("opaque payload abbreviated for display") == 2


def test_restore_recovers_full_snippet_via_disclosure_key(tool_driver_path: str) -> None:
    """After an HTML-cache round-trip (_tcData expando dropped), Show more
    must still recover the FULL snippet for tool cards that lack anchor-scene
    attrs (worklog / ordered-transparent rows) — via the durable
    data-tool-disclosure-key → S.toolCalls lookup."""
    assert NODE is not None
    full = "D" * 200_000
    tc = {"name": "terminal", "tid": "call_abc123", "done": True, "snippet": full}

    result = subprocess.run(
        [NODE, tool_driver_path, str(UI_JS_PATH)],
        input=json.dumps({"tc": tc, "mode": "recover"}),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    assert data["disclosureKey"] == "id:call_abc123"
    assert data["recoveredName"] == "terminal"
    assert data["recoveredSnippet"] == len(full)
