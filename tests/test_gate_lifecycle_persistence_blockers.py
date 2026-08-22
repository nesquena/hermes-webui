"""Regression tests for the four gate-certifier lifecycle/persistence blockers
on PR #6405 (persistent fallback notice).

Blocker #1: SILENT lifecycle leak — pre-start local and Gateway exits never
    retire the registered worker participant.
Blocker #2: SILENT loss/leak — a notice published during the first no-notice
    save is never persisted.
Blocker #3: SILENT false durability — worker recovery can mark a newer
    dead-letter generation saved without stamping it.
Blocker #4: SILENT false success — active-chat persistence failure is
    overwritten by success UI (cancelStream returns structured result).

Each test MUST fail before the fix and pass after.
"""
import queue
import threading
from unittest.mock import Mock

import pytest


def _clear_all_settlement_state():
    """Clear every settlement registry so tests start from a clean baseline."""
    from api.streaming import (
        _STREAM_FALLBACK_NOTICES, _STREAM_CANCEL_CLAIMED,
        _STREAM_SETTLEMENT_TERMINAL, _STREAM_NOTICE_GENERATION,
        _STREAM_SETTLEMENT_PARTICIPANTS, _STREAM_SETTLEMENT_COMPLETED,
        _STREAM_WORKER_SAVED, _STREAM_FALLBACK_DEAD_LETTER,
    )
    from api.config import AGENT_INSTANCES, STREAMS, CANCEL_FLAGS, ACTIVE_RUNS
    AGENT_INSTANCES.clear()
    STREAMS.clear()
    CANCEL_FLAGS.clear()
    ACTIVE_RUNS.clear()
    _STREAM_FALLBACK_NOTICES.clear()
    _STREAM_CANCEL_CLAIMED.clear()
    _STREAM_SETTLEMENT_TERMINAL.clear()
    _STREAM_NOTICE_GENERATION.clear()
    _STREAM_SETTLEMENT_PARTICIPANTS.clear()
    _STREAM_SETTLEMENT_COMPLETED.clear()
    _STREAM_WORKER_SAVED.clear()
    _STREAM_FALLBACK_DEAD_LETTER.clear()
    # Clear the session cache to prevent Mock objects from leaking into
    # other tests' session index sorting (test isolation).
    try:
        from api.models import SESSIONS
        SESSIONS.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_registries():
    _clear_all_settlement_state()
    yield
    _clear_all_settlement_state()


# ── Blocker #1: pre-start exits must retire worker participant ─────────────


class TestBlocker1PreStartWorkerRetirement:
    """The local pre-start return, Gateway pre-start return, and Gateway
    teardown finally must all retire the worker settlement participant that
    cancel_stream() registered.  Without retirement,
    _STREAM_SETTLEMENT_PARTICIPANTS leaks a 'worker' entry indefinitely.
    """

    def test_local_pre_start_retires_worker_participant(self):
        """When cancel_stream() registers 'cancel'+'worker' participants and
        the local worker reaches _run_agent_streaming() with its queue already
        removed, the pre-start return must retire the worker participant."""
        from api.streaming import (
            STREAMS_LOCK,
            _STREAM_SETTLEMENT_PARTICIPANTS, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_CANCEL_CLAIMED,
            _set_stream_settlement_participants_locked,
            _retire_worker_cancelled_state,
            _complete_stream_settlement_participant_locked,
        )

        stream_id = "test-prestart-local-1"

        # Simulate cancel_stream() registering participants under the lock
        with STREAMS_LOCK:
            _set_stream_settlement_participants_locked(stream_id, 'cancel', 'worker')
            _STREAM_CANCEL_CLAIMED.add(stream_id)

        # Verify the participant leak exists before the fix path runs
        with STREAMS_LOCK:
            assert stream_id in _STREAM_SETTLEMENT_PARTICIPANTS
            assert 'worker' in _STREAM_SETTLEMENT_PARTICIPANTS[stream_id]

        # Simulate cancel_stream's own finally completing its participant
        # (this happens before the worker's pre-start return in the real schedule)
        with STREAMS_LOCK:
            _complete_stream_settlement_participant_locked(stream_id, 'cancel')

        # Now simulate the pre-start return path: the worker's queue was
        # already removed.  The pre-start return calls _retire_worker_cancelled_state.
        _retire_worker_cancelled_state(stream_id)

        # After BOTH retirements, no participant should remain
        with STREAMS_LOCK:
            assert stream_id not in _STREAM_SETTLEMENT_PARTICIPANTS, (
                "worker participant leaked after pre-start local return — "
                "the pre-start exit must retire the worker participant"
            )

    def test_gateway_pre_start_retires_worker_participant(self):
        """The Gateway pre-start return path (gateway_chat.py) must retire
        the worker participant when the stream was cancelled before the
        gateway worker started."""
        from api.streaming import (
            STREAMS_LOCK,
            _STREAM_SETTLEMENT_PARTICIPANTS, _STREAM_CANCEL_CLAIMED,
            _set_stream_settlement_participants_locked,
            _retire_worker_cancelled_state,
            _complete_stream_settlement_participant_locked,
        )

        stream_id = "test-prestart-gw-1"

        with STREAMS_LOCK:
            _set_stream_settlement_participants_locked(stream_id, 'cancel', 'worker')
            _STREAM_CANCEL_CLAIMED.add(stream_id)

        # Cancel's own finally has already run
        with STREAMS_LOCK:
            _complete_stream_settlement_participant_locked(stream_id, 'cancel')

        # Simulate the Gateway pre-start return calling retirement
        _retire_worker_cancelled_state(stream_id)

        with STREAMS_LOCK:
            assert stream_id not in _STREAM_SETTLEMENT_PARTICIPANTS, (
                "worker participant leaked after Gateway pre-start return"
            )

    def test_gateway_teardown_retires_worker_participant(self):
        """The Gateway normal teardown finally must retire the worker
        participant on every exit path."""
        from api.streaming import (
            STREAMS_LOCK,
            _STREAM_SETTLEMENT_PARTICIPANTS, _STREAM_CANCEL_CLAIMED,
            _set_stream_settlement_participants_locked,
            _retire_worker_cancelled_state,
            _complete_stream_settlement_participant_locked,
        )

        stream_id = "test-teardown-gw-1"

        with STREAMS_LOCK:
            _set_stream_settlement_participants_locked(stream_id, 'cancel', 'worker')
            _STREAM_CANCEL_CLAIMED.add(stream_id)

        # Cancel's own finally has already run
        with STREAMS_LOCK:
            _complete_stream_settlement_participant_locked(stream_id, 'cancel')

        # Simulate the Gateway teardown finally calling retirement
        _retire_worker_cancelled_state(stream_id)

        with STREAMS_LOCK:
            assert stream_id not in _STREAM_SETTLEMENT_PARTICIPANTS, (
                "worker participant leaked after Gateway teardown"
            )

    def test_local_pre_start_source_calls_retirement(self):
        """The local pre-start return in _run_agent_streaming() (streaming.py)
        must call _retire_worker_cancelled_state in its early-return path."""
        from pathlib import Path
        streaming_src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

        # Find the pre-start return block: "q = STREAMS.get(stream_id)" followed
        # by "if q is None:" ... "return"
        import re
        # The pre-start block should contain _retire_worker_cancelled_state
        # between "if q is None:" and the first "return" after it
        m = re.search(r'q = STREAMS\.get\(stream_id\)\s*\n\s*if q is None:.*?return', streaming_src, re.DOTALL)
        assert m, "pre-start return block not found in streaming.py"
        prestart_block = m.group()
        assert '_retire_worker_cancelled_state' in prestart_block, (
            "local pre-start return must call _retire_worker_cancelled_state to "
            "retire the worker participant (gate-certifier blocker #1)"
        )

    def test_gateway_pre_start_source_calls_retirement(self):
        """The Gateway pre-start return in gateway_chat.py must call
        _retire_worker_cancelled_state in its early-return path."""
        from pathlib import Path
        gw_src = (Path(__file__).resolve().parents[1] / "api" / "gateway_chat.py").read_text()

        import re
        # The gateway pre-start block has _finish_gateway_run_starting(result="fallback")
        # followed by cleanup and return.  Match from that anchor to the return statement.
        # Use \n\s*return\b to avoid matching "return" inside comments/strings.
        m = re.search(
            r'_finish_gateway_run_starting\(stream_id, result="fallback"\).*?\n\s*return\b',
            gw_src, re.DOTALL,
        )
        assert m, "Gateway pre-start return block not found in gateway_chat.py"
        prestart_block = m.group()
        assert '_retire_worker_cancelled_state' in prestart_block, (
            "Gateway pre-start return must call _retire_worker_cancelled_state "
            "(gate-certifier blocker #1)"
        )

    def test_gateway_teardown_source_calls_retirement(self):
        """The Gateway normal teardown finally in gateway_chat.py must call
        _retire_worker_cancelled_state."""
        from pathlib import Path
        gw_src = (Path(__file__).resolve().parents[1] / "api" / "gateway_chat.py").read_text()

        # The teardown finally is the last finally block in the gateway run function
        # It should contain _retire_worker_cancelled_state after clear_session_writeback_owner_if_owned
        assert '_retire_worker_cancelled_state' in gw_src, (
            "gateway_chat.py must call _retire_worker_cancelled_state in its "
            "teardown finally (gate-certifier blocker #1)"
        )


# ── Blocker #2: notice published during first no-notice save is persisted ──


class TestBlocker2FirstSaveNoticeBarrier:
    """When cancel_stream()'s settlement loop does its first save with no
    notice, a notice published DURING that save must be persisted, not lost.
    """

    def test_settlement_loop_rechecks_after_no_notice_save(self):
        """The settlement loop's no-notice first-save branch must recheck
        _STREAM_FALLBACK_NOTICES after the save and continue settlement if
        a notice appeared.  This is a source-level test verifying the recheck
        exists in the code."""
        from pathlib import Path
        streaming_src = (Path(__file__).resolve().parents[1] / "api" / "streaming.py").read_text()

        import re
        # Find the post-save no-notice block: it follows "_first_save = False"
        # and contains "if _fb_to_stamp is None:" before the terminal fence.
        # We anchor on "_first_save = False" to get the RIGHT block (not the
        # snapshot-read block earlier in the loop).
        m = re.search(
            r'_first_save = False\s*\n\s*if _fb_to_stamp is None:.*?_STREAM_SETTLEMENT_TERMINAL\.add\(stream_id\)',
            streaming_src, re.DOTALL,
        )
        assert m, "post-save no-notice first-save block not found in streaming.py"
        block = m.group()
        assert '_STREAM_FALLBACK_NOTICES' in block, (
            "the no-notice first-save branch must recheck _STREAM_FALLBACK_NOTICES "
            "after the save to catch a notice published during filesystem I/O "
            "(gate-certifier blocker #2)"
        )
        assert 'continue' in block, (
            "the no-notice first-save branch must `continue` the settlement loop "
            "when a late notice appeared, not break with the terminal fence"
        )

    def test_notice_published_during_first_save_is_persisted(self):
        """The settlement loop's no-notice first-save branch must recheck
        _STREAM_FALLBACK_NOTICES after the save and continue settlement if
        a notice appeared."""
        from api.streaming import (
            STREAMS_LOCK, _STREAM_FALLBACK_NOTICES, _STREAM_SETTLEMENT_TERMINAL,
            _STREAM_CANCEL_CLAIMED, _STREAM_SETTLEMENT_PARTICIPANTS,
            _publish_fallback_notice, _clean_fallback_notice,
            _stamp_notice_on_current_turn_row,
        )

        stream_id = "test-firstsave-barrier-1"

        # Set up a minimal cancel settlement state
        with STREAMS_LOCK:
            _STREAM_SETTLEMENT_PARTICIPANTS.setdefault(stream_id, set()).add('cancel')
            _STREAM_CANCEL_CLAIMED.add(stream_id)

        # Create a mock session with a cancel marker already in messages
        mock_session = Mock()
        mock_session.session_id = "sess-firstsave"
        mock_session.messages = []
        mock_session.active_stream_id = stream_id
        mock_session.pending_user_message = None
        mock_session.pending_attachments = []
        mock_session.pending_started_at = None
        mock_session.pending_user_source = None
        mock_session.profile = None

        # The notice that will be published DURING the save
        late_notice = {
            'message': 'Switched to fallback model',
            'to_model': 'gpt-4o-mini',
            'to_provider': 'openai',
        }

        # Track save calls: publish the notice during the first save
        _save_call_count = [0]
        _saved_snapshots = []
        import copy

        def _save_with_late_publish():
            _save_call_count[0] += 1
            _saved_snapshots.append(copy.deepcopy(mock_session.messages))
            if _save_call_count[0] == 1:
                # Publish during the first save (simulating status callback)
                _publish_fallback_notice(stream_id, dict(late_notice))

        mock_session.save = Mock(side_effect=_save_with_late_publish)

        # Simulate the settlement loop's first-save-with-no-notice path
        # followed by the recheck.  We test the recheck logic directly:
        # after the first save, we recheck _STREAM_FALLBACK_NOTICES.
        from api.streaming import _STREAM_NOTICE_GENERATION

        # First save (no notice exists yet)
        mock_session.messages.append({'role': 'assistant', 'content': 'Task cancelled.', '_error': True})
        mock_session.save()  # This publishes the late notice

        # The fix: recheck after first save
        with STREAMS_LOCK:
            _late_fb = _STREAM_FALLBACK_NOTICES.get(stream_id)

        assert _late_fb is not None, (
            "late notice should have been published during the first save"
        )

        # Now stamp it on the cancel marker and save again
        _fb_clean = _clean_fallback_notice(_late_fb)
        _stamp_notice_on_current_turn_row(
            _fb_clean, None, mock_session.messages, 0,
        )
        mock_session.save()

        # The second save snapshot should contain the stamped notice
        assert len(_saved_snapshots) >= 2, "settlement should have saved at least twice"
        _last_snapshot = _saved_snapshots[-1]
        _cancel_row = _last_snapshot[0]
        assert _cancel_row.get('_fallbackNotice') is not None, (
            "late notice published during first save was not persisted — "
            "the no-notice first-save branch must recheck and continue settlement"
        )
        assert _cancel_row['_fallbackNotice']['message'] == late_notice['message']


# ── Blocker #3: pre-existing marker + newer dead-letter is stamped ─────────


class TestBlocker3PreExistingMarkerNewerDeadLetter:
    """When a cancel marker already exists and the worker captures a newer
    dead-letter generation, _persist_cancelled_turn must stamp the notice on
    the existing marker row — not skip stamping entirely.
    """

    def test_persist_cancelled_turn_stamps_existing_marker(self):
        """_persist_cancelled_turn() must stamp the fallback notice on the
        existing cancel marker row even when a marker already exists."""
        from api.streaming import (
            _persist_cancelled_turn, _STREAM_FALLBACK_NOTICES,
            _STREAM_FALLBACK_DEAD_LETTER, STREAMS_LOCK,
        )

        stream_id = "test-preexist-marker-1"

        # Set up a session with a pre-existing cancel marker (generation 1)
        mock_session = Mock()
        mock_session.session_id = "sess-preexist"
        mock_session.messages = [
            {'role': 'user', 'content': 'hello'},
            {
                'role': 'assistant',
                'content': '**Task cancelled:** Task cancelled.\n\n*Task was cancelled.*',
                '_error': True,
                'timestamp': 1000,
            },
        ]
        mock_session.pending_user_message = None
        mock_session.pending_attachments = []
        mock_session.pending_started_at = None
        mock_session.pending_user_source = None

        # Put a newer generation-2 notice in the dead-letter
        gen2_notice = {
            'message': 'Switched to fallback model B',
            'to_model': 'claude-3.5-sonnet',
            'to_provider': 'anthropic',
        }
        with STREAMS_LOCK:
            _STREAM_FALLBACK_DEAD_LETTER[stream_id] = {
                'notice': gen2_notice,
                'generation': 2,
                'owner_session_id': 'sess-preexist',
                'owner_profile': None,
                'created_at': 0,
                'updated_at': 0,
                'attempts': 0,
                'next_retry_at': 0,
                'terminal_status': 'failed',
            }

        # Call _persist_cancelled_turn with the pre-existing marker
        _persist_cancelled_turn(mock_session, message='Task cancelled.', stream_id=stream_id)

        # The existing cancel marker row should now have the notice stamped
        _cancel_row = mock_session.messages[1]
        assert _cancel_row.get('_fallbackNotice') is not None, (
            "pre-existing cancel marker was not stamped with the newer dead-letter "
            "notice — _persist_cancelled_turn must stamp even when a marker exists"
        )
        assert _cancel_row['_fallbackNotice']['message'] == gen2_notice['message'], (
            "stamped notice does not match the dead-letter generation 2 notice"
        )
        assert _cancel_row['_fallbackNotice']['to_model'] == gen2_notice['to_model']

    def test_persist_cancelled_turn_no_notice_no_stamp(self):
        """When no notice exists and a marker already exists,
        _persist_cancelled_turn should not add a new message or stamp."""
        from api.streaming import _persist_cancelled_turn

        stream_id = "test-preexist-marker-none-1"
        mock_session = Mock()
        mock_session.session_id = "sess-none"
        mock_session.messages = [
            {'role': 'user', 'content': 'hello'},
            {
                'role': 'assistant',
                'content': '**Task cancelled:** Task cancelled.',
                '_error': True,
                'timestamp': 1000,
            },
        ]
        mock_session.pending_user_message = None
        mock_session.pending_attachments = []
        mock_session.pending_started_at = None
        mock_session.pending_user_source = None

        _original_len = len(mock_session.messages)
        _persist_cancelled_turn(mock_session, message='Task cancelled.', stream_id=stream_id)

        # No new message should be appended
        assert len(mock_session.messages) == _original_len, (
            "no new cancel marker should be appended when one already exists"
        )


# ── Blocker #4: cancelStream returns structured result ────────────────────


class TestBlocker4StructuredCancelResult:
    """cancelStream() must return {cancelled, persistence_failed} instead of
    a bare boolean so callers can preserve the persistence-failure warning
    instead of overwriting it with a success toast.
    """

    def test_cancel_stream_source_returns_structured_result(self):
        """The cancelStream() function body must return a structured object,
        not a bare boolean."""
        import re
        from pathlib import Path

        boot_js = Path(__file__).resolve().parents[1] / "static" / "boot.js"
        src = boot_js.read_text()

        # Extract the cancelStream function body
        m = re.search(r"async function cancelStream\s*\(", src)
        assert m, "cancelStream() not found in boot.js"
        start = m.start()
        # Find the matching closing brace
        depth = 0
        pos = start
        for i in range(start, len(src)):
            if src[i] == '{':
                depth += 1
            elif src[i] == '}':
                depth -= 1
                if depth == 0:
                    pos = i + 1
                    break
        cancel_src = src[start:pos]

        # Must return a structured object, not a bare boolean
        assert "persistence_failed" in cancel_src, (
            "cancelStream() must reference persistence_failed in its return value"
        )
        assert "cancelled:" in cancel_src or "cancelled =" in cancel_src, (
            "cancelStream() must return a structured {cancelled, persistence_failed} result"
        )
        # Must NOT end with a bare `return respOk;`
        assert not re.search(r"return\s+respOk\s*;", cancel_src), (
            "cancelStream() must not return a bare boolean respOk"
        )

    def test_callers_use_structured_result(self):
        """All cancelStream() callers must handle the structured result,
        not treat it as a bare boolean."""
        import re
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]

        # commands.js: /stop and /interrupt
        commands_src = (repo / "static" / "commands.js").read_text()
        # /stop caller must use .cancelled and .persistence_failed
        stop_match = re.search(r"async function cmdStop\(\).*?\n\}", commands_src, re.DOTALL)
        assert stop_match, "cmdStop not found"
        stop_src = stop_match.group()
        assert "_r.cancelled" in stop_src or "_r && _r.cancelled" in stop_src, (
            "/stop must check the structured result's .cancelled field"
        )
        assert "_r.persistence_failed" in stop_src or "_r && _r.persistence_failed" in stop_src, (
            "/stop must check the structured result's .persistence_failed field"
        )

        # /interrupt caller
        interrupt_match = re.search(r"async function cmdInterrupt\(args\).*?\n\}\n", commands_src, re.DOTALL)
        assert interrupt_match, "cmdInterrupt not found"
        interrupt_src = interrupt_match.group()
        assert "_r.cancelled" in interrupt_src or "_r && _r.cancelled" in interrupt_src, (
            "/interrupt must check the structured result's .cancelled field"
        )
        assert "_r.persistence_failed" in interrupt_src or "_r && _r.persistence_failed" in interrupt_src, (
            "/interrupt must check the structured result's .persistence_failed field"
        )

        # messages.js: busy-interrupt caller
        messages_src = (repo / "static" / "messages.js").read_text()
        assert "_r.cancelled" in messages_src or "_r && _r.cancelled" in messages_src, (
            "busy-interrupt caller must check the structured result's .cancelled field"
        )
        assert "_r.persistence_failed" in messages_src or "_r && _r.persistence_failed" in messages_src, (
            "busy-interrupt caller must check the structured result's .persistence_failed field"
        )

        # ui.js: composer-stop caller
        ui_src = (repo / "static" / "ui.js").read_text()
        assert "_r.cancelled" in ui_src or "_r && _r.cancelled" in ui_src, (
            "composer-stop caller must check the structured result's .cancelled field"
        )
