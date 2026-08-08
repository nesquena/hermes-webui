"""Regression checks for PR #6176 active-run indicator."""

from pathlib import Path

ROOT = Path(__file__).parent.parent
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


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


class TestSetBusyChokepoint:
    """setBusy() is the single chokepoint that drives _updateActiveRunDot() and _persistActiveRunState()."""

    def test_setbusy_calls_update_active_run_dot(self):
        # setBusy must call _updateActiveRunDot() after S.busy = v
        assert 'try{_updateActiveRunDot();}catch(_){}' in UI_JS, \
            "setBusy must call _updateActiveRunDot()"

    def test_setbusy_calls_persist_active_run_state(self):
        # setBusy must call _persistActiveRunState() after S.busy = v
        assert 'try{_persistActiveRunState();}catch(_){}' in UI_JS, \
            "setBusy must call _persistActiveRunState()"

    def test_setbusy_calls_update_before_persist(self):
        # _updateActiveRunDot must appear before _persistActiveRunState in setBusy
        update_idx = UI_JS.index('_updateActiveRunDot')
        persist_idx = UI_JS.index('_persistActiveRunState')
        assert update_idx < persist_idx, \
            "_updateActiveRunDot must be called before _persistActiveRunState in setBusy"


class TestUpdateActiveRunDot:
    """_updateActiveRunDot() reflects ANY active run, not just S.busy."""

    def test_update_active_run_dot_exists(self):
        assert 'function _updateActiveRunDot()' in SESSIONS_JS, \
            "_updateActiveRunDot() function must exist in sessions.js"

    def test_checks_s_busy(self):
        assert '!!S.busy' in SESSIONS_JS, "_updateActiveRunDot must check S.busy"

    def test_checks_inflight_for_any_active_run(self):
        # Must check INFLIGHT for any active run, not just S.busy
        assert "typeof INFLIGHT !== 'undefined'" in SESSIONS_JS, \
            "_updateActiveRunDot must guard INFLIGHT access"
        assert "Object.keys(INFLIGHT).length > 0" in SESSIONS_JS, \
            "_updateActiveRunDot must check INFLIGHT for background runs"

    def test_sets_active_run_dot_class_when_active(self):
        assert "dot.className = 'active-run-dot'" in SESSIONS_JS, \
            "Must set active-run-dot class when active"

    def test_sets_hidden_class_when_inactive(self):
        assert "dot.className = 'active-run-dot-hidden'" in SESSIONS_JS, \
            "Must set active-run-dot-hidden class when inactive"

    def test_sets_title_agent_is_running(self):
        assert "dot.title = 'Agent is running'" in SESSIONS_JS, \
            "Must set title attribute for accessibility"


class TestPersistActiveRunState:
    """_persistActiveRunState() writes correct state to sessionStorage."""

    def test_persist_uses_correct_session_id_accessor(self):
        # Must use S.session && S.session.session_id, never the phantom S.sessionId
        assert "S.session && S.session.session_id" in SESSIONS_JS, \
            "_persistActiveRunState must use S.session && S.session.session_id"
        assert "S.sessionId" not in SESSIONS_JS, \
            "Phantom S.sessionId must not appear in the codebase"

    def test_persist_key_is_active_session_id(self):
        assert 'activeSessionId:' in SESSIONS_JS, \
            "sessionStorage key must be activeSessionId (not the misleading sessionId)"

    def test_persist_includes_busy_flag(self):
        assert 'busy: !!S.busy' in SESSIONS_JS

    def test_persist_includes_active_stream_id(self):
        assert 'activeStreamId:' in SESSIONS_JS

    def test_persist_includes_timestamp(self):
        assert 'timestamp: Date.now()' in SESSIONS_JS

    def test_persist_wraps_in_try_catch(self):
        # sessionStorage access may throw (quota, private browsing); must be guarded
        func_start = SESSIONS_JS.index('function _persistActiveRunState()')
        func_end = func_start + 500  # reasonable window
        func_text = SESSIONS_JS[func_start:func_end]
        assert 'try {' in func_text and 'catch(e) {}' in func_text, \
            "_persistActiveRunState must wrap in try/catch"


class TestRestoreActiveRunState:
    """_restoreActiveRunState() restores and paints from sessionStorage."""

    def test_restore_exists(self):
        assert 'function _restoreActiveRunState()' in SESSIONS_JS, \
            "_restoreActiveRunState() function must exist"

    def test_restore_calls_update_active_run_dot(self):
        func_start = SESSIONS_JS.index('function _restoreActiveRunState()')
        func_end = SESSIONS_JS.index('function _updateActiveRunDot()')
        func_text = SESSIONS_JS[func_start:func_end]
        assert '_updateActiveRunDot()' in func_text, \
            "_restoreActiveRunState must call _updateActiveRunDot() to paint the dot"

    def test_restore_sets_s_busy_true(self):
        func_start = SESSIONS_JS.index('function _restoreActiveRunState()')
        func_end = SESSIONS_JS.index('function _updateActiveRunDot()')
        func_text = SESSIONS_JS[func_start:func_end]
        assert 'S.busy = true' in func_text, \
            "_restoreActiveRunState must set S.busy = true"

    def test_restore_reads_active_session_id(self):
        func_start = SESSIONS_JS.index('function _restoreActiveRunState()')
        func_end = SESSIONS_JS.index('function _updateActiveRunDot()')
        func_text = SESSIONS_JS[func_start:func_end]
        assert 'state.activeSessionId' in func_text, \
            "_restoreActiveRunState must read state.activeSessionId"

    def test_restore_checks_thirty_second_window(self):
        assert '(Date.now() - state.timestamp) < 30000' in SESSIONS_JS, \
            "_restoreActiveRunState must enforce 30-second recency window"

    def test_restore_clears_session_storage_after_read(self):
        assert "sessionStorage.removeItem('hermes-webui-active-run')" in SESSIONS_JS, \
            "_restoreActiveRunState must remove the item to prevent stale restores"

    def test_restore_wraps_in_try_catch(self):
        func_start = SESSIONS_JS.index('function _restoreActiveRunState()')
        func_end = SESSIONS_JS.index('function _updateActiveRunDot()')
        func_text = SESSIONS_JS[func_start:func_end]
        assert 'try {' in func_text and 'catch(e) {}' in func_text, \
            "_restoreActiveRunState must wrap in try/catch"


class TestBootIntegration:
    """boot.js calls _restoreActiveRunState() early."""

    def test_boot_calls_restore(self):
        assert '_restoreActiveRunState()' in BOOT_JS, \
            "boot.js must call _restoreActiveRunState() for cross-reload persistence"

    def test_boot_restore_wrapped_in_try_catch(self):
        assert "try{_restoreActiveRunState();}catch(e){}" in BOOT_JS, \
            "boot.js restore call must be wrapped in try/catch"


class TestLoadSessionSurvival:
    """loadSession carries forward restored active-run state for matching session."""

    def test_loadsession_carries_forward_restored_stream(self):
        # When server doesn't report activeStreamId but restore has one for this sid,
        # loadSession must carry it forward.
        assert 'S._restoredActiveSessionId === sid' in SESSIONS_JS, \
            "loadSession must check restored session ID before clobbering busy state"

    def test_loadsession_clears_restored_flag_after_use(self):
        assert 'delete S._restoredActiveSessionId' in SESSIONS_JS, \
            "loadSession must clear S._restoredActiveSessionId after consuming it"


class TestPerAssignmentSites:
    """The 6 per-assignment _updateActiveRunDot() calls in sessions.js remain as guardrails."""

    def test_reconcile_idle_calls_update_dot(self):
        assert "S.busy=false;_updateActiveRunDot();" in SESSIONS_JS

    def test_new_session_calls_update_dot(self):
        # newSession should call _updateActiveRunDot after S.busy=false
        assert "S.busy=false;_updateActiveRunDot();" in SESSIONS_JS

    def test_loadsession_busy_path_calls_update_and_persist(self):
        assert "S.busy=true;_updateActiveRunDot();_persistActiveRunState();" in SESSIONS_JS

    def test_loadsession_active_stream_path_calls_update_and_persist(self):
        assert "S.busy=!!activeStreamId;_updateActiveRunDot();_persistActiveRunState();" in SESSIONS_JS
