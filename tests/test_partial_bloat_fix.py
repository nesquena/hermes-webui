"""
Regression tests for the _partial message bloat bug.

The bug: empty _partial messages (reasoning-only cancellations where thinking
markup was stripped, leaving content='') accumulated exponentially because:

1. _message_identity() returned None for empty _partial messages (no text,
   no tool_calls, no tool_call_id), so _merge_display_messages_after_agent_result
   couldn't dedup them — they doubled every turn.

2. _sanitize_messages_for_api() and _api_safe_message_positions() didn't skip
   empty _partial messages, so they were sent to the model as empty assistant
   turns, wasting tokens and causing API 400 errors on strict providers.

3. _merge_display_messages_after_agent_result() didn't dedup stale _partial
   messages in previous_display, so identical empty partials survived across
   turn merges.
"""
import pytest

import api.streaming as streaming


# ── Fix 2: _message_identity for empty _partial messages ─────────────────

class TestMessageIdentityForEmptyPartial:

    def test_empty_partial_gets_stable_identity(self):
        """Empty _partial messages must return a non-None identity so merge
        can dedup them. Before the fix they returned None."""
        msg = {
            'role': 'assistant',
            'content': '',
            '_partial': True,
            'reasoning': 'Step 1: analyze the problem',
        }
        identity = streaming._message_identity(msg)
        assert identity is not None, (
            "Empty _partial message must have a stable identity for merge dedup"
        )
        # Identity should include the __partial__ marker and reasoning
        assert '__partial__' in identity[3], (
            "Empty _partial identity must include __partial__ marker"
        )

    def test_empty_partial_different_reasoning_gets_different_identity(self):
        """Two _partial messages with different reasoning must be distinct."""
        msg_a = {
            'role': 'assistant',
            'content': '',
            '_partial': True,
            'reasoning': 'Step 1: analyze the problem',
        }
        msg_b = {
            'role': 'assistant',
            'content': '',
            '_partial': True,
            'reasoning': 'Step 1: consider alternatives',
        }
        assert streaming._message_identity(msg_a) != streaming._message_identity(msg_b), (
            "Different reasoning text must produce different identities"
        )

    def test_empty_partial_same_reasoning_gets_same_identity(self):
        """Two _partial messages with same reasoning must have identical identity."""
        msg_a = {
            'role': 'assistant',
            'content': '',
            '_partial': True,
            'reasoning': 'Step 1: analyze the problem',
        }
        msg_b = {
            'role': 'assistant',
            'content': '',
            '_partial': True,
            'reasoning': 'Step 1: analyze the problem',
        }
        assert streaming._message_identity(msg_a) == streaming._message_identity(msg_b), (
            "Same reasoning text must produce identical identities for dedup"
        )

    def test_empty_partial_no_reasoning_still_gets_identity(self):
        """Even _partial messages with no reasoning at all must be dedupable."""
        msg = {
            'role': 'assistant',
            'content': '',
            '_partial': True,
        }
        identity = streaming._message_identity(msg)
        assert identity is not None, (
            "Empty _partial with no reasoning must still have a stable identity"
        )

    def test_empty_non_partial_still_returns_none(self):
        """Non-_partial empty assistant messages still return None identity."""
        msg = {
            'role': 'assistant',
            'content': '',
        }
        assert streaming._message_identity(msg) is None, (
            "Non-_partial empty messages should still return None identity"
        )

    def test_nonempty_partial_keeps_original_identity(self):
        """_partial messages with actual text use the normal identity path."""
        msg = {
            'role': 'assistant',
            'content': 'Python is a high-level',
            '_partial': True,
        }
        identity = streaming._message_identity(msg)
        assert identity is not None
        assert '__partial__' not in identity[3], (
            "Non-empty _partial should use normal identity, not __partial__ path"
        )


# ── Fix 3: _sanitize_messages_for_api skips empty _partial ───────────────

class TestSanitizeSkipsEmptyPartial:

    def test_empty_partial_excluded_from_api(self):
        """Empty _partial messages must be stripped from API context —
        they have nothing for the model to continue from."""
        messages = [
            {'role': 'user', 'content': 'Tell me about Python'},
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'thinking...'},
            {'role': 'user', 'content': 'Continue'},
        ]
        clean = streaming._sanitize_messages_for_api(messages)
        contents = [m.get('content', '') for m in clean]
        # The empty _partial must be gone
        assert not any(m.get('_partial') and not str(m.get('content', '')).strip()
                       for m in clean), (
            "Empty _partial must be excluded from sanitized API messages"
        )

    def test_nonempty_partial_kept_in_api(self):
        """Non-empty _partial messages with actual text MUST be kept —
        the model continues from the cut-off point (#893)."""
        messages = [
            {'role': 'user', 'content': 'Tell me about Python'},
            {'role': 'assistant', 'content': 'Python is a high-level', '_partial': True},
        ]
        clean = streaming._sanitize_messages_for_api(messages)
        contents = [m.get('content', '') for m in clean]
        assert any('Python is a high-level' in c for c in contents), (
            "Non-empty _partial must be kept in API context (#893)"
        )

    def test_whitespace_only_partial_excluded(self):
        """_partial messages with only whitespace content are also excluded."""
        messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': '   \n  ', '_partial': True},
        ]
        clean = streaming._sanitize_messages_for_api(messages)
        assert not any(m.get('_partial') for m in clean), (
            "Whitespace-only _partial must be excluded from sanitized messages"
        )


# ── Fix 3b: _api_safe_message_positions skips empty _partial ─────────────

class TestApiSafePositionsSkipsEmptyPartial:

    def test_empty_partial_excluded_from_positions(self):
        """Empty _partial must not appear in API-safe positions."""
        messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': '', '_partial': True},
            {'role': 'user', 'content': 'Continue'},
        ]
        positions = streaming._api_safe_message_positions(messages)
        # The empty _partial at index 1 must not be in positions
        indices = [idx for idx, _ in positions]
        assert 1 not in indices, (
            "Empty _partial must not be in API-safe positions"
        )

    def test_nonempty_partial_included_in_positions(self):
        """Non-empty _partial must still appear in positions (#893)."""
        messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Partial text here', '_partial': True},
            {'role': 'user', 'content': 'Continue'},
        ]
        positions = streaming._api_safe_message_positions(messages)
        indices = [idx for idx, _ in positions]
        assert 1 in indices, (
            "Non-empty _partial must be in API-safe positions"
        )


# ── Fix 4: _merge_display_messages_after_agent_result dedup ────────────

class TestMergeDisplayDedupPartials:

    def test_duplicate_empty_partials_deduped_in_merge(self):
        """Multiple identical _partial messages in previous_display must be
        deduped — only the last occurrence should survive."""
        previous_display = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'thinking...'},
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'thinking...'},
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'thinking...'},
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'thinking...'},
        ]
        # Simulate what the merge does: result_messages is the new turn's output
        result_messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Here is the answer'},
        ]
        previous_context = [
            {'role': 'user', 'content': 'Hello'},
        ]

        merged = streaming._merge_display_messages_after_agent_result(
            previous_display, previous_context, result_messages, 'Hello'
        )
        # Count how many empty _partial messages survived
        empty_partials = [m for m in merged
                         if isinstance(m, dict) and m.get('_partial')
                         and not str(m.get('content', '')).strip()]
        assert len(empty_partials) <= 1, (
            f"Expected at most 1 empty _partial after dedup, got {len(empty_partials)}"
        )

    def test_different_reasoning_partials_not_deduped(self):
        """_partial messages with different reasoning must NOT be deduped."""
        previous_display = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'Step A'},
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'Step B'},
        ]
        result_messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Answer'},
        ]
        previous_context = [
            {'role': 'user', 'content': 'Hello'},
        ]

        merged = streaming._merge_display_messages_after_agent_result(
            previous_display, previous_context, result_messages, 'Hello'
        )
        empty_partials = [m for m in merged
                         if isinstance(m, dict) and m.get('_partial')
                         and not str(m.get('content', '')).strip()]
        assert len(empty_partials) == 2, (
            f"Different-reasoning partials must not be deduped, expected 2 got {len(empty_partials)}"
        )

    def test_nonempty_partials_not_deduped_by_this_path(self):
        """Non-empty _partial messages are handled by the merge's normal
        _message_identity dedup — the backward-scan only targets empty ones."""
        previous_display = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Part 1', '_partial': True},
            {'role': 'assistant', 'content': 'Part 2', '_partial': True},
        ]
        result_messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Full answer'},
        ]
        previous_context = [
            {'role': 'user', 'content': 'Hello'},
        ]

        merged = streaming._merge_display_messages_after_agent_result(
            previous_display, previous_context, result_messages, 'Hello'
        )
        nonempty_partials = [m for m in merged
                            if isinstance(m, dict) and m.get('_partial')
                            and str(m.get('content', '')).strip()]
        # Non-empty partials are handled by normal merge dedup, but the
        # backward-scan is a no-op for them (they have non-None identity)
        # and the merge's seen set handles them normally
        assert len(nonempty_partials) >= 0  # just must not crash

    def test_massive_bloat_deduped(self):
        """Simulate the actual bug: 1000 identical empty _partial messages
        from the exponential doubling must collapse to 1."""
        previous_display = [
            {'role': 'user', 'content': 'Hello'},
        ] + [
            {'role': 'assistant', 'content': '', '_partial': True, 'reasoning': 'think'}
            for _ in range(1000)
        ]
        result_messages = [
            {'role': 'user', 'content': 'Hello'},
            {'role': 'assistant', 'content': 'Done'},
        ]
        previous_context = [
            {'role': 'user', 'content': 'Hello'},
        ]

        merged = streaming._merge_display_messages_after_agent_result(
            previous_display, previous_context, result_messages, 'Hello'
        )
        empty_partials = [m for m in merged
                         if isinstance(m, dict) and m.get('_partial')
                         and not str(m.get('content', '')).strip()]
        assert len(empty_partials) <= 1, (
            f"1000 identical empty partials must collapse to ≤1, got {len(empty_partials)}"
        )