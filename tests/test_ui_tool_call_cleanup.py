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
        assert "byActivity = new Map()" in fn, (
            "Settled tool rendering should bucket by worklog segments/bursts."
        )
        assert "_toolWorklogListEl(group)" in fn, (
            "Settled tools should render through the worklog list container."
        )
        assert "_syncToolCallGroupSummary(state.group)" in fn, (
            "Settled worklog groups should refresh summary state."
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
        assert "isSimplifiedToolCalling()" not in live_fn, (
            "Live streaming tool cards should no longer branch on compact/timeline mode."
        )
        assert "ensureLiveWorklogContainer" in live_fn, (
            "Live tool rendering should use the direct Worklog container."
        )
        assert "ensureActivityGroup" not in live_fn, (
            "Live tool rendering must not show the settled L1 Activity summary while streaming."
        )
        assert "_toolWorklogListEl(group)" in live_fn, (
            "Live tool cards should insert into the worklog list container."
        )
        step_fn = _function_body(UI_JS, "_appendWorklogStep")
        assert "buildToolCard" in live_fn and "buildToolCard" in step_fn and "_appendWorklogStep" in settled_fn, (
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
        assert "_copyActivityDisclosureState('live:'+streamId, 'assistant:'" not in done_fn, (
            "Live disclosure state must not transfer to the final assistant turn; final L1 starts collapsed."
        )

    def test_live_tool_worklog_is_direct_until_settled(self):
        live_fn = _function_body(UI_JS, "appendLiveToolCard")
        live_container = _function_body(UI_JS, "ensureLiveWorklogContainer")
        helper = _function_body(UI_JS, "ensureActivityGroup")
        assert "ensureLiveWorklogContainer" in live_fn, (
            "Live tool events should append into the direct Worklog timeline."
        )
        assert "tool-worklog-list" in live_container and "data-live-worklog-shell" in live_container, (
            "The direct live Worklog shell should own the L2 list without an L1 summary row."
        )
        assert "activity-summary" not in live_container and "tool-call-group-summary" not in live_container, (
            "The settled Activity summary should not be present while the stream is running."
        )
        assert "savedState==='open'" in helper or 'savedState==="open"' in helper, (
            "Live Activity groups can still restore explicit live open state."
        )
        assert "if(live && savedState==='open')" in helper or 'if(live && savedState==="open")' in helper, (
            "Saved open state must be scoped to live groups so final L1 defaults collapsed."
        )
        assert "savedState==='closed'" in helper or 'savedState==="closed"' in helper, (
            "A saved closed Activity group should still override the live expanded default."
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

    def test_terminal_worklog_titles_summarize_common_diagnostic_commands(self):
        start = UI_JS.find("function _toolCommandTitle")
        end = UI_JS.find("function _toolQueryTitle", start)
        assert start != -1 and end != -1, "_toolCommandTitle() source window not found"
        command_fn = UI_JS[start:end]
        assert "git fetch" in command_fn and "git ahead/behind" in command_fn, (
            "Terminal Worklog rows should distinguish common git audit commands "
            "instead of falling back to the generic 'command' title."
        )
        assert "git log" in command_fn, (
            "Commit/PR audit commands should show a git log title instead of "
            "the generic command fallback."
        )
        assert "health check" in command_fn, (
            "curl localhost /health checks should get a readable L2 title."
        )
        assert "process check" in command_fn and "port ${m[1]} check" in command_fn, (
            "ps/grep and lsof diagnostics should be scannable in L2 while full "
            "commands remain in L3 detail."
        )
        assert "launchctl" in command_fn, (
            "launchd service checks should keep their service intent visible in "
            "the Worklog row title."
        )
        assert "return _shortToolLabel(normalized,72);" in command_fn, (
            "Long shell diagnostics should still expose a short L2 command "
            "summary instead of falling back to the bare 'command' title."
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
            "Tool calls should still use the shared compact Activity disclosure helper."
        )
        assert "data-agent-activity-group" in UI_JS, (
            "The Activity disclosure needs a stable data-agent-activity-group hook."
        )
        render_fn = _function_body(UI_JS, "renderMessages")
        assert "isSimplifiedToolCalling()" in render_fn and "assistantThinking.set(rawIdx, thinkingText)" in render_fn, (
            "Compact settled transcript rendering should keep reasoning metadata available without promoting it to visible prose."
        )
        helper = _function_body(UI_JS, "_worklogReasoningTextFromMessage")
        assert "return '';" in helper, (
            "Provider reasoning metadata should not render as Worklog prose."
        )
        assert "_appendWorklogStep" in render_fn, (
            "Visible assistant anchors and tools should still build the compact Activity disclosure."
        )
        assert ".wl-reason[data-worklog-reason-source=\"reasoning\"]" in render_fn, (
            "Settled rerenders must remove previously inserted reasoning Worklog rows before rebuilding."
        )
        assert "seg.insertAdjacentHTML('beforeend', _thinkingCardHtml(thinkingText))" in render_fn, (
            "The non-simplified path should preserve standalone settled thinking cards."
        )

    def test_live_visible_interim_text_preserves_timeline_boundary(self):
        live_thinking_fn = _function_body(UI_JS, "appendThinking")
        live_tool_fn = _function_body(UI_JS, "appendLiveToolCard")
        helper = _function_body(UI_JS, "ensureActivityGroup")
        assert "isSimplifiedToolCalling()" not in live_thinking_fn, (
            "Live provider thinking should not render through either compact or legacy thinking-card UI."
        )
        assert "_worklogReasonNodeFromText(thinkingText" not in live_thinking_fn, (
            "Provider reasoning should not render as live Worklog prose."
        )
        assert "thinking-card-row" not in live_thinking_fn and "_renderThinkingInto" not in live_thinking_fn, (
            "Live provider thinking should stay diagnostic-only instead of leaking as a thinking card."
        )
        assert "Provider reasoning/thinking is retained as diagnostics" in live_thinking_fn, (
            "The live Thinking path should document that visible interim assistant text is the Worklog prose source."
        )
        assert "removeAttribute('data-live-activity-current')" not in live_thinking_fn, (
            "Reasoning/Thinking updates alone should not split consecutive tools into one-tool Activity rows."
        )
        assert '.tool-call-group[data-live-tool-call-group="1"][data-live-activity-current="1"]' in helper, (
            "Live tool cards should only reuse the current Activity burst, not the first group in the turn."
        )
        assert "group.setAttribute('data-live-activity-current','1')" in helper, (
            "New live Activity bursts must be marked current so later tools append to the right group."
        )
        assert "querySelector" in live_tool_fn and "data-live-tid" in live_tool_fn, (
            "tool_complete must still update its current live Activity burst by tool id."
        )
        finalize_fn = _function_body(UI_JS, "finalizeThinkingCard")
        assert "turn.querySelector('.wl-reason[data-worklog-reason-active=\"1\"]')" in finalize_fn, (
            "Finalization should still clean up any legacy active reasoning marker."
        )
        assert "data-worklog-reason-active" not in live_thinking_fn, (
            "New live reasoning text should not create active Worklog prose rows."
        )
        reset_fn = _function_body(MESSAGES_JS, "_resetAssistantSegment")
        assert "function closeCurrentLiveActivityGroup()" in UI_JS, (
            "Visible interim assistant progress needs a shared helper to close the current Activity burst."
        )
        interim_match = re.search(r"source\.addEventListener\('interim_assistant',e=>\{(.*?)\n\s*\}\);", MESSAGES_JS, re.S)
        assert interim_match and "closeCurrentLiveActivityGroup()" in interim_match.group(1), (
            "Visible interim assistant progress is timeline content and must split the current Activity burst."
        )
        assert interim_match and "ensureAssistantRow(true)" in interim_match.group(1), (
            "Visible interim assistant progress must create a visible assistant timeline segment."
        )
        assert interim_match and "_flushPendingSegmentRender({force:true})" in interim_match.group(1), (
            "Visible interim assistant progress must be synchronously rendered before the segment reset."
        )
        timer_fn = _function_body(UI_JS, "_updateActiveActivityElapsedTimer")
        assert "data-live-activity-current" in timer_fn, (
            "Elapsed timers should clear once an Activity group is no longer current."
        )
        tool_start_segment = MESSAGES_JS.split("source.addEventListener('tool',e=>{", 1)[1].split("source.addEventListener('tool_complete'", 1)[0]
        assert "_resetAssistantSegment();" in tool_start_segment, (
            "Tool starts should reset the next assistant text segment without closing the current Activity burst."
        )
        assert "_resetAssistantSegment({closeActivity:true});" not in tool_start_segment, (
            "Tool starts must not split consecutive tools into one-tool Activity rows."
        )

    def test_live_compression_card_splits_current_tool_activity_burst(self):
        compression_fn = _function_body(UI_JS, "appendLiveCompressionCard")
        close_fn = _function_body(UI_JS, "closeCurrentLiveActivityGroup")
        assert "closeCurrentLiveActivityGroup();" in compression_fn, (
            "Auto-compression cards should close the current live Activity burst so later tools start a fresh group."
        )
        assert "data-live-activity-current" in close_fn, (
            "The live compression boundary helper must clear the current Activity marker."
        )
        assert "removeAttribute('data-live-activity-current')" in close_fn, (
            "Closing a live Activity burst should leave the row rendered but stop later tools from reusing it."
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
        tool_card_rule = css_min.rsplit(".tool-card{", 1)[1].split("}", 1)[0]
        rows_rule = css_min.split(".tg-rows{", 1)[1].split("}", 1)[0]
        assert "background:transparent" in tool_card_rule
        assert "border:0" in tool_card_rule
        assert "border-left:0" in tool_card_rule
        assert "border-left:1pxsolidvar(--border-subtle)" in rows_rule, (
            "Nested tool groups should be expressed with only a subtle left guide line."
        )

    def test_tool_card_header_and_text_use_spacing_and_font_tokens(self):
        css_min = re.sub(r"\s+", "", CSS)
        assert ".tool-card-header{" in css_min, ".tool-card-header rule missing"
        header_rule = css_min.rsplit(".tool-card-header{", 1)[1].split("}", 1)[0]
        title_rule = css_min.split(".tl-title{", 1)[1].split("}", 1)[0]
        assert "gap:7px" in header_rule
        assert "padding:3px8px" in header_rule
        assert "border-radius:7px" in header_rule
        assert ".tool-card-name{" in css_min and "font-size:var(--message-body-font-size)" in css_min
        assert "font-size:var(--message-body-font-size)" in title_rule
        assert "font-family:var(--font-mono)" in title_rule
