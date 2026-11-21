"""#8748 re-gate — Unicode delimiter spans + mixed-content purity marker.

Regression tests for:
1. Unicode delimiter spans in _compaction_carrier_envelope_decision and
   compaction_handoff_remainder (offsets from untouched text).
2. Purity display marker on mixed structured content when authentic text survives
   in any supported form (bare string, typeless dict, typed text).
3. Browser mirror _compactionSummarySegment in static/ui.js using regex search
   directly on the original string.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import api.routes as routes
from api.compression_anchor import (
    PURE_CARRIER_DISPLAY_FLAG,
    _CARRIER_AUTHENTIC_REMAINDER,
    _CARRIER_NOT_A_CARRIER,
    _CARRIER_PROVEN_SYNTHETIC,
    _compaction_carrier_envelope_decision,
    compaction_handoff_remainder,
    project_compaction_carrier_for_display,
)

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

HEADER = "[PRIOR CONTEXT]"
DELIMITER = "[end of prior context]"
SUMMARY = "[context compaction folded summary text]"
PURE_CARRIER_BLOCK = f"{HEADER}\n\n{DELIMITER}\n\n{SUMMARY}"


def _extract_fn(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.find(marker)
    assert start >= 0, f"{name} not found in source"
    body_start = source.find("{", start)
    depth = 0
    for idx in range(body_start, len(source)):
        c = source[idx]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"unbalanced braces for {name}")


def _eval_node_script(script: str):
    assert NODE is not None, "node executable is required"
    proc = subprocess.run(
        [NODE],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


# ── 1. _compaction_carrier_envelope_decision on single U+0130 (İ) ─────────────


def test_compaction_carrier_envelope_decision_unicode_single_i():
    text = f"{HEADER}\nİstanbul retained answer\n\n{DELIMITER}\n{SUMMARY}"
    decision, remainder = _compaction_carrier_envelope_decision(text)
    assert decision == _CARRIER_AUTHENTIC_REMAINDER
    assert remainder == "İstanbul retained answer"
    assert not remainder.endswith("[")


# ── 2. _compaction_carrier_envelope_decision on 64 U+0130 (İ) characters ──────


def test_compaction_carrier_envelope_decision_unicode_64_i():
    authentic_head = ("İ" * 64) + " retained answer"
    text = f"{HEADER}\n{authentic_head}\n\n{DELIMITER}\n{SUMMARY}"
    decision, remainder = _compaction_carrier_envelope_decision(text)
    assert decision == _CARRIER_AUTHENTIC_REMAINDER
    assert remainder == authentic_head
    assert len(remainder) == len(authentic_head)
    assert remainder.endswith("retained answer")
    assert "[" not in remainder
    assert "summary text" not in remainder


# ── 3. ASCII control ───────────────────────────────────────────────────────────


def test_compaction_carrier_envelope_decision_ascii_control():
    text = f"{HEADER}\nIstanbul retained answer\n\n{DELIMITER}\n{SUMMARY}"
    decision, remainder = _compaction_carrier_envelope_decision(text)
    assert decision == _CARRIER_AUTHENTIC_REMAINDER
    assert remainder == "Istanbul retained answer"

    not_carrier_decision, _ = _compaction_carrier_envelope_decision("plain authored message")
    assert not_carrier_decision == _CARRIER_NOT_A_CARRIER


# ── 4. compaction_handoff_remainder on İ carrier ──────────────────────────────


def test_compaction_handoff_remainder_unicode():
    single_i_text = f"{HEADER}\nİstanbul retained answer\n\n{DELIMITER}\n{SUMMARY}"
    msg_1 = {
        "role": "assistant",
        "content": single_i_text,
        "_compressed_summary": True,
    }
    remainder_1 = compaction_handoff_remainder(msg_1)
    assert remainder_1 == "İstanbul retained answer"

    head_64 = ("İ" * 64) + " retained answer"
    msg_64 = {
        "role": "assistant",
        "content": f"{HEADER}\n{head_64}\n\n{DELIMITER}\n{SUMMARY}",
        "_compressed_summary": True,
    }
    remainder_64 = compaction_handoff_remainder(msg_64)
    assert remainder_64 == head_64


# ── 5. project_compaction_carrier_for_display on string-content İ carrier ─────


def test_project_compaction_carrier_for_display_unicode_string():
    single_i_text = f"{HEADER}\nİstanbul retained answer\n\n{DELIMITER}\n{SUMMARY}"
    msg_1 = {
        "role": "assistant",
        "content": single_i_text,
        "_compressed_summary": True,
    }
    projected_1 = project_compaction_carrier_for_display(msg_1)
    assert projected_1["content"] == "İstanbul retained answer"
    assert "_compressed_summary" not in projected_1
    assert PURE_CARRIER_DISPLAY_FLAG not in projected_1

    head_64 = ("İ" * 64) + " retained answer"
    msg_64 = {
        "role": "assistant",
        "content": f"{HEADER}\n{head_64}\n\n{DELIMITER}\n{SUMMARY}",
        "_compressed_summary": True,
    }
    projected_64 = project_compaction_carrier_for_display(msg_64)
    assert projected_64["content"] == head_64
    assert "_compressed_summary" not in projected_64
    assert PURE_CARRIER_DISPLAY_FLAG not in projected_64


# ── 6. Mixed structured content: bare-str neighbour ───────────────────────────


def test_mixed_structured_content_bare_str_neighbour():
    content = [
        {"type": "text", "text": PURE_CARRIER_BLOCK},
        "authentic visible text",
    ]
    msg = {
        "role": "assistant",
        "content": content,
        "_compressed_summary": True,
    }
    projected = project_compaction_carrier_for_display(msg)
    assert "authentic visible text" in projected["content"]
    assert PURE_CARRIER_DISPLAY_FLAG not in projected
    assert routes._message_counts_as_renderable_for_window(projected) is True


# ── 7. Mixed structured content: typeless dict neighbour ──────────────────────


def test_mixed_structured_content_typeless_dict_neighbour():
    content_text_key = [
        {"type": "text", "text": PURE_CARRIER_BLOCK},
        {"text": "authentic visible text"},
    ]
    msg_text_key = {
        "role": "assistant",
        "content": content_text_key,
        "_compressed_summary": True,
    }
    projected_text_key = project_compaction_carrier_for_display(msg_text_key)
    assert PURE_CARRIER_DISPLAY_FLAG not in projected_text_key
    assert routes._message_counts_as_renderable_for_window(projected_text_key) is True

    content_content_key = [
        {"type": "text", "text": PURE_CARRIER_BLOCK},
        {"content": "authentic visible text via content key"},
    ]
    msg_content_key = {
        "role": "assistant",
        "content": content_content_key,
        "_compressed_summary": True,
    }
    projected_content_key = project_compaction_carrier_for_display(msg_content_key)
    assert PURE_CARRIER_DISPLAY_FLAG not in projected_content_key
    assert routes._message_counts_as_renderable_for_window(projected_content_key) is True


# ── 8. Control: typed-text neighbour ──────────────────────────────────────────


def test_control_typed_text_neighbour():
    content = [
        {"type": "text", "text": PURE_CARRIER_BLOCK},
        {"type": "text", "text": "authentic visible text"},
    ]
    msg = {
        "role": "assistant",
        "content": content,
        "_compressed_summary": True,
    }
    projected = project_compaction_carrier_for_display(msg)
    assert PURE_CARRIER_DISPLAY_FLAG not in projected
    assert routes._message_counts_as_renderable_for_window(projected) is True


# ── 9. Control: carrier-only row ──────────────────────────────────────────────


def test_control_carrier_only_row():
    synthetic_decision, _ = _compaction_carrier_envelope_decision(PURE_CARRIER_BLOCK)
    assert synthetic_decision == _CARRIER_PROVEN_SYNTHETIC

    # Structured carrier-only
    msg_list = {
        "role": "assistant",
        "content": [{"type": "text", "text": PURE_CARRIER_BLOCK}],
        "_compressed_summary": True,
    }
    projected_list = project_compaction_carrier_for_display(msg_list)
    assert projected_list.get(PURE_CARRIER_DISPLAY_FLAG) is True
    assert routes._message_counts_as_renderable_for_window(projected_list) is False

    # String carrier-only
    msg_str = {
        "role": "assistant",
        "content": PURE_CARRIER_BLOCK,
        "_compressed_summary": True,
    }
    projected_str = project_compaction_carrier_for_display(msg_str)
    assert projected_str.get(PURE_CARRIER_DISPLAY_FLAG) is True
    assert routes._message_counts_as_renderable_for_window(projected_str) is False


# ── 10. Control: merged carrier with authentic remainder ──────────────────────


def test_control_merged_carrier_with_authentic_remainder():
    merged_block = f"{HEADER}\nAuthentic retained reply\n\n{DELIMITER}\n{SUMMARY}"

    # Structured merged carrier
    msg_list = {
        "role": "assistant",
        "content": [{"type": "text", "text": merged_block}],
        "_compressed_summary": True,
    }
    projected_list = project_compaction_carrier_for_display(msg_list)
    assert PURE_CARRIER_DISPLAY_FLAG not in projected_list
    assert routes._message_counts_as_renderable_for_window(projected_list) is True

    # String merged carrier
    msg_str = {
        "role": "assistant",
        "content": merged_block,
        "_compressed_summary": True,
    }
    projected_str = project_compaction_carrier_for_display(msg_str)
    assert PURE_CARRIER_DISPLAY_FLAG not in projected_str
    assert routes._message_counts_as_renderable_for_window(projected_str) is True


# ── 11. Browser mirror: _compactionSummarySegment in node ─────────────────────


def test_browser_mirror_compaction_summary_segment_unicode():
    source = UI_JS.read_text(encoding="utf-8")
    fn_code = _extract_fn(source, "_compactionSummarySegment")

    single_i = f"{HEADER}\nİstanbul retained answer\n\n{DELIMITER}\n{SUMMARY}"
    sixty_four_i = f"{HEADER}\n" + ("İ" * 64) + f" retained answer\n\n{DELIMITER}\n{SUMMARY}"
    ascii_text = f"{HEADER}\nIstanbul retained answer\n\n{DELIMITER}\n{SUMMARY}"

    script = f"""
{fn_code}

const inputs = {{
  single: {json.dumps(single_i)},
  sixty_four: {json.dumps(sixty_four_i)},
  ascii: {json.dumps(ascii_text)},
}};

const results = {{
  single: _compactionSummarySegment(inputs.single),
  sixty_four: _compactionSummarySegment(inputs.sixty_four),
  ascii: _compactionSummarySegment(inputs.ascii),
}};

console.log(JSON.stringify(results));
"""
    results = _eval_node_script(script)

    assert results["single"] == SUMMARY
    assert results["single"].startswith("[context compaction")
    assert DELIMITER not in results["single"]

    assert results["sixty_four"] == SUMMARY
    assert results["sixty_four"].startswith("[context compaction")
    assert DELIMITER not in results["sixty_four"]

    assert results["ascii"] == SUMMARY
    assert results["ascii"].startswith("[context compaction")
    assert DELIMITER not in results["ascii"]
