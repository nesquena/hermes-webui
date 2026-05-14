"""Regression tests for accurate cancelled/interrupted turn status.

A user pressing Stop/Cancel must not be shown provider-empty guidance like
"No response from provider". Provider-empty remains valid only when there was
no explicit cancel/interruption signal.
"""
from __future__ import annotations

import pathlib

from api.streaming import (
    _CANCEL_MARKER_PATTERNS,
    _cancelled_turn_content,
    _classify_provider_error,
    _finalize_cancelled_turn,
)

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


class _DummySession:
    def __init__(self, path: str = ''):
        self.path = path
        self.messages = []
        self.active_stream_id = 'stream-1'
        self.pending_user_message = 'hello'
        self.pending_attachments = ['a.txt']
        self.pending_started_at = 123
        self.saved = 0

    def save(self, *args, **kwargs):
        self.saved += 1


class TestCancelledTurnClassification:
    def test_user_cancelled_error_is_not_provider_no_response(self):
        result = _classify_provider_error("Cancelled by user", Exception("Cancelled by user"))

        assert result["type"] == "cancelled"
        assert result["label"] == "Task cancelled"
        assert "provider returned no content" not in result.get("hint", "").lower()
        assert "rate limit" not in result.get("hint", "").lower()
        assert "no provider failure" in result.get("hint", "").lower()

    def test_string_only_cancelled_error_repr_is_cancelled(self):
        result = _classify_provider_error("<CancelledError>", None, silent_failure=True)

        assert result["type"] == "cancelled"
        assert result["label"] == "Task cancelled"
        assert "provider returned no content" not in result.get("hint", "").lower()

    def test_interrupted_or_aborted_error_is_not_provider_no_response(self):
        for text in (
            "Interrupted by user",
            "Operation aborted before provider response completed",
            "AbortError: request was aborted",
        ):
            result = _classify_provider_error(text, RuntimeError(text))
            assert result["type"] == "interrupted", text
            assert result["label"] == "Response interrupted", text
            assert "provider returned no content" not in result.get("hint", "").lower()

    def test_provider_empty_response_still_uses_no_response(self):
        result = _classify_provider_error("", None, silent_failure=True)

        assert result["type"] == "no_response"
        assert result["label"] == "No response from provider"
        assert "provider returned no content" in result.get("hint", "").lower()


class TestCancelledTurnFinalizer:
    def test_persistent_cancel_finalizer_clears_pending_and_saves_cancel_marker(self):
        session = _DummySession()

        _finalize_cancelled_turn(session, ephemeral=False)

        assert session.active_stream_id is None
        assert session.pending_user_message is None
        assert session.pending_attachments == []
        assert session.pending_started_at is None
        assert session.saved == 1
        assert session.messages[-1]['content'] == _cancelled_turn_content('Task cancelled.')
        assert '**Task cancelled:** Task cancelled.' in session.messages[-1]['content']
        assert 'No provider failure occurred' in session.messages[-1]['content']
        assert session.messages[-1]['provider_details'] == 'Task cancelled.'
        assert session.messages[-1]['provider_details_label'] == 'Cancellation details'
        assert session.messages[-1]['_error'] is True

    def test_ephemeral_cancel_finalizer_unlinks_temp_session_without_saving_error_marker(self, tmp_path):
        temp_session = tmp_path / 'btw-session.json'
        temp_session.write_text('{}', encoding='utf-8')
        session = _DummySession(str(temp_session))

        _finalize_cancelled_turn(session, ephemeral=True)

        assert session.active_stream_id is None
        assert session.pending_user_message is None
        assert session.pending_attachments == []
        assert session.pending_started_at is None
        assert session.saved == 0
        assert session.messages == []
        assert not temp_session.exists()


    def test_message_renderer_allows_non_provider_details_label(self):
        src = _read("static/ui.js")
        assert "provider_details_label||'Provider details'" in src
        assert "provider-error-details" in src


class TestCancelledTurnPersistenceGuards:
    def test_cancel_marker_patterns_are_centralized_for_dedupe(self):
        assert _CANCEL_MARKER_PATTERNS == ('task cancelled', 'task canceled', 'response interrupted')
        src = _read("api/streaming.py")
        assert "any(pattern in normalized for pattern in _CANCEL_MARKER_PATTERNS)" in src
        assert "any(pattern in _content for pattern in _CANCEL_MARKER_PATTERNS)" in src

    def test_silent_failure_path_checks_cancel_event_before_persisting_provider_error(self):
        src = _read("api/streaming.py")
        silent_idx = src.find("# ── Detect silent agent failure")
        assert silent_idx != -1, "silent-failure block not found"
        apperror_idx = src.find("put('apperror', _error_payload)", silent_idx)
        assert apperror_idx != -1, "silent-failure apperror emission not found"
        block = src[silent_idx:apperror_idx]

        assert "cancel_event.is_set()" in block, (
            "When a user cancels and the interrupted agent returns no assistant text, "
            "the silent-failure path must not persist a provider no_response error."
        )
        assert "cancelled" in block.lower(), (
            "The cancellation guard should persist/report a cancelled turn, not silently drop state."
        )

    def test_exception_path_classifies_after_cancel_event_before_generic_error(self):
        src = _read("api/streaming.py")
        except_idx = src.find("print('[webui] stream error:")
        assert except_idx != -1, "stream exception handler not found"
        classify_idx = src.find("_classify_provider_error", except_idx)
        generic_idx = src.find("_exc_label, _exc_type, _exc_hint = 'Error', 'error', ''", except_idx)
        assert classify_idx != -1 and generic_idx != -1
        block = src[except_idx:generic_idx]

        assert "cancel_event.is_set()" in block, (
            "Exception handling must distinguish user-cancelled/aborted runs before generic errors."
        )
        assert "cancelled" in block.lower() or "interrupted" in block.lower()
        assert "provider_details_label" in src
        assert "Cancellation details" in src
        assert "Interruption details" in src

    def test_post_run_cancel_guard_runs_before_normal_success_merge(self):
        src = _read("api/streaming.py")
        run_idx = src.find("result = agent.run_conversation(")
        merge_idx = src.find("_result_messages = result.get", run_idx)
        assert run_idx != -1 and merge_idx != -1, "run/merge path not found"
        block = src[run_idx:merge_idx]

        assert "cancel_event.is_set()" in block, (
            "If cancellation arrives after tokens streamed but before run_conversation returns, "
            "the worker must emit/persist cancel before normal merge/save/completed handling."
        )
        assert "put('cancel'" in block
        assert "_cleanup_ephemeral_cancelled_turn" in block or "_finalize_cancelled_turn" in block, (
            "Ephemeral cancels must clean up their temporary session before returning."
        )
        assert "return" in block

    def test_frontend_has_cancelled_and_interrupted_labels_for_apperror_fallbacks(self):
        src = _read("static/messages.js")
        start = src.find("source.addEventListener('apperror'")
        end = src.find("source.addEventListener('warning'", start)
        assert start != -1 and end != -1, "apperror handler not found"
        block = src[start:end]

        assert "d.type==='cancelled'" in block or 'd.type==="cancelled"' in block
        assert "d.type==='interrupted'" in block or 'd.type==="interrupted"' in block
        assert "Task cancelled" in block
        assert "Response interrupted" in block
        assert "No response from provider" in block
        assert "Cancellation details" in block
        assert "Interruption details" in block
