"""#8039 release-stage follow-up: a recovered prompt that opens the history and was answered is kept.

A first turn interrupted by a restart is saved as ``[recovered prompt, journaled answer, error marker]``
(``_recover_journaled_output_and_terminal_error`` in ``api/models.py``). The shared rule used to keep a
``_recovered`` user row only between two assistant turns, so the question was dropped and the next turn's history
started with the answer on both backends (master's Gateway path sent it). Found by Codex (CORE) and the senior review
on the #8039 gate.
"""
from __future__ import annotations

import pytest

from tests.test_issue8034_gateway_runs_history_filter import ERROR_ROW, _gateway_history, _pairs
from tests.test_issue8038_gateway_runs_history_rows import _legacy_pairs

Q1 = {"role": "user", "content": "first question", "timestamp": 10, "_recovered": True}
A1 = {"role": "assistant", "content": "journaled answer", "timestamp": 11}
Q2 = {"role": "user", "content": "second question", "timestamp": 12, "_recovered": True}
A2 = {"role": "assistant", "content": "second answer", "timestamp": 13}
FOLLOW = {"role": "user", "content": "follow up", "timestamp": 14}


@pytest.mark.parametrize("rows, expected", [
    # first turn interrupted by a restart, then the user continues
    ([Q1, A1, ERROR_ROW, FOLLOW], [("user", "first question"), ("assistant", "journaled answer"), ("user", "follow up")]),
    # two restarts: the unanswered first prompt is stale, the answered second one opens the history
    ([Q1, ERROR_ROW, Q2, A2, FOLLOW], [("user", "second question"), ("assistant", "second answer"), ("user", "follow up")]),
    # an unanswered recovered prompt as the only row stays out
    ([Q1, FOLLOW], [("user", "follow up")]),
], ids=["first-turn-answered", "two-restarts", "unanswered"])
def test_a_leading_recovered_prompt_with_its_answer_is_kept_on_both_backends(rows, expected):
    assert _pairs(_gateway_history(rows, tag="recovered-leading")) == expected
    assert _legacy_pairs(rows) == expected
