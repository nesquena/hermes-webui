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
MESSAGES_JS = (REPO / "static" / "messages.js").read_text(encoding="utf-8")


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

    def test_simplified_tool_calling_autosave_hot_applies_renderer_mode(self):
        panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
        fn = _function_body(panels, "_autosavePreferencesSettings")
        assert "window._simplifiedToolCalling" in fn, (
            "Autosaving Compact tool activity should update the live renderer flag immediately."
        )
        assert "clearMessageRenderCache()" in fn, (
            "Autosaving Compact tool activity should invalidate cached transcript HTML."
        )
        assert "renderMessages()" in fn, (
            "Autosaving Compact tool activity should rebuild the visible transcript without a refresh."
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

    def test_activity_summary_omits_redundant_trailing_count_badge(self):
        helper = _function_body(UI_JS, "ensureActivityGroup")
        sync_fn = _function_body(UI_JS, "_syncToolCallGroupSummary")
        assert "tool-call-group-count" not in helper, (
            "Compact Activity summaries already state tool counts in the label; "
            "do not render a second trailing count badge."
        )
        assert "tool-call-group-count" not in sync_fn, (
            "The summary sync path should not update a hidden/removed trailing count badge."
        )

    def test_activity_summary_keeps_header_compact_without_tool_names_or_thinking_prefix(self):
        helper = _function_body(UI_JS, "ensureActivityGroup")
        sync_fn = _function_body(UI_JS, "_syncToolCallGroupSummary")
        assert "tool-call-group-list" not in helper, (
            "The compact Activity row should not allocate a secondary tool-name/thinking summary span."
        )
        assert "tool-call-group-list" not in sync_fn, (
            "The summary sync path should not populate a redundant tool-name/thinking list."
        )
        assert "Activity: thinking +" not in sync_fn, (
            "When tools are present, thinking is expected and should not be repeated in the label."
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

    def test_activity_disclosure_state_is_session_and_turn_scoped(self):
        helper = _function_body(UI_JS, "ensureActivityGroup")
        toggle_fn = _function_body(UI_JS, "_toggleActivityGroup")
        key_fn = _function_body(UI_JS, "_activityDisclosureStorageKey")
        render_fn = _function_body(UI_JS, "renderMessages")
        live_fn = _function_body(UI_JS, "appendLiveToolCard")
        thinking_fn = _function_body(UI_JS, "appendThinking")
        done_fn = (REPO / "static" / "messages.js").read_text(encoding="utf-8")
        assert "hermes-activity-disclosure:" in UI_JS, (
            "Activity disclosure state should use a dedicated localStorage namespace."
        )
        assert "S.session.session_id" in key_fn, (
            "Activity disclosure state must be scoped to the current chat/session."
        )
        assert "data-activity-disclosure-key" in helper, (
            "Each Activity group needs a stable per-turn key for persisted disclosure state."
        )
        assert "_readActivityDisclosureState" in helper, (
            "ensureActivityGroup() should hydrate the saved open/closed state before using defaults."
        )
        assert "_writeActivityDisclosureState" in toggle_fn, (
            "Clicking the Activity summary should persist the new open/closed state."
        )
        assert "assistant:" in render_fn, (
            "Settled Activity groups should be keyed by assistant message index."
        )
        assert "live:" in live_fn + thinking_fn, (
            "Live Activity groups should be keyed by active stream id."
        )
        assert "_copyActivityDisclosureState('live:'+streamId, 'assistant:'" in done_fn, (
            "When a live turn settles, its saved disclosure state should transfer to the persisted assistant turn."
        )

    def test_live_tool_activity_defaults_collapsed_unless_saved_open(self):
        live_fn = _function_body(UI_JS, "appendLiveToolCard")
        helper = _function_body(UI_JS, "ensureActivityGroup")
        assert "collapsed:false" not in re.sub(r"\s+", "", live_fn), (
            "Compact live tool activity should not force-open every time a chat is revisited."
        )
        assert "savedState==='open'" in helper or 'savedState==="open"' in helper, (
            "A previously-open Activity group should still restore open from persisted state."
        )

    def test_live_activity_summary_shows_readable_progress_without_persisted_content(self):
        sync_fn = _function_body(UI_JS, "_syncToolCallGroupSummary")
        progress_fn = _function_body(UI_JS, "_activityProgressLabelForToolName")
        live_progress_fn = _function_body(UI_JS, "_activityLiveProgressLabel")
        assert "_activityLiveProgressLabel" in sync_fn, (
            "Live compact Activity rows should expose a readable transient progress label."
        )
        assert "durationEl.textContent" in sync_fn and "filter(Boolean).join(' · ')" in sync_fn, (
            "Progress should share the existing non-persistent summary/duration slot, not become transcript text."
        )
        for label in ("Searching workspace", "Reading files", "Updating files", "Running command"):
            assert label in progress_fn
        assert "tool-card-running" in live_progress_fn, (
            "The live progress label should prefer the currently running tool over older completed tools."
        )
        assert "tool-call-group-list" not in sync_fn, (
            "Readable progress must not reintroduce the noisy secondary tool-name list."
        )

    def test_live_thinking_suppresses_visible_interim_echoes(self):
        interim_match = re.search(r"source\.addEventListener\('interim_assistant',e=>\{(.*?)\n\s*\}\);", MESSAGES_JS, re.S)
        assert interim_match, "interim_assistant listener not found"
        interim_fn = interim_match.group(1)
        live_thinking_fn = _function_body(MESSAGES_JS, "_liveThinkingText")

        assert "visibleInterimSnippets.push(visible)" in interim_fn, (
            "Visible interim commentary should be remembered so the live Thinking card does not echo it."
        )
        assert "_stripLiveVisibleAssistantEchoFromThinking" in live_thinking_fn, (
            "Live Thinking text should suppress exact visible interim commentary echoes."
        )

    def test_settled_thinking_suppresses_visible_assistant_echoes(self):
        render_fn = _function_body(UI_JS, "renderMessages")
        helper = _function_body(UI_JS, "_stripVisibleAssistantEchoFromThinking")
        assert "_stripVisibleAssistantEchoFromThinking(thinkingText, displayContent)" in render_fn, (
            "Settled Thinking cards should not repeat text already rendered as visible assistant content."
        )
        assert "s.length>=20" in helper, (
            "Thinking echo suppression should ignore tiny snippets to avoid over-stripping reasoning."
        )
        assert "out.split(snippet).join('')" in helper, (
            "Thinking echo suppression should remove exact visible assistant snippets from reasoning display."
        )

    def test_compact_activity_keeps_thinking_cards_after_session_switch(self):
        ui_min = re.sub(r"\s+", "", UI_JS)
        assert "functionensureActivityGroup(" in ui_min, (
            "Tool calls should still use the shared Activity disclosure helper."
        )
        assert "data-agent-activity-group" in UI_JS, (
            "The Activity disclosure needs a stable data-agent-activity-group hook."
        )
        render_fn = _function_body(UI_JS, "renderMessages")
        assert "isSimplifiedToolCalling()" in render_fn and "assistantThinking.set(rawIdx, thinkingText)" in render_fn, (
            "Compact settled transcript rendering should preserve Thinking cards after switching sessions."
        )
        assert "_thinkingActivityNode(thinkingText, false)" in render_fn, (
            "Settled Thinking cards should render as collapsed timeline entries before related tools."
        )
        assert "anchorParent.insertBefore(thinkingNode, anchorRow)" in render_fn, (
            "Settled Thinking cards should appear before their visible assistant process text."
        )
        assert "seg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText))" in render_fn, (
            "The non-simplified path should preserve standalone settled thinking cards."
        )

    def test_live_thinking_is_shown_while_still_splitting_tool_bursts(self):
        live_thinking_fn = _function_body(UI_JS, "appendThinking")
        live_tool_fn = _function_body(UI_JS, "appendLiveToolCard")
        helper = _function_body(UI_JS, "ensureActivityGroup")
        assert "isSimplifiedToolCalling()" in live_thinking_fn, (
            "Live thinking should branch on the Compact tool activity toggle."
        )
        assert 'data-live-activity-current' in live_thinking_fn, (
            "Starting a new live thinking block should close the previous live tool burst."
        )
        assert "body.insertBefore(row, body.firstChild)" not in live_thinking_fn, (
            "Live thinking should not be moved into the top Activity dropdown."
        )
        assert "_thinkingActivityNode(thinkingText, false)" in live_thinking_fn, (
            "Compact live thinking should render a collapsed Thinking card in the timeline."
        )
        assert '[data-live-activity-current="1"]' in live_thinking_fn, (
            "Starting a new Thinking card should mark the previous live tool burst as no longer current."
        )
        assert "body.querySelector" in live_tool_fn and "data-live-tid" in live_tool_fn, (
            "tool_complete must still update its current live Activity burst by tool id."
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
