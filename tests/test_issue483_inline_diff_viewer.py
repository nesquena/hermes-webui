"""Tests for issue #483 — inline diff/patch viewer."""
import pytest


class TestFencedDiffRenderer:
    """Fenced ```diff blocks should render with colored line spans."""

    def test_diff_block_has_diff_block_class(self):
        """diff blocks should get a 'diff-block' class on <pre>."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "diff-block" in content, "Missing diff-block class"
        # Should be in the fenced block renderer
        assert "pre class=\"diff-block\"" in content

    def test_diff_lines_get_span_classes(self):
        """Each diff line should be wrapped in a span with appropriate class."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "diff-line diff-plus" in content
        assert "diff-line diff-minus" in content
        assert "diff-line diff-hunk" in content

    def test_diff_lang_detection(self):
        """Both 'diff' and 'patch' language hints should trigger diff rendering."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "lang==='diff'||lang==='patch'" in content

    def test_diff_line_escape(self):
        """Diff lines must be HTML-escaped (using esc() function)."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        # In the fenced diff block renderer, lines should be escaped
        # Check the pattern: esc(code...).split('\\n').map
        assert "esc(code.replace" in content


class TestMediaDiffInline:
    """MEDIA: .patch/.diff files should render inline instead of download."""

    def test_patch_extension_detected(self):
        """.patch and .diff extensions should trigger inline rendering."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "\\.(patch|diff)$" in content

    def test_diff_inline_load_placeholder(self):
        """Should emit a placeholder div while loading."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "diff-inline-load" in content
        assert "data-path" in content

    def test_loadDiffInline_function_exists(self):
        """loadDiffInline() function should be defined."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "function loadDiffInline()" in content

    def test_loadDiffInline_called_in_post_render(self):
        """loadDiffInline() should be called in post-render (after addCopyButtons)."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        count = content.count("loadDiffInline()")
        assert count >= 2, f"loadDiffInline() called {count} times, expected >= 2 (cached + fresh render)"

    def test_diff_inline_error_class(self):
        """Should have error state class."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "diff-inline-error" in content


class TestDiffCSS:
    """CSS classes for diff coloring."""

    def test_diff_css_classes_exist(self):
        with open("static/style.css", "r", encoding="utf-8") as f:
            content = f.read()
        for cls in (".diff-block", ".diff-line", ".diff-plus", ".diff-minus",
                    ".diff-hunk", ".diff-inline-load", ".diff-inline", ".diff-inline-error"):
            assert cls in content, f"Missing CSS class: {cls}"

    def test_diff_colors_are_present(self):
        """Green for plus, red for minus should use rgba colors."""
        with open("static/style.css", "r", encoding="utf-8") as f:
            content = f.read()
        assert "rgba(34,197,94" in content or "#22c55e" in content, "Missing green color for diff-plus"
        assert "rgba(239,68,68" in content or "#ef4444" in content, "Missing red color for diff-minus"


class TestDiffI18n:
    """i18n keys for diff viewer."""

    def test_diff_loading_key_in_all_locales(self):
        with open("static/i18n.js", "r", encoding="utf-8") as f:
            content = f.read()
        count = content.count("diff_loading")
        assert count >= 8, f"diff_loading found {count} times, expected >= 8 (one per locale)"
