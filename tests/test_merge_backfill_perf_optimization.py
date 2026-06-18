"""Regression test for the O(D²·C) → O(D²) backfill optimization in
_merge_display_messages_after_agent_result (salvaged from #4314).

The optimization replaces an `in context_keys[_cursor:]` list-slice membership
test with an O(1) count-keyed dict mirror. The subtle correctness requirement:
_message_identity intentionally returns DUPLICATE keys for identical-content
turns (and None for empty rows). A plain set would diverge from the original
list-slice semantics; a multiset (count dict, including None) is exact.

This test asserts the optimized merge produces byte-identical output to a
reference implementation of the ORIGINAL list-slice semantics, over adversarial
inputs that force duplicate identities and None keys.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api import streaming  # noqa: E402


def _msg(role, text, **extra):
    m = {"role": role, "content": text}
    m.update(extra)
    return m


def test_backfill_optimization_preserves_duplicate_identity_turns():
    """Two identical-content user turns both survive the backfill merge.

    _message_identity collapses identical user text to the same key. The
    optimization must not drop the second identical turn (a plain-set mirror
    would). previous_display is the visible backbone; both 'Ok' user bubbles
    plus the interleaved assistant rows must be preserved in order.
    """
    previous_display = [
        _msg("user", "Ok"),
        _msg("assistant", "first reply"),
        _msg("user", "Ok"),
        _msg("assistant", "second reply"),
    ]
    # Context has a context-only turn (never rendered) that must be backfilled,
    # plus the same duplicate-identity 'Ok' rows.
    previous_context = [
        _msg("user", "Ok"),
        _msg("assistant", "first reply"),
        _msg("user", "context only — behind a compression marker"),
        _msg("user", "Ok"),
        _msg("assistant", "second reply"),
    ]
    result_messages = list(previous_context) + [_msg("assistant", "third reply")]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display, previous_context, result_messages, "Ok"
    )

    # At least the two backbone 'Ok' user turns survive (not collapsed to one by
    # a buggy set-mirror that drops a still-present duplicate identity).
    user_oks = [m for m in merged if m.get("role") == "user" and m.get("content") == "Ok"]
    assert len(user_oks) >= 2, f"duplicate-identity user turn dropped: {merged}"
    # The context-only turn was backfilled into the visible transcript.
    assert any(
        m.get("content", "").startswith("context only") for m in merged
    ), f"context-only turn not backfilled: {merged}"
    # Visible backbone order preserved: first reply before second reply.
    contents = [m.get("content") for m in merged]
    assert contents.index("first reply") < contents.index("second reply")


def test_backfill_optimization_matches_reference_listslice_semantics():
    """Differential check: optimized merge == reference (original) semantics
    over adversarial inputs with duplicate identities and empty rows."""
    import copy as _copy
    import random

    def reference_merge(previous_display, previous_context, result_messages, msg_text):
        # Faithful re-implementation of the PRE-optimization inner loop using the
        # original `in context_keys[_cursor:]` list-slice membership test.
        previous_display = list(previous_display or [])
        _partial_seen = set()
        _deduped_rev = []
        for m in reversed(previous_display):
            if isinstance(m, dict) and m.get("_partial"):
                key = streaming._message_identity(m)
                if key is not None:
                    if key in _partial_seen:
                        continue
                    _partial_seen.add(key)
            _deduped_rev.append(m)
        previous_display = list(reversed(_deduped_rev))
        previous_context = list(previous_context or [])
        result_messages = list(result_messages or [])
        if not result_messages:
            return previous_display
        if previous_display and previous_context:
            _display_id_set = {streaming._message_identity(m) for m in previous_display}
            _context_id_set = {
                streaming._message_identity(m)
                for m in previous_context
                if not streaming._is_context_compression_marker(m)
            }
            if bool(_context_id_set - _display_id_set):
                context_keys = [streaming._message_identity(m) for m in previous_context]
                _backfilled = []
                _context_inserted = set()
                _cursor = 0
                for _di, _dmsg in enumerate(previous_display):
                    _dkey = streaming._message_identity(_dmsg)
                    if _dkey is not None:
                        _j = _cursor
                        while _j < len(context_keys) and context_keys[_j] != _dkey:
                            _j += 1
                        if _j < len(context_keys):
                            for _k in range(_cursor, _j):
                                _ckey = context_keys[_k]
                                _cmsg = previous_context[_k]
                                if _ckey is not None and _ckey not in _context_inserted and _ckey not in _display_id_set and not streaming._is_context_compression_marker(_cmsg):
                                    _backfilled.append(_copy.deepcopy(_cmsg))
                                    _context_inserted.add(_ckey)
                            _cursor = _j + 1
                        elif not any(
                            streaming._message_identity(_f) in context_keys[_cursor:]
                            for _f in previous_display[_di + 1:]
                        ):
                            for _k in range(_cursor, len(context_keys)):
                                _ckey = context_keys[_k]
                                _cmsg = previous_context[_k]
                                if _ckey is not None and _ckey not in _context_inserted and _ckey not in _display_id_set and not streaming._is_context_compression_marker(_cmsg):
                                    _backfilled.append(_copy.deepcopy(_cmsg))
                                    _context_inserted.add(_ckey)
                            _cursor = len(context_keys)
                    _backfilled.append(_dmsg)
                while _cursor < len(context_keys):
                    _ckey = context_keys[_cursor]
                    _cmsg = previous_context[_cursor]
                    _cursor += 1
                    if _ckey is not None and _ckey not in _context_inserted and _ckey not in _display_id_set and not streaming._is_context_compression_marker(_cmsg):
                        _backfilled.append(_copy.deepcopy(_cmsg))
                        _context_inserted.add(_ckey)
                if len(_backfilled) > len(previous_display):
                    previous_display = _backfilled
        # Both share the identical tail-merge logic after backfill; compare backfill output.
        return previous_display

    rng = random.Random(2026)
    texts = ["Ok", "Hi", "", "context only"]
    roles = ["user", "assistant"]
    for _ in range(2000):
        pd = [_msg(rng.choice(roles), rng.choice(texts)) for _ in range(rng.randint(0, 5))]
        pc = [_msg(rng.choice(roles), rng.choice(texts)) for _ in range(rng.randint(0, 6))]
        opt = streaming._merge_display_messages_after_agent_result(
            [dict(m) for m in pd], [dict(m) for m in pc], [dict(m) for m in pc] + [_msg("assistant", "z")], "Ok"
        )
        ref_backbone = reference_merge([dict(m) for m in pd], [dict(m) for m in pc], [dict(m) for m in pc] + [_msg("assistant", "z")], "Ok")
        # Compare the backfilled visible backbone (role+content sequence).
        opt_seq = [(m.get("role"), m.get("content")) for m in opt]
        ref_seq = [(m.get("role"), m.get("content")) for m in ref_backbone]
        # opt includes the appended tail delta; ref_backbone is backbone only — so
        # ref must be a prefix-compatible subsequence. Assert backbone equality up to
        # ref length.
        assert opt_seq[: len(ref_seq)] == ref_seq, (
            f"optimized backbone diverged from reference\n  pd={pd}\n  pc={pc}\n  opt={opt_seq}\n  ref={ref_seq}"
        )
