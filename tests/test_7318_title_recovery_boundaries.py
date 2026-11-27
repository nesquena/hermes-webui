"""Regression cases from the invalid-title recovery review."""
import pytest

from api.streaming import (
    _looks_invalid_generated_title,
    _sanitize_generated_title,
    _title_exchange_for_unresolved_title,
)


@pytest.mark.parametrize('separator', [', ', '; ', '\n'])
def test_unquoted_candidate_boundaries(separator):
    text = 'Here are some title options: ' + separator.join(['Alpha', 'Beta', 'Gamma'])
    assert _looks_invalid_generated_title(text)
    assert _sanitize_generated_title(text) == ''


def test_quoted_terms_are_not_candidate_boundaries():
    text = 'Title Suggestions: Comparing "REST" and "GraphQL"'
    assert not _looks_invalid_generated_title(text)
    assert _sanitize_generated_title(text).startswith('Title Suggestions: Comparing "REST" and "GraphQL')


def test_unfinished_latest_turn_never_becomes_the_title_source():
    """The automatic path titles from the OPENING exchange, never the last turn.

    Two invariants share this case: a trailing unanswered user row must not pull
    an older assistant reply across the boundary (the original #7318 rule), and
    it must not supply the title either (#9388). Both hold when the selector
    returns the opening pair.
    """
    messages = [
        {'role': 'user', 'content': 'Old question'},
        {'role': 'assistant', 'content': 'Old answer'},
        {'role': 'user', 'content': 'New question'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x'}]},
    ]
    assert _title_exchange_for_unresolved_title(messages) == ('Old question', 'Old answer')


def test_queued_opening_turn_pairs_forward_never_backward():
    """A queued opener takes the first answer AFTER it — never one from before.

    This is the #7318 boundary that the latest-exchange branch used to cover: a
    user+assistant pair must never be assembled by reaching backwards past a
    user row. Scanning past consecutive opening user rows (#7543) is the only
    permitted lookahead, and it moves forward, so the opener keeps its own
    question and pairs with the first complete answer that follows it.
    """
    messages = [
        {'role': 'user', 'content': 'Opening question'},
        {'role': 'user', 'content': 'Queued follow-up'},
        {'role': 'assistant', 'content': 'Answer to the follow-up'},
    ]
    assert _title_exchange_for_unresolved_title(messages) == (
        'Opening question',
        'Answer to the follow-up',
    )


def test_opening_turn_with_no_answer_anywhere_stays_unpaired():
    """No assistant text anywhere means an empty snippet, not a borrowed one."""
    messages = [
        {'role': 'user', 'content': 'Only question'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x'}]},
    ]
    assert _title_exchange_for_unresolved_title(messages) == ('Only question', '')
