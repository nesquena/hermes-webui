"""Regression tests for visible ultra-compact compaction cards."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"


def _extract_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"function {name} not found")
    brace = source.find("{", start)
    depth = 1
    pos = brace + 1
    while depth and pos < len(source):
        if source[pos] == "{":
            depth += 1
        elif source[pos] == "}":
            depth -= 1
        pos += 1
    return source[start:pos]


def _run_node(script: str) -> None:
    completed = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_compaction_digest_and_preview_drop_instruction_envelope() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in ("_compactionDigestText", "_compactionCardPreview")
    )
    envelope = (
        "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
        "into the summary below. This is a handoff from a previous context window."
    )
    digest = (
        '## Historical Task Snapshot\nUser asked: "fix the planning bug"\n'
        "## Goal\nFix the MES planning defect.\n"
        "## Active State\n- worktree ready\n"
        "## Détail complet\nRésumé intégral : lire la note "
        "/opt/obsidian/vault/MES/Hermes/Compactions/compaction-test.md"
    )
    marker = envelope + "\n" + digest + "\n--- END OF CONTEXT SUMMARY ---"
    script = f"""
const assert = require('assert');
{functions}
const marker = {json.dumps(marker)};
const digest = _compactionDigestText(marker);
assert.ok(digest.startsWith('## Historical Task Snapshot'));
assert.ok(!digest.includes('REFERENCE ONLY'));
assert.ok(digest.includes('compaction-test.md'));
const preview = _compactionCardPreview(marker);
assert.ok(preview.includes('fix the planning bug'));
assert.ok(!/REFERENCE ONLY|compacted into the summary/i.test(preview));
assert.ok(preview.length <= 220);
"""
    _run_node(script)


def test_loaded_compaction_marker_scan_finds_every_non_tool_marker() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    functions = "\n".join(
        _extract_function(source, name)
        for name in (
            "_compactionSummarySegment",
            "_isContextCompactionText",
            "_isContextCompactionMessage",
            "_loadedCompactionMarkerRawIdxs",
        )
    )
    marker = "[CONTEXT COMPACTION — REFERENCE ONLY]\n## Goal\nKeep the digest visible."
    messages = [
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": marker},
        {"role": "tool", "content": marker},
        {"role": "assistant", "content": "normal answer"},
        {"role": "assistant", "content": marker},
    ]
    script = f"""
const assert = require('assert');
const msgContent = (message) => message && typeof message.content === 'string' ? message.content : '';
{functions}
assert.deepStrictEqual(_loadedCompactionMarkerRawIdxs({json.dumps(messages)}), [1, 4]);
"""
    _run_node(script)


def test_compaction_placement_selection_covers_window_boundaries() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    helper = _extract_function(source, "_selectCompactionCardPlacements")
    script = f"""
const assert = require('assert');
{helper}
assert.deepStrictEqual(
  _selectCompactionCardPlacements([3, 11, 49, 50, 88], 50),
  {{
    inlineMarkers: [50, 88],
    latestPreWindowMarker: 49,
    taskOwner: {{kind: 'inline', rawIdx: 88}},
  }},
);
assert.deepStrictEqual(
  _selectCompactionCardPlacements([3, 11, 49], 50),
  {{
    inlineMarkers: [],
    latestPreWindowMarker: 49,
    taskOwner: {{kind: 'pre-window', rawIdx: 49}},
  }},
);
assert.deepStrictEqual(
  _selectCompactionCardPlacements([50, 88], 50),
  {{
    inlineMarkers: [50, 88],
    latestPreWindowMarker: null,
    taskOwner: {{kind: 'inline', rawIdx: 88}},
  }},
);
assert.deepStrictEqual(
  _selectCompactionCardPlacements([], 50),
  {{inlineMarkers: [], latestPreWindowMarker: null, taskOwner: null}},
);
"""
    _run_node(script)


def test_settled_reference_is_pinned_at_top_when_marker_is_outside_tail() -> None:
    source = UI_JS.read_text(encoding="utf-8")
    pin_helper = _extract_function(source, "_pinCompactionCardAtTop")
    helper = _extract_function(source, "_pinSettledCompressionReferenceAtTop")
    script = f"""
const assert = require('assert');
{pin_helper}
{helper}
const children = [];
const inner = {{ appendChild(node) {{ children.push(node); return node; }} }};
const card = {{ id: 'compaction-card' }};
assert.strictEqual(_pinSettledCompressionReferenceAtTop(inner, card, -1), true);
assert.deepStrictEqual(children, [card]);
assert.strictEqual(_pinSettledCompressionReferenceAtTop(inner, {{id:'inline'}}, 12), false);
assert.deepStrictEqual(children, [card]);
"""
    _run_node(script)
