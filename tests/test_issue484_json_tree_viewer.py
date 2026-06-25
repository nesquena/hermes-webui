"""Tests for issue #484 — collapsible JSON/YAML tree viewer.

The original feature hardcoded the default view: valid JSON/YAML fenced blocks
opened in Tree view at 10+ lines (`const showTree=lineCount>=10;`). That default
is now user-configurable (#484 follow-up) via two settings:

  - structured_code_default_view: "auto" | "on" | "off"
        on   => always default to Tree
        off  => always default to Raw
        auto => Tree only when the block line count >= the configured threshold
  - structured_code_auto_tree_lines: integer 1..1000 (default 10)

`auto` + threshold 10 reproduces the original behavior, so the default is
preserved. These tests pin the new configurable shape while keeping the existing
Tree/Raw renderer invariants (wrapper class, helpers, value types, toggle, YAML
support) intact.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")


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


class TestConfigurableDefaultView:
    """The default Tree/Raw decision is configurable, not hardcoded (#484 follow-up)."""

    def test_hardcoded_threshold_is_gone(self):
        """The original `lineCount>=10` hardcode must no longer drive the default."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "const showTree=lineCount>=10;" not in content, (
            "The hardcoded `const showTree=lineCount>=10;` must be replaced by the "
            "configurable decision helper."
        )
        assert "lineCount>=10" not in content, (
            "No hardcoded `lineCount>=10` comparison should remain; the threshold "
            "is configurable and the default is read from a sanitized helper."
        )

    def test_decision_helper_exists(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "function _structuredCodeShowTree(mode,threshold,lineCount)" in content
        assert "function _structuredCodeMode(" in content
        assert "function _structuredCodeThreshold(" in content

    def test_all_three_modes_represented(self):
        """auto / on / off must all be handled in the renderer."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        start = content.find("function _structuredCodeShowTree")
        body = content[start:start + 400]
        assert "'on'" in body, "mode 'on' (always Tree) must be handled"
        assert "'off'" in body, "mode 'off' (always Raw) must be handled"
        # 'auto' is the fallthrough; the mode reader sanitizes to it.
        mode_start = content.find("function _structuredCodeMode")
        mode_body = content[mode_start:mode_start + 300]
        assert "'auto'" in mode_body, "mode 'auto' must be the sanitized fallback"

    def test_default_threshold_fallback_is_10(self):
        """An invalid/missing threshold must fall back to 10 (original behavior)."""
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        thr_start = content.find("function _structuredCodeThreshold")
        thr_body = content[thr_start:thr_start + 300]
        assert ":10" in thr_body or "?10" in thr_body or " 10" in thr_body, (
            "_structuredCodeThreshold must fall back to 10 for invalid/missing values"
        )
        # Sanitization bounds (1..1000) keep the value safe.
        assert "1000" in thr_body, "threshold should be clamped to an upper bound"

    def test_renderer_uses_helper_for_decision(self):
        with open("static/ui.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "_structuredCodeShowTree(_structuredCodeMode(),_structuredCodeThreshold(),lineCount)" in content

    @pytest.mark.skipif(NODE is None, reason="node not on PATH")
    def test_decision_helper_behaviour_via_node(self, tmp_path):
        """Evaluate the real _structuredCodeShowTree() against the prompt's cases.

          - off, lineCount 999            => False
          - on, lineCount 1               => True
          - auto, threshold 10, count 9   => False
          - auto, threshold 10, count 10  => True
          - auto, invalid threshold       => fallback 10 (count 10 => True, 9 => False)
        """
        src = UI_JS_PATH.read_text(encoding="utf-8")
        # Extract the brace-balanced body of _structuredCodeShowTree.
        marker = "function _structuredCodeShowTree("
        start = src.find(marker)
        assert start >= 0, "_structuredCodeShowTree not found"
        i = src.index("{", start)
        depth = 1
        i += 1
        while depth > 0 and i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        fn_src = src[start:i]

        driver = tmp_path / "driver.js"
        driver.write_text(
            fn_src
            + "\n"
            + r"""
const cases = [
  ['off', 5, 999, false],
  ['on', 5, 1, true],
  ['auto', 10, 9, false],
  ['auto', 10, 10, true],
  ['auto', 11, 10, false],
  // invalid threshold falls back to 10
  ['auto', NaN, 10, true],
  ['auto', NaN, 9, false],
  // out-of-range thresholds fall back to 10 inside the pure helper
  ['auto', 0, 10, true],
  ['auto', 0, 9, false],
  ['auto', 99999, 10, true],
  ['auto', 99999, 9, false],
  // unknown mode is treated as auto by the pure helper's fallthrough
  ['bogus', 10, 10, true],
  ['bogus', 10, 9, false],
];
let failures = [];
for (const [mode, thr, count, want] of cases) {
  const got = _structuredCodeShowTree(mode, thr, count);
  if (got !== want) failures.push(`${mode},${thr},${count} => ${got} (want ${want})`);
}
if (failures.length) { console.error('FAIL: ' + failures.join(' | ')); process.exit(1); }
console.log('OK');
""",
            encoding="utf-8",
        )
        result = subprocess.run(
            [NODE, str(driver)], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, (
            f"_structuredCodeShowTree behaviour mismatch: {result.stdout}{result.stderr}"
        )


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

    def test_structured_code_setting_i18n_keys_present(self):
        """The new settings labels/options/help text must exist in i18n."""
        with open("static/i18n.js", "r", encoding="utf-8") as f:
            content = f.read()
        for key in (
            "settings_label_structured_code",
            "settings_option_structured_code_auto",
            "settings_option_structured_code_on",
            "settings_option_structured_code_off",
            "settings_label_structured_code_auto_lines",
            "settings_desc_structured_code",
        ):
            assert f"{key}:" in content, f"Missing i18n key: {key}"


class TestStructuredCodeSettingsWiring:
    """The setting must be a real, persisted WebUI setting — not a localStorage hack."""

    def test_server_defaults_and_validation(self):
        with open("api/config.py", "r", encoding="utf-8") as f:
            content = f.read()
        # Defaults preserve current behavior: auto + threshold 10.
        assert '"structured_code_default_view": "auto"' in content
        assert '"structured_code_auto_tree_lines": 10' in content
        # Enum + range validation are registered.
        assert '"structured_code_default_view": {"auto", "on", "off"}' in content
        assert '"structured_code_auto_tree_lines": (1, 1000)' in content

    def test_settings_ui_controls_present(self):
        with open("static/index.html", "r", encoding="utf-8") as f:
            content = f.read()
        assert 'id="settingsStructuredCodeMode"' in content
        assert 'id="settingsStructuredCodeAutoLines"' in content

    def test_boot_initializes_runtime_globals(self):
        with open("static/boot.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "window._structuredCodeDefaultView" in content
        assert "window._structuredCodeAutoTreeLines" in content

    def test_panel_persists_setting(self):
        with open("static/panels.js", "r", encoding="utf-8") as f:
            content = f.read()
        assert "_structuredCodeViewFromUi" in content
        assert "structured_code_default_view" in content
        assert "structured_code_auto_tree_lines" in content
