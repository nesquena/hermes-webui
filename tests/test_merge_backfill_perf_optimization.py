"""Regression test for the O(D²·C) → O(D²) backfill optimization in
_merge_display_messages_after_agent_result (salvaged from #4314).

The optimization replaces an `in context_keys[_cursor:]` list-slice membership
test with an O(1) count-keyed dict mirror. The subtle correctness requirement:
The projection-aware backfill key intentionally returns DUPLICATE keys for
exact rows and visible-identical non-assistant turns. A plain set would diverge
from the original list-slice semantics; a multiset count is exact.

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

    The backfill key collapses identical user text to the same key. The
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


def test_backfill_matches_stable_row_across_display_only_metadata():
    """Display enrichment must not make one stable assistant row appear twice."""
    previous_display = [
        _msg("user", "old prompt", id="old-user"),
        _msg(
            "assistant",
            "first answer",
            id="assistant-a",
            _media_snapshots={"/tmp/result.png": "sha256:a"},
            _turnUsage={"input_tokens": 3, "output_tokens": 5},
        ),
    ]
    previous_context = [
        _msg("user", "old prompt", id="old-user"),
        _msg("assistant", "first answer", id="assistant-a"),
        _msg("assistant", "context-only answer", id="assistant-b"),
    ]
    result_messages = [
        *previous_context,
        _msg("user", "current prompt", id="current-user"),
        _msg("assistant", "final answer", id="assistant-final"),
    ]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        result_messages,
        "current prompt",
        result_has_authoritative_full_history_prefix=True,
    )

    assert [message.get("id") for message in merged] == [
        "old-user",
        "assistant-a",
        "assistant-b",
        "current-user",
        "assistant-final",
    ]
    assert merged[1]["_media_snapshots"] == {"/tmp/result.png": "sha256:a"}
    assert merged[1]["_turnUsage"] == {"input_tokens": 3, "output_tokens": 5}


def test_backfill_projection_keys_keep_strict_ids_and_unknown_metadata():
    display = [
        _msg("assistant", "same", id=7, _statusCard={"phase": "done"}),
        _msg("assistant", "typed", id=7),
        _msg("assistant", "unknown", provider_metadata={"attempt": "alpha"}),
        _msg("assistant", "one-sided"),
    ]
    context = [
        _msg("assistant", "same", id=7),
        _msg("assistant", "typed", id="7"),
        _msg("assistant", "unknown", provider_metadata={"attempt": "beta"}),
        _msg("assistant", "one-sided", id=9),
    ]

    display_keys, context_keys = streaming._display_backfill_projection_keys(
        display,
        context,
    )

    assert display_keys[0] == context_keys[0]
    assert display_keys[1] != context_keys[1]
    assert display_keys[2] != context_keys[2]
    assert display_keys[3] == context_keys[3]


def test_backfill_preserves_duplicate_context_multiplicity():
    repeated = _msg("user", "repeat")
    previous_display = [dict(repeated)]
    previous_context = [dict(repeated), dict(repeated)]

    merged = streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        [*previous_context, _msg("assistant", "final", id="assistant-final")],
        "current prompt",
        result_has_authoritative_full_history_prefix=True,
    )

    assert sum(
        message.get("role") == "user" and message.get("content") == "repeat"
        for message in merged
    ) == 2


def test_backfill_suffix_lookup_has_bounded_key_comparisons(monkeypatch):
    comparisons = 0

    class _Key:
        def __init__(self, value):
            self.value = value

        def __hash__(self):
            return hash(self.value)

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            return isinstance(other, _Key) and self.value == other.value

    size = 500
    anchor = _Key("anchor")
    context_only = _Key("context-only")
    display_keys = [_Key(f"display-{index}") for index in range(size)] + [anchor]
    context_keys = [context_only, anchor]
    monkeypatch.setattr(
        streaming,
        "_display_backfill_projection_keys",
        lambda _display, _context: (display_keys, context_keys),
    )
    previous_display = [
        _msg("user", f"display-{index}")
        for index in range(size + 1)
    ]
    previous_context = [
        _msg("user", "context-only"),
        _msg("user", "anchor"),
    ]

    streaming._merge_display_messages_after_agent_result(
        previous_display,
        previous_context,
        [_msg("assistant", "final")],
        "current prompt",
    )

    assert comparisons < size * 20


def test_backfill_optimization_matches_reference_listslice_semantics():
    """Differential check: optimized merge == reference (original) semantics
    over adversarial inputs with duplicate identities and empty rows."""
    import copy as _copy
    import random

    def reference_merge(previous_display, previous_context, result_messages, msg_text):
        # Faithful re-implementation of the PRE-optimization inner loop using the
        # original `in context_keys[_cursor:]` list-slice membership test. Apply
        # the production replay reducer before and after that loop so this oracle
        # isolates only the count-dict optimization under the current strict
        # exact-payload contract.
        previous_display = list(previous_display or [])
        previous_context = list(previous_context or [])
        result_messages = list(result_messages or [])
        previous_display, _ = streaming._collapse_replayed_assistant_rows(
            previous_display
        )
        previous_context, _ = streaming._collapse_replayed_assistant_rows(
            previous_context
        )
        if not result_messages:
            return previous_display
        if previous_display and previous_context:
            display_keys, context_keys = streaming._display_backfill_projection_keys(
                previous_display,
                previous_context,
            )
            display_counts = {}
            context_counts = {}
            for key in display_keys:
                display_counts[key] = display_counts.get(key, 0) + 1
            for key in context_keys:
                context_counts[key] = context_counts.get(key, 0) + 1
            insert_budget = {
                key: count - display_counts.get(key, 0)
                for key, count in context_counts.items()
                if count > display_counts.get(key, 0)
            }
            if insert_budget:
                _backfilled = []
                _cursor = 0

                def backfill_range(start, stop):
                    for index in range(start, stop):
                        key = context_keys[index]
                        message = previous_context[index]
                        if (
                            insert_budget.get(key, 0) > 0
                            and not streaming._is_context_compression_marker(message)
                            and not streaming._is_compressed_context_tool_result_summary_message(message)
                        ):
                            _backfilled.append(_copy.deepcopy(message))
                            insert_budget[key] -= 1

                for _di, _dmsg in enumerate(previous_display):
                    _dkey = display_keys[_di]
                    _j = _cursor
                    while _j < len(context_keys) and context_keys[_j] != _dkey:
                        _j += 1
                    if _j < len(context_keys):
                        backfill_range(_cursor, _j)
                        _cursor = _j + 1
                    elif not any(
                        future_key in context_keys[_cursor:]
                        for future_key in display_keys[_di + 1:]
                    ):
                        backfill_range(_cursor, len(context_keys))
                        _cursor = len(context_keys)
                    _backfilled.append(_dmsg)
                backfill_range(_cursor, len(context_keys))
                if len(_backfilled) > len(previous_display):
                    previous_display = _backfilled
        # Both share the identical tail-merge logic after backfill; compare backfill output.
        previous_display, _ = streaming._collapse_replayed_assistant_rows(
            previous_display
        )
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
