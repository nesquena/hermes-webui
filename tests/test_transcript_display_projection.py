"""Executable coverage for bounded, non-mutating transcript display text."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DRIVER_SRC = r"""
const _pending = [];
// Collect the declaration SOURCE of the helpers under test, write it once as a
// CommonJS module, require() it, then publish onto globalThis so the driver
// bodies below can keep referring to those helpers by their bare names.
function _loadModule(entries) {
  const fs2 = require('fs'), os2 = require('os'), path2 = require('path');
  const kept = entries.filter(e => e && e[1]);
  const file = path2.join(
    os2.tmpdir(),
    'ui_extract_' + process.pid + '_' + Math.random().toString(36).slice(2) + '.js',
  );
  fs2.writeFileSync(
    file,
    kept.map(e => e[1]).join('\n')
      + '\nmodule.exports = {' + kept.map(e => e[0]).join(',') + '};\n',
  );
  const M = require(file);
  try { fs2.unlinkSync(file); } catch (_) {}
  Object.assign(globalThis, M);
  return M;
}
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractConst(name) {
  const match = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!match) throw new Error(name + ' not found');
  _pending.push([name, 'const ' + name + '=' + match[1] + ';']);
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
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_HEAD');
extractConst('_TRANSCRIPT_DISPLAY_NOTICE');
try{extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_DATA_RE');}catch(_){}
try{extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_RE');}catch(_){}
try{_pending.push(['_isB64Code', extractFunc('_isB64Code')]);_pending.push(['_scanBase64Gap', extractFunc('_scanBase64Gap')]);_pending.push(['_opaqueRunSpans', extractFunc('_opaqueRunSpans')]);_pending.push(['_boundOpaqueRuns', extractFunc('_boundOpaqueRuns')]);}catch(_){}
_pending.push(['_isSafeDataImageUri', extractFunc('_isSafeDataImageUri')]);
_pending.push(['_projectTranscriptTextForDisplay', extractFunc('_projectTranscriptTextForDisplay')]);
_pending.push(['_stripXmlToolCallsDisplay', extractFunc('_stripXmlToolCallsDisplay')]);
_pending.push(['_sanitizeThinkingDisplayText', extractFunc('_sanitizeThinkingDisplayText')]);
_pending.push(['_renderThinkingInto', extractFunc('_renderThinkingInto')]);
_loadModule(_pending);

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
const _pending = [];
// Collect the declaration SOURCE of the helpers under test, write it once as a
// CommonJS module, require() it, then publish onto globalThis so the driver
// bodies below can keep referring to those helpers by their bare names.
function _loadModule(entries) {
  const fs2 = require('fs'), os2 = require('os'), path2 = require('path');
  const kept = entries.filter(e => e && e[1]);
  const file = path2.join(
    os2.tmpdir(),
    'ui_extract_' + process.pid + '_' + Math.random().toString(36).slice(2) + '.js',
  );
  fs2.writeFileSync(
    file,
    kept.map(e => e[1]).join('\n')
      + '\nmodule.exports = {' + kept.map(e => e[0]).join(',') + '};\n',
  );
  const M = require(file);
  try { fs2.unlinkSync(file); } catch (_) {}
  Object.assign(globalThis, M);
  return M;
}
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
function extractConst2(name) {
  const match = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!match) throw new Error(name + ' not found');
  _pending.push([name, 'const ' + name + '=' + match[1] + ';']);
}
extractConst2('_DATA_IMAGE_RE');
extractConst2('_DATA_IMAGE_SVG_RE');
extractConst2('_DATA_IMAGE_MAX_LEN');
extractConst2('_TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT');
try{extractConst2('_TRANSCRIPT_DISPLAY_OPAQUE_HEAD');}catch(_){}
extractConst2('_TRANSCRIPT_DISPLAY_NOTICE');
try{extractConst2('_TRANSCRIPT_DISPLAY_OPAQUE_DATA_RE');}catch(_){}
try{extractConst2('_TRANSCRIPT_DISPLAY_OPAQUE_RE');}catch(_){}
_pending.push(['_isSafeDataImageUri', extractFunc('_isSafeDataImageUri')]);
try{_pending.push(['_isB64Code', extractFunc('_isB64Code')]);_pending.push(['_scanBase64Gap', extractFunc('_scanBase64Gap')]);_pending.push(['_opaqueRunSpans', extractFunc('_opaqueRunSpans')]);_pending.push(['_boundOpaqueRuns', extractFunc('_boundOpaqueRuns')]);}catch(_){}
_pending.push(['_projectTranscriptTextForDisplay', extractFunc('_projectTranscriptTextForDisplay')]);
_pending.push(['_messageSessionIndexBase', extractFunc('_messageSessionIndexBase')]);_pending.push(['_messageSessionIndexForRawIdx', extractFunc('_messageSessionIndexForRawIdx')]);_pending.push(['_toolDisclosureOwnerIndex', extractFunc('_toolDisclosureOwnerIndex')]);_pending.push(['_toolDisclosureIdentity', extractFunc('_toolDisclosureIdentity')]);
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

globalThis.S = {toolCalls: [], messages: []};
_pending.push(['_stampToolCallOrdinals', extractFunc('_stampToolCallOrdinals')]);_pending.push(['_toolCallByDisclosureKey', extractFunc('_toolCallByDisclosureKey')]);
_pending.push(['_toggleToolDiff', extractFunc('_toggleToolDiff')]);
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
_pending.push(['buildToolCard', extractFunc('buildToolCard')]);
_pending.push(['_transparentToolDetailHtml', extractFunc('_transparentToolDetailHtml')]);
_loadModule(_pending);

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
  if (payload.mode === 'toggle-canonical-miss') {
    // Build the card for real, then reproduce the state Show more actually
    // hits after an innerHTML restore when EVERY canonical recovery route
    // misses: the _tcData expando is gone, there is no anchor-scene helper,
    // and S.toolCalls no longer holds the call. The button is wired from the
    // attributes buildToolCard really serialized, and the real _toggleToolDiff
    // is driven -- the fallback expression is never reimplemented here.
    const row = buildToolCard(payload.tc);
    try { delete row._tcData; } catch (_) {}
    S.toolCalls = [];
    const html = row.innerHTML;
    const grab = re => (html.match(re) || [])[1];
    const unesc = v => String(v == null ? '' : v)
      .replace(/&quot;/g, '"').replace(/&gt;/g, '>')
      .replace(/&lt;/g, '<').replace(/&amp;/g, '&');
    const fullRaw = grab(/data-full="([^"]*)"/);
    const shortRaw = grab(/data-short="([^"]*)"/);
    const moreLabel = unesc(grab(/data-more-label="([^"]*)"/));
    const lessLabel = unesc(grab(/data-less-label="([^"]*)"/));
    const pre = {textContent: ''};
    const resultBox = {querySelector: sel => (sel === 'pre' ? pre : null)};
    const btn = {
      dataset: {
        // Absent attribute must read as undefined, exactly like a real DOM.
        full: fullRaw == null ? undefined : unesc(fullRaw),
        short: unesc(shortRaw),
        isDiff: grab(/data-is-diff="([^"]*)"/),
        moreLabel: moreLabel,
        lessLabel: lessLabel,
      },
      // textContent === moreLabel is what makes _toggleToolDiff expand.
      textContent: moreLabel,
      closest: sel => (sel === '.tool-card-result'
        ? resultBox
        : (sel === '.tool-card-row' ? row : null)),
    };
    _toggleToolDiff(btn);
    process.stdout.write(JSON.stringify({
      hasFullAttribute: fullRaw != null,
      shortPreview: unesc(shortRaw),
      expandedText: pre.textContent,
    }));
    return;
  }
  const row = buildToolCard(payload.tc);
  process.stdout.write(JSON.stringify({
    htmlLength: row.innerHTML.length,
    hasFullPayloadAttribute: row.innerHTML.includes('data-full='),
    fullPayload: (row.innerHTML.match(/data-full="([^"]*)"/) || [])[1] || '',
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


def _project(driver_path: str, value: str, *, surface: str = "message", streaming: bool = False) -> dict[str, str]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=json.dumps({"value": value, "options": {"surface": surface, "streaming": streaming}}),
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


def _tool_toggle_canonical_miss(tool_driver_path: str, tc: dict) -> dict[str, object]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, tool_driver_path, str(UI_JS_PATH)],
        input=json.dumps({"tc": tc, "mode": "toggle-canonical-miss"}),
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


def test_malformed_near_limit_data_image_is_linear(driver_path: str) -> None:
    # The candidate is almost at the accepted URI limit but contains an invalid
    # delimiter near the end. Projection must not repeatedly shorten and re-test
    # millions of prefixes before bounding the remaining opaque text.
    payload = "data:image/png;base64," + ("A" * 2_000_000) + "!" + ("B" * 100_000)

    started = time.monotonic()
    result = _project(driver_path, payload, surface="assistant")
    elapsed = time.monotonic() - started

    assert result["source"] == payload
    assert elapsed < 2.0
    assert "opaque payload abbreviated for display" in result["display"]


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


def test_show_more_consumes_bounded_fallback_when_canonical_recovery_misses(
    tool_driver_path: str,
) -> None:
    """Show more must render the bounded serialized fallback, not the short preview.

    buildToolCard serializes a BOUNDED projection into data-full; before this
    was consumed, _toggleToolDiff still fell back to data-short, so the
    attribute was present in the DOM but never reached the user -- an
    attribute-presence assertion passed against a complete no-op. This drives
    the real _toggleToolDiff with every canonical recovery route missing and
    asserts on what the expanded <pre> actually receives.
    """
    payload = "D" * 200_000
    raw = "data:application/octet-stream;base64," + payload
    result = _tool_toggle_canonical_miss(
        tool_driver_path,
        {"name": "terminal", "done": True, "snippet": raw},
    )

    expanded = str(result["expandedText"])
    short_preview = str(result["shortPreview"])

    # The producer emitted a bounded fallback at all.
    assert result["hasFullAttribute"] is True

    # ...and the consumer actually rendered it rather than the short preview.
    assert expanded != short_preview
    assert len(expanded) > len(short_preview)
    assert "opaque payload abbreviated for display" in expanded

    # Bounded, and never the raw unbounded payload.
    assert payload not in expanded
    assert len(expanded) < 70_000


def test_non_abbreviated_show_more_deliberately_has_no_bounded_fallback(
    tool_driver_path: str,
) -> None:
    """Ordinary output past the preview cut gets Show more but NO data-full.

    data-full is emitted only when projection actually shortened the snippet.
    Ordinary prose well past the ~800-char preview cut is therefore serialized
    WITHOUT a bounded fallback, and on canonical-recovery miss Show more shows
    the short preview. That is deliberate, not a coverage gap: for a
    non-abbreviated snippet the "bounded" form is the canonical text, so
    serializing it would put unbounded canonical data in the DOM -- the exact
    contract rounds 6 and 9 removed data-full to protect. This pins the
    boundary so the narrower-than-it-reads condition cannot be silently
    "fixed" into a contract breach.
    """
    prose = "ordinary tool output line. " * 400  # ~10.8k chars, no opaque run
    result = _tool_toggle_canonical_miss(
        tool_driver_path,
        {"name": "terminal", "done": True, "snippet": prose},
    )

    # Projection did not shorten this snippet, so no bounded fallback exists...
    assert result["hasFullAttribute"] is False

    # ...and Show more therefore renders the short preview, by design.
    expanded = str(result["expandedText"])
    short_preview = str(result["shortPreview"])
    assert expanded == short_preview

    # The canonical text was never serialized into the DOM.
    assert len(expanded) < 1_200
    assert prose not in expanded


def test_tool_card_does_not_duplicate_unbounded_ordinary_output(tool_driver_path: str) -> None:
    prose = "ordinary output " * 20_000
    result = _tool_render(
        tool_driver_path,
        {"name": "terminal", "done": True, "snippet": prose},
    )

    # The full 200,000-char snippet must never be serialized into the DOM
    # (a prior regression re-added a data-full attribute holding the
    # complete payload, defeating the bounded-display/cache contract this
    # test is named for). Show-more recovers the canonical value by
    # identity (see test_restore_recovers_full_snippet_via_disclosure_key
    # for the recovery path itself) instead of reading it back off the DOM.
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


def test_idless_ordinal_reordered_candidates_bind_correctly(tool_driver_path: str) -> None:
    """Two same-name ID-less calls must bind correctly even when the
    candidate array is reordered or contains duplicates — ordinal is
    immutable, not recounted from live order. Uses real functions."""
    assert NODE is not None
    import subprocess, json, tempfile, os
    driver_js = r"""
const _pending=[];/* Collect declaration SOURCE, write one temp CommonJS module, require() it, publish onto globalThis. */function _loadModule(entries){const fs2=require('fs'),os2=require('os'),path2=require('path');const kept=entries.filter(e=>e&&e[1]);const file=path2.join(os2.tmpdir(),'ui_extract_'+process.pid+'_'+Math.random().toString(36).slice(2)+'.js');fs2.writeFileSync(file,kept.map(e=>e[1]).join('\n')+'\nmodule.exports={'+kept.map(e=>e[0]).join(',')+'};\n');const M=require(file);try{fs2.unlinkSync(file);}catch(_){}Object.assign(globalThis,M);return M;}

const fs=require('fs');const src=fs.readFileSync(process.argv[2],'utf8');
function extractFunc(name){const s=src.search(new RegExp('function\\s+'+name+'\\s*\\('));if(s<0)throw new Error(name+' not found');let c=src.indexOf('{',s);let d=1;c++;while(d&&c<src.length){if(src[c]=='{')d++;else if(src[c]==='}')d--;c++;}return src.slice(s,c);}
function extractConst(name){const m=src.match(new RegExp('const '+name+'=([^\\n]*);'));if(!m)throw new Error(name+' not found');_pending.push([name,'const '+name+'='+m[1]+';']);}
extractConst('_DATA_IMAGE_RE');extractConst('_DATA_IMAGE_SVG_RE');extractConst('_DATA_IMAGE_MAX_LEN');extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT');try{extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_HEAD');}catch(_){}extractConst('_TRANSCRIPT_DISPLAY_NOTICE');try{extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_DATA_RE');}catch(_){}try{extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_RE');}catch(_){}
globalThis.esc=v=>String(v==null?'':v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');globalThis.li=()=>'';globalThis.toolIcon=()=>'';globalThis._toolActionKind=()=>'shell';globalThis._toolActionLabelText=()=>String(arguments[0]&&arguments[0].name||'tool');globalThis._toolDisplayName=()=>String(arguments[0]&&arguments[0].name||'tool');globalThis._toolCardAllowsDetail=()=>true;globalThis._toolCardPreviewText=(tc,s)=>s;globalThis._formatToolArgPreview=()=>'';globalThis._toolTargetLabel=()=>'';globalThis._toolFullCommandLabel=()=>'';globalThis._toolDetailLeadLabel=()=>'Shell';globalThis._redactToolTargetLabel=v=>v;globalThis._isMemorySave=()=>false;globalThis._isSkillUpdate=()=>false;globalThis._snippetLooksLikeDiff=()=>false;globalThis._colorDiffLines=globalThis.esc;globalThis.t=k=>k;
_pending.push(['_isSafeDataImageUri', extractFunc('_isSafeDataImageUri')]);try{_pending.push(['_isB64Code', extractFunc('_isB64Code')]);_pending.push(['_scanBase64Gap', extractFunc('_scanBase64Gap')]);_pending.push(['_opaqueRunSpans', extractFunc('_opaqueRunSpans')]);_pending.push(['_boundOpaqueRuns', extractFunc('_boundOpaqueRuns')]);}catch(_){}_pending.push(['_projectTranscriptTextForDisplay', extractFunc('_projectTranscriptTextForDisplay')]);_pending.push(['_messageSessionIndexBase', extractFunc('_messageSessionIndexBase')]);_pending.push(['_messageSessionIndexForRawIdx', extractFunc('_messageSessionIndexForRawIdx')]);_pending.push(['_toolDisclosureOwnerIndex', extractFunc('_toolDisclosureOwnerIndex')]);_pending.push(['_toolDisclosureIdentity', extractFunc('_toolDisclosureIdentity')]);globalThis.S={toolCalls:[],messages:[]};_pending.push(['_stampToolCallOrdinals', extractFunc('_stampToolCallOrdinals')]);_pending.push(['_toolCallByDisclosureKey', extractFunc('_toolCallByDisclosureKey')]);globalThis.document={createElement:()=>({dataset:{},_attrs:{},setAttribute(k,v){this._attrs[k]=String(v);},getAttribute(k){return this._attrs[k]||null},removeAttribute(k){delete this._attrs[k];},querySelector(){return null},closest(){return null},classList:{add(){},remove(){},contains(){return false}},innerHTML:''})};_pending.push(['buildToolCard', extractFunc('buildToolCard')]);
_loadModule(_pending);
let input='';process.stdin.on('data',c=>input+=c);process.stdin.on('end',()=>{
  const tcA={name:'terminal',assistant_msg_idx:3,snippet:'AAA',done:true};
  const tcB={name:'terminal',assistant_msg_idx:3,snippet:'BBB',done:true};
  // Do NOT hand-assign _ordinal/_disclosureOrdinal here: that would assume the
  // very normalization invariant this test depends on. Let the real entry
  // point mint them, exactly as renderMessages() does. (messages[3] so the
  // minted owner matches assistant_msg_idx:3.)
  S.messages=[{},{},{},{role:'assistant',tool_calls:[tcA,tcB]}];
  _stampToolCallOrdinals(S.messages);
  if(tcA._disclosureOrdinal===undefined||tcB._disclosureOrdinal===undefined) throw new Error('normalization did not mint ordinals');
  if(tcA._disclosureOrdinal===tcB._disclosureOrdinal) throw new Error('normalization minted colliding ordinals');
  const rowA=buildToolCard(tcA);const rowB=buildToolCard(tcB);
  const keyA=rowA.getAttribute('data-tool-disclosure-key');const keyB=rowB.getAttribute('data-tool-disclosure-key');
  S.toolCalls=[tcA,tcB];S.messages=[];
  const gotA2=_toolCallByDisclosureKey(keyA);
  const gotB2=_toolCallByDisclosureKey(keyB);
  S.toolCalls=[tcB,tcA];
  const gotA3=_toolCallByDisclosureKey(keyA);
  const gotB3=_toolCallByDisclosureKey(keyB);
  process.stdout.write(JSON.stringify({keyA,keyB,gotA2:gotA2&&gotA2.snippet,gotB2:gotB2&&gotB2.snippet,gotA3:gotA3&&gotA3.snippet,gotB3:gotB3&&gotB3.snippet}));
});
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False) as f:
        f.write(driver_js)
        fname = f.name
    try:
        result = subprocess.run([NODE, fname, str(UI_JS_PATH)], input="{}", capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["keyA"] != data["keyB"], f"keys not distinct: {data}"
        assert data["gotA2"] == "AAA", f"expected AAA got {data['gotA2']}"
        assert data["gotB2"] == "BBB", f"expected BBB got {data['gotB2']}"
        assert data["gotA3"] == "AAA", f"reordered gotA3 expected AAA got {data['gotA3']}"
        assert data["gotB3"] == "BBB", f"reordered gotB3 expected BBB got {data['gotB3']}"
    finally:
        os.unlink(fname)


def test_streaming_projection_is_prefix_stable_across_threshold_growth(driver_path: str) -> None:
    """#7040 round-8 re-gate item 2: a growing live payload must never have
    its already-emitted display text retracted as it crosses the 60,000-byte
    bound threshold. Simulate a live stream one chunk at a time (each call
    passes the FULL text so far, same as the real
    _smdWrite/_anchorProseIncrementalNode callers do), growing an opaque run
    PAST the threshold while still open (undelimited) -- every intermediate
    result must be identical (the prefix only, run withheld throughout, per
    test_streaming_projection_withholds_open_run_until_delimiter), and only
    once the run finally closes does its (now-bounded) content appear, in
    one single reveal rather than a raw-then-retracted sequence."""
    prefix = "Here is the result:\n\n"
    growing = prefix
    results = []
    for _ in range(70):
        growing += "A" * 1000  # crosses the 60,000 bound threshold partway through, still open throughout
        result = _project(driver_path, growing, surface="assistant", streaming=True)
        results.append(result["display"])
    assert len(set(results)) == 1, (
        f"an open run must never appear before it closes, regardless of how large it grows -- "
        f"display must stay identical across every growth step: {set(results)!r}"
    )
    stable_prefix = results[0]
    assert stable_prefix.startswith("Here is the result:")

    closed = _project(driver_path, growing + "\n\nDone.", surface="assistant", streaming=True)
    assert closed["display"].startswith(stable_prefix)
    assert "opaque payload abbreviated for display" in closed["display"], (
        "a run this large (well past 60,000 bytes) must resolve to the bounded form once closed"
    )
    assert "Done." in closed["display"]


def test_streaming_projection_withholds_open_run_until_delimiter(driver_path: str) -> None:
    """While a run is still growing (touches the end of the available live
    text), it must not appear in the output at all -- not raw, not bounded --
    until something closes it. Applies regardless of size: showing a short
    run now and hiding it once it grows is the same class of retraction as
    the original 60,000-byte bug."""
    growing = "prefix " + "A" * 5000
    result = _project(driver_path, growing, surface="assistant", streaming=True)
    assert result["display"] == "prefix "

    closed = growing + "\n\nDone."
    result2 = _project(driver_path, closed, surface="assistant", streaming=True)
    assert result2["display"].startswith("prefix ")
    assert "Done." in result2["display"]


def test_streaming_holds_back_only_the_open_trailing_word(driver_path: str) -> None:
    """Ordinary streamed prose has a space every few characters, so the
    withheld open-run tail should be at most the last in-progress word, not
    a whole sentence -- and it must appear promptly once a delimiter (space)
    follows it, not stay stuck."""
    growing = "The quick brown fox jumps over the lazy do"  # "dog" still typing
    result = _project(driver_path, growing, surface="assistant", streaming=True)
    assert result["display"] == "The quick brown fox jumps over the lazy "

    growing += "g "  # word completes, delimiter arrives
    result2 = _project(driver_path, growing, surface="assistant", streaming=True)
    assert result2["display"] == "The quick brown fox jumps over the lazy dog "


def test_streaming_blank_line_stops_run_from_absorbing_trailing_prose(driver_path: str) -> None:
    """#7040 round-8 re-gate item 2: the CR/LF-inclusive base64 scan must not
    consume real prose that follows a blank line after an opaque run."""
    opaque = "A" * 5000
    prose = "This is a normal sentence describing what just happened in the tool call."
    text = opaque + "\n\n" + prose
    result = _project(driver_path, text, surface="assistant", streaming=False)
    assert prose in result["display"], (
        "trailing prose after a blank-line paragraph break must survive intact, "
        f"got: {result['display']!r}"
    )


def test_settled_non_streaming_calls_keep_original_behavior(driver_path: str) -> None:
    """Every existing one-shot (non-streaming) call site passes a complete,
    final string -- default (streaming omitted/false) behavior must be
    byte-identical to the pre-fix behavior for those, since there is no
    ambiguity about whether a trailing run is still growing."""
    payload = "prefix data:application/octet-stream;base64," + ("A" * 200_000)
    default_result = _project(driver_path, payload, surface="tool")
    explicit_false_result = _project(driver_path, payload, surface="tool", streaming=False)
    assert default_result == explicit_false_result
    assert "opaque payload abbreviated for display" in default_result["display"]


# ── Incremental opaque-run cache (perf: session-load/streaming-latency round 9) ──
#
# _boundOpaqueRuns/_opaqueRunSpans re-scanned the ENTIRE accumulated text on
# every streaming update -- O(n) per call, O(n^2) cumulative over a long
# stream. _createIncrementalOpaqueRunCache lets a per-stream caller reuse
# prior scan progress and only process newly-appended bytes each call. These
# tests prove the cache's output is byte-identical to a full, non-cached
# _boundOpaqueRuns call at every single growth step (not just the final
# result) across a wide range of randomized growth patterns, including the
# specific edge cases that broke naive incremental re-entry point choices
# during development: a blank line trimmed to nothing with nothing left to
# absorb, a colon/semicolon-separated data-uri literal prefix fragmenting
# into short base64-charset sub-runs, a bare data-uri prefix with zero
# trailing payload chars (no match yet), a decoy "data:" substring embedded
# in an unrelated word, and two adjacent data-uri literals with no separator
# between them (where the first match's greedy payload group can swallow the
# second literal's own "data" letters as more payload).

_INCREMENTAL_CACHE_DRIVER_SRC = r"""
const _pending = [];
// Collect the declaration SOURCE of the helpers under test, write it once as a
// CommonJS module, require() it, then publish onto globalThis so the driver
// bodies below can keep referring to those helpers by their bare names.
function _loadModule(entries) {
  const fs2 = require('fs'), os2 = require('os'), path2 = require('path');
  const kept = entries.filter(e => e && e[1]);
  const file = path2.join(
    os2.tmpdir(),
    'ui_extract_' + process.pid + '_' + Math.random().toString(36).slice(2) + '.js',
  );
  fs2.writeFileSync(
    file,
    kept.map(e => e[1]).join('\n')
      + '\nmodule.exports = {' + kept.map(e => e[0]).join(',') + '};\n',
  );
  const M = require(file);
  try { fs2.unlinkSync(file); } catch (_) {}
  Object.assign(globalThis, M);
  return M;
}
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

function extractConst(name) {
  const match = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!match) throw new Error(name + ' not found');
  _pending.push([name, 'const ' + name + '=' + match[1] + ';']);
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

extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT');
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_HEAD');
extractConst('_TRANSCRIPT_DISPLAY_NOTICE');
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_DATA_RE');
_pending.push(['_isB64Code', extractFunc('_isB64Code')]);
_pending.push(['_scanBase64Gap', extractFunc('_scanBase64Gap')]);
_pending.push(['_opaqueRunSpans', extractFunc('_opaqueRunSpans')]);
_pending.push(['_boundOpaqueRuns', extractFunc('_boundOpaqueRuns')]);
_pending.push(['_createIncrementalOpaqueRunCache', extractFunc('_createIncrementalOpaqueRunCache')]);
_loadModule(_pending);

let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const payload = JSON.parse(input);
  const cache = _createIncrementalOpaqueRunCache();
  const results = payload.steps.map(step => {
    const inc = cache.project(step.text, !!step.streaming, false);
    const full = _boundOpaqueRuns(step.text, !!step.streaming);
    return { inc, full, match: inc === full };
  });
  process.stdout.write(JSON.stringify({ results }));
});
"""


@pytest.fixture(scope="module")
def incremental_cache_driver_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    path = tmp_path_factory.mktemp("incremental_opaque_cache") / "driver.js"
    path.write_text(_INCREMENTAL_CACHE_DRIVER_SRC, encoding="utf-8")
    return str(path)


def _run_incremental_vs_full(driver_path: str, steps: list[dict]) -> list[dict]:
    assert NODE is not None
    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH)],
        input=json.dumps({"steps": steps}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)["results"]


def _growth_steps(full_text: str, chunk_sizes: list[int]) -> list[dict]:
    """Grow full_text one chunk at a time (streaming=True until the final,
    complete step, matching how a real live caller passes the FULL
    text-so-far on every update)."""
    steps = []
    pos = 0
    i = 0
    while pos < len(full_text):
        pos = min(len(full_text), pos + chunk_sizes[i % len(chunk_sizes)])
        i += 1
        steps.append({"text": full_text[:pos], "streaming": pos < len(full_text)})
    steps.append({"text": full_text, "streaming": False})
    return steps


def _assert_all_match(results: list[dict], scenario: str) -> None:
    for i, r in enumerate(results):
        assert r["match"], (
            f"[{scenario}] incremental cache diverged from full rescan at step {i}:\n"
            f"  incremental: {r['inc']!r}\n"
            f"  full rescan: {r['full']!r}"
        )


def test_incremental_opaque_cache_matches_full_rescan_across_randomized_growth(
    incremental_cache_driver_path: str,
) -> None:
    """Broad randomized fuzz: many chunk-growth patterns mixing plain prose,
    blank lines, base64 runs, and data-uri literals (including some with
    zero trailing payload chars, i.e. no regex match yet) -- the incremental
    cache must match a full, non-cached rescan at every single step, not
    just the final result."""
    import random

    b64_alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    words = ["the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog", "and", "runs"]

    def build_scenario(seed: int, piece_count: int) -> str:
        rng = random.Random(seed)
        pieces = []
        for _ in range(piece_count):
            r = rng.random()
            if r < 0.30:
                pieces.append("".join(rng.choice(b64_alphabet) for _ in range(rng.randint(1, 300))))
            elif r < 0.45:
                # sometimes zero trailing payload chars -- no regex match yet
                tail_len = rng.randint(0, 60)
                pieces.append("data:image/png;base64," + "".join(rng.choice(b64_alphabet) for _ in range(tail_len)))
            elif r < 0.50:
                pieces.append(
                    "data:application/octet-stream;charset=utf-8;base64,"
                    + "".join(rng.choice(b64_alphabet) for _ in range(rng.randint(1, 80)))
                )
            elif r < 0.55:
                pieces.append("metadata:something")  # decoy "data:" substring inside another word
            else:
                pieces.append(" " + " ".join(rng.choice(words) for _ in range(rng.randint(1, 6))) + " ")
                for _ in range(rng.randint(0, 3)):
                    pieces.append("\n")
        return "".join(pieces)

    chunk_size_variants = [
        [1, 2, 3, 4, 6, 9, 14, 23, 37],
        [5, 5, 5],
        [1],
        [50, 17, 3],
    ]
    for seed in range(60):
        full_text = build_scenario(seed, 60)
        steps = _growth_steps(full_text, chunk_size_variants[seed % len(chunk_size_variants)])
        results = _run_incremental_vs_full(incremental_cache_driver_path, steps)
        _assert_all_match(results, f"seed={seed}")


def test_incremental_opaque_cache_handles_two_adjacent_data_uris_with_no_separator(
    incremental_cache_driver_path: str,
) -> None:
    """The specific case that broke a naive bounded backward re-entry search
    during development: a data-uri match's final payload group is GREEDY
    over the same charset base64 payload uses, so it can swallow what looks
    like a second, later "data:" literal's own letters as more of the FIRST
    match's payload (stopping only at the following ':'). Whether that
    happens depends on everything back to the first match's own "data:"
    start, which can be arbitrarily far behind any bounded local slice --
    the cache must fall back to a full rescan for calls near a literal
    "data:", rather than try to reverse-engineer this cross-boundary
    behavior, and must still match a full rescan exactly at every step."""
    first_payload = "ftXnzvXTw5bjQk0qZZZUR92Fg7EtCgTjOSzv9C1FjXUfGZxbC1a1UCZa5Cm7dGVG3M0FERwqPdTC4YpToGXorUmNm3ZpbalddHbmSl3TjdXVdHCk2nwCgczLSdPyd7WxcVoKvjafAMAAxtv"
    second_payload = "SBGKW6QoLzZzYb3JhY"
    full_text = (
        "steps over \n\ndata:image/png;base64," + first_payload + "data:image/png;base64," + second_payload
    )
    steps = _growth_steps(full_text, [1, 2, 3, 5, 8, 13, 21])
    results = _run_incremental_vs_full(incremental_cache_driver_path, steps)
    _assert_all_match(results, "adjacent-data-uris")


def test_incremental_opaque_cache_handles_bare_data_uri_prefix_with_no_trailing_chars(
    incremental_cache_driver_path: str,
) -> None:
    """A data-uri literal with ZERO base64 payload chars after "base64," yet
    produces no span at all (the regex requires >=1 trailing char) -- the
    original _boundOpaqueRuns does NOT withhold this (there's nothing for it
    to recognize as a run), so the incremental cache must match that exactly
    rather than over-eagerly treating it as pending/unstable."""
    full_text = " the quick brown fox jumps over \n\ndata:image/png;base64,"
    steps = _growth_steps(full_text, [1, 3, 7])
    results = _run_incremental_vs_full(incremental_cache_driver_path, steps)
    _assert_all_match(results, "bare-prefix-no-trailing-chars")
    assert results[-1]["full"] == full_text, "a non-matching bare prefix must render as plain text, unmodified"


def test_incremental_opaque_cache_handles_trailing_blank_line_then_new_run(
    incremental_cache_driver_path: str,
) -> None:
    """A lone trailing newline that _scanBase64Gap trims down to nothing (a
    blank-line stop with nothing left to absorb) isn't a durable verdict --
    once more text follows, that same newline can become the start of a
    fresh run. Covers the exact regression found during development."""
    full_text = "the quick brown fox jumps over \n\n" + "".join(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"[i % 64] for i in range(80)
    )
    steps = _growth_steps(full_text, [1, 3, 7, 13])
    results = _run_incremental_vs_full(incremental_cache_driver_path, steps)
    _assert_all_match(results, "blank-line-then-new-run")


def test_incremental_opaque_cache_scan_work_is_sub_quadratic(
    incremental_cache_driver_path: str,
) -> None:
    """Prove the actual fix: total _opaqueRunSpans scan work across a full
    stream of many small updates must be proportional to the text length,
    not to (text length)^2. A full-rescan-per-call baseline would do
    O(total_length) work on every single call; this asserts the cache's
    real total is at least two orders of magnitude less for a realistic
    stream (short updates, occasional blank lines, no opaque payloads)."""
    driver_src = r"""
const _pending=[];/* Collect declaration SOURCE, write one temp CommonJS module, require() it, publish onto globalThis. */function _loadModule(entries){const fs2=require('fs'),os2=require('os'),path2=require('path');const kept=entries.filter(e=>e&&e[1]);const file=path2.join(os2.tmpdir(),'ui_extract_'+process.pid+'_'+Math.random().toString(36).slice(2)+'.js');fs2.writeFileSync(file,kept.map(e=>e[1]).join('\n')+'\nmodule.exports={'+kept.map(e=>e[0]).join(',')+'};\n');const M=require(file);try{fs2.unlinkSync(file);}catch(_){}Object.assign(globalThis,M);return M;}
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function extractConst(name) {
  const match = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  _pending.push([name, 'const ' + name + '=' + match[1] + ';']);
}
function extractFunc(name) {
  const start = src.search(new RegExp('function\\s+' + name + '\\s*\\('));
  let cursor = src.indexOf('{', start) + 1;
  let depth = 1;
  while (depth && cursor < src.length) {
    if (src[cursor] === '{') depth++;
    else if (src[cursor] === '}') depth--;
    cursor++;
  }
  return src.slice(start, cursor);
}
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT');
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_HEAD');
extractConst('_TRANSCRIPT_DISPLAY_NOTICE');
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_DATA_RE');
_pending.push(['_isB64Code', extractFunc('_isB64Code')]);
_pending.push(['_scanBase64Gap', extractFunc('_scanBase64Gap')]);
globalThis.scanCalls = 0; globalThis.scanChars = 0;
let opaqueRunSpansSrc = extractFunc('_opaqueRunSpans').replace(
  'function _opaqueRunSpans(chunk){',
  'function _opaqueRunSpans(chunk){ scanCalls++; scanChars += chunk.length;'
);
_pending.push(['_opaqueRunSpans', opaqueRunSpansSrc]);
_pending.push(['_boundOpaqueRuns', extractFunc('_boundOpaqueRuns')]);
_pending.push(['_createIncrementalOpaqueRunCache', extractFunc('_createIncrementalOpaqueRunCache')]);
_loadModule(_pending);

let input = '';
process.stdin.on('data', chunk => { input += chunk; });
process.stdin.on('end', () => {
  const payload = JSON.parse(input);
  const fullText = payload.text;
  const chunkSize = payload.chunkSize;
  let fullRescanWork = 0;
  for (let p = chunkSize; p <= fullText.length; p += chunkSize) fullRescanWork += p;
  globalThis.scanCalls = 0; globalThis.scanChars = 0;
  const cache = _createIncrementalOpaqueRunCache();
  for (let p = chunkSize; p <= fullText.length; p += chunkSize) {
    cache.project(fullText.slice(0, p), p < fullText.length, false);
  }
  process.stdout.write(JSON.stringify({ fullRescanWork, incrementalWork: globalThis.scanChars }));
});
"""
    driver = incremental_cache_driver_path.rsplit("/", 1)[0] + "/perf_driver.js"
    Path(driver).write_text(driver_src, encoding="utf-8")
    words = ["the", "quick", "brown", "fox", "jumps", "over", "lazy", "dog"]
    import random

    rng = random.Random(1)
    text = ""
    for _ in range(20_000):
        text += rng.choice(words) + " "
        if rng.random() < 0.05:
            text += "\n\n"

    result = subprocess.run(
        [NODE, driver, str(UI_JS_PATH)],
        input=json.dumps({"text": text, "chunkSize": 50}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    data = json.loads(result.stdout)
    assert data["incrementalWork"] * 100 < data["fullRescanWork"], (
        f"incremental cache should do at least ~100x less scan work than full-rescan-per-call "
        f"on a realistic plain-text stream, got incremental={data['incrementalWork']} "
        f"vs full-rescan={data['fullRescanWork']}"
    )


def test_production_projector_work_is_linear_on_a_long_open_payload(tmp_path) -> None:
    """#7040 round 9: measure the COMPLETE production projector, not one
    internal scanner.

    The previous perf test called ``cache.project()`` directly and counted only
    ``_opaqueRunSpans`` input, on ordinary spaced prose. That could not see the
    wrapper's own full-text ``_candidateScanRe`` safe-image sweep, the
    ``raw.startsWith(rawSnapshot)`` full-prefix comparison, or the
    ``data:``-guard's slice-to-end-of-string — each of which was independently
    O(n) per call, so the "incremental" path was still cumulatively O(n^2).

    This drives the real entry point (``_projectTranscriptTextForDisplay`` with
    a ``liveCache``) on the exact target shape — ONE long, still-growing opaque
    payload — and counts every O(n) primitive the implementation can reach:
    ``_opaqueRunSpans`` input, ``String#toLowerCase``, ``String#startsWith``,
    and ``RegExp#exec`` input. It also measures the no-cache baseline in the
    same process so the assertion is a ratio, not a hand-tuned constant.
    """
    driver = r"""
const _pending=[];/* Collect declaration SOURCE, write one temp CommonJS module, require() it, publish onto globalThis. */function _loadModule(entries){const fs2=require('fs'),os2=require('os'),path2=require('path');const kept=entries.filter(e=>e&&e[1]);const file=path2.join(os2.tmpdir(),'ui_extract_'+process.pid+'_'+Math.random().toString(36).slice(2)+'.js');fs2.writeFileSync(file,kept.map(e=>e[1]).join('\n')+'\nmodule.exports={'+kept.map(e=>e[0]).join(',')+'};\n');const M=require(file);try{fs2.unlinkSync(file);}catch(_){}Object.assign(globalThis,M);return M;}
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
function extractConst(name) {
  const match = src.match(new RegExp('const ' + name + '=([^\\n]*);'));
  if (!match) throw new Error(name + ' not found');
  _pending.push([name, 'const ' + name + '=' + match[1] + ';']);
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
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_HEAD');
extractConst('_TRANSCRIPT_DISPLAY_NOTICE');
extractConst('_TRANSCRIPT_DISPLAY_OPAQUE_DATA_RE');
_pending.push(['_isB64Code', extractFunc('_isB64Code')]);
_pending.push(['_scanBase64Gap', extractFunc('_scanBase64Gap')]);

// ── instrument every O(n) primitive the projector can reach ──────────────
globalThis.scanned = 0;
globalThis.counting = false;
const _origLower = String.prototype.toLowerCase;
const _origStarts = String.prototype.startsWith;
const _origExec = RegExp.prototype.exec;
String.prototype.toLowerCase = function () { if (globalThis.counting) globalThis.scanned += this.length; return _origLower.call(this); };
String.prototype.startsWith = function (s) { if (globalThis.counting) globalThis.scanned += this.length; return _origStarts.call(this, s); };
RegExp.prototype.exec = function (s) { if (globalThis.counting && typeof s === 'string') globalThis.scanned += s.length; return _origExec.call(this, s); };

let spansSrc = extractFunc('_opaqueRunSpans').replace(
  'function _opaqueRunSpans(chunk){',
  'function _opaqueRunSpans(chunk){ if (globalThis.counting) globalThis.scanned += chunk.length;'
);
_pending.push(['_opaqueRunSpans', spansSrc]);
_pending.push(['_boundOpaqueRuns', extractFunc('_boundOpaqueRuns')]);
_pending.push(['_isSafeDataImageUri', extractFunc('_isSafeDataImageUri')]);
_pending.push(['_createIncrementalOpaqueRunCache', extractFunc('_createIncrementalOpaqueRunCache')]);
_pending.push(['_projectTranscriptTextForDisplay', extractFunc('_projectTranscriptTextForDisplay')]);
_loadModule(_pending);

let input = '';
process.stdin.on('data', c => { input += c; });
process.stdin.on('end', () => {
  const { total, chunk } = JSON.parse(input);
  // ONE long, still-growing opaque run: the exact case this change targets.
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  let payload = '';
  for (let i = 0; i < total; i++) payload += alphabet[i % alphabet.length];
  const text = 'here is the tool output ' + payload;

  const run = (useCache) => {
    const cache = useCache ? _createIncrementalOpaqueRunCache() : null;
    globalThis.scanned = 0;
    globalThis.counting = true;
    let last = '';
    for (let pos = chunk; pos <= text.length; pos += chunk) {
      const opts = { surface: 'assistant', streaming: true };
      if (cache) opts.liveCache = cache;
      last = _projectTranscriptTextForDisplay(text.slice(0, pos), opts);
    }
    globalThis.counting = false;
    return { scanned: globalThis.scanned, last };
  };

  const cached = run(true);
  const baseline = run(false);
  // Equivalence spot-check: the cached path must agree with a fresh,
  // non-cached projection of the same final text.
  const reference = _projectTranscriptTextForDisplay(
    text.slice(0, Math.floor(text.length / chunk) * chunk),
    { surface: 'assistant', streaming: true }
  );
  process.stdout.write(JSON.stringify({
    cachedScanned: cached.scanned,
    baselineScanned: baseline.scanned,
    textLength: text.length,
    matchesReference: cached.last === reference,
  }));
});
"""
    driver_path = tmp_path / "perf_driver.js"
    driver_path.write_text(driver, encoding="utf-8")

    total, chunk = 120_000, 600
    result = subprocess.run(
        [NODE, str(driver_path), str(UI_JS_PATH)],
        input=json.dumps({"total": total, "chunk": chunk}),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    # Correctness first: a faster wrong answer is not a fix.
    assert data["matchesReference"], "cached projection diverged from a fresh projection"

    n = data["textLength"]
    cached_scanned = data["cachedScanned"]
    baseline_scanned = data["baselineScanned"]

    # The full-rescan baseline is quadratic: ~n^2/(2*chunk).
    assert baseline_scanned > 10 * n, (
        "baseline should be super-linear; the measurement harness is not "
        f"capturing the scans (baseline={baseline_scanned}, n={n})"
    )
    # The cached production path must stay within a small multiple of the text
    # length. A reintroduced full-text sweep anywhere in the wrapper (safe-image
    # scan, prefix compare, data:-guard slicing to end-of-string) immediately
    # pushes this into the same order as the baseline.
    assert cached_scanned < 6 * n, (
        f"production projector is not linear: scanned {cached_scanned} chars "
        f"for a {n}-char stream (baseline {baseline_scanned})"
    )
