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


def test_unfinished_latest_turn_never_pairs_with_older_assistant():
    messages = [
        {'role': 'user', 'content': 'Old question'},
        {'role': 'assistant', 'content': 'Old answer'},
        {'role': 'user', 'content': 'New question'},
        {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'x'}]},
    ]
    assert _title_exchange_for_unresolved_title(messages) == ('New question', '')
