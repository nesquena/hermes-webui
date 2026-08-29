"""Regression coverage for session context/display alignment."""

import copy
import random

import api.session_ops as session_ops
import pytest


def _old_matcher(context, display, keep):
    """Test-local copy of the pre-indexed matcher for differential checks."""
    if keep <= 0:
        return []
    ctx = context if isinstance(context, list) else []
    display = display if isinstance(display, list) else []
    if not ctx or not display:
        return []
    if len(ctx) == len(display):
        return ctx[:keep]

    def signature(row):
        if not isinstance(row, dict):
            return None
        tool_calls = row.get("tool_calls")
        return (
            str(row.get("role") or ""),
            str(row.get("content") or ""),
            str(row.get("tool_call_id") or ""),
            str(row.get("tool_use_id") or ""),
            str(row.get("tool_name") or row.get("name") or ""),
            session_ops.json.dumps(tool_calls, sort_keys=True, default=str)
            if tool_calls else "",
        )

    def first_match(message, start):
        msg_sig = signature(message)
        if msg_sig is None:
            return None, None
        weak = []
        for idx in range(start, len(ctx)):
            row = ctx[idx]
            row_sig = signature(row)
            if row_sig is None:
                continue
            row_id = row.get("id")
            msg_id = message.get("id")
            if row_id is not None and msg_id is not None:
                if row_id == msg_id:
                    return idx, None
                continue
            if row_sig != msg_sig:
                continue
            row_ts = row.get("timestamp")
            msg_ts = message.get("timestamp")
            if row_ts is not None and msg_ts is not None:
                if row_ts == msg_ts:
                    return idx, None
                continue
            weak.append(idx)
            if len(weak) > 1:
                return None, weak[0]
        return (weak[0], None) if len(weak) == 1 else (None, None)

    matches = []
    ambiguous = []
    start = 0
    for message in display:
        match, ambiguous_match = first_match(message, start)
        matches.append(match)
        ambiguous.append(ambiguous_match)
        if match is not None:
            start = match + 1
    if keep < len(display):
        last = matches[keep - 1] if keep else None
        first = matches[keep]
        if first is not None:
            if (
                last is not None
                and isinstance(display[keep - 1], dict)
                and display[keep - 1].get("role") == "user"
            ):
                return ctx[:last + 1]
            return ctx[:first]
        if last is not None:
            if (
                ambiguous[keep] is not None
                and isinstance(display[keep - 1], dict)
                and display[keep - 1].get("role") != "user"
            ):
                return ctx[:ambiguous[keep]]
            return ctx[:last + 1]
        if len(ctx) < len(display):
            for i in range(keep - 1, -1, -1):
                resolved = matches[i] if matches[i] is not None else ambiguous[i]
                if resolved is not None:
                    return ctx[:resolved + 1]
    prefix_len = max(0, len(ctx) - len(display))
    return ctx[:prefix_len] + ctx[prefix_len:prefix_len + keep]


def test_alignment_constructs_each_signature_once(monkeypatch):
    display = [
        {"role": "assistant", "content": f"display-{i}", "tool_calls": [{"id": i}], "id": f"d-{i}"}
        for i in range(30)
    ]
    context = [
        {"role": "assistant", "content": f"context-{i}", "tool_calls": [{"id": i}], "id": f"c-{i}"}
        for i in range(15)
    ]
    calls = 0
    original = session_ops.json.dumps

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(session_ops.json, "dumps", counted)
    session_ops.truncate_context_for_display_keep(context, display, 10)
    assert calls <= len(display) + len(context)


def test_indexed_alignment_matches_reference_for_ordering_hazards():
    cases = [
        # One weak candidate before an exact id match: exact wins.
        (
            [{"role": "assistant", "content": "x"}, {"role": "assistant", "content": "x", "id": "exact"}, {"role": "assistant", "content": "context-tail"}],
            [{"role": "assistant", "content": "x", "id": "exact"}, {"role": "assistant", "content": "tail"}],
        ),
        # Two weak candidates before a later exact match: remain ambiguous.
        (
            [{"role": "assistant", "content": "x"}, {"role": "assistant", "content": "x"}, {"role": "assistant", "content": "x", "id": "exact"}, {"role": "assistant", "content": "context-tail"}],
            [{"role": "assistant", "content": "x", "id": "exact"}, {"role": "assistant", "content": "tail"}],
        ),
        # The same ordering rule using timestamps for the exact candidate.
        (
            [{"role": "assistant", "content": "x"}, {"role": "assistant", "content": "x", "timestamp": 2}, {"role": "assistant", "content": "context-tail"}],
            [{"role": "assistant", "content": "x", "timestamp": 2}, {"role": "assistant", "content": "tail"}],
        ),
    ]
    for context, display in cases:
        expected = _old_matcher(copy.deepcopy(context), copy.deepcopy(display), 1)
        actual = session_ops.truncate_context_for_display_keep(context, display, 1)
        assert actual == expected


def test_alignment_accepts_unhashable_metadata_and_malformed_rows():
    context = [None, {"role": "assistant", "content": "x", "id": ["same"], "timestamp": {"n": 1}}, {"role": "assistant", "content": "context-tail"}]
    display = [
        {"role": "assistant", "content": "x", "id": ["same"], "timestamp": {"n": 1}},
        {"role": "assistant", "content": "tail"},
    ]
    assert session_ops.truncate_context_for_display_keep(context, display, 1) == context[:2]


class _HashableValue:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return getattr(other, "value", object()) == self.value

    def __hash__(self):
        return hash(self.value)


class _NonReflexiveHashableValue(_HashableValue):
    def __eq__(self, other):
        return False

    __hash__ = _HashableValue.__hash__


class _UnhashableValue(_HashableValue):
    __hash__ = None


class _UnhashableEqualValue:
    def __init__(self, value):
        self.value = value

    def __eq__(self, other):
        return self.value == other

    __hash__ = None


def test_alignment_preserves_cross_hashability_equality():
    context = [
        {"role": "assistant", "content": "id", "id": _UnhashableValue("same")},
        {
            "role": "assistant",
            "content": "timestamp",
            "timestamp": _UnhashableValue("same-time"),
        },
        {"role": "assistant", "content": "tail"},
    ]
    display = [
        {"role": "assistant", "content": "id", "id": _HashableValue("same")},
        {
            "role": "assistant",
            "content": "timestamp",
            "timestamp": _HashableValue("same-time"),
        },
        {"role": "assistant", "content": "tail"},
    ]
    expected = _old_matcher(copy.deepcopy(context), copy.deepcopy(display), 2)
    actual = session_ops.truncate_context_for_display_keep(context, display, 2)
    assert actual == expected == context[:2]


def test_alignment_matches_old_matcher_for_non_reflexive_id():
    nan = float("nan")
    context = [
        {"role": "assistant", "content": "x", "id": nan},
    ]
    display = [
        {"role": "assistant", "content": "x", "id": nan},
        {"role": "assistant", "content": "x"},
    ]

    expected = _old_matcher(context, display, 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)
    assert actual == expected == []


def test_alignment_matches_unsafe_context_id_against_safe_display_id():
    context = [
        {"role": "system", "content": "prefix"},
        {"role": "assistant", "content": "id-target", "id": True},
        {"role": "assistant", "content": "tail"},
    ]
    display = [
        {"role": "assistant", "content": "id-target", "id": 1},
        {"role": "assistant", "content": "tail"},
    ]

    expected = _old_matcher(copy.deepcopy(context), copy.deepcopy(display), 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)
    assert actual == expected == context[:2]


def test_alignment_matches_unsafe_context_timestamp_against_safe_display_timestamp():
    context = [
        {"role": "system", "content": "prefix"},
        {
            "role": "assistant",
            "content": "timestamp-target",
            "timestamp": _UnhashableEqualValue(7),
        },
        {"role": "assistant", "content": "tail"},
    ]
    display = [
        {"role": "assistant", "content": "timestamp-target", "timestamp": 7},
        {"role": "assistant", "content": "tail"},
    ]

    expected = _old_matcher(copy.deepcopy(context), copy.deepcopy(display), 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)
    assert actual == expected == context[:2]


def test_alignment_matches_old_matcher_for_non_reflexive_timestamp():
    nan = float("nan")
    context = [
        {"role": "assistant", "content": "x", "timestamp": nan},
    ]
    display = [
        {"role": "assistant", "content": "x", "timestamp": nan},
        {"role": "assistant", "content": "x"},
    ]

    expected = _old_matcher(context, display, 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)
    assert actual == expected == []


def test_alignment_matches_old_matcher_for_non_reflexive_hashable_object():
    value = _NonReflexiveHashableValue("same")
    context = [{"role": "assistant", "content": "x", "id": value}]
    display = [
        {"role": "assistant", "content": "x", "id": value},
        {"role": "assistant", "content": "x"},
    ]

    expected = _old_matcher(context, display, 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)
    assert actual == expected == []


def test_shorter_context_reverse_fallback_prefers_ambiguous_kept_boundary():
    exact_row = {"role": "assistant", "content": "exact", "id": "exact"}
    context = [
        exact_row,
        {"role": "assistant", "content": "x"},
        {"role": "assistant", "content": "x"},
    ]
    display = [
        {"role": "assistant", "content": "exact", "id": "exact"},
        {"role": "assistant", "content": "x"},
        {"role": "assistant", "content": "unmatched boundary"},
        {"role": "assistant", "content": "extra unmatched"},
    ]

    expected = _old_matcher(copy.deepcopy(context), copy.deepcopy(display), 2)
    actual = session_ops.truncate_context_for_display_keep(context, display, 2)
    assert actual == expected == context[:2]


def test_alignment_differential_randomized_against_origin_matcher():
    rng = random.Random(5096)
    roles = ["user", "assistant", "tool", None]
    collision_values = [True, False, 0, 0.0, 1, 1.0]
    for _ in range(500):
        context_len = rng.randrange(0, 30)
        display_len = rng.randrange(0, 30)
        def make_row(index):
            if rng.random() < 0.15:
                return rng.choice([None, "malformed", 17])
            row = {
                "role": rng.choice(roles),
                "content": rng.choice(["same", "other", ""]),
            }
            if rng.random() < 0.55:
                row["id"] = rng.choice(["a", "b", None, *collision_values])
            if rng.random() < 0.55:
                row["timestamp"] = rng.choice([1, 2, None, *collision_values])
            if rng.random() < 0.35:
                row["tool_calls"] = [{"id": index % 2}]
            return row

        context = [make_row(i) for i in range(context_len)]
        display = [make_row(i + 20) for i in range(display_len)]
        keep = rng.randrange(-1, display_len + 2)
        expected = _old_matcher(copy.deepcopy(context), copy.deepcopy(display), keep)
        actual = session_ops.truncate_context_for_display_keep(context, display, keep)
        assert actual == expected, (context, display, keep)


class _CountingDict(dict):
    def __init__(self, counter, **kwargs):
        super().__init__(**kwargs)
        self._counter = counter

    def get(self, key, default=None):
        if key == "id":
            self._counter["id_gets"] += 1
        return super().get(key, default)


def test_alignment_prefilters_timestamp_candidates_once():
    counter = {"id_gets": 0}
    context = [
        _CountingDict(
            counter, role="assistant", content="same", timestamp=7, id=f"ctx-{i}"
        )
        for i in range(500)
    ]
    display = [
        {"role": "assistant", "content": "same", "timestamp": 7, "id": f"msg-{i}"}
        for i in range(250)
    ]
    session_ops.truncate_context_for_display_keep(context, display, 125)
    assert counter["id_gets"] <= len(context) + 2


def test_alignment_preserves_reached_raising_context_signature():
    context = [
        {
            "role": "assistant",
            "content": "reached",
            "tool_calls": [{1: "integer key", "string key": "mixed keys"}],
        },
        {"role": "assistant", "content": "context-tail", "id": "tail"},
    ]
    display = [
        {"role": "assistant", "content": "display-head"},
        {"role": "assistant", "content": "display-tail"},
        {"role": "assistant", "content": "display-extra"},
    ]

    with pytest.raises(TypeError):
        _old_matcher(context, display, 1)
    with pytest.raises(TypeError):
        session_ops.truncate_context_for_display_keep(context, display, 1)


def test_alignment_defers_unreachable_raising_context_signature():
    context = [
        {"role": "assistant", "content": "first", "id": "first"},
        {"role": "assistant", "content": "second", "id": "second"},
        {
            "role": "assistant",
            "content": "unreachable",
            "tool_calls": [{1: "integer key", "string key": "mixed keys"}],
        },
    ]
    display = [
        {"role": "assistant", "content": "first", "id": "first"},
        {"role": "assistant", "content": "second", "id": "second"},
    ]

    expected = _old_matcher(context, display, 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)

    assert expected == context[:1]
    assert actual == expected


class _UnsafeEquality:
    def __eq__(self, other):
        raise AssertionError("unsafe equality was reached")

    __hash__ = object.__hash__


def test_alignment_stops_before_unsafe_id_after_weak_ambiguity():
    context = [
        {"role": "assistant", "content": "same"},
        {"role": "assistant", "content": "same"},
        {"role": "assistant", "content": "same", "id": _UnsafeEquality()},
    ]
    display = [
        {"role": "assistant", "content": "same", "id": "target"},
        {"role": "assistant", "content": "tail"},
    ]

    expected = _old_matcher(context, display, 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)

    assert expected == context[:2]
    assert actual == expected


def test_alignment_stops_before_unsafe_timestamp_after_weak_ambiguity():
    context = [
        {"role": "assistant", "content": "same"},
        {"role": "assistant", "content": "same"},
        {
            "role": "assistant",
            "content": "same",
            "timestamp": _UnsafeEquality(),
        },
    ]
    display = [
        {"role": "assistant", "content": "same", "timestamp": 7},
        {"role": "assistant", "content": "tail"},
    ]

    expected = _old_matcher(context, display, 1)
    actual = session_ops.truncate_context_for_display_keep(context, display, 1)

    assert expected == context[:2]
    assert actual == expected
