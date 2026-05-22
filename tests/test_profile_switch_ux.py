"""
Tests for profile-switch UX improvements — spinner indicator + parallelized fetches.

Two changes:
1. switchToProfile() shows a spinner on the profile chip during the async switch,
   with an optimistic name update and error revert.
2. loadWorkspaceList() refreshes in the background and the model catalog is
   invalidated for lazy refresh instead of holding the visible switch animation open.
"""
import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()


class TestProfileSwitchSpinner:
    """Static-analysis tests for the spinner loading indicator."""

    JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    def _get_switch_fn(self):
        idx = self.JS.find("async function switchToProfile(name) {")
        assert idx != -1, "switchToProfile not found in panels.js"
        depth = 0
        for i, ch in enumerate(self.JS[idx:], idx):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.JS[idx: i + 1]
        raise AssertionError("Could not extract switchToProfile")

    def test_switching_class_added_on_start(self):
        """The switching CSS class must be added before any awaits."""
        fn = self._get_switch_fn()
        assert "classList.add('switching')" in fn, (
            "switchToProfile() does not add 'switching' CSS class to the chip."
        )

    def test_switching_class_removed_in_finally(self):
        """The switching class must be removed in a finally block."""
        fn = self._get_switch_fn()
        finally_idx = fn.find("} finally {")
        assert finally_idx != -1, "switchToProfile() has no finally block."
        assert "classList.remove('switching')" in fn[finally_idx:], (
            "The finally block does not remove 'switching' class."
        )

    def test_optimistic_name_set_before_api_call(self):
        """Chip label must be updated to new name before the API call."""
        fn = self._get_switch_fn()
        api_call_idx = fn.find("await api('/api/profile/switch'")
        opt_name_idx = fn.find("_chipLabel.textContent = name")
        assert opt_name_idx != -1, "No optimistic name update found."
        assert opt_name_idx < api_call_idx, (
            "Optimistic name update must happen BEFORE the API call."
        )

    def test_chip_disabled_during_switch(self):
        """Chip must be disabled to prevent double-clicks."""
        fn = self._get_switch_fn()
        assert "_chip.disabled = true" in fn, (
            "switchToProfile() does not disable the chip."
        )
        finally_idx = fn.find("} finally {")
        assert finally_idx != -1
        assert "_chip.disabled = false" in fn[finally_idx:], (
            "The finally block does not re-enable the chip."
        )

    def test_error_reverts_chip_label_to_previous_name(self):
        """On error, the chip label must revert to the previous name."""
        fn = self._get_switch_fn()
        catch_idx = fn.find("} catch (e) {")
        assert catch_idx != -1
        assert "_prevProfileName" in fn[catch_idx:], (
            "The catch block does not restore _prevProfileName."
        )


class TestParallelizedFetches:
    """Verify that background refresh work does not block visible profile switching."""

    JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    def _get_switch_fn(self):
        idx = self.JS.find("async function switchToProfile(name) {")
        assert idx != -1
        depth = 0
        for i, ch in enumerate(self.JS[idx:], idx):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.JS[idx: i + 1]
        raise AssertionError("Could not extract switchToProfile")

    def test_workspace_refresh_in_background_and_model_catalog_lazy(self):
        """Workspace refresh should run behind completion; model catalog waits for picker open."""
        fn = self._get_switch_fn()
        assert "_refreshProfileSwitchBackground(_switchGen)" in fn, (
            "switchToProfile() must schedule non-visible refreshes after the switch."
        )
        assert "window._modelDropdownReady=null" in self.JS
        assert "await Promise.all([populateModelDropdown(), loadWorkspaceList()])" not in fn, (
            "Profile switching still awaits full model/workspace catalog refreshes."
        )

    def test_no_sequential_await_pattern(self):
        """The old sequential await pattern must be gone."""
        fn = self._get_switch_fn()
        sequential = re.search(
            r"await populateModelDropdown\(\)\s*;\s*\n\s*await loadWorkspaceList",
            fn
        )
        assert not sequential, (
            "Old sequential await pattern still present — both fetches would run twice."
        )

    def test_apply_steps_after_promise_all(self):
        """Model defaults must apply before background catalog refresh starts."""
        fn = self._get_switch_fn()
        background_idx = fn.find("_refreshProfileSwitchBackground(_switchGen)")
        apply_model_idx = fn.find("S._pendingProfileModel = modelToUse")
        assert apply_model_idx != -1
        assert background_idx != -1
        assert apply_model_idx < background_idx, (
            "Model defaults must apply before background refresh starts."
        )
        assert "existingDefaultOpt.dataset.provider = providerId" in fn

    def test_workspace_load_is_awaited_only_when_visible(self):
        """Profile switches should not duplicate workspace-tree loads."""
        fn = self._get_switch_fn()
        assert "awaitWorkspaceLoad: workspaceVisible" in fn
        sessions_js = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
        assert "if(options&&options.awaitWorkspaceLoad) await dirLoad" in sessions_js
        assert "loadDir('.') is fire-and-forget while the workspace panel is closed" in sessions_js


class TestSpinnerCss:
    """Verify the spinner CSS class is defined correctly."""

    CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_switching_class_defined(self):
        assert ".composer-profile-chip.switching" in self.CSS

    def test_switching_class_has_cursor_wait(self):
        idx = self.CSS.find(".composer-profile-chip.switching")
        assert idx != -1
        block = self.CSS[idx: idx + 200]
        assert "cursor:wait" in block

    def test_switching_class_has_pointer_events_none(self):
        idx = self.CSS.find(".composer-profile-chip.switching")
        assert idx != -1
        block = self.CSS[idx: idx + 200]
        assert "pointer-events:none" in block


class TestProfileSessionListFlip:
    """Verify session-list refreshes use row-level FLIP motion."""

    JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_profile_refresh_captures_row_positions(self):
        assert "function _captureSessionListFlipPositions()" in self.JS
        start = self.JS.index("function _captureSessionListFlipPositions()")
        end = self.JS.index("function _sessionListPrefersReducedMotion()", start)
        fn = self.JS[start:end]
        assert "querySelectorAll('.session-item[data-sid]')" in fn
        assert "positions.set(row.dataset.sid,row.getBoundingClientRect().top);" in fn

    def test_profile_refresh_reflows_existing_rows(self):
        assert "function _playSessionListFlipAnimation(before)" in self.JS
        start = self.JS.index("function _playSessionListFlipAnimation(before)")
        end = self.JS.index("function _isOptimisticFirstTurnSessionRow(s)", start)
        fn = self.JS[start:end]
        assert "const delta=oldTop-row.getBoundingClientRect().top;" in fn
        assert "row.style.setProperty('--session-reflow-offset',delta+'px');" in fn
        assert "row.classList.add('session-reflowing');" in fn

    def test_profile_refresh_flips_new_rows(self):
        assert "session-list-flip-enter" in self.JS
        assert "@keyframes sessionListFlipIn" in self.CSS
        assert "rotateX" in self.CSS

    def test_profile_refresh_captures_before_render_and_plays_after_rows_exist(self):
        capture = self.JS.index("const flipBefore=animateRefresh?_captureSessionListFlipPositions():null;")
        clear = self.JS.index("list.innerHTML='';", capture)
        row_render = self.JS.index("body.appendChild(_renderOneSession", clear)
        play = self.JS.index("_playSessionListFlipAnimation(flipBefore);", row_render)

        assert capture < clear < row_render < play

    def test_first_non_empty_session_render_is_animated(self):
        assert "_sessionListFirstRenderAnimated" in self.JS
        assert "animateNextSessionListRefresh({enterAll:true});" in self.JS
        assert "_sessionListFirstRenderAnimated=true;" in self.JS
        assert "if(S&&S._bootReady) _sessionListFirstRenderAnimated=true;" not in self.JS
        assert "enterAllAnimatedRows" in self.JS

    def test_profile_refresh_is_not_whole_list_fade(self):
        assert "session-list.profile-refresh" not in self.CSS
