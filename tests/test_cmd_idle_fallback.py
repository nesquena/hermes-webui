"""Regression tests for idle-state fallback on /queue, /interrupt, /steer.

When the agent is idle (S.busy=false, S.activeStreamId=null), these commands
previously showed an error toast instead of sending the message. They now
fall through to a direct send() call, matching CLI behaviour:

  - /queue msg  → send when idle, queue when busy
  - /interrupt msg → send when idle, cancel+requeue when busy+streaming
  - /steer msg  → send when idle, inject mid-turn when busy+streaming
"""
import re
import pathlib

COMMANDS_JS = (pathlib.Path(__file__).parent.parent / "static" / "commands.js").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Source-level structural checks
# ─────────────────────────────────────────────────────────────────────────────

class TestIdleFallbackStructure:
    """Each handler must contain an idle-path that calls send() instead of
    showing an error toast."""

    def _get_function_body(self, fn_name: str, window: int = 800) -> str:
        idx = COMMANDS_JS.find(f"async function {fn_name}(")
        assert idx >= 0, f"{fn_name} not found in commands.js"
        return COMMANDS_JS[idx: idx + window]

    # /queue ──────────────────────────────────────────────────────────────────

    def test_queue_idle_path_calls_send(self):
        """cmdQueue must call send() when !S.busy (idle)."""
        body = self._get_function_body("cmdQueue")
        assert "send()" in body, (
            "cmdQueue must call send() when idle instead of showing an error toast"
        )

    def test_queue_idle_path_sets_input_value(self):
        """cmdQueue must populate the message input before calling send()."""
        body = self._get_function_body("cmdQueue")
        assert "inp.value=msg" in body or "inp.value = msg" in body, (
            "cmdQueue must set input value before calling send()"
        )

    def test_queue_idle_path_not_toast_only(self):
        """cmdQueue must NOT show cmd_queue_not_busy toast as its only idle action."""
        body = self._get_function_body("cmdQueue")
        # The old code returned after a toast; new code returns after send().
        # The toast key may still be in the file for other reasons but must not
        # be the only thing that happens when !S.busy.
        idle_branch_start = body.find("if(!S.busy)")
        assert idle_branch_start >= 0, "cmdQueue must have an if(!S.busy) branch"
        idle_branch = body[idle_branch_start: idle_branch_start + 300]
        assert "send()" in idle_branch, (
            "cmdQueue's idle branch must call send(), not just show a toast"
        )

    # /interrupt ──────────────────────────────────────────────────────────────

    def test_interrupt_idle_path_calls_send(self):
        """cmdInterrupt must call send() when idle (!S.busy || !S.activeStreamId)."""
        body = self._get_function_body("cmdInterrupt")
        assert "send()" in body, (
            "cmdInterrupt must call send() when idle instead of showing an error toast"
        )

    def test_interrupt_idle_path_sets_input_value(self):
        body = self._get_function_body("cmdInterrupt")
        assert "inp.value=msg" in body or "inp.value = msg" in body

    def test_interrupt_idle_branch_exists(self):
        body = self._get_function_body("cmdInterrupt")
        # Either !S.busy||!S.activeStreamId or !S.busy block with send()
        has_idle = "!S.busy||!S.activeStreamId" in body or "!S.busy" in body
        assert has_idle, "cmdInterrupt must have an idle guard"
        idle_start = body.find("!S.busy")
        assert "send()" in body[idle_start: idle_start + 350], (
            "cmdInterrupt idle branch must call send()"
        )

    # /steer ──────────────────────────────────────────────────────────────────

    def test_steer_idle_path_calls_send(self):
        """cmdSteer must call send() when idle (!S.busy || !S.activeStreamId)."""
        body = self._get_function_body("cmdSteer")
        assert "send()" in body, (
            "cmdSteer must call send() when idle instead of showing an error toast"
        )

    def test_steer_idle_path_sets_input_value(self):
        body = self._get_function_body("cmdSteer")
        assert "inp.value=msg" in body or "inp.value = msg" in body

    def test_steer_idle_branch_before_trySteer(self):
        """The idle fallback must appear BEFORE the _trySteer call so steer text
        is sent normally when there is nothing to steer."""
        body = self._get_function_body("cmdSteer")
        idle_idx = body.find("!S.busy")
        steer_idx = body.find("_trySteer")
        assert idle_idx >= 0, "cmdSteer must have an idle guard"
        assert steer_idx >= 0, "cmdSteer must call _trySteer for active sessions"
        assert idle_idx < steer_idx, (
            "Idle fallback must come before _trySteer — otherwise steer text is "
            "sent to the endpoint even when there is no active stream"
        )

    # Old error paths removed ─────────────────────────────────────────────────

    def test_queue_no_longer_toasts_only_when_idle(self):
        """The old `showToast(t('cmd_queue_not_busy')); return` should not be the
        sole handler for the idle case — that was the bug."""
        body = self._get_function_body("cmdQueue")
        idle_idx = body.find("if(!S.busy)")
        assert idle_idx >= 0
        idle_block = body[idle_idx: idle_idx + 250]
        # send() must appear in the idle block
        assert "send()" in idle_block, (
            "cmdQueue idle block must contain send(), not just a toast+return"
        )

    def test_steer_no_longer_calls_no_active_task_toast_when_idle(self):
        """cmdSteer must not show 'no_active_task' toast as its response to being
        called while idle — that wording implied steer cancels a task, which it
        does not."""
        body = self._get_function_body("cmdSteer")
        idle_idx = body.find("!S.busy")
        assert idle_idx >= 0
        idle_block = body[idle_idx: idle_idx + 250]
        assert "no_active_task" not in idle_block, (
            "cmdSteer idle path must not show 'no_active_task' — steer doesn't "
            "stop tasks, and when idle it should send normally"
        )


class TestBusyPathsStillWork:
    """Active-session paths must be unchanged — guards are preserved."""

    def _get_function_body(self, fn_name: str, window: int = 800) -> str:
        idx = COMMANDS_JS.find(f"async function {fn_name}(")
        assert idx >= 0
        return COMMANDS_JS[idx: idx + window]

    def test_queue_still_queues_when_busy(self):
        """When S.busy, cmdQueue must still call queueSessionMessage."""
        body = self._get_function_body("cmdQueue")
        assert "queueSessionMessage" in body, (
            "cmdQueue must still queue messages when S.busy is true"
        )

    def test_interrupt_still_cancels_when_busy(self):
        """When S.busy && S.activeStreamId, cmdInterrupt must still call cancelStream."""
        body = self._get_function_body("cmdInterrupt", window=1200)
        assert "cancelStream" in body

    def test_steer_still_calls_trySteer_when_busy(self):
        """When S.busy && S.activeStreamId, cmdSteer must still call _trySteer."""
        body = self._get_function_body("cmdSteer")
        assert "_trySteer" in body

    def test_stop_command_unchanged(self):
        """cmdStop still uses no_active_task toast — that's correct for /stop."""
        idx = COMMANDS_JS.find("async function cmdStop(")
        body = COMMANDS_JS[idx: idx + 400]
        assert "no_active_task" in body, (
            "/stop should still show 'no active task' when idle — stopping nothing is an error"
        )
