"""Regression checks for PR #6176 active-run indicator.

Covers:
- DOM element existence outside opt-in profile button in titlebar
- Theme token CSS styling without #2ecc71 and reduced-motion animation guards
- Zero layout shift between idle and active dot
- setBusy chokepoint drives _updateActiveRunDot with no sessionStorage persistence
- _updateActiveRunDot reflects S.busy, INFLIGHT, and server session list streaming
- Complete removal of sessionStorage active-run machinery
- Direct S.busy writes repaint via _updateActiveRunDot
- _purgeStaleInflightEntries and _applySessionListPayload repaint on update
"""

from pathlib import Path

ROOT = Path(__file__).parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
COMMANDS_JS = (ROOT / "static" / "commands.js").read_text(encoding="utf-8")


class TestActiveRunDotElement:
    """The dot element exists in the DOM and has CSS styling."""

    def test_html_contains_active_run_dot_span(self):
        assert 'id="activeRunDot"' in INDEX_HTML, "activeRunDot span missing from index.html"

    def test_html_dot_is_sibling_of_profile_label(self):
        assert 'id="titlebarProfileLabel"' in INDEX_HTML
        assert 'id="activeRunDot"' in INDEX_HTML
        # dot must appear after the profile label span
        label_idx = INDEX_HTML.index('id="titlebarProfileLabel"')
        dot_idx = INDEX_HTML.index('id="activeRunDot"')
        assert dot_idx > label_idx, "activeRunDot must follow titlebarProfileLabel in HTML"

    def test_html_dot_outside_profile_button(self):
        """Dot must be in an always-visible container, outside the opt-in profile button."""
        btn_start = INDEX_HTML.index('id="titlebarProfileBtn"')
        btn_end = INDEX_HTML.index('</button>', btn_start)
        dot_idx = INDEX_HTML.index('id="activeRunDot"')
        assert dot_idx > btn_end, "activeRunDot must sit outside the opt-in #titlebarProfileBtn"

    def test_html_dot_has_accessibility_attributes(self):
        """Dot must have role='status' and aria-label for accessibility."""
        dot_start = INDEX_HTML.index('id="activeRunDot"')
        dot_tag = INDEX_HTML[dot_start - 30 : dot_start + 120]
        assert 'role="status"' in dot_tag
        assert 'aria-label=' in dot_tag

    def test_css_defines_active_run_dot_visible_class(self):
        assert '#activeRunDot.active-run-dot{' in STYLE_CSS.replace(' ', ''), \
            "Missing .active-run-dot CSS rule for visible dot"

    def test_css_defines_active_run_dot_hidden_class(self):
        assert '#activeRunDot.active-run-dot-hidden{' in STYLE_CSS.replace(' ', ''), \
            "Missing .active-run-dot-hidden CSS rule"

    def test_css_defines_pulse_animation(self):
        # @keyframes active-run-pulse with spaces collapsed
        css_no_spaces = STYLE_CSS.replace(' ', '').replace('\n', '').replace('\t', '')
        assert '@keyframesactive-run-pulse' in css_no_spaces, \
            "Missing @keyframes active-run-pulse animation"

    def test_css_no_hardcoded_green_or_rgba(self):
        """Must use theme tokens, never hard-coded #2ecc71 or rgba."""
        assert '#2ecc71' not in STYLE_CSS, "CSS must not contain hard-coded #2ecc71"
        assert 'rgba(46,204,113' not in STYLE_CSS.replace(' ', ''), "CSS must not contain #2ecc71 rgba shadow"

    def test_css_guards_reduced_motion(self):
        """Pulsing animation must not animate under prefers-reduced-motion: reduce."""
        css_no_spaces = STYLE_CSS.replace(' ', '').replace('\n', '').replace('\t', '')
        assert 'prefers-reduced-motion:reduce' in css_no_spaces
        assert '#activeRunDot.active-run-dot{animation:none' in css_no_spaces

    def test_css_reserves_space_without_layout_shift(self):
        """Hidden dot must preserve geometry so the titlebar does not shift when lit."""
        css_no_spaces = STYLE_CSS.replace(' ', '').replace('\n', '').replace('\t', '')
        assert '#activeRunDot.active-run-dot-hidden{display:none' not in css_no_spaces
        assert '#activeRunDot.active-run-dot-hidden{display:inline-block;width:8px;height:8px' in css_no_spaces


class TestSetBusyChokepoint:
    """setBusy() is the chokepoint that drives _updateActiveRunDot() without sessionStorage."""

    def test_setbusy_calls_update_active_run_dot(self):
        # setBusy must call _updateActiveRunDot() after S.busy = v
        assert 'try{_updateActiveRunDot();}catch(_){}' in UI_JS, \
            "setBusy must call _updateActiveRunDot()"

    def test_setbusy_does_not_call_persist(self):
        """setBusy must not call _persistActiveRunState; sessionStorage machinery is deleted."""
        assert '_persistActiveRunState' not in UI_JS, \
            "UI_JS must not reference _persistActiveRunState"


class TestUpdateActiveRunDot:
    """_updateActiveRunDot() projects server truth: viewed busy, INFLIGHT, or any session streaming."""

    def test_update_active_run_dot_exists(self):
        assert 'function _updateActiveRunDot()' in SESSIONS_JS, \
            "_updateActiveRunDot() function must exist in sessions.js"

    def test_checks_s_busy(self):
        assert 'S.busy' in SESSIONS_JS, "_updateActiveRunDot must check S.busy"

    def test_checks_inflight_for_any_active_run(self):
        # Must check INFLIGHT for any active run, not just S.busy
        assert "typeof INFLIGHT !== 'undefined'" in SESSIONS_JS, \
            "_updateActiveRunDot must guard INFLIGHT access"
        assert "Object.keys(INFLIGHT).length > 0" in SESSIONS_JS, \
            "_updateActiveRunDot must check INFLIGHT for background runs"

    def test_checks_all_sessions_streaming(self):
        """Must project server truth from _allSessions and _isSessionEffectivelyStreaming."""
        func_start = SESSIONS_JS.index('function _updateActiveRunDot()')
        func_end = SESSIONS_JS.index('}', func_start + 200)
        func_text = SESSIONS_JS[func_start:func_end]
        assert '_allSessions' in func_text, "_updateActiveRunDot must check _allSessions"
        assert '_isSessionEffectivelyStreaming' in func_text, \
            "_updateActiveRunDot must evaluate _isSessionEffectivelyStreaming"

    def test_sets_active_run_dot_class_when_active(self):
        assert "dot.className = 'active-run-dot'" in SESSIONS_JS, \
            "Must set active-run-dot class when active"

    def test_sets_hidden_class_when_inactive(self):
        assert "dot.className = 'active-run-dot-hidden'" in SESSIONS_JS, \
            "Must set active-run-dot-hidden class when inactive"

    def test_sets_title_agent_is_running(self):
        assert "dot.title = 'Agent is running'" in SESSIONS_JS, \
            "Must set title attribute for accessibility"


class TestNoSessionStorageRunState:
    """The client sessionStorage machinery is completely deleted; server is authoritative."""

    def test_no_active_run_storage_key_in_static(self):
        assert 'hermes-webui-active-run' not in SESSIONS_JS
        assert 'hermes-webui-active-run' not in BOOT_JS
        assert 'hermes-webui-active-run' not in UI_JS

    def test_persist_active_run_state_deleted(self):
        assert 'function _persistActiveRunState' not in SESSIONS_JS

    def test_restore_active_run_state_deleted(self):
        assert 'function _restoreActiveRunState' not in SESSIONS_JS

    def test_boot_does_not_call_restore(self):
        assert '_restoreActiveRunState' not in BOOT_JS

    def test_loadsession_does_not_carry_forward_restored_stream(self):
        assert '_restoredActiveSessionId' not in SESSIONS_JS


class TestDirectBusyWriteRepaintSites:
    """Direct S.busy writes that bypass setBusy() must repaint inside try/catch."""

    def test_messages_stream_abort_idle_repaints(self):
        assert "else { S.busy=false; try{_updateActiveRunDot();}catch(_){} }" in MESSAGES_JS

    def test_messages_turn_done_repaints(self):
        assert "S.busy=false; try{_updateActiveRunDot();}catch(_){}" in MESSAGES_JS

    def test_messages_server_stream_attach_repaints(self):
        assert "S.busy = true; try{_updateActiveRunDot();}catch(_){}" in MESSAGES_JS

    def test_messages_server_stream_error_repaints(self):
        assert "S.busy = false; try{_updateActiveRunDot();}catch(_){}" in MESSAGES_JS

    def test_commands_steer_clear_dead_run_repaints(self):
        assert "if(S.busy){S.busy=false;try{_updateActiveRunDot();}catch(_){}changed=true;}" in COMMANDS_JS

    def test_boot_sidebar_only_and_composer_prefill_repaint(self):
        assert "S.session=null; S.messages=[]; S.activeStreamId=null; S.busy=false; try{_updateActiveRunDot();}catch(_){}" in BOOT_JS

    def test_purge_stale_inflight_entries_repaints(self):
        purge_start = SESSIONS_JS.index('function _purgeStaleInflightEntries(')
        purge_end = SESSIONS_JS.index('function _hasOwnedOpenLiveStream(', purge_start)
        purge_text = SESSIONS_JS[purge_start:purge_end]
        assert 'try{_updateActiveRunDot();}catch(_){}' in purge_text

    def test_apply_session_list_payload_repaints(self):
        apply_start = SESSIONS_JS.index('function _applySessionListPayload(')
        apply_end = SESSIONS_JS.index('function _mergeRenderSessionListOptions(', apply_start)
        apply_text = SESSIONS_JS[apply_start:apply_end]
        assert 'try{_updateActiveRunDot();}catch(_){}' in apply_text


class TestPerAssignmentSites:
    """The per-assignment _updateActiveRunDot() calls in sessions.js remain as guardrails."""

    def test_reconcile_idle_calls_update_dot(self):
        assert "S.busy=false;_updateActiveRunDot();" in SESSIONS_JS

    def test_new_session_calls_update_dot(self):
        assert "S.busy=false;_updateActiveRunDot();" in SESSIONS_JS

    def test_loadsession_busy_path_calls_update(self):
        assert "S.busy=true;_updateActiveRunDot();" in SESSIONS_JS

    def test_loadsession_active_stream_path_calls_update(self):
        assert "S.busy=!!activeStreamId;_updateActiveRunDot();" in SESSIONS_JS
