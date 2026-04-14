"""Sprint 42 tests: context indicator prefers latest usage over stale session data (issue #437)"""
import os

SESSIONS_JS = os.path.join(os.path.dirname(__file__), '..', 'static', 'sessions.js')


def _read_sessions_js():
    with open(SESSIONS_JS, 'r') as f:
        return f.read()


def test_context_indicator_uses_pick_helper():
    """The _pick helper must be present in sessions.js to prefer latest over stale values."""
    content = _read_sessions_js()
    assert '_pick' in content, "_pick helper not found in static/sessions.js"


def test_context_indicator_old_pattern_removed():
    """The old || pattern that preferred stale session data must be gone."""
    content = _read_sessions_js()
    assert '_s.input_tokens||u.input_tokens' not in content, \
        "Old stale-data-first pattern '_s.input_tokens||u.input_tokens' still present in static/sessions.js"


def test_context_indicator_all_six_fields():
    """All six token/cost fields must appear in the _syncCtxIndicator call."""
    content = _read_sessions_js()
    fields = [
        'input_tokens',
        'output_tokens',
        'estimated_cost',
        'context_length',
        'last_prompt_tokens',
        'threshold_tokens',
    ]
    for field in fields:
        assert field in content, \
            f"Field '{field}' not found in static/sessions.js _syncCtxIndicator call"
