"""#7653 re-gate round 17 — ownerless ordinal keys and history-prefix counting.

The 11-23 re-review (item 3) proved the remaining duplication with four keys
from one composed run: ``call:call_0@1#0`` / ``call:call_0@3#1`` plus the
ownerless journal spellings ``call:call_0#0`` / ``call:call_0#1``. Two causes:

- ``_anchor_scene_row_tid_fallback_index`` split only at ``@``, so an ownerless
  ``tool:<tid>#<occ>`` key parsed the ordinal as part of the tid and the two
  spellings of one occurrence never paired;
- journal replay numbered occurrences from this turn's first message, while
  the settled/hydrated projections count RESOLVED results from the first
  message of the supplied history, so a later turn's single call hydrated as
  two.

These drive the REAL ``api.routes`` helpers.
"""

from __future__ import annotations

from api import routes


# ---------------------------------------------------------------------------
# _anchor_scene_row_tid_fallback_index — ownerless ordinal spellings
# ---------------------------------------------------------------------------


def test_tid_fallback_index_splits_ownerless_occurrence_key():
    """``tool:<tid>#<occ>`` pairs with its own occurrence, not with a bare tid."""
    seen = {"tool:call_0#0": 0, "tool:call_0#1": 1}
    assert routes._anchor_scene_row_tid_fallback_index("tool:call_0#1", seen) == 1
    assert routes._anchor_scene_row_tid_fallback_index("tool:call_0#0", seen) == 0


def test_tid_fallback_index_refuses_ambiguous_bare_key():
    """A key that names no ordinal cannot pick between two occurrences."""
    seen = {"tool:call_0#0": 0, "tool:call_0#1": 1}
    assert routes._anchor_scene_row_tid_fallback_index("tool:call_0", seen) is None


def test_tid_fallback_index_matches_both_coordinate_spellings():
    """``tool:<tid>@<owner>#<occ>`` and ``tool:<tid>#<occ>`` are one occurrence."""
    seen = {"tool:call_0@3#1": 2}
    assert routes._anchor_scene_row_tid_fallback_index("tool:call_0#1", seen) == 2


def test_tid_fallback_index_keeps_bare_and_owner_keys_working():
    seen = {"tool:call_0": 0, "tool:other@2": 1}
    assert routes._anchor_scene_row_tid_fallback_index("tool:call_0@1", seen) == 0
    assert routes._anchor_scene_row_tid_fallback_index("tool:other", seen) == 1


# ---------------------------------------------------------------------------
# _anchor_scene_tool_call_occurrences — history-prefix numbering
# ---------------------------------------------------------------------------


def test_journal_occurrences_count_the_history_prefix():
    """A later turn's single call is occurrence 1, not 0.

    Turn 1 emits and resolves ``call_0``; turn 2 emits it again. The journal
    replay must number turn 2's call as the SECOND occurrence, exactly as the
    settled/hydrated projections do — a restart at 0 made the one call render
    as two cards.
    """
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "call_id": "call_0", "name": "terminal", "input": {}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_0", "content": "ok"},
        {"role": "user", "content": "again"},
        {
            "role": "assistant",
            "content": "run",
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
    ]
    out = routes._anchor_scene_tool_call_occurrences(messages, 2, 4)
    assert out[(3, "tool_calls", 0)] == 1


def test_journal_occurrences_ignore_unresolved_calls_in_the_prefix():
    """A prefix call that never resolved must not shift the later occurrence.

    The settled projections count RESOLVED results only, so the replay has to
    use the same rule: turn 1's declared-but-unfinished ``call_0`` contributes
    nothing and turn 2's resolved call stays occurrence 0.
    """
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "call_id": "call_0", "name": "terminal", "input": {}}
            ],
        },
        {"role": "user", "content": "again"},
        {
            "role": "assistant",
            "content": "run",
            "tool_calls": [
                {
                    "id": "call_0",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }
            ],
        },
    ]
    out = routes._anchor_scene_tool_call_occurrences(messages, 1, 3)
    assert out[(2, "tool_calls", 0)] == 0
