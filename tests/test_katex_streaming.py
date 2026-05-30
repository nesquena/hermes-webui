"""Regression coverage for streaming KaTeX rendering (#2976)."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.find(marker)
    assert start >= 0, f"{name} not found"
    brace = src.find("{", start)
    assert brace >= 0, f"{name} body not found"
    depth = 1
    i = brace + 1
    while i < len(src) and depth:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name} body did not close"
    return src[start:i]


def test_streaming_katex_scheduler_marks_live_pass_as_streaming():
    """The live 150ms KaTeX debounce must identify streaming passes.

    Without the explicit streaming flag, renderKatexBlocks() cannot distinguish a
    live parser-owned <equation-block> that is still being filled from a settled
    DOM node, so it may mark partial math as data-rendered permanently.
    """
    fn = _extract_function(MESSAGES_JS, "_scheduleStreamingKatex")
    assert "renderKatexBlocks(assistantBody,{streaming:true})" in fn


def test_render_katex_blocks_skips_pending_streaming_equation_before_rendered_flag():
    """Streaming equation placeholders must be skipped before data-rendered.

    The guard has to run before `el.dataset.rendered='true'`; otherwise a long
    equation that is still receiving text becomes permanently ineligible for the
    final complete KaTeX render.
    """
    fn = _extract_function(UI_JS, "renderKatexBlocks")
    assert "function _isStreamingEquationPending" in UI_JS
    pending_idx = fn.find("_isStreamingEquationPending")
    rendered_idx = fn.find("el.dataset.rendered='true'")
    assert pending_idx != -1, "renderKatexBlocks must check pending streaming equations"
    assert rendered_idx != -1, "renderKatexBlocks must still set data-rendered when rendering"
    assert pending_idx < rendered_idx, "pending guard must run before data-rendered is set"


def test_final_katex_render_keeps_default_non_streaming_path():
    """Final renderKatexBlocks() calls must still render all math placeholders."""
    fn = _extract_function(UI_JS, "renderKatexBlocks")
    assert "const streaming=Boolean" in fn
    assert "if(streaming&&_isStreamingEquationPending" in fn
    done_fn = _extract_function(MESSAGES_JS, "_smdEndParser")
    assert "renderKatexBlocks" not in done_fn, "done rendering remains in done handler after parser_end"
