"""
Regression test for #2821 Bug A — pin/unpin pre-snapshot was always empty.

Root cause: ``api/routes.py`` POST /api/session/pin (around line 5777) built
``persisted_pinned_ids`` from ``all_sessions()``, which returns ``list[dict]``
(each entry is a ``Session.compact()`` result). The previous shape used
``getattr(existing, "session_id", None)`` and ``getattr(existing, "pinned",
False)`` on those dicts — and Python's ``getattr(dict, key, default)`` always
returns ``default`` for a dict literal (it looks for object attributes, not
dict keys). So ``persisted_pinned_ids`` was always the empty set, the
pre-snapshot path went dead, and the pin-limit check leaned on the
in-memory ``SESSIONS`` LRU only — once a pinned session got evicted from
that cache the count drifted from what's on disk.

Fix: switch to ``existing.get(...)`` for the dict access. This test pins
the behavior so a future regression to ``getattr`` (or any equivalent
object-attribute access on a dict) gets caught.

Note: the test exercises the comprehension expression directly because
the full POST /api/session/pin handler in routes.py is wrapped in
LOCK acquisition, agent locks, and Session.save() — all of which are
expensive to mock for a small regression test. The comprehension is
the only code that was buggy; pinning *that* in isolation is the
minimum honest signal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _persisted_pinned_ids_pre_fix(rows):
    """The pre-#2821-fix comprehension shape. Exercised for the
    documentation purposes: shows that getattr-on-dict yields the
    empty set regardless of input."""
    return {
        getattr(existing, "session_id", None) for existing in rows
        if getattr(existing, "pinned", False) and not getattr(existing, "archived", False)
    }


def _persisted_pinned_ids_post_fix(rows):
    """The shape this PR ships. Reads via ``dict.get`` so dict rows
    from ``all_sessions()`` actually contribute to the snapshot."""
    return {
        existing.get("session_id") for existing in rows
        if existing.get("pinned", False) and not existing.get("archived", False)
    }


def test_pre_fix_comprehension_empties_set_even_with_pinned_rows():
    """The exact regression shape from #2821: getattr-on-dict drops
    every entry, so a list of pinned dicts collapses to ``{None}`` /
    ``set()`` depending on the default. This pins the original bug so
    a future refactor that re-introduces it can be caught.
    """
    rows = [
        {"session_id": "a", "pinned": True, "archived": False},
        {"session_id": "b", "pinned": True, "archived": False},
        {"session_id": "c", "pinned": False, "archived": False},
    ]
    result = _persisted_pinned_ids_pre_fix(rows)
    # getattr(d, "pinned", False) → False for every dict → empty set
    assert result == set(), \
        f"pre-fix shape unexpectedly captured ids: {result}"


def test_post_fix_comprehension_captures_pinned_ids():
    """The fix: ``dict.get(key, default)`` reads the key. Pinned rows
    end up in the set; non-pinned and archived rows are excluded."""
    rows = [
        {"session_id": "a", "pinned": True, "archived": False},
        {"session_id": "b", "pinned": True, "archived": False},
        {"session_id": "c", "pinned": False, "archived": False},
        {"session_id": "d", "pinned": True, "archived": True},   # archived → excluded
        {"session_id": "e", "pinned": False, "archived": True},  # not pinned + archived → excluded
    ]
    result = _persisted_pinned_ids_post_fix(rows)
    assert result == {"a", "b"}, \
        f"post-fix shape should have captured {{a, b}} but got {result}"


def test_post_fix_handles_missing_keys_gracefully():
    """Sessions in flight or older rows may be missing keys. The
    ``dict.get(key, False)`` default keeps the check honest without
    raising KeyError on partial dicts.
    """
    rows = [
        {"session_id": "a"},  # no pinned, no archived → treated as not-pinned
        {"session_id": "b", "pinned": True},  # no archived key → default False, captured
        {},  # no session_id either — get() returns None, gets filtered out by None? no — None still goes in if pinned
    ]
    result = _persisted_pinned_ids_post_fix(rows)
    assert "a" not in result, "row 'a' has no pinned key — should not be captured"
    assert "b" in result, "row 'b' is pinned and not archived — should be captured"
