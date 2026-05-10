"""
Tests for #660: session queue persistence across page refresh.

The queue is stored to sessionStorage when entries are added/removed,
and restored from sessionStorage on session load when the agent is idle.
"""

import pathlib

UI_JS = pathlib.Path(__file__).parent.parent / "static" / "ui.js"
SESSIONS_JS = pathlib.Path(__file__).parent.parent / "static" / "sessions.js"

ui_src = UI_JS.read_text(encoding="utf-8")
sess_src = SESSIONS_JS.read_text(encoding="utf-8")


class TestQueuePersistence:
    """queueSessionMessage persists to sessionStorage."""

    def test_queue_writes_to_session_storage(self):
        """queueSessionMessage must write to sessionStorage after enqueueing."""
        assert "sessionStorage.setItem('hermes-queue-'+sid" in ui_src

    def test_queue_stamps_queued_at_timestamp(self):
        """Each queue entry must have a _queued_at timestamp for stale-entry detection."""
        assert "_queued_at" in ui_src

    def test_shift_removes_from_session_storage(self):
        """shiftQueuedSessionMessage must remove/update sessionStorage on dequeue."""
        assert "sessionStorage.removeItem('hermes-queue-'+sid)" in ui_src

    def test_shift_updates_session_storage_when_items_remain(self):
        """When queue still has items after shift, sessionStorage is updated (not removed)."""
        # After shift: if queue still has items, update storage with remaining
        assert (
            "sessionStorage.setItem('hermes-queue-'+sid, JSON.stringify(q))" in ui_src
        )
        # Counts: should appear in both add and update paths (2 occurrences minimum)
        count = ui_src.count("sessionStorage.setItem('hermes-queue-'+sid")
        assert count >= 2, f"Expected >=2 sessionStorage.setItem calls, found {count}"


class TestQueueRestore:
    """Queue is restored from sessionStorage on session load when agent is idle."""

    def test_restore_reads_session_storage(self):
        """sessions.js must read from sessionStorage in the idle-session load path."""
        assert "sessionStorage.getItem('hermes-queue-'+sid)" in sess_src

    def test_restore_uses_timestamp_guard(self):
        """Stale entries (created before last assistant response) must be dropped."""
        assert "_queued_at" in sess_src
        assert "_lastAsst" in sess_src

    def test_restore_shows_toast(self):
        """User must see a toast notification when a queue is restored."""
        assert "queued message" in sess_src.lower() and "restored" in sess_src.lower()

    def test_restore_puts_text_in_composer(self):
        """First queued message goes into the composer input, not auto-sent."""
        assert "_msg.value=_first.text" in sess_src

    def test_restore_clears_stale_storage(self):
        """On timestamp mismatch, stale sessionStorage entry is removed."""
        assert "sessionStorage.removeItem('hermes-queue-'+sid)" in sess_src

    def test_restore_wrapped_in_try_catch(self):
        """sessionStorage access must be wrapped in try/catch (private browsing may block it)."""
        # The restore block must have a catch that clears the bad key
        assert "catch(_){sessionStorage.removeItem" in sess_src

    def test_active_session_not_restored_as_draft(self):
        """When agent is active (INFLIGHT), queue restore must NOT run."""
        # The restore block must be inside the else branch (idle path), not the INFLIGHT branch
        inflight_pos = sess_src.find("if(INFLIGHT[sid]){")
        restore_pos = sess_src.find("sessionStorage.getItem('hermes-queue-'")
        else_pos = sess_src.find("}else{", inflight_pos)
        assert restore_pos > else_pos, (
            "Queue restore must be inside the else (idle) branch, not the INFLIGHT branch"
        )
