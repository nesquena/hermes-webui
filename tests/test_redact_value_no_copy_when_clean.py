"""Redaction: do not rebuild what has not changed.

``_redact_value`` used to rebuild every dict/list in full, i.e. a deep copy of
the whole payload on every response, even when nothing is redacted. On a 22 MB
session, 97.4% of the strings contain no sensitive marker: the copy is
therefore pure wasted work, and it holds the GIL (json/allocations do not
release it), which serializes the tabs.

Contract verified here:
  1. When nothing is redacted, the RETURNED value IS the original object
     (identity, not just equality) and no container proportional to the
     payload is allocated along the way.
  2. When something is redacted, a NEW object is returned and the original
     is NOT mutated (fail-closed: no leak through aliasing).
  3. The result stays equal to the one of the previous implementation.
"""
import sys
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.helpers import _redact_value  # noqa: E402


SECRET = "authorization=Bearer sk-live-abcdef0123456789"


def _reference_redact(v, *, _enabled):
    """Original implementation (systematic copy), kept for comparison."""
    from api.helpers import _redact_text

    if isinstance(v, str):
        return _redact_text(v, _enabled=_enabled)
    if isinstance(v, dict):
        return {k: _reference_redact(x, _enabled=_enabled) for k, x in v.items()}
    if isinstance(v, list):
        return [_reference_redact(x, _enabled=_enabled) for x in v]
    return v


def test_clean_payload_is_returned_by_identity():
    """Nothing sensitive -> no dict/list must be rebuilt."""
    payload = {
        "messages": [
            {"role": "user", "content": "hello, where is order 4512 at?"},
            {"role": "assistant", "content": [{"type": "text", "text": "it ships tomorrow"}]},
        ],
        "meta": {"workspace": "/workspace/project", "count": 3, "ok": True},
    }

    out = _redact_value(payload, _enabled=True)

    assert out is payload, "clean payload rebuilt needlessly"
    assert out["messages"] is payload["messages"]
    assert out["messages"][0] is payload["messages"][0]
    assert out["meta"] is payload["meta"]


def test_clean_payload_does_not_allocate_eager_container_copies():
    """The clean path must stay O(1) in temporary container allocation."""
    payload = {
        "dict": {index: index for index in range(20_000)},
        "list": list(range(20_000)),
    }

    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        out = _redact_value(payload, _enabled=True)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not was_tracing:
            tracemalloc.stop()

    assert out is payload
    # A temporary list of 20,000 pointers alone exceeds 160 KB. The threshold
    # deliberately stays an order of magnitude below that, without depending
    # on the exact allocator details of a given CPython version.
    assert peak - before < 16_384, f"temporary copies detected: {peak - before} bytes"


def test_sensitive_payload_is_copied_and_original_untouched():
    """Something is redacted -> new object, original intact."""
    inner = {"role": "user", "content": SECRET}
    payload = {"messages": [inner], "meta": {"workspace": "/tmp"}}

    out = _redact_value(payload, _enabled=True)

    # a new object is returned along the modified path
    assert out is not payload
    assert out["messages"] is not payload["messages"]
    assert out["messages"][0] is not inner

    # the original is not mutated (no leak through aliasing)
    assert inner["content"] == SECRET
    assert payload["messages"][0]["content"] == SECRET

    # the secret is indeed masked in the output
    assert out["messages"][0]["content"] != SECRET

    # the UNmodified branches stay shared (that is the whole point)
    assert out["meta"] is payload["meta"]


def test_copy_on_first_change_preserves_order_types_and_clean_identity():
    """A late redaction copies only its own path without reordering the JSON."""
    before = {"clean": ["before", 1]}
    after = {"clean": ["after", 2]}
    payload = {
        "before": before,
        "items": [before, {"secret": SECRET}, after],
        "after": after,
    }

    out = _redact_value(payload, _enabled=True)

    assert type(out) is dict
    assert type(out["items"]) is list
    assert list(out) == list(payload)
    assert out["before"] is before
    assert out["after"] is after
    assert out["items"][0] is before
    assert out["items"][2] is after
    assert out["items"][1] is not payload["items"][1]
    assert out["items"][1]["secret"] != SECRET


def test_matches_reference_implementation():
    """The result must stay identical to the original implementation."""
    payload = {
        "messages": [
            {"role": "user", "content": "plain text"},
            {"role": "assistant", "content": SECRET},
            {"role": "user", "content": ["clean", SECRET, 42, None]},
        ],
        "nested": {"a": {"b": {"c": SECRET}}, "d": {"e": "nothing"}},
        "scalars": [1, 2.5, True, None],
    }

    assert _redact_value(payload, _enabled=True) == _reference_redact(payload, _enabled=True)


def test_disabled_redaction_still_shares():
    """Redaction disabled -> nothing must be copied."""
    payload = {"messages": [{"role": "user", "content": SECRET}]}
    out = _redact_value(payload, _enabled=False)
    assert out is payload


def test_non_container_values_pass_through():
    for v in (42, 2.5, True, None):
        assert _redact_value(v, _enabled=True) is v
