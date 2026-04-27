"""
Regression test for #1169 — Live model race condition resets session model.

When a user's session model exists only in the live model list (e.g. Kimi K2.6)
and NOT in the static catalog, the following race condition could corrupt the
session model:

  1. loadSession() populates dropdown with static models only
  2. _fetchLiveModels() starts async (takes 100-500ms)
  3. syncTopbar() fires, sees session model not in dropdown, resets to first
     static model and PERSISTS the incorrect value to the backend
  4. _fetchLiveModels() completes — too late, model is already corrupted

Fix: _liveModelFetchPending flag prevents syncTopbar() from persisting when
a live fetch is in flight. _reapplySessionModelIfFound() corrects any premature
visual reset once live models are loaded.

Sprint/commit: v0.50.227+
"""
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


class TestLiveModelRaceCondition1169(unittest.TestCase):
    """Verify the _liveModelFetchPending guard exists and is correctly wired."""

    # ── Flag declaration ────────────────────────────────────────────────

    def test_live_model_fetch_pending_declared(self):
        """_liveModelFetchPending must be declared as an object."""
        self.assertIn(
            "_liveModelFetchPending", UI_JS,
            "ui.js must declare _liveModelFetchPending to track in-flight fetches (#1169)"
        )
        # Must be an object (const x={})
        idx = UI_JS.find("_liveModelFetchPending")
        snippet = UI_JS[idx:idx + 60]
        self.assertIn("={}", snippet,
                      "_liveModelFetchPending should be initialised as an empty object")

    # ── _fetchLiveModels sets/clears flag ───────────────────────────────

    def test_fetch_live_models_sets_flag_before_fetch(self):
        """_fetchLiveModels must set _liveModelFetchPending[key]=true before the
        network request so syncTopbar() can detect the in-flight fetch."""
        func_start = UI_JS.find("async function _fetchLiveModels")
        self.assertGreater(func_start, -1, "_fetchLiveModels function must exist")

        # The flag must be set BEFORE the fetch call
        flag_set = UI_JS.find("_liveModelFetchPending[", func_start)
        fetch_call = UI_JS.find("fetch(", func_start)
        self.assertGreater(flag_set, -1,
                           "_fetchLiveModels must reference _liveModelFetchPending")
        self.assertLess(flag_set, fetch_call,
                        "_liveModelFetchPending must be set BEFORE the fetch() call")

    def test_fetch_live_models_clears_flag_in_finally(self):
        """The flag must be cleared in a finally block so it's always cleaned up,
        even if the fetch throws or the response is unauthorised."""
        func_start = UI_JS.find("async function _fetchLiveModels")
        # Look for delete in a finally block
        finally_idx = UI_JS.find("finally{", func_start)
        self.assertGreater(finally_idx, -1,
                           "_fetchLiveModels must have a finally block to clear the flag")
        # End of the finally block (closing brace of the function)
        finally_block = UI_JS[finally_idx:finally_idx + 200]
        self.assertIn("delete _liveModelFetchPending[", finally_block,
                      "finally block must delete _liveModelFetchPending[key]")

    # ── syncTopbar defers persistence when flag is set ─────────────────

    def test_sync_topbar_checks_live_fetch_pending(self):
        """syncTopbar must check _liveModelFetchPending before persisting a model
        reset to the backend."""
        # Find the model-not-found branch in syncTopbar
        sync_start = UI_JS.find("function syncTopbar")
        self.assertGreater(sync_start, -1, "syncTopbar function must exist")

        # Find the deferred model correction block
        defer_idx = UI_JS.find("deferModelCorrection", sync_start)
        self.assertGreater(defer_idx, -1,
                           "syncTopbar must reference deferModelCorrection")

        # _liveModelFetchPending must be checked nearby
        live_fetch_idx = UI_JS.find("_liveModelFetchPending[", sync_start)
        self.assertGreater(live_fetch_idx, -1,
                           "syncTopbar must check _liveModelFetchPending (#1169)")

        # The check must be inside the model-not-found branch (after deferModelCorrection)
        self.assertGreater(live_fetch_idx, defer_idx,
                           "_liveModelFetchPending check must be near deferModelCorrection")

        # The persist guard must use && !liveFetchPending
        persist_block = UI_JS[defer_idx:live_fetch_idx + 800]
        # The guard condition that prevents persisting
        self.assertIn("!liveFetchPending", persist_block,
                      "Persist guard must include !liveFetchPending condition (#1169)")

    # ── _reapplySessionModelIfFound exists and is called ───────────────

    def test_reapply_session_model_function_exists(self):
        """_reapplySessionModelIfFound must exist to correct premature resets."""
        self.assertIn(
            "function _reapplySessionModelIfFound", UI_JS,
            "ui.js must define _reapplySessionModelIfFound to restore correct model (#1169)"
        )

    def test_reapply_called_after_adding_live_models(self):
        """_reapplySessionModelIfFound must be called in both the cache-hit and
        network-fetch paths of _fetchLiveModels."""
        func_start = UI_JS.find("async function _fetchLiveModels")
        self.assertGreater(func_start, -1)

        # Count occurrences of _reapplySessionModelIfFound in _fetchLiveModels body
        func_end = UI_JS.find("\n}", UI_JS.find("finally{", func_start) + 10)
        func_body = UI_JS[func_start:func_end]

        call_count = func_body.count("_reapplySessionModelIfFound")
        self.assertGreaterEqual(call_count, 2,
            "_reapplySessionModelIfFound must be called in both cache-hit and "
            "network-fetch paths of _fetchLiveModels")

    # ── End-to-end: flag lifecycle ─────────────────────────────────────

    def test_flag_set_and_cleared_around_fetch(self):
        """Integration: the flag must be set before fetch and cleared in finally,
        with _reapplySessionModelIfFound called inside the try block."""
        func_start = UI_JS.find("async function _fetchLiveModels")
        try_idx = UI_JS.find("try{", func_start)
        finally_idx = UI_JS.find("finally{", func_start)
        func_end = UI_JS.find("\n}", finally_idx + 10) + 2
        func_body = UI_JS[func_start:func_end]

        # Flag set between function start and try block (or at start of try)
        flag_set_before_try = func_body.find("_liveModelFetchPending[", 0, try_idx - func_start + 100)
        self.assertGreater(flag_set_before_try, -1,
                           "Flag must be set before or at start of try block")

        # _reapplySessionModelIfFound inside try block
        reapply_in_try = func_body.find("_reapplySessionModelIfFound", try_idx - func_start, finally_idx - func_start)
        self.assertGreater(reapply_in_try, -1,
                           "_reapplySessionModelIfFound must be called inside try block")

        # Flag cleared in finally
        flag_clear_in_finally = func_body.find("delete _liveModelFetchPending", finally_idx - func_start)
        self.assertGreater(flag_clear_in_finally, -1,
                           "Flag must be cleared in finally block")
