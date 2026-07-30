"""Regression tests for issue #3587: intermediate assistant message reasoning lost.

During multi-turn streaming (assistant reasons → calls tool → reasons again →
responds), the flat _reasoning_text accumulator was written only to the LAST
assistant message on settlement. Intermediate assistant messages (before tool
calls) permanently lost their reasoning traces.

Fix: replace the flat accumulator with a per-message dict (_reasoning_segments),
track assistant message transitions via on_interim_assistant, and iterate forward
through s.messages on settlement so each assistant message receives its own
reasoning segment.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent


def read(rel):
    return (REPO / rel).read_text(encoding='utf-8')


# ── 1. Flat accumulator is replaced ──────────────────────────────────────────


class TestAccumulatorReplaced:
    """The flat string accumulator must be replaced by a per-message dict."""

    def test_bare_string_declaration_removed(self):
        src = read('api/streaming.py')
        # The old declaration was exactly: _reasoning_text = ''
        # It must no longer exist as a bare string assignment (the comment that
        # mentions it by name is allowed, but the assignment itself must be gone).
        assert "_reasoning_text = ''" not in src, (
            "_reasoning_text = '' bare declaration must be replaced by the "
            "per-message _reasoning_segments dict (#3587)"
        )

    def test_segments_dict_declared(self):
        src = read('api/streaming.py')
        assert '_reasoning_segments' in src, (
            "_reasoning_segments dict must be declared in api/streaming.py"
        )
        assert '_current_reasoning_idx' in src, (
            "_current_reasoning_idx counter must be declared in api/streaming.py"
        )

    def test_segments_dict_is_dict_type(self):
        src = read('api/streaming.py')
        # Declaration must be an empty dict, not a string
        assert re.search(r'_reasoning_segments\s*(?::\s*dict\s*)?\=\s*\{\}', src), (
            "_reasoning_segments must be initialized as an empty dict"
        )


# ── 2. on_reasoning indexes into per-message dict ────────────────────────────


class TestOnReasoningPerMessageIndexing:
    """The on_reasoning callback must index into _reasoning_segments using
    _current_reasoning_idx instead of appending to a flat string."""

    def _on_reasoning_body(self):
        src = read('api/streaming.py')
        m = re.search(
            r'def on_reasoning\(text\):\s*\n(.*?)(?=\n\s{12}def |\n\s{8}def )',
            src, re.DOTALL,
        )
        assert m, "on_reasoning function not found in api/streaming.py"
        return m.group(1)

    def test_on_reasoning_uses_segments_not_flat_string(self):
        body = self._on_reasoning_body()
        assert '_reasoning_segments' in body, (
            "on_reasoning must accumulate into _reasoning_segments, not a flat string"
        )
        assert "_reasoning_text +=" not in body, (
            "on_reasoning must not use the old flat _reasoning_text += pattern"
        )

    def test_on_reasoning_indexes_by_current_idx(self):
        body = self._on_reasoning_body()
        assert '_current_reasoning_idx' in body, (
            "on_reasoning must reference _current_reasoning_idx to attribute "
            "reasoning deltas to the correct assistant message"
        )

    def test_stream_reasoning_text_mirror_still_present(self):
        """cancel_stream() uses STREAM_REASONING_TEXT for its own partial-message
        persist path; this mirror must remain even after the per-message fix."""
        body = self._on_reasoning_body()
        assert 'STREAM_REASONING_TEXT' in body, (
            "on_reasoning must still mirror to STREAM_REASONING_TEXT so "
            "cancel_stream() can persist reasoning on mid-stream cancellation"
        )


# ── 3. on_interim_assistant advances the index ───────────────────────────────


class TestInterimAssistantAdvancesIndex:
    """on_interim_assistant fires when a new assistant segment starts after tool
    results. It must increment _current_reasoning_idx so subsequent reasoning
    deltas are attributed to the next assistant message."""

    def _interim_body(self):
        src = read('api/streaming.py')
        m = re.search(
            r'def on_interim_assistant\(text.*?\):\s*\n(.*?)(?=\n\s{12}def |\n\s{8}def )',
            src, re.DOTALL,
        )
        assert m, "on_interim_assistant function not found in api/streaming.py"
        return m.group(1)

    def test_interim_assistant_increments_idx(self):
        body = self._interim_body()
        assert '_current_reasoning_idx' in body, (
            "on_interim_assistant must increment _current_reasoning_idx to "
            "advance the per-message reasoning segment pointer (#3587)"
        )
        assert re.search(r'_current_reasoning_idx\s*\+=\s*1', body), (
            "on_interim_assistant must use += 1 to advance the segment index"
        )


# ── 4. Settlement loop iterates forward, not reversed+break ──────────────────


class TestSettlementLoopForward:
    """The settlement loop must iterate forward through s.messages so each
    assistant message can be matched to its own reasoning segment by index.
    The old reversed()+break pattern only wrote reasoning to the last message."""

    def _settlement_block(self):
        """Extract the reasoning-persistence settlement block from streaming.py."""
        src = read('api/streaming.py')
        # Anchor on the comment that appears just before the settlement block
        start = src.find('# #3587: use per-message segments')
        assert start >= 0, (
            "Settlement block comment '#3587: use per-message segments' not found; "
            "the block may have been moved or the comment changed"
        )
        # Grab enough context to cover the loop
        return src[start:start + 1500]

    def test_settlement_does_not_reverse_iterate_with_break(self):
        block = self._settlement_block()
        # The old pattern was: for _rm in reversed(s.messages): ... break
        # Both conditions must be gone from the settlement block.
        has_reversed_break = (
            'reversed(s.messages)' in block and
            re.search(r'\bbreak\b', block)
        )
        assert not has_reversed_break, (
            "Settlement loop must not use reversed(s.messages)+break; "
            "that pattern writes reasoning only to the last assistant message"
        )

    def test_settlement_iterates_forward_with_counter(self):
        block = self._settlement_block()
        # Forward iteration with an assistant counter
        assert 'for _rm in s.messages' in block, (
            "Settlement loop must iterate forward (for _rm in s.messages) "
            "to match each assistant message to its reasoning segment"
        )
        assert '_asst_count' in block, (
            "Settlement loop must use an assistant message counter (_asst_count) "
            "to index into _reasoning_segments"
        )

    def test_settlement_reads_from_segments_dict(self):
        block = self._settlement_block()
        assert '_reasoning_segments.get' in block, (
            "Settlement loop must read from _reasoning_segments.get(idx) "
            "to retrieve the per-message reasoning trace"
        )


# ── 5. Multi-turn offset prevents cross-turn reasoning clobber ──────────────


class TestMultiTurnOffset:
    """The settlement loop must skip prior-turn assistant messages so that
    _reasoning_segments (indexed from 0 for this turn only) doesn't overwrite
    reasoning stored on earlier turns."""

    def _settlement_block(self):
        src = read('api/streaming.py')
        start = src.find('# #3587: use per-message segments')
        assert start >= 0, 'Settlement block not found'
        return src[start:start + 1500]

    def test_settlement_computes_prev_asst_offset(self):
        block = self._settlement_block()
        assert '_prev_asst' in block, (
            "Settlement loop must compute _prev_asst (count of assistant "
            "messages in _previous_messages) to offset the segment index"
        )

    def test_settlement_skips_prior_turn_messages(self):
        block = self._settlement_block()
        assert re.search(r'if\s+_turn_idx\s*<\s*_prev_asst\s*:', block), (
            "Settlement loop must skip prior-turn messages with "
            "if _turn_idx < _prev_asst: continue"
        )

    def test_segment_index_subtracts_offset(self):
        block = self._settlement_block()
        assert re.search(r'_turn_idx\s*-\s*_prev_asst', block), (
            "Segment index must subtract _prev_asst offset so indexing "
            "starts at 0 for this turn's first assistant message"
        )


# ── 6. Tool-call boundary advances reasoning index ────────────────────────────


class TestToolCallBoundary:
    """on_interim_assistant is suppressed for contentless tool-call assistant
    messages (run_agent.py:3834 early-returns when content is empty). The
    reasoning index must advance at tool-call boundaries instead, so reasoning
    accumulated before a tool-call-only assistant message gets its own segment."""

    def _on_tool_body(self):
        src = read('api/streaming.py')
        m = re.search(
            r'def on_tool\(\*cb_args.*?\):\s*\n(.*?)(?=\n\s{12}def |\n\s{8}def )',
            src, re.DOTALL,
        )
        assert m, "on_tool function not found in api/streaming.py"
        return m.group(1)

    def test_on_tool_advances_reasoning_idx(self):
        body = self._on_tool_body()
        assert '_current_reasoning_idx' in body, (
            "on_tool must reference _current_reasoning_idx to advance the "
            "reasoning segment at tool-call boundaries (#3587)"
        )

    def test_tool_boundary_guard_prevents_double_advance(self):
        body = self._on_tool_body()
        assert '_tool_boundary_advanced' in body, (
            "on_tool must use a _tool_boundary_advanced guard so multiple "
            "tool calls in one assistant message only advance the index once"
        )

    def test_tool_boundary_flag_declared(self):
        src = read('api/streaming.py')
        assert '_tool_boundary_advanced' in src, (
            "_tool_boundary_advanced flag must be declared in streaming.py"
        )

    def test_reasoning_resets_tool_boundary_flag(self):
        """New reasoning arriving after a tool boundary must reset the guard
        so the next tool-call batch can advance the index again."""
        src = read('api/streaming.py')
        m = re.search(
            r'def on_reasoning\(text\):\s*\n(.*?)(?=\n\s{12}def |\n\s{8}def )',
            src, re.DOTALL,
        )
        assert m, "on_reasoning function not found"
        body = m.group(1)
        assert '_tool_boundary_advanced' in body, (
            "on_reasoning must reset _tool_boundary_advanced so the next "
            "tool-call batch can advance the reasoning index"
        )


# ── 7. Settlement counter increments exactly once per assistant message ────


class TestSettlementCounterSingleIncrement:
    """_asst_count must increment exactly once per assistant message in the
    settlement loop. A double increment causes every message after the first
    to look up a segment index that doesn't exist, silently discarding its
    reasoning (the exact data-loss scenario the refactor was meant to fix)."""

    def _settlement_block(self):
        src = read('api/streaming.py')
        start = src.find('# #3587: use per-message segments')
        assert start >= 0
        return src[start:start + 1500]

    def test_single_increment_per_iteration(self):
        block = self._settlement_block()
        count = block.count('_asst_count += 1')
        assert count == 1, (
            f"_asst_count must be incremented exactly once per loop iteration, "
            f"found {count} increments. A double increment causes segment index "
            f"doubling: message N looks up segment 2*N instead of N."
        )
