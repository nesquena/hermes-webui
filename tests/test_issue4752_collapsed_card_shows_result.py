"""Regression tests for issue #4752 — collapsed tool card shows call result, not args.

Extracts _toolResultOneLiner, _toolCardPreviewText, and _formatToolArgPreview from
static/ui.js by line range and runs them via Node.js to verify the fix.

Before the fix, _toolCardPreviewText always fell through to argPreview for completed
tools, so 'Found 3 matches' would show as 'path=src/' in the collapsed header.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

UI_JS = os.path.join(os.path.dirname(__file__), '..', 'static', 'ui.js')


def _extract_js_block():
    """Extract the tool-card preview functions from ui.js by finding their definition range."""
    lines = open(UI_JS, encoding='utf-8').readlines()
    src = "".join(lines)

    # Find start of the earliest function we need
    fn_names = [
        "_toolArgPreviewValue",
        "_toolArgPreviewKeyIsHidden",
        "_formatToolArgPreview",
        "_toolResultOneLiner",
        "_toolCardPreviewText",
    ]

    # Find start line of _toolArgPreviewValue (the earliest dependency)
    start_line = None
    for i, line in enumerate(lines):
        if re.search(r'^function _toolArgPreviewValue\(', line):
            start_line = i
            break
    if start_line is None:
        raise AssertionError("_toolArgPreviewValue not found in ui.js")

    # Find end line of _toolCardPreviewText (the last function we need)
    # It ends at the function _toolCardAllowsDetail that follows it
    end_line = None
    for i in range(start_line, len(lines)):
        if re.search(r'^function _toolCardAllowsDetail\(', lines[i]):
            end_line = i
            break
    if end_line is None:
        raise AssertionError("_toolCardAllowsDetail (end sentinel) not found in ui.js")

    return "".join(lines[start_line:end_line])


def _run_js(js_body, *args):
    """Run a Node.js snippet with optional JSON arguments, return stdout."""
    fn_defs = _extract_js_block()
    js_code = fn_defs + "\n" + js_body
    tf = tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8')
    tf.write(js_code)
    tf.close()
    try:
        result = subprocess.run(
            [NODE, tf.name] + [json.dumps(a) for a in args],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"node error: {result.stderr}")
        return result.stdout
    finally:
        os.unlink(tf.name)


def _preview_text(tc):
    """Call _toolCardPreviewText(tc) via Node.js."""
    return _run_js(
        "var tc=JSON.parse(process.argv[2]);\n"
        "process.stdout.write(String(_toolCardPreviewText(tc)));\n",
        tc
    )


def _result_one_liner(preview):
    """Call _toolResultOneLiner(preview) via Node.js."""
    return _run_js(
        "var p=JSON.parse(process.argv[2]);\n"
        "process.stdout.write(String(_toolResultOneLiner(p)));\n",
        preview
    )


class TestToolResultOneLiner:
    """Unit tests for the _toolResultOneLiner helper."""

    def test_empty_string_returns_empty(self):
        assert _result_one_liner("") == ""

    def test_plain_text_returned(self):
        assert _result_one_liner("Found 3 matches") == "Found 3 matches"

    def test_json_object_suppressed(self):
        assert _result_one_liner('{"key":"val"}') == ""

    def test_json_array_suppressed(self):
        assert _result_one_liner('[1,2,3]') == ""

    def test_multiline_collapses_to_first_nonempty_line(self):
        assert _result_one_liner("Line 1\nLine 2\nLine 3") == "Line 1"

    def test_leading_blank_lines_skipped(self):
        assert _result_one_liner("\n\nActual result") == "Actual result"

    def test_truncation_at_180_chars(self):
        long_line = "x" * 200
        result = _result_one_liner(long_line)
        assert result.endswith("…")
        # 177 ASCII chars + 1 ellipsis character = 178 chars total
        assert len(result) == 178

    def test_short_line_not_truncated(self):
        line = "x" * 180
        result = _result_one_liner(line)
        assert len(result) == 180
        assert not result.endswith("…")


class TestToolCardPreviewText:
    """Integration tests for the updated _toolCardPreviewText function."""

    def test_result_wins_on_completion(self):
        """Completed tool with both preview and args returns the preview result."""
        tc = {"done": True, "preview": "Found 3 matches", "args": {"path": "src/"}}
        assert _preview_text(tc) == "Found 3 matches"

    def test_args_fallback_when_no_preview(self):
        """Completed tool without preview falls back to arg preview."""
        tc = {"done": True, "preview": "", "args": {"path": "src/"}}
        result = _preview_text(tc)
        assert "path" in result
        assert "src" in result

    def test_json_preview_suppressed_falls_through_to_args(self):
        """JSON body in preview is suppressed; falls back to arg preview."""
        tc = {"done": True, "preview": '{"key":"val"}', "args": {"path": "src/"}}
        result = _preview_text(tc)
        # JSON suppressed, so falls through to arg preview
        assert result != '{"key":"val"}'
        assert "path" in result or "src" in result

    def test_array_preview_suppressed_falls_through_to_args(self):
        """Array body in preview is suppressed; falls back to arg preview."""
        tc = {"done": True, "preview": "[1,2,3]", "args": {"path": "src/"}}
        result = _preview_text(tc)
        assert result != "[1,2,3]"
        assert "path" in result or "src" in result

    def test_multiline_preview_collapses_to_first_line(self):
        """Multiline preview collapses to first non-empty line."""
        tc = {"done": True, "preview": "Line 1\nLine 2\nLine 3", "args": {}}
        assert _preview_text(tc) == "Line 1"

    def test_running_preview_unchanged(self):
        """Running tool (done===false) returns the explicit preview unchanged."""
        tc = {"done": False, "preview": "Searching...", "args": {"path": "src/"}}
        assert _preview_text(tc) == "Searching..."

    def test_long_preview_truncated(self):
        """Single-line preview >180 chars is truncated with ellipsis."""
        long_preview = "x" * 200
        tc = {"done": True, "preview": long_preview, "args": {}}
        result = _preview_text(tc)
        assert result.endswith("…")
        assert len(result) == 178  # 177 chars + ellipsis

    def test_no_args_no_preview_returns_completed(self):
        """Completed tool with no args and no preview returns 'Completed'."""
        tc = {"done": True, "preview": "", "args": {}}
        assert _preview_text(tc) == "Completed"

    def test_error_state_returns_failed(self):
        """Error tool with no preview or args returns 'Failed'."""
        tc = {"done": True, "is_error": True, "preview": "", "args": {}}
        assert _preview_text(tc) == "Failed"

    def test_running_no_preview_returns_running(self):
        """Running tool with no preview returns 'Running'."""
        tc = {"done": False, "preview": "", "args": {}}
        assert _preview_text(tc) == "Running"

    def test_snippet_fallback_on_cold_load(self):
        """Cold-loaded tool with result in snippet (no preview) uses snippet."""
        tc = {"done": True, "preview": "", "snippet": "Found 3 matches", "args": {"path": "src/"}}
        assert _preview_text(tc) == "Found 3 matches"

    def test_error_with_text_snippet_shows_message(self):
        """Error tool with a plain-text snippet shows the error message, not 'Failed'."""
        tc = {"done": True, "is_error": True, "preview": "", "snippet": "Permission denied", "args": {}}
        assert _preview_text(tc) == "Permission denied"

    def test_snippet_json_suppressed(self):
        """Cold-loaded tool with JSON in snippet falls through to args."""
        tc = {"done": True, "preview": "", "snippet": '{"key":"val"}', "args": {"path": "src/"}}
        result = _preview_text(tc)
        assert result != '{"key":"val"}'
        assert "path" in result or "src" in result
