"""Context replay retains its greedy/block and summary identity semantics."""

import copy
import json
import random

import pytest

from api import streaming


def reference_summary(previous, candidate):
    if not isinstance(previous, dict) or not isinstance(candidate, dict):
        return False
    if previous.get("role") != candidate.get("role"):
        return False

    def wire(row):
        value = row.get("api_content")
        return " ".join(value.split()) if isinstance(value, str) and value else None

    if wire(previous) != wire(candidate):
        return False
    a = " ".join(streaming._message_text(previous.get("content", "")).split())
    b = " ".join(streaming._message_text(candidate.get("content", "")).split())
    return (
        len(a) >= 2000
        and len(b) >= 2000
        and a.startswith("[Session Arc Summary")
        and b.startswith("[Session Arc Summary")
        and a[:1500] == b[:1500]
    )


def reference(existing, incoming):
    old = [streaming._message_replay_key(m) for m in existing]
    new = [streaming._message_replay_key(m) for m in incoming]
    cleaned = []
    idx = 0
    while idx < len(incoming):
        if any(reference_summary(m, incoming[idx]) for m in existing):
            idx += 1
            continue
        best = 0
        for start in range(len(old)):
            length = 0
            while (
                idx + length < len(new)
                and start + length < len(old)
                and new[idx + length] == old[start + length]
            ):
                length += 1
            best = max(best, length)
        if best >= 3:
            idx += best
        else:
            cleaned.append(incoming[idx])
            idx += 1
    return cleaned


def test_context_replay_matches_reference_for_periodic_and_structured_rows():
    rows = [
        None,
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "again"},
        {"role": "assistant", "content": "answer"},
        {"role": "assistant", "content": "answer", "api_content": "wire A"},
        {"role": "assistant", "content": "answer", "api_content": "wire B"},
        {"role": "assistant", "content": [{"type": "text", "text": "answer"}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call"}]},
        {"role": "tool", "content": "ok", "tool_call_id": "call"},
    ]
    rng = random.Random(8073)
    for _ in range(1500):
        old = [copy.deepcopy(rng.choice(rows)) for _ in range(rng.randrange(30))]
        new = [copy.deepcopy(rng.choice(rows)) for _ in range(rng.randrange(30))]
        if old and rng.random() < 0.7:
            start = rng.randrange(len(old))
            insert_at = rng.randrange(len(new) + 1)
            new[insert_at:insert_at] = copy.deepcopy(old[start:])
        before = copy.deepcopy((old, new))
        result = streaming._strip_replayed_context_items(old, new)
        assert result == reference(old, new)
        assert json.dumps(result) == json.dumps(reference(old, new))
        assert all(any(row is candidate for candidate in new) for row in result)
        assert (old, new) == before


def test_summary_role_payload_and_refreshed_hint_rules_stay_exact():
    text = "[Session Arc Summary " + ("long context " * 200)
    old = [{"role": "user", "content": text + " hint A", "api_content": "wire   same"}]
    for role in ("user", "assistant", None, ["unusual"]):
        for api_content in (None, "wire same", "wire different", {"opaque": 1}):
            new = [
                {"role": role, "content": text + " hint B", "api_content": api_content},
                {"role": role, "content": text[:1600], "api_content": api_content},
            ]
            assert streaming._strip_replayed_context_items(old, new) == reference(
                old, new
            )
    old[0]["role"] = ["unusual"]
    new = [{**old[0], "content": text + " another hint"}]
    assert (
        streaming._strip_replayed_context_items(old, new) == reference(old, new) == []
    )


def test_nonmatching_context_normalization_is_linear(monkeypatch):
    size = 200
    old = [{"role": "assistant", "content": f"old {i}"} for i in range(size)]
    new = [{"role": "assistant", "content": f"new {i}"} for i in range(size)]
    original = streaming._message_text
    calls = 0

    def counted(value):
        nonlocal calls
        calls += 1
        assert calls <= 8 * size, "re-normalized old context for every candidate"
        return original(value)

    monkeypatch.setattr(streaming, "_message_text", counted)
    assert streaming._strip_replayed_context_items(old, new) == new


@pytest.mark.parametrize("shape", ["disjoint", "periodic", "near_miss"])
def test_block_search_has_linear_comparison_budget(monkeypatch, shape):
    size = 256
    comparisons = 0

    class Key:
        def __init__(self, value):
            self.value = value

        def __hash__(self):
            return hash(self.value)

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            assert comparisons < size * 32, (
                "rescanned every old block for each candidate"
            )
            return self.value == other.value

    monkeypatch.setattr(
        streaming, "_message_replay_key", lambda row: Key(row["content"])
    )
    old = [{"role": "assistant", "content": f"old {i}"} for i in range(size)]
    new = [{"role": "assistant", "content": f"new {i}"} for i in range(size)]
    expected = new
    if shape != "disjoint":
        old = [{"role": "assistant", "content": "repeated"} for _ in range(size)]
        new = copy.deepcopy(old)
        expected = []
        if shape == "near_miss":
            old[-1]["content"] = "old tail"
            new[-1]["content"] = "new tail"
            expected = new[-1:]
    assert streaming._strip_replayed_context_items(old, new) == expected


@pytest.mark.parametrize(
    "old,new", [(None, None), ([], []), ([], [{"content": "new"}]), ([None], [])]
)
def test_empty_inputs(old, new):
    assert streaming._strip_replayed_context_items(old, new) == list(new or [])


def test_greedy_longest_block_retains_short_repeats_and_new_rows():
    def rows(text):
        return [{"role": "assistant", "content": token} for token in text.split()]

    old = rows("a b c d x a b c y")
    new = rows("a b c d a b fresh a b c y tail")
    assert streaming._strip_replayed_context_items(old, new) == rows("a b fresh tail")
    assert streaming._strip_replayed_context_items(old, rows("a b")) == rows("a b")
    assert streaming._strip_replayed_context_items(old, rows("a b c")) == []


@pytest.mark.parametrize("length", [1999, 2000, 2001])
def test_summary_length_marker_and_prefix_boundaries(length):
    text = ("[Session Arc Summary " + "x" * length)[:length]
    old = [{"role": "user", "content": text}]
    for change in (0, 1499, 1500, length - 1):
        new = [{"role": "user", "content": text[:change] + "z" + text[change + 1 :]}]
        expected = reference(old, new)
        assert streaming._strip_replayed_context_items(old, new) == expected
        assert streaming._looks_like_replayed_session_arc_summary(
            old[0], new[0]
        ) == reference_summary(old[0], new[0])


@pytest.mark.parametrize("entry", ["prefix", "assistant_delta", "repaired_user"])
def test_context_reconciliation_call_sites_keep_current_turn_and_history(entry):
    block = [{"role": "assistant", "content": f"old {i}"} for i in range(3)]
    old = block + [{"role": "user", "content": "earlier question"}]
    fresh = {"role": "assistant", "content": "fresh answer"}
    current = {"role": "user", "content": "current question"}
    if entry == "prefix":
        incoming = copy.deepcopy(old) + [current] + copy.deepcopy(block) + [fresh]
        expected = old + [current, fresh]
    elif entry == "assistant_delta":
        incoming = copy.deepcopy(block) + [fresh]
        expected = old + [fresh]
    else:
        incoming = copy.deepcopy(old[:-1]) + [current] + copy.deepcopy(block) + [fresh]
        expected = old + [current, fresh]
    before = copy.deepcopy((old, incoming))
    assert (
        streaming._dedupe_replayed_context_messages(old, incoming, "current question")
        == expected
    )
    assert (old, incoming) == before
