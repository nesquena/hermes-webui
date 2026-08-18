"""Regression coverage for mixed RTL/LTR message direction handling.

The review on PR #6560 called out that message direction must be applied at the
prose/container level, not by isolating every inline emphasis/link, and that
ordinary Markdown tables need explicit LTR treatment as machine-oriented
content. These tests pin the UI source contract so the helper cannot silently
lose the relevant selectors or call sites.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_source(name: str) -> str:
    start = UI_JS.find(f"function {name}(")
    assert start != -1, f"{name} not found in static/ui.js"
    brace = UI_JS.find("{", start)
    assert brace != -1, f"opening brace not found for {name}"
    depth = 1
    i = brace + 1
    while depth and i < len(UI_JS):
        if UI_JS[i] == "{":
            depth += 1
        elif UI_JS[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"unterminated function {name}"
    return UI_JS[start:i]


class TestMixedDirectionHelper:
    def test_helper_exists_and_handles_blocks_tables_and_machine_content(self):
        src = _function_source("_applyAutomaticMessageDirections")
        assert "body.setAttribute('dir','auto');" in src
        assert "const blockSelector='p,li,blockquote,h1,h2,h3,h4,h5,h6,ul,ol,table,thead,tbody,tfoot,tr,th,td';" in src
        assert "const machineSelector=[" in src
        assert "'.csv-table-wrap','.csv-table'" in src
        assert "'table,thead,tbody,tfoot,tr,th,td'" in src or "table,thead,tbody,tfoot,tr,th,td" in src
        assert "a,strong,em" not in src
        assert "MutationObserver" in src
        assert "record.addedNodes" in src
        assert "_pruneAutomaticMessageDirectionObservers();" in src

    def test_helper_is_invoked_from_render_and_postprocess_paths(self):
        render_src = _function_source("renderMessages")
        post_src = _function_source("postProcessRenderedMessages")
        assert "_applyAutomaticMessageDirections(inner);" in render_src
        assert "_applyAutomaticMessageDirections(container);" in post_src

    def test_observer_cleanup_helper_exists(self):
        src = _function_source("_disconnectAutomaticMessageDirections")
        assert "_automaticMessageDirectionObservers.get(body)" in src
        assert "observer.disconnect()" in src
