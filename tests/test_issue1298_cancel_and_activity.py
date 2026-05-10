"""Regression tests for #1298 — Activity panel UI state and Stop/Cancel data loss.

Two distinct bugs reported in YanTianlong-01's bug report on v0.50.240:

  1. The expanded Activity list collapses automatically when new activity arrives.
  2. The latest user message disappears after clicking Stop/Cancel during streaming.

Bug 2 is server-side data loss (the message is gone from session JSON, not just
the in-memory client copy) caused by cancel_stream() clearing pending_user_message
without first persisting it to s.messages. This test suite locks down both fixes.
"""

import pathlib
import queue
import re
import threading
from unittest.mock import Mock

import pytest

import api.config as config
import api.models as models
from api.models import Session
from api.streaming import cancel_stream

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_session_dir(tmp_path, monkeypatch):
    """Redirect SESSION_DIR / SESSION_INDEX_FILE to an isolated temp dir."""
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    index_file = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_file)
    models.SESSIONS.clear()
    yield
    models.SESSIONS.clear()


@pytest.fixture(autouse=True)
def _isolate_stream_state():
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()
    yield
    config.STREAMS.clear()
    config.CANCEL_FLAGS.clear()
    config.AGENT_INSTANCES.clear()
    config.STREAM_PARTIAL_TEXT.clear()


@pytest.fixture(autouse=True)
def _isolate_agent_locks():
    config.SESSION_AGENT_LOCKS.clear()
    yield
    config.SESSION_AGENT_LOCKS.clear()


def _make_pending_session(
    session_id="cancel_sid_1298",
    pending_msg="Help me debug this issue",
    messages=None,
    attachments=None,
):
    """Build a session in mid-stream state: pending_user_message set, messages may be empty."""
    s = Session(
        session_id=session_id,
        title="Test Session",
        messages=messages or [],
    )
    s.pending_user_message = pending_msg
    s.pending_attachments = list(attachments or [])
    s.pending_started_at = None
    s.active_stream_id = "stream_1298"
    s.save()
    models.SESSIONS[session_id] = s
    return s


def _setup_cancel_stream_state(session_id, stream_id="stream_1298"):
    """Wire up STREAMS/CANCEL_FLAGS/AGENT_INSTANCES so cancel_stream() can run."""
    config.STREAMS[stream_id] = queue.Queue()
    config.CANCEL_FLAGS[stream_id] = threading.Event()
    mock_agent = Mock()
    mock_agent.session_id = session_id
    mock_agent.interrupt = Mock()
    config.AGENT_INSTANCES[stream_id] = mock_agent
    return stream_id, mock_agent


# ── Server-side: cancel preserves pending_user_message in s.messages ────────


class TestIssue1298CancelPreservesUserMessage:
    """Issue 2: Latest user message disappears after Stop/Cancel during streaming.

    Root cause: cancel_stream() at api/streaming.py:2575+ clears
    s.pending_user_message before the streaming thread's
    _merge_display_messages_after_agent_result() has a chance to merge the
    user turn into s.messages. The session is saved with neither
    pending_user_message nor a corresponding s.messages entry, so the user's
    typed text is lost permanently.

    Fix: synthesize a user turn from pending_user_message into s.messages when
    the most recent message isn't already that turn.
    """

    def test_cancel_synthesizes_user_message_when_messages_empty(self):
        """When the agent thread is killed before it can append the user turn,
        cancel_stream() must persist pending_user_message into s.messages so
        the typed text survives a session reload."""
        s = _make_pending_session(
            session_id="cancel_sid_empty",
            pending_msg="What's the weather forecast?",
            messages=[],
        )
        stream_id, _agent = _setup_cancel_stream_state(s.session_id)

        result = cancel_stream(stream_id)
        assert result is True

        # Reload from disk to confirm save happened
        s2 = models.SESSIONS[s.session_id]
        roles = [m.get("role") for m in s2.messages if isinstance(m, dict)]
        contents = [m.get("content") for m in s2.messages if isinstance(m, dict)]

        assert "user" in roles, (
            f"Expected user turn synthesized into s.messages — got roles={roles}"
        )
        assert "What's the weather forecast?" in contents, (
            "Expected pending_user_message text preserved verbatim in s.messages — "
            f"got contents={contents}"
        )
        assert s2.pending_user_message is None, (
            "pending_user_message must be cleared after cancel"
        )
        assert s2.active_stream_id is None

    def test_cancel_does_not_double_append_when_streaming_thread_already_merged(self):
        """If the streaming thread won the race and already merged the user turn
        into s.messages before cancel_stream() got the lock, cancel must not
        append a duplicate."""
        prior_user = {"role": "user", "content": "Run a tool for me"}
        s = _make_pending_session(
            session_id="cancel_sid_already_merged",
            pending_msg="Run a tool for me",
            messages=[prior_user],
        )
        stream_id, _agent = _setup_cancel_stream_state(s.session_id)

        cancel_stream(stream_id)

        s2 = models.SESSIONS[s.session_id]
        user_messages = [
            m for m in s2.messages if isinstance(m, dict) and m.get("role") == "user"
        ]
        # Exactly one user turn — no duplicate
        matching = [
            m
            for m in user_messages
            if "Run a tool for me" in str(m.get("content") or "")
        ]
        assert len(matching) == 1, (
            "Expected exactly one user turn matching pending_user_message — "
            f"got {len(matching)} ({user_messages})"
        )

    def test_cancel_synthesized_user_message_carries_attachments(self):
        """A cancelled turn that had attachments uploaded should keep them on
        the recovered user message."""
        s = _make_pending_session(
            session_id="cancel_sid_attachments",
            pending_msg="Look at this screenshot",
            messages=[],
            attachments=["bug_screenshot.png", "stack_trace.txt"],
        )
        stream_id, _agent = _setup_cancel_stream_state(s.session_id)

        cancel_stream(stream_id)

        s2 = models.SESSIONS[s.session_id]
        user_msgs = [
            m for m in s2.messages if isinstance(m, dict) and m.get("role") == "user"
        ]
        assert user_msgs, "User turn must be persisted on cancel"
        recovered = user_msgs[0]
        assert recovered.get("attachments") == [
            "bug_screenshot.png",
            "stack_trace.txt",
        ], (
            "Attachment list must be preserved on the synthesized user turn — "
            f"got {recovered.get('attachments')}"
        )

    def test_cancel_no_pending_user_message_does_nothing_extra(self):
        """When there is no pending_user_message (e.g. cancel after the agent
        has already returned), cancel_stream() must not synthesize a phantom
        user turn."""
        s = Session(
            session_id="cancel_sid_no_pending",
            title="Test",
            messages=[{"role": "user", "content": "earlier turn"}],
        )
        s.active_stream_id = "stream_1298"
        s.pending_user_message = None
        s.save()
        models.SESSIONS[s.session_id] = s
        stream_id, _agent = _setup_cancel_stream_state(s.session_id)

        cancel_stream(stream_id)

        s2 = models.SESSIONS[s.session_id]
        user_messages = [
            m for m in s2.messages if isinstance(m, dict) and m.get("role") == "user"
        ]
        # Still exactly one — the original earlier turn
        assert len(user_messages) == 1
        assert user_messages[0].get("content") == "earlier turn"

    def test_cancel_synthesizes_when_prior_turn_content_is_substring_of_pending(self):
        """Regression for Opus pre-release review of v0.50.246 (PR #1338):

        The substring guard in cancel_stream() was symmetric — it would skip
        synthesis if the prior user turn's content was a substring of the new
        pending message. Common confirmation replies ("ok", "yes", "go") would
        match longer follow-up prompts ("ok please continue") and the
        synthesis would be skipped, re-introducing the data-loss bug.

        The fix: gate the substring check on a timestamp comparison —
        only treat the latest user turn as "already merged by the streaming
        thread" if its timestamp is at or after pending_started_at. Earlier
        turns whose content happens to be a substring must not short-circuit
        the synthesis path.
        """
        import time as _time

        # Prior reply was "ok" (a common short reply).
        prior_ts = int(_time.time()) - 60  # 1 minute ago
        prior_user = {
            "role": "user",
            "content": "ok",
            "timestamp": prior_ts,
        }
        s = _make_pending_session(
            session_id="cancel_sid_substring_collision",
            pending_msg="ok please continue with the analysis",
            messages=[prior_user],
        )
        # The pending turn started AFTER the prior turn was logged.
        s.pending_started_at = prior_ts + 10
        s.save()
        models.SESSIONS[s.session_id] = s

        stream_id, _agent = _setup_cancel_stream_state(s.session_id)
        cancel_stream(stream_id)

        s2 = models.SESSIONS[s.session_id]
        user_messages = [
            m for m in s2.messages if isinstance(m, dict) and m.get("role") == "user"
        ]
        contents = [m.get("content") for m in user_messages]

        assert "ok please continue with the analysis" in contents, (
            "Pending user message must be synthesized — the substring 'ok' from a prior turn "
            "must NOT cause the synthesis to be skipped. "
            f"Got contents={contents}"
        )
        assert len(user_messages) == 2, (
            "Expected both the original prior turn AND the synthesized new turn — "
            f"got {len(user_messages)} user messages"
        )


# ── Client-side: ui.js source-level guards for activity-group state ─────────


class TestIssue1298ActivityGroupExpandPersistence:
    """Issue 1: Expanded Activity list collapses automatically when new
    activity arrives.

    Root cause:
      - ensureActivityGroup() (static/ui.js) creates the live activity group
        with `tool-call-group-collapsed` whenever it's missing
      - finalizeThinkingCard() force-adds `tool-call-group-collapsed` on every
        tool boundary, regardless of user intent
      - The user's manually-set expand state lives only on a DOM class list,
        so any destroy/recreate cycle (which fires on every thinking → tool →
        thinking transition) wipes it.

    Fix: track the user's last explicit toggle in a per-turn singleton, and
    skip the force-collapse when the user has explicitly expanded.
    """

    def test_ui_js_tracks_user_expand_intent_for_live_activity_group(self):
        src = (REPO_ROOT / "static" / "ui.js").read_text()
        assert "_liveActivityUserExpanded" in src, (
            "ui.js must declare a per-turn tracker for the user's expand intent "
            "on the live activity group (#1298)"
        )
        assert "_onLiveActivityToggle" in src, (
            "ui.js must expose a helper that records the user's manual toggle "
            "of the live activity group"
        )

    def test_ensure_activity_group_restores_expand_intent(self):
        """ensureActivityGroup() must consult _liveActivityUserExpanded when
        creating a fresh live group so the user's prior expand survives the
        destroy/recreate cycle."""
        src = (REPO_ROOT / "static" / "ui.js").read_text()
        # Find the ensureActivityGroup function body
        m = re.search(
            r"function ensureActivityGroup\(inner, opts\)\{(.*?)\n\}",
            src,
            re.DOTALL,
        )
        assert m, "ensureActivityGroup() must exist in ui.js"
        body = m.group(1)
        assert "_liveActivityUserExpanded" in body, (
            "ensureActivityGroup() body must reference the user-expand tracker "
            "to restore intent on re-create (#1298)"
        )
        assert "live" in body and "_liveActivityUserExpanded === true" in body, (
            "ensureActivityGroup() must override the default `collapsed` flag "
            "when the user previously expanded the live group"
        )

    def test_finalize_thinking_card_respects_user_expand(self):
        """finalizeThinkingCard() must NOT force-collapse the live activity
        group when the user has explicitly expanded it (#1298)."""
        src = (REPO_ROOT / "static" / "ui.js").read_text()
        m = re.search(
            r"function finalizeThinkingCard\(\)\{(.*?)\n\}",
            src,
            re.DOTALL,
        )
        assert m, "finalizeThinkingCard() must exist in ui.js"
        body = m.group(1)
        assert "_liveActivityUserExpanded" in body, (
            "finalizeThinkingCard() must respect the user's expand intent — "
            "without this guard, the panel snaps shut on every tool boundary"
        )
        # Hard fail if force-collapse is unconditional
        assert (
            "_liveActivityUserExpanded !== true" in body
            or "_liveActivityUserExpanded!==true" in body.replace(" ", "")
        ), (
            "finalizeThinkingCard() must skip the force-collapse path when "
            "_liveActivityUserExpanded === true"
        )

    def test_inline_onclick_records_user_intent(self):
        """The summary button's click path must call _onLiveActivityToggle
        so user clicks update the tracker (#1298)."""
        src = (REPO_ROOT / "static" / "ui.js").read_text()
        # The summary button is built inline inside ensureActivityGroup.
        assert "_onLiveActivityToggle" in src, (
            "_onLiveActivityToggle helper must be defined"
        )
        assert "function _toggleActivityGroup" in src, (
            "Activity summary clicks should route through the shared toggle helper"
        )
        # The inline onclick may delegate to _toggleActivityGroup(); that helper
        # must still call _onLiveActivityToggle(group) so user toggles are
        # captured into _liveActivityUserExpanded.
        m = re.search(r'class="tool-call-group-summary"[^`]*`', src)
        assert m, "live activity summary button template must be present"
        assert 'onclick="_toggleActivityGroup(this)"' in m.group(0), (
            "ensureActivityGroup() summary button should use the shared toggle helper"
        )
        toggle_body = re.search(
            r"function _toggleActivityGroup\(summary\)\{(.*?)\n\}",
            src,
            re.DOTALL,
        )
        assert toggle_body and "_onLiveActivityToggle(group)" in toggle_body.group(1), (
            "_toggleActivityGroup() must invoke _onLiveActivityToggle(group) "
            "so user clicks update the tracker"
        )

    def test_clear_live_tool_cards_resets_expand_intent(self):
        """clearLiveToolCards() — invoked between turns — must reset the
        per-turn user-expand tracker so the next turn starts collapsed by
        default (#1298)."""
        src = (REPO_ROOT / "static" / "ui.js").read_text()
        m = re.search(
            r"function clearLiveToolCards\(\)\{(.*?)\n\}",
            src,
            re.DOTALL,
        )
        assert m, "clearLiveToolCards() must exist"
        body = m.group(1)
        assert "_clearLiveActivityUserIntent" in body, (
            "clearLiveToolCards() must reset _liveActivityUserExpanded between "
            "turns so prior expand intent doesn't bleed into the next turn"
        )
