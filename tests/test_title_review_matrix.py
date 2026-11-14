"""PR #7539 review matrix: real workers and durable session metadata."""
import json
from unittest.mock import patch

import pytest
import api.streaming as streaming
from tests._aux_client_helpers import auxiliary_client_modules, patch_tg_config
from tests import test_title_extraction_guards as guards
from tests.test_title_reasoning_trace_guard import _fixture_agent, _response

worker_scene = guards.scene

SUBJECTS = (
    'Fix [1, 2] Parsing Bug', 'Handle {} In Config Parser',
    'Parse {"a": 1} Payload Example', 'Login Repair [2026]',
    'Fix array[0], Index Error', 'Debug [] Empty List Handling',
    'Compare [1,2,3] vs (1,2,3)', 'Explain {"ok": true} Responses',
    'Maybe Monad Error Handling', 'Options: Portfolio Optimization',
    'Topic Label Accessibility', 'Options: Stocks, Bonds, ETFs',
    'Maybe Later Feature Flags', 'Understanding 3-8 Words in Regex',
    'Good Title: A Style Guide', 'Inspect {}', 'Compare [1, 2]',
    '[1, 2] Parsing Tutorial', '{} Config Parser Guide',
    'Surely JSON Is Useful', 'Sure Thing Configuration',
    'Sure: A Confidence Guide',
    'Array [0] Index Out Of Range', 'Fix [1] Footnote Rendering',
    'Python list[str] Typing Help', 'Support {name} Placeholders',
    'Use ["a", "b"] As Default List', 'Parse {"key": "value"} Example',
    'Perhaps Later Roadmap Notes', 'Maybe Monad vs Either',
    'Maybe We Should Talk Podcast', 'Perhaps I Will Review',
    'Maybe I Should Learn Rust vs Go', "Maybe I'll Be There Lyrics",
    'Maybe Use Cases For Monads', 'Perhaps, Perhaps, Perhaps Lyrics',
    'Maybe-Monad Pattern', 'Maybelline Product Review',
    "Rename 'title' Field In Django", 'Session Title Bar Layout',
)
# Visible text that still carries a JSON title payload, or a hedged reasoning
# sentence: never saved verbatim, always one compatibility recovery call.
LEAKS = (
    '*Maybe* I should call it Rejected Candidate',
    '_Maybe_ let me think about Rejected Candidate',
    '__Perhaps__ I should call it Rejected Candidate',
    '**Perhaps** we could name it Rejected Candidate',
    'Title: {“title”: “Rejected Candidate”}',
    r'Title: {\"title\": \"Rejected Candidate\"}',
    'Title: {"title": "Rejected Candidate"}',
    'I think {"title": "Rejected Candidate"} works',
    '{"title": "Rejected Candidate"} is my answer',
    '{"title": "Rejected Candidate"} Done',
    '{"title": "Rejected Candidate"}{"title": "Other"}',
    '{"title": "Rejected Candidate"} {"title": "Other"}',
    '["Option A", "Option B"] Title Ideas',
    '{"reasoning": "user wants login", "title": "Rejected"} ok',
    '{"title": "Rejected", "extra": "forbidden"} Parser Notes',
    '{"result": {"title": "Nested Pick"}} Parser Guide',
    '```json\n{"title": "Rejected Candidate"}\n```\nDone',
    '{"title": "Rejected Candidate"} trailing}',
    'Okay, here is the JSON: {"title": "Rejected Candidate"}',
    "Here's the JSON: {\"title\": \"Rejected Candidate\"}",
    'Surely {"title": "Rejected Candidate"}',
    "Here's the JSON: {\"title\": \"Rejected Candidate\"",
    'Sure thing! {"result": {"title": "Nested Pick"}',
    'Output: [{"title": "Nested Pick"',
    'Maybe I should call it Rejected Candidate',
    'Maybe the user wants a login title',
    'Perhaps something like Rejected Candidate',
    'Maybe we could go with Rejected Candidate',
    # A title label is stripped by the sanitizer, so a container after it is
    # the whole candidate.
    'Title: ["Rejected Candidate", "Other Option"]',
    '**Title:** ["Rejected Candidate", "Other Option"]',
    'Session title: ["Rejected Candidate", "Other Option"]',
    'Title: {"name": "Rejected Candidate"}',
    'Title: {"titles": ["Rejected Candidate", "Other"]}',
    "Title: {'title': 'Rejected Candidate'}",
    'Title: {‘title’: ‘Rejected Candidate’}',
    # JSON strings that themselves carry a payload.
    json.dumps(json.dumps({'title': 'Rejected Candidate'})),
    json.dumps({'title': json.dumps({'title': 'Rejected Candidate'})}),
    json.dumps({'title': json.dumps(['Rejected Candidate', 'Other'])}),
    # Hedged sentences: casing, contractions, verbs and mixed emphasis.
    'Maybe We should call it Rejected Candidate',
    "Maybe I'll call it Rejected Candidate",
    'Maybe we can call it Rejected Candidate',
    'Maybe the title could be Rejected Candidate',
    'MAYBE I SHOULD CALL IT REJECTED CANDIDATE',
    '*Maybe*, *I should call it Rejected Candidate*',
    '**Maybe** _I should_ call it Rejected Candidate',
    '_Maybe_ __we__ could go with Rejected Candidate',
)
BROKEN = (
    'Here is the JSON: {"title": "Rejected Candidate"',
    'Here is the JSON: {"result": {"title": "Nested Pick"}',
    'Here is the JSON: [{"title": "Nested Pick"',
    'Sure. {"result": {"title": "Nested Pick"}',
    'Here is the JSON: ["Rejected Candidate"',
    'Here is the JSON: [-1, true',
    '```json\n{"title": "Rejected Candidate"\n```',
    'Here is the JSON: ```json\n{"title": "Rejected Candidate"\n```',
    'Here is the JSON: {"title": "Rejected Candidate" trailing prose',
    '```json\n[{"title": "Rejected Candidate"}\n```',
)


def run_worker(worker_scene, monkeypatch, route, refresh, answers):
    session, path = worker_scene
    if refresh:
        session.title = 'Existing Title'
        session.llm_title_generated = True
        session.save()
    old_title = session.title
    monkeypatch.setattr(streaming, '_aux_title_configured', lambda: route == 'aux')
    calls, events = [], []

    def respond(**kwargs):
        calls.append(kwargs)
        return _response(answers[min(len(calls) - 1, len(answers) - 1)])

    agent = _fixture_agent(respond) if route == 'agent' else None
    worker = streaming._run_background_title_refresh if refresh else streaming._run_background_title_update
    with auxiliary_client_modules(), patch_tg_config({'provider': 'custom', 'model': 'fixture'}):
        with patch('agent.auxiliary_client.call_llm', side_effect=respond, create=True):
            worker(session.session_id, 'Fix login.', 'Inspect login.', old_title,
                   lambda name, data: events.append((name, data)), agent=agent)
    # No file means no Session.save(): the provisional title was kept.
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    return session, saved, calls, events


@pytest.mark.parametrize('route', ('aux', 'agent'))
@pytest.mark.parametrize('refresh', (False, True))
@pytest.mark.parametrize('schema', (False, True))
@pytest.mark.parametrize('title', SUBJECTS)
def test_review_ordinary_titles_persist(worker_scene, monkeypatch, route, refresh, schema, title):
    answer = json.dumps({'title': title}) if schema else title
    session, saved, calls, events = run_worker(worker_scene, monkeypatch, route, refresh, [answer])
    assert session.title == saved['title'] == title
    assert saved['llm_title_generated'] is True
    assert len(calls) == 1
    assert [data['title'] for name, data in events if name == 'title'] == [title]


@pytest.mark.parametrize('route', ('aux', 'agent'))
@pytest.mark.parametrize('refresh', (False, True))
@pytest.mark.parametrize('content', BROKEN + LEAKS)
def test_review_broken_envelope_recovers(worker_scene, monkeypatch, route, refresh, content):
    session, saved, calls, events = run_worker(
        worker_scene, monkeypatch, route, refresh, [content, 'Valid Recovery Title'])
    assert session.title == saved['title'] == 'Valid Recovery Title'
    assert saved['llm_title_generated'] is True
    assert len(calls) == 2
    assert [data['title'] for name, data in events if name == 'title'] == ['Valid Recovery Title']


@pytest.mark.parametrize('route', ('aux', 'agent'))
@pytest.mark.parametrize('refresh', (False, True))
@pytest.mark.parametrize('content', LEAKS)
def test_review_persistent_leak_never_saved(worker_scene, monkeypatch, route, refresh, content):
    session, saved, calls, events = run_worker(worker_scene, monkeypatch, route, refresh, [content])
    published = [data['title'] for name, data in events if name == 'title']
    if saved is None:
        assert not refresh and session.title == 'Untitled' and not published
        return
    for title in [saved['title'], session.title, *published]:
        assert title != content
        assert 'Rejected' not in title and 'Nested' not in title and '"title"' not in title
    if refresh:
        assert saved['title'] == 'Existing Title' and not published


@pytest.mark.parametrize("count", (32, 1000, 10000))
def test_hedge_chains_are_bounded(count):
    assert streaming._is_bad_new_title("Maybe " * count + "Login Repair")
