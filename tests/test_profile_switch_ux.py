"""
Tests for profile-switch UX improvements — spinner indicator + parallelized fetches.

Two changes:
1. switchToProfile() shows a spinner on the profile chip during the async switch,
   with an optimistic name update and error revert.
2. populateModelDropdown() and loadWorkspaceList() are now parallelized via Promise.all
   instead of sequential awaits.
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
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.JS[idx : i + 1]
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
    """Verify that model and workspace fetches are parallelized."""

    JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    def _get_switch_fn(self):
        idx = self.JS.find("async function switchToProfile(name) {")
        assert idx != -1
        depth = 0
        for i, ch in enumerate(self.JS[idx:], idx):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return self.JS[idx : i + 1]
        raise AssertionError("Could not extract switchToProfile")

    def test_populate_and_workspace_in_promise_all(self):
        """Both fetches must be inside Promise.all([...])."""
        fn = self._get_switch_fn()
        assert "Promise.all([populateModelDropdown(), loadWorkspaceList()])" in fn, (
            "populateModelDropdown() and loadWorkspaceList() are not parallelized."
        )

    def test_no_sequential_await_pattern(self):
        """The old sequential await pattern must be gone."""
        fn = self._get_switch_fn()
        sequential = re.search(
            r"await populateModelDropdown\(\)\s*;\s*\n\s*await loadWorkspaceList", fn
        )
        assert not sequential, (
            "Old sequential await pattern still present — both fetches would run twice."
        )

    def test_apply_steps_after_promise_all(self):
        """Model apply step must come after Promise.all resolves."""
        fn = self._get_switch_fn()
        promise_all_idx = fn.find("await Promise.all(")
        apply_model_idx = fn.find("S._pendingProfileModel = modelToUse")
        assert apply_model_idx != -1
        assert apply_model_idx > promise_all_idx, (
            "Model apply step must come AFTER Promise.all resolves."
        )


class TestSpinnerCss:
    """Verify the spinner CSS class is defined correctly."""

    CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")

    def test_switching_class_defined(self):
        assert ".composer-profile-chip.switching" in self.CSS

    def test_switching_class_has_cursor_wait(self):
        idx = self.CSS.find(".composer-profile-chip.switching")
        assert idx != -1
        block = self.CSS[idx : idx + 200]
        assert "cursor:wait" in block

    def test_switching_class_has_pointer_events_none(self):
        idx = self.CSS.find(".composer-profile-chip.switching")
        assert idx != -1
        block = self.CSS[idx : idx + 200]
        assert "pointer-events:none" in block
