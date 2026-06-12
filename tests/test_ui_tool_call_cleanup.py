"""Static UI tests for quieter tool-call rendering and shared design tokens.

These tests intentionally follow the repo's existing pytest style: read static
source files, isolate the relevant function/rule, and assert implementation
invariants before changing the UI.
"""
import json
import pathlib
import re
import subprocess

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


def _function_src(src: str, name: str) -> str:
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
    return src[match.start():i]


def _run_thinking_echo_helper(*args: str) -> str:
    helpers = "\n".join(
        _function_src(UI_JS, name)
        for name in (
            "_stripXmlToolCallsDisplay",
            "_sanitizeThinkingDisplayText",
            "_normalizeThinkingEchoCompare",
            "_stripVisibleAssistantEchoFromThinking",
        )
    )
    script = (
        helpers
        + "\nconst args=JSON.parse(process.argv[1]);"
        + "\nprocess.stdout.write(JSON.stringify(_stripVisibleAssistantEchoFromThinking(...args)));"
    )
    out = subprocess.run(
        ["node", "-e", script, json.dumps(list(args))],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


class TestToolCallGroupingStatic:
    def test_simplified_tool_calling_setting_is_hidden_from_frontend(self):
        assert "settingsSimplifiedToolCalling" not in (REPO / "static" / "index.html").read_text(encoding="utf-8"), (
            "Settings should no longer expose the deprecated Compact tool activity checkbox."
        )
        panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
        assert "settingsSimplifiedToolCalling" not in panels, (
            "Settings panel should not load or save the deprecated simplified_tool_calling setting."
        )

    def test_simplified_tool_calling_renderer_is_forced_to_worklog_mode(self):
        boot = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
        assert "window._simplifiedToolCalling=true" in boot, (
            "Boot should keep the Compact Worklog renderer enabled regardless of legacy saved values."
        )
        panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
        fn = _function_body(panels, "_autosavePreferencesSettings")
        assert "simplified_tool_calling" not in fn and "window._simplifiedToolCalling" not in fn, (
            "Preferences autosave should no longer hot-apply the deprecated renderer switch."
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

    def test_render_rebuild_preserves_worklog_detail_disclosure_click_state(self):
        render_fn = _function_body(UI_JS, "renderMessages")
        capture_fn = _function_body(UI_JS, "_captureWorklogDetailDisclosureState")
        restore_fn = _function_body(UI_JS, "_restoreWorklogDetailDisclosureState")
        apply_fn = _function_body(UI_JS, "_setWorklogDetailDisclosureOpen")
        capture_pos = render_fn.index("const worklogDetailDisclosureState=_captureWorklogDetailDisclosureState(inner);")
        cache_pos = render_fn.index("if(sid&&sid!==_sessionHtmlCacheSid&&!INFLIGHT[sid]&&!hasTransientTranscriptUi)")
        cache_return_pos = render_fn.index("return;", cache_pos)
        wipe_pos = render_fn.index("inner.innerHTML='';")
        restore_pos = render_fn.index("_restoreWorklogDetailDisclosureState(inner, worklogDetailDisclosureState);")
        fail_safe_pos = render_fn.find("Fail-safe invariant (#3875)")
        assert cache_pos < cache_return_pos < capture_pos, (
            "renderMessages() should not traverse the previous session DOM when "
            "the HTML-cache fast path can return early."
        )
        assert capture_pos < wipe_pos, (
            "renderMessages() must capture manual Worklog detail open/closed state "
            "before wiping msgInner for a rebuild."
        )
        assert restore_pos > wipe_pos, (
            "renderMessages() must restore manual Worklog detail state after the "
            "new Thinking/Tool DOM has been rebuilt."
        )
        assert fail_safe_pos == -1 or restore_pos < fail_safe_pos, (
            "The blank-turn fail-safe must still be allowed to expand otherwise "
            "invisible Worklog content after manual detail state is restored."
        )
        assert "_worklogDetailDisclosureSelector" in capture_fn, (
            "The rebuild-state capture should use the shared Worklog detail selector."
        )
        selector_match = re.search(r"const _worklogDetailDisclosureSelector='([^']+)'", UI_JS)
        assert selector_match, "Shared Worklog detail selector is missing."
        selector = selector_match.group(1)
        assert ".thinking-card" in selector and ".tool-card" in selector, (
            "The rebuild-state capture must cover both Thinking cards and Tool cards."
        )
        assert "data-tool-worklog-tool-group" in selector, (
            "The rebuild-state capture must cover multi-tool Worklog detail groups."
        )
        assert "_setWorklogDetailDisclosureOpen" in restore_fn, (
            "Restoration should use the shared disclosure-state applier."
        )
        assert "tool-worklog-tool-group-collapsed" in apply_fn and "aria-expanded" in apply_fn, (
            "Restoring multi-tool groups must sync both CSS state and accessibility state."
        )

    def test_worklog_detail_keys_stay_stable_while_streaming_content_grows(self):
        key_fn = _function_body(UI_JS, "_worklogDetailBaseKey")
        append_thinking_fn = _function_body(UI_JS, "appendThinking")
        append_step_fn = _function_body(UI_JS, "_appendWorklogStep")
        build_tool_fn = _function_body(UI_JS, "buildToolCard")
        sync_tools_fn = _function_body(UI_JS, "_syncToolRowsContainer")

        thinking_branch = re.search(
            r"if\(el\.classList\.contains\('thinking-card'\)\)\{(?P<body>.*?)\n  \}\n  if\(el\.classList\.contains\('tool-card'\)\)",
            key_fn,
            re.S,
        )
        assert thinking_branch, "Thinking-card disclosure-key branch is missing."
        thinking_body = thinking_branch.group("body")
        assert "data-thinking-key" in thinking_body and "data-live-thinking-key" in thinking_body, (
            "Thinking-card disclosure keys must prefer render-time stable row keys."
        )
        assert ".thinking-card-body pre" not in thinking_body and "textContent" not in thinking_body, (
            "Thinking-card disclosure keys must not depend on streaming body text."
        )
        assert "_thinkingActivityNode(clean, false, thinkingKey)" in append_thinking_fn, (
            "Live streaming Thinking rows must stamp the stable thinking key at creation time."
        )
        assert "_thinkingActivityNode(thinkingText, false, thinkingDisclosureKey)" in append_step_fn, (
            "Settled Worklog Thinking rows must stamp the stable thinking key at creation time."
        )
        assert "thinkingDisclosureKey:thinkingText?`thinking:${entry.key}`:''" in _function_body(UI_JS, "renderMessages"), (
            "Settled Worklog Thinking keys should come from activity coordinates, not text."
        )

        tool_branch = re.search(
            r"if\(el\.classList\.contains\('tool-card'\)\)\{(?P<body>.*?)\n  \}\n  if\(el\.matches&&el\.matches\('\.tool-group",
            key_fn,
            re.S,
        )
        assert tool_branch, "Tool-card disclosure-key branch is missing."
        tool_body = tool_branch.group("body")
        assert "data-tool-disclosure-key" in tool_body, (
            "Tool-card disclosure keys must prefer a stable render-time tool key."
        )
        assert ".tool-card-preview" not in tool_body, (
            "Tool-card disclosure keys must not depend on result preview text."
        )
        assert "_toolDisclosureIdentity(tc)" in build_tool_fn, (
            "buildToolCard() must stamp a stable disclosure key on each tool row."
        )
        assert "tc.snippet" not in _function_body(UI_JS, "_toolDisclosureIdentity"), (
            "Derived tool disclosure keys must not include changing result snippets."
        )
        assert "tc.args" not in _function_body(UI_JS, "_toolDisclosureIdentity"), (
            "Derived tool disclosure keys must not include streaming tool arguments."
        )

        group_branch = re.search(
            r"if\(el\.matches&&el\.matches\('\.tool-group\[data-tool-worklog-tool-group=\"1\"\],\.tool-worklog-tool-group'\)\)\{(?P<body>.*?)\n  \}\n  return '';",
            key_fn,
            re.S,
        )
        assert group_branch, "Multi-tool Worklog group disclosure-key branch is missing."
        group_body = group_branch.group("body")
        assert "data-tool-group-disclosure-key" in group_body, (
            "Multi-tool Worklog groups must prefer a stable render-time group key."
        )
        assert "_worklogDetailTextKey" not in group_body and "textContent" not in group_body, (
            "Multi-tool Worklog group disclosure keys must not depend on changing summary text."
        )
        assert "data-tool-group-disclosure-key" in sync_tools_fn and "stepIdx" in sync_tools_fn, (
            "Grouped Worklog tool rows must stamp a stable per-step disclosure key."
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
        assert "tc.tid||tc.id||tc.tool_call_id||tc.tool_use_id||tc.call_id" in live_fn, (
            "Live replay should replace restored cards for all known tool id aliases, not only tc.tid."
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

    def test_live_thinking_does_not_rewrite_visible_interim_echoes(self):
        interim_match = re.search(r"source\.addEventListener\('interim_assistant',e=>\{(.*?)\n\s*\}\);", MESSAGES_JS, re.S)
        assert interim_match, "interim_assistant listener not found"
        interim_fn = interim_match.group(1)
        live_thinking_fn = _function_body(MESSAGES_JS, "_liveThinkingText")

        assert "visibleInterimSnippets.push(visible)" in interim_fn, (
            "Visible interim commentary should remain available for process-prose boundaries."
        )
        assert "_stripLiveVisibleAssistantEchoFromThinking" not in live_thinking_fn, (
            "Live Thinking should not run content-level echo suppression; the card is already low-priority Worklog detail."
        )
        assert "String(liveReasoningText||'').trim()" in live_thinking_fn, (
            "Live Thinking should render the provider reasoning text as-is after normal trimming."
        )

    def test_settled_exact_duplicate_thinking_suppressed(self):
        assert _run_thinking_echo_helper(
            "  I will check the PR status.\nThen inspect the diff. ",
            "I will check the PR status. Then inspect the diff.",
            "The final answer is different.",
        ) == "", (
            "Settled Thinking should be suppressed when normalized text exactly "
            "matches visible process prose."
        )

    def test_genuine_reasoning_preserved_when_not_exact(self):
        reasoning = "I need to inspect the stream state before deciding."
        assert _run_thinking_echo_helper(
            reasoning,
            "I need to inspect the stream state.",
            "The stream was running.",
        ) == reasoning, (
            "Non-exact reasoning should stay available as a Worklog Thinking Card."
        )
        helper = _function_body(UI_JS, "_stripVisibleAssistantEchoFromThinking")
        assert ".split(snippet).join('')" not in helper
        assert ".includes(" not in helper

    def test_reasoning_first_interim_later_does_not_duplicate_settled_worklog(self):
        render_fn = _function_body(UI_JS, "renderMessages")
        helper = _function_body(UI_JS, "_worklogReasoningTextFromMessage")
        assert "assistantTurnFinalVisibleContentByRawIdx" in render_fn, (
            "renderMessages must compute current assistant-turn final text so "
            "reasoning-first/interim-later turns can be compared at settlement."
        )
        assert "assistantTurnVisibleContentByRawIdx" in render_fn, (
            "If done-time reasoning is attached to the final assistant message, "
            "settlement must still compare against earlier visible process prose "
            "from the same assistant turn."
        )
        assert "_worklogReasoningTextFromMessage(m, rawIdx, toolCallAssistantIdxs, displayContent, turnFinalVisibleContent, turnVisibleContents)" in render_fn
        assert "_stripVisibleAssistantEchoFromThinking(thinkingText, visibleContent, turnFinalVisibleContent, ...visibleTexts)" in helper
        assert _run_thinking_echo_helper(
            "I am checking the 3401 review blocker.",
            "I am checking the 3401 review blocker.",
            "Conclusion: Thinking dedupe needs a small fix.",
        ) == ""

    def test_settled_thinking_uses_exact_dedupe_not_live_rewrite(self):
        render_fn = _function_body(UI_JS, "renderMessages")
        helper = _function_body(UI_JS, "_stripVisibleAssistantEchoFromThinking")
        assert "_stripVisibleAssistantEchoFromThinking(thinkingText, displayContent)" not in render_fn, (
            "Settled Thinking dedupe needs process prose plus turn-final answer, "
            "not the old single visible-text input."
        )
        assert "_normalizeThinkingEchoCompare" in helper and "visibleNorm===thinkingNorm" in helper, (
            "Settled Thinking dedupe must be exact / normalized-exact only."
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
        assert "_assistantReasoningPayloadText(m)" in helper and "_stripVisibleAssistantEchoFromThinking" in helper, (
            "Provider reasoning metadata should feed a sanitized Worklog Thinking Card "
            "after settled exact-duplicate suppression."
        )
        assert "data-worklog-thinking-card" in UI_JS, (
            "Thinking should be an explicit Worklog item, independent from Tool Cards."
        )
        render_min = re.sub(r"\s+", "", render_fn)
        assert "thinkingKey:thinkingText?`thinking:${_normalizeThinkingEchoCompare(thinkingText)}`:''" in render_min, (
            "Settled Worklog should keep normalized-content Thinking dedupe so sibling messages do not duplicate cards."
        )
        assert "thinkingDisclosureKey:thinkingText?`thinking:${entry.key}`:''" in render_min, (
            "Settled Worklog should separately key disclosure state by stable activity coordinates "
            "so streaming text growth does not reset manual collapse state."
        )
        assert "_appendWorklogStep" in render_fn, (
            "Visible assistant anchors, Thinking Cards, and tools should still build the compact Worklog disclosure."
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
        assert "_worklogReasonNodeFromText(thinkingText" not in live_thinking_fn, (
            "Provider reasoning should not render as live Worklog process prose."
        )
        assert "_thinkingActivityNode(clean, false, thinkingKey)" in live_thinking_fn and "data-live-thinking" in live_thinking_fn, (
            "Live provider thinking should render as a collapsed Worklog Thinking Card."
        )
        assert "ensureLiveWorklogContainer" in live_thinking_fn, (
            "Live Thinking Cards should use the shared Worklog container, not a Tool Card group."
        )
        assert "removeAttribute('data-live-activity-current')" not in live_thinking_fn, (
            "Reasoning/Thinking updates alone should not split consecutive tools into one-tool Worklog rows."
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

    def test_live_thinking_card_is_segment_scoped_not_global_singleton(self):
        live_thinking_fn = _function_body(UI_JS, "appendThinking")
        placement_fn = _function_body(MESSAGES_JS, "_liveThinkingPlacement")
        update_fn = _function_body(MESSAGES_JS, "_updateLiveThinkingCard")
        interim_match = re.search(r"source\.addEventListener\('interim_assistant',e=>\{(.*?)\n\s*\}\);", MESSAGES_JS, re.S)
        assert interim_match, "interim_assistant listener not found"
        interim_fn = interim_match.group(1)

        assert "data-live-thinking-key" in live_thinking_fn, (
            "Live Thinking rows need a segment/burst key so later reasoning does not update "
            "the first Thinking Card in the turn."
        )
        assert 'data-live-thinking="1"][data-live-thinking-key="' in live_thinking_fn, (
            "appendThinking() must query the current segment's live Thinking Card, not a "
            "turn-global singleton."
        )
        assert "segmentSeq" in placement_fn and "_currentLiveSegmentSeq" in placement_fn, (
            "Thinking placement should reuse the live segment sequence instead of inventing "
            "a second placement model."
        )
        assert "burstId:_currentActivityBurstId" in placement_fn, (
            "Thinking placement should carry the current activity burst for Worklog ordering."
        )
        assert "updateThinking(text, opts)" in update_fn, (
            "messages.js should pass segment placement into the UI Thinking helper."
        )
        assert "updateThinking('')" not in interim_fn, (
            "Live interim boundaries should finalize the current Thinking Card instead of "
            "clearing it mid-stream."
        )
        assert "finalizeThinkingCard()" in interim_fn, (
            "Visible interim assistant progress must close the current Thinking segment "
            "before the next segment starts."
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

    def test_worklog_thinking_card_uses_quiet_tool_row_hierarchy(self):
        selector = ".tool-worklog-list > .agent-activity-thinking .thinking-card,"
        assert selector in CSS, "Worklog Thinking Card quiet override missing"
        card_rule = re.sub(r"\s+", "", CSS.split(selector, 1)[1].split("}", 1)[0])
        header_rule = re.sub(
            r"\s+",
            "",
            CSS.split(".tool-worklog-list > .agent-activity-thinking .thinking-card-header{", 1)[1].split("}", 1)[0],
        )
        label_rule = re.sub(
            r"\s+",
            "",
            CSS.split(".tool-worklog-list > .agent-activity-thinking .thinking-card-label{", 1)[1].split("}", 1)[0],
        )
        icon_rule = re.sub(
            r"\s+",
            "",
            CSS.split(".tool-worklog-list > .agent-activity-thinking .thinking-card-icon,", 1)[1].split("}", 1)[0],
        )
        body_rule = re.sub(
            r"\s+",
            "",
            CSS.split(".tool-worklog-list > .agent-activity-thinking .thinking-card.open .thinking-card-body{", 1)[1].split("}", 1)[0],
        )
        pre_rule = re.sub(
            r"\s+",
            "",
            CSS.split(".tool-worklog-list > .agent-activity-thinking .thinking-card-body pre{", 1)[1].split("}", 1)[0],
        )

        assert "background:transparent" in card_rule
        assert "border:0" in card_rule
        assert "border-radius:0" in card_rule
        assert "display:flex" in header_rule and "align-items:center" in header_rule
        assert "color:var(--muted)" in header_rule
        assert "font-size:var(--message-body-font-size)" in header_rule
        assert "font-weight:400" in header_rule
        assert "font-weight:400" in label_rule
        assert "letter-spacing:0" in label_rule
        assert "color:var(--muted)" in icon_rule
        assert "padding:6px8px7px8px" in body_rule
        assert "font-size:var(--message-body-font-size)" in pre_rule
        assert "line-height:var(--message-body-line-height)" in pre_rule
