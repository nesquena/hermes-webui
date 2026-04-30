"""Static UI tests for quieter tool-call rendering and shared design tokens.

These tests intentionally follow the repo's existing pytest style: read static
source files, isolate the relevant function/rule, and assert implementation
invariants before changing the UI.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    assert match, f"{name}() not found"
    brace = src.find("{", match.end())
    assert brace != -1, f"{name}() has no body"
    depth = 1
    i = brace + 1
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    while i < len(src) and depth:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < len(src) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in "'\"`":
            in_string = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"{name}() body did not close"
    return src[brace + 1:i - 1]


class TestToolCallGroupingStatic:
    def test_simplified_tool_calling_setting_is_wired_through_frontend(self):
        assert "settingsSimplifiedToolCalling" in (REPO / "static" / "index.html").read_text(encoding="utf-8"), (
            "Settings should expose a Compact tool activity checkbox."
        )
        assert "window._simplifiedToolCalling" in (REPO / "static" / "boot.js").read_text(encoding="utf-8"), (
            "Boot should hydrate simplified_tool_calling into a runtime flag."
        )
        panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
        assert "settingsSimplifiedToolCalling" in panels and "simplified_tool_calling" in panels, (
            "Settings panel should load and save the simplified_tool_calling setting."
        )

    def test_render_messages_gates_settled_activity_grouping(self):
        fn = _function_body(UI_JS, "renderMessages")
        helper = _function_body(UI_JS, "ensureActivityGroup")
        assert "isSimplifiedToolCalling()" in fn, (
            "Settled tool/thinking grouping should be gated by the Compact tool activity toggle."
        )
        assert "tool-cards-toggle" in fn, (
            "The non-simplified path should preserve the upstream loose tool-card controls."
        )
        assert "data-tool-call-group" in helper, (
            "Tool-call groups need a stable data-tool-call-group attribute for CSS and tests."
        )
        assert re.search(r"cards\.length|toolCount|toolCalls\.length|group\.length", fn + helper), (
            "The simplified group header should derive its summary/count from the number of tool calls."
        )

    def test_tool_call_groups_default_collapsed_with_summary_visible(self):
        fn = _function_body(UI_JS, "renderMessages")
        helper = _function_body(UI_JS, "ensureActivityGroup")
        assert "tool-call-group-collapsed" in fn or "collapsed" in fn, (
            "Historical tool-call groups should default to a collapsed state."
        )
        assert "tool-call-group-summary" in helper, (
            "Collapsed groups must expose a visible summary/header row."
        )
        assert "tool-call-group-body" in helper, (
            "Tool-card detail rows should live inside a group body that can be "
            "expanded/collapsed."
        )
        assert "aria-expanded" in helper, (
            "The expand/collapse control must expose aria-expanded."
        )

    def test_live_tool_cards_use_grouping_only_when_simplified(self):
        live_fn = _function_body(UI_JS, "appendLiveToolCard")
        settled_fn = _function_body(UI_JS, "renderMessages")
        assert "isSimplifiedToolCalling()" in live_fn, (
            "Live streaming tool cards should branch on the Compact tool activity toggle."
        )
        assert "ensureActivityGroup" in live_fn, (
            "Compact live tool rendering should use the grouped activity container."
        )
        assert "toolRunningRow" in live_fn, (
            "The non-simplified live tool path should preserve the upstream running-dots row."
        )
        assert "buildToolCard" in live_fn and "buildToolCard" in settled_fn, (
            "Live and settled tool rendering should share buildToolCard() for consistent markup."
        )
        assert "data-live-tid" in live_fn, (
            "Live grouping must preserve data-live-tid so tool_start/tool_complete updates still replace the correct card."
        )

    def test_tools_and_thinking_share_one_collapsed_activity_dropdown(self):
        ui_min = re.sub(r"\s+", "", UI_JS)
        assert "functionensureActivityGroup(" in ui_min, (
            "Tool calls and thinking should share one agent-activity disclosure helper."
        )
        assert "data-agent-activity-group" in UI_JS, (
            "The shared tools/thinking disclosure needs a stable data-agent-activity-group hook."
        )
        assert "agent-activity-thinking" in UI_JS, (
            "Thinking content should be nested inside the shared activity dropdown, not rendered separately."
        )
        render_fn = _function_body(UI_JS, "renderMessages")
        assert "isSimplifiedToolCalling()" in render_fn and "assistantThinking.set(rawIdx, thinkingText)" in render_fn, (
            "Settled thinking should move into the shared activity dropdown only when Compact tool activity is enabled."
        )
        assert "seg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText))" in render_fn, (
            "The non-simplified path should preserve standalone settled thinking cards."
        )

    def test_live_thinking_uses_shared_activity_dropdown_only_when_simplified(self):
        live_thinking_fn = _function_body(UI_JS, "appendThinking")
        assert "isSimplifiedToolCalling()" in live_thinking_fn, (
            "Live thinking should branch on the Compact tool activity toggle."
        )
        assert "ensureActivityGroup" in live_thinking_fn, (
            "Compact live thinking should be inserted into the shared activity dropdown."
        )
        assert "thinkingRow" in live_thinking_fn, (
            "The non-simplified live thinking path should preserve the upstream #thinkingRow card."
        )


class TestToolCardDesignTokens:
    def test_root_defines_shared_layout_design_tokens(self):
        for token in (
            "--radius-sm",
            "--radius-md",
            "--radius-card",
            "--space-1",
            "--space-2",
            "--space-3",
            "--font-size-xs",
            "--font-size-sm",
            "--surface-subtle",
            "--border-subtle",
        ):
            assert token in CSS, f"Missing design token {token} in style.css"

    def test_base_dark_palette_restores_upstream_gold_tokens(self):
        css_min = re.sub(r"\s+", "", CSS)
        expected_tokens = (
            "--bg:#0D0D1A",
            "--sidebar:#141425",
            "--border:#2A2A45",
            "--text:#FFF8DC",
            "--muted:#C0C0C0",
            "--accent:#FFD700",
            "--surface:#1A1A2E",
            "--topbar-bg:rgba(20,20,37,.98)",
        )
        for token in expected_tokens:
            assert token in css_min, f"Base dark palette token missing: {token}"

    def test_base_light_palette_restores_upstream_gold_tokens(self):
        css_min = re.sub(r"\s+", "", CSS)
        expected_tokens = (
            "--bg:#FEFCF7",
            "--sidebar:#FAF7F0",
            "--border:#E0D8C8",
            "--text:#1A1610",
            "--muted:#5C5344",
            "--accent:#B8860B",
            "--surface:#F3EEE3",
        )
        for token in expected_tokens:
            assert token in css_min, f"Base light palette token missing: {token}"

    def test_default_skin_preview_stays_upstream(self):
        boot_min = re.sub(r"\s+", "", BOOT_JS)
        assert "{name:'Default',colors:['#FFD700','#FFBF00','#CD7F32']}" in boot_min, (
            "The Default skin swatch should stay aligned with the upstream gold base."
        )

    def test_tool_card_css_uses_design_tokens_for_chrome(self):
        css_min = re.sub(r"\s+", "", CSS)
        assert ".tool-card{" in css_min, ".tool-card rule missing"
        assert "border-radius:var(--radius-card)" in css_min, (
            ".tool-card border radius should use --radius-card, not hardcoded px."
        )
        assert "background:var(--surface-subtle)" in css_min, (
            ".tool-card background should use --surface-subtle."
        )
        assert "border:1pxsolidvar(--border-subtle)" in css_min, (
            ".tool-card border should use --border-subtle."
        )

    def test_tool_card_header_and_text_use_spacing_and_font_tokens(self):
        css_min = re.sub(r"\s+", "", CSS)
        assert ".tool-card-header{" in css_min, ".tool-card-header rule missing"
        assert "gap:var(--space-2)" in css_min, (
            ".tool-card-header gap should use --space-2."
        )
        assert "padding:var(--space-1)var(--space-3)" in css_min, (
            ".tool-card-header padding should use spacing tokens."
        )
        assert ".tool-card-name{" in css_min and "font-size:var(--font-size-xs)" in css_min, (
            ".tool-card-name should use --font-size-xs."
        )
        assert ".tool-card-preview{" in css_min and "font-size:var(--font-size-xs)" in css_min, (
            ".tool-card-preview should use --font-size-xs."
        )
