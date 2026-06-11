"""Tests for issue #484 — collapsible JSON/YAML tree viewer."""
import pytest


class TestTreeRenderer:
    """Fenced JSON/YAML blocks should get a tree view toggle."""

    def test_json_blocks_get_tree_wrapper(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "code-tree-wrap" in content
        assert "data-raw" in content
        assert "data-lang" in content

    def test_json_yaml_lang_detection(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "lang==='json'||lang==='yaml'" in content

    def test_initTreeViews_function_exists(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "function initTreeViews" in content

    def test_buildTreeDOM_function_exists(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "function _buildTreeDOM(val, depth)" in content

    def test_initTreeViews_called_in_post_render(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "requestAnimationFrame(()=>postProcessRenderedMessages(inner))" in content
        start = content.find("function postProcessRenderedMessages")
        body = content[start:start + 500]
        assert "initTreeViews(container)" in body

    def test_tree_handles_all_value_types(self):
        """_buildTreeDOM should handle null, boolean, number, string, array, object."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        for cls in ("tree-null", "tree-bool", "tree-num", "tree-str", "tree-array", "tree-object"):
            assert cls in content, f"Missing type class: {cls}"

    def test_tree_collapse_support(self):
        """Tree nodes should be collapsible with collapsed/expanded states."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "tree-collapsed" in content
        assert "tree-collapsible" in content
        assert "classList.toggle" in content

    def test_tree_depth_auto_collapse(self):
        """Nested levels beyond depth 2 should be collapsed by default."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "depth>=2" in content

    def test_toggle_button_uses_i18n(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "t('raw_view')" in content
        assert "t('tree_view')" in content

    def test_yaml_support_via_jsyaml(self):
        """YAML should be parsed via jsyaml if available."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "jsyaml" in content

    def test_short_json_defaults_to_raw(self):
        """Blocks under 10 lines should default to raw view."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "lineCount>=10" in content


class TestTreeCSS:
    """CSS classes for tree viewer."""

    def test_tree_css_classes_exist(self):
        with open("static/style.css", "r", encoding="utf-8") as f:
            content = f.read()
        for cls in (".code-tree-wrap", ".tree-view", ".tree-hidden", ".tree-toggle-btn",
                    ".tree-node", ".tree-collapsible", ".tree-children", ".tree-collapsed",
                    ".tree-key", ".tree-str", ".tree-num", ".tree-bool", ".tree-null",
                    ".tree-comma", ".tree-item"):
            assert cls in content, f"Missing CSS: {cls}"

    def test_tree_colors_match_types(self):
        with open("static/style.css", "r", encoding="utf-8") as f:
            content = f.read()
        # Green strings, blue numbers, amber booleans
        assert "#4ade80" in content  # tree-str green
        assert "#60a5fa" in content  # tree-key/tree-num blue
        assert "#fbbf24" in content  # tree-bool amber


class TestTreeI18n:
    def test_i18n_keys_present(self):
        with open("static/i18n.js", "r", encoding="utf-8") as f:
            content = f.read()
        for key in ("tree_view", "raw_view"):
            count = content.count(key)
            assert count >= 7, f"{key} found {count} times, expected >= 7"
