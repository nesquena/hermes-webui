"""
Tests for streaming error handling fixes:
  #739 — quota/credit exhaustion detected as distinct error type + persisted to session
  #652 — context compaction session_id rotation: stream_end uses original session_id
  #653 — bad tool call hang: same stream_end fix applies

All static tests (no live server required).
"""

import re
import pathlib

STREAMING = pathlib.Path(__file__).parent.parent / "api" / "streaming.py"
MESSAGES_JS = pathlib.Path(__file__).parent.parent / "static" / "messages.js"

streaming_src = STREAMING.read_text(encoding="utf-8")
messages_js_src = MESSAGES_JS.read_text(encoding="utf-8")


# ── #739: Quota exhaustion detection ─────────────────────────────────────────


class TestQuotaDetection:
    """Quota-exhausted errors must be classified separately from rate limits."""

    def test_quota_patterns_present_in_silent_failure_path(self):
        """The silent-failure path checks for credit/quota strings."""
        block = streaming_src
        assert "insufficient credit" in block
        assert "credit balance" in block
        assert "credits exhausted" in block
        assert "quota_exceeded" in block
        assert "exceeded your current quota" in block

    def test_quota_type_emitted_as_quota_exhausted(self):
        """The apperror type is 'quota_exhausted', not 'error' or 'rate_limit'."""
        assert (
            "'quota_exhausted'" in streaming_src or '"quota_exhausted"' in streaming_src
        )

    def test_quota_checked_before_rate_limit(self):
        """Quota check must appear before the rate-limit check in the exception path.
        OpenAI billing 429s overlap with rate-limit patterns."""
        quota_pos = streaming_src.find("_exc_is_quota")
        rate_pos = streaming_src.find("_exc_is_rate_limit")
        assert quota_pos != -1, "_exc_is_quota not found in exception path"
        assert rate_pos != -1, "_exc_is_rate_limit not found in exception path"
        assert quota_pos < rate_pos, "Quota check must appear before rate-limit check"

    def test_rate_limit_excludes_quota(self):
        """Rate-limit detection must be guarded so quota errors don't also match."""
        # The pattern: _exc_is_rate_limit = (not _exc_is_quota) and (...)
        assert "(not _exc_is_quota)" in streaming_src

    def test_js_quota_label_present(self):
        """messages.js renders a 'quota_exhausted' apperror with a distinct label."""
        assert "quota_exhausted" in messages_js_src
        assert "Out of credits" in messages_js_src


# ── #739: Error persistence across reload ─────────────────────────────────────


class TestErrorPersistence:
    """Errors must be saved to the session so they survive page reload."""

    def test_silent_failure_appends_error_message(self):
        """Silent-failure path appends an _error-marked message before returning."""
        # Must append to s.messages with _error key
        assert "s.messages.append(" in streaming_src
        assert "'_error': True" in streaming_src

    def test_silent_failure_calls_save_before_return(self):
        """save() must be called after appending the error message."""
        # Find the silent failure block area and verify save precedes return
        pattern = re.compile(
            r"s\.messages\.append\(.*?'_error': True.*?\).*?s\.save\(\).*?return",
            re.DOTALL,
        )
        assert pattern.search(streaming_src), (
            "save() must be called after appending the error message in the silent-failure path"
        )

    def test_exception_path_appends_error_message(self):
        """Exception path also persists the error to the session."""
        # Both paths should have _error persistence
        count = streaming_src.count("'_error': True")
        assert count >= 2, (
            f"Expected at least 2 _error persistence sites, found {count}"
        )

    def test_sanitize_skips_error_messages(self):
        """_sanitize_messages_for_api must not send _error messages to the LLM."""
        assert (
            "msg.get('_error')" in streaming_src or 'msg.get("_error")' in streaming_src
        )
        # The skip must come before the role/tool filtering logic
        error_skip_pos = streaming_src.find("msg.get('_error')")
        tool_filter_pos = streaming_src.find("if role == 'tool':")
        assert error_skip_pos < tool_filter_pos, (
            "_error skip must appear before the tool-role filter in _sanitize_messages_for_api"
        )


# ── #652/#653: Context compaction stream_end fix ──────────────────────────────


class TestStreamEndSessionId:
    """stream_end must use the original session_id param, not s.session_id."""

    def test_non_bg_title_stream_end_uses_session_id_param(self):
        """When no background title is spawned, stream_end should use original session_id."""
        # The fixed code: put('stream_end', {'session_id': session_id})
        # Not: put('stream_end', {'session_id': s.session_id})
        # Verify the pattern appears in the non-background-title branch
        assert "put('stream_end', {'session_id': session_id})" in streaming_src

    def test_background_title_thread_stream_end_uses_session_id_param(self):
        """Background title thread also emits stream_end with original session_id."""
        # In _run_background_title_update: put_event('stream_end', {'session_id': session_id})
        # The session_id param is passed from the caller with the original value
        assert "put_event('stream_end', {'session_id': session_id})" in streaming_src

    def test_s_session_id_not_used_in_stream_end(self):
        """s.session_id (which may be rotated after compaction) must not appear in stream_end."""
        # Find all stream_end emissions and verify none use s.session_id
        for match in re.finditer(r"put[_a-z]*\('stream_end',[^)]+\)", streaming_src):
            assert "s.session_id" not in match.group(), (
                f"stream_end uses s.session_id (may be rotated): {match.group()}"
            )

    def test_title_event_uses_original_session_id(self):
        """title event in background title thread uses original session_id, not s.session_id."""
        # Client guard: if((d.session_id||activeSid)!==activeSid) return;
        # So title must be emitted with the original id
        assert "put_event('title', {'session_id': session_id," in streaming_src
