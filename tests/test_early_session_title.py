import sys
import time
from concurrent.futures import Future
from io import BytesIO
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

import pytest


def test_prepare_chat_start_sets_provisional_title_for_default_session(tmp_path, monkeypatch):
    from api.models import Session
    from api.routes import _prepare_chat_start_session_for_stream

    saved = []

    def fake_save(self, *args, **kwargs):
        saved.append(
            {
                "title": self.title,
                "pending_user_message": self.pending_user_message,
                "active_stream_id": self.active_stream_id,
            }
        )

    monkeypatch.setattr(Session, "save", fake_save)

    s = Session(session_id="test-early-title", title="Untitled")
    _prepare_chat_start_session_for_stream(
        s,
        msg="Can you conclude whether early WebUI session titles are possible?",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-1",
        started_at=123.0,
    )

    assert s.title != "Untitled"
    assert "early WebUI session titles" in s.title or s.title.startswith("Can you conclude")
    assert saved[-1]["title"] == s.title
    assert saved[-1]["pending_user_message"] == "Can you conclude whether early WebUI session titles are possible?"
    assert saved[-1]["active_stream_id"] == "stream-1"


def test_prepare_chat_start_sets_provisional_title_in_eager_save_mode(tmp_path, monkeypatch):
    from api.models import Session
    import api.routes as routes

    saved = []

    def fake_save(self, *args, **kwargs):
        saved.append({"title": self.title, "messages": list(self.messages)})

    monkeypatch.setattr(Session, "save", fake_save)
    monkeypatch.setattr(routes, "get_webui_session_save_mode", lambda: "eager")

    s = Session(session_id="test-eager-early-title", title="Untitled")
    routes._prepare_chat_start_session_for_stream(
        s,
        msg="Can eager session save also get early titles?",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-1",
        started_at=123.0,
    )

    assert s.title != "Untitled"
    assert s.messages[-1]["role"] == "user"
    assert s.messages[-1]["content"] == "Can eager session save also get early titles?"
    assert saved[-1]["title"] == s.title


def test_prepare_chat_start_does_not_overwrite_manual_title(tmp_path, monkeypatch):
    from api.models import Session
    from api.routes import _prepare_chat_start_session_for_stream

    monkeypatch.setattr(Session, "save", lambda self, *a, **k: None)

    s = Session(session_id="test-manual-title", title="My Manual Title")
    _prepare_chat_start_session_for_stream(
        s,
        msg="This prompt should not replace the title",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
        stream_id="stream-1",
        started_at=123.0,
    )

    assert s.title == "My Manual Title"


def test_start_chat_stream_response_includes_provisional_title(tmp_path, monkeypatch):
    from api.models import Session
    import api.routes as routes

    monkeypatch.setattr(Session, "save", lambda self, *a, **k: None)
    monkeypatch.setattr(routes, "set_last_workspace", lambda workspace, **_kw: None)
    monkeypatch.setattr(routes, "create_stream_channel", lambda: object())
    monkeypatch.setattr(routes, "_run_agent_streaming", lambda *a, **k: None)

    class ImmediateThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            return None

    monkeypatch.setattr(routes.threading, "Thread", ImmediateThread)

    s = Session(session_id="test-start-response-title", title="Untitled")
    response = routes._start_chat_stream_for_session(
        s,
        msg="Please design early session titles for Hermes WebUI",
        attachments=[],
        workspace=str(tmp_path),
        model="test-model",
        model_provider=None,
    )

    try:
        routes.STREAMS.pop(response["stream_id"], None)
    except Exception:
        pass

    assert response["title"] == s.title
    assert response["title"] != "Untitled"


def test_prompt_provisional_title_still_counts_as_provisional_after_response():
    from api.models import title_from
    from api.streaming import _is_provisional_title

    messages = [
        {"role": "user", "content": "Can you implement early session titles in Hermes WebUI?"},
        {"role": "assistant", "content": "Yes, here is the plan..."},
    ]
    provisional = title_from(messages, "Untitled")
    assert _is_provisional_title(provisional, messages)


def test_prompt_prefix_manual_title_is_not_treated_as_provisional():
    from api.models import title_from
    from api.streaming import _is_provisional_title

    messages = [
        {"role": "user", "content": "Can you implement early session titles in Hermes WebUI?"},
        {"role": "assistant", "content": "Yes, here is the plan..."},
    ]
    provisional = title_from(messages, "Untitled")
    manual_prefix = provisional[: max(3, min(12, len(provisional) - 1))]

    assert manual_prefix
    assert provisional.startswith(manual_prefix)
    assert manual_prefix != provisional
    assert not _is_provisional_title(manual_prefix, messages)


def test_messages_js_applies_chat_start_title():
    src = Path("static/messages.js").read_text(encoding="utf-8")
    assert "applySessionTitleUpdate" in src
    assert "startData.title" in src or "provisional_title" in src
    assert "addEventListener('title'" in src
    assert "_sessionTitleLooksDefaultOrProvisional" in src
    assert "options.force" in src
    assert "_sessionTitleProvisionalBySid" in src
    assert "rememberProvisional:true" in src
    assert "provisionalText:displayText.slice(0,64)" in src


@pytest.mark.parametrize('timing', ['early', 'late', 'after_fallback', 'manual', 'cancelled'])
def test_agent_title_reaches_saved_session_and_sidebar(tmp_path, monkeypatch, timing):
    from api import models, streaming
    from api.session_events import subscribe_session_events, unsubscribe_session_events
    from api.session_ops import apply_session_title_rename

    monkeypatch.setattr(models, 'SESSION_DIR', tmp_path)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', tmp_path / '_index.json')
    monkeypatch.setattr(models, 'SESSIONS', models.SESSIONS.copy())
    monkeypatch.setattr(streaming, 'SESSIONS', models.SESSIONS)

    prompt = 'Investigate why the title in the sidebar contains my opening sentence'
    messages = [
        {'role': 'user', 'content': prompt, 'timestamp': 123},
        {'role': 'assistant', 'content': 'The two stores disagree.', 'timestamp': 124},
    ]
    provisional = models.title_from(messages)
    session = models.Session(
        session_id='agent-title-session', profile='work', title=provisional, messages=messages,
    )
    session.save()
    models.SESSIONS[session.session_id] = session
    updated_at = session.updated_at
    fallback = {'fallback_title': None}
    settled = Future()
    events = subscribe_session_events()
    summary = 'Investigate sidebar title synchronization'
    try:
        on_title = streaming._agent_title_callback(
            session.session_id, 'work', settled, fallback, clear_generation=session.clear_generation,
        )
        on_title(provisional, 'derived')
        assert events.empty()
        if timing == 'early':
            on_title(summary, 'llm')
            assert models.Session.load(session.session_id).title == provisional
            assert events.empty()
        if timing == 'after_fallback':
            session.title = 'Local automatic summary'
            session.llm_title_generated = True
            fallback['fallback_title'] = session.title
            session.save(touch_updated_at=False)
        if timing == 'manual':
            apply_session_title_rename(session, 'Keep my title')
            session.save(touch_updated_at=False)
        if timing == 'cancelled':
            settled.cancel()
        else:
            settled.set_result(None)
        if timing != 'early':
            on_title(summary, 'llm')

        saved = models.Session.load(session.session_id)
        assert saved.updated_at == updated_at
        assert saved.messages == messages
        if timing in ('manual', 'cancelled'):
            assert saved.title == ('Keep my title' if timing == 'manual' else provisional)
            assert events.empty()
        else:
            assert saved.title == summary
            assert saved.llm_title_generated and not saved.manual_title
            event = events.get_nowait()
            assert event['reason'] == 'agent_title'
            assert event['profile'] == 'work'
            assert event['session_id'] == session.session_id
            on_title(summary, 'llm')
            assert events.empty()
    finally:
        unsubscribe_session_events(events)


@pytest.mark.parametrize('clear', [False, True])
def test_agent_title_callback_survives_stream_end(tmp_path, monkeypatch, clear):
    from api import config, models, routes, streaming
    from api.session_events import subscribe_session_events, unsubscribe_session_events

    monkeypatch.setattr(models, 'SESSION_DIR', tmp_path)
    monkeypatch.setattr(models, 'SESSION_INDEX_FILE', tmp_path / '_index.json')
    monkeypatch.setattr(streaming, 'SESSION_DIR', tmp_path)
    monkeypatch.setattr(config, 'SESSION_DIR', tmp_path)
    monkeypatch.setattr(models, '_sync_sidecar_from_state_db_if_newer', lambda *_a, **_kw: False)
    monkeypatch.setattr('api.state_sync.sync_session_cwd_background', lambda *_a, **_kw: None)
    monkeypatch.setattr(config, 'get_config', lambda *_a, **_kw: {})
    monkeypatch.setattr(config, '_resolve_cli_toolsets', lambda *_a, **_kw: [])
    monkeypatch.setitem(sys.modules, 'hermes_state', SimpleNamespace(SessionDB=lambda *_a, **_kw: object()))
    monkeypatch.setattr(streaming, 'resolve_model_provider', lambda *_a, **_kw: ('gpt-4o', 'openai', None))
    monkeypatch.setattr(streaming, '_title_exchange_for_unresolved_title', lambda _messages: (None, None))

    callbacks = []

    class ControlledAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs['session_id']
            self.context_compressor = None
            self.session_prompt_tokens = self.session_completion_tokens = 0
            self.session_estimated_cost_usd = None
            self.reasoning_config = self.ephemeral_system_prompt = None

        def run_conversation(self, **kwargs):
            callbacks.append(self._on_session_title)
            return {'messages': [
                {'role': 'user', 'content': kwargs['persist_user_message']},
                {'role': 'assistant', 'content': 'I checked the title update.'},
            ]}

        def commit_memory_session(self):
            pass

    monkeypatch.setattr(streaming, '_get_ai_agent', lambda: ControlledAgent)
    sid = 'late-title-after-clear' if clear else 'late-title-after-stream-end'
    session = models.Session(session_id=sid, workspace=str(tmp_path), model='gpt-4o')
    monkeypatch.setitem(models.SESSIONS, sid, session)

    def run_turn(prompt, stream_id):
        session.active_stream_id = stream_id
        session.pending_user_message = prompt
        session.pending_started_at = time.time()
        session.pending_user_source = 'webui'
        session.save()
        frames = Queue()
        monkeypatch.setitem(config.STREAMS, stream_id, frames)
        config.register_stream_owner(stream_id, sid)
        streaming._run_agent_streaming(sid, prompt, 'gpt-4o', str(tmp_path), stream_id)
        assert [frame[0] for frame in frames.queue if frame[0] in ('done', 'stream_end', 'apperror')] == [
            'done', 'stream_end',
        ]

    try:
        run_turn('Investigate the first sidebar issue', 'late-title-first')
        if clear:
            body = ('{"session_id":"%s"}' % sid).encode()
            handler = SimpleNamespace(headers={'Content-Length': str(len(body))}, rfile=BytesIO(body))
            monkeypatch.setattr(routes, '_check_csrf', lambda _handler: True)
            monkeypatch.setattr(routes, 'j', lambda _handler, payload: payload)
            assert routes.handle_post(handler, SimpleNamespace(path='/api/session/clear'))['ok']
            assert models.Session.load(sid).clear_generation
            run_turn('Investigate a different sidebar issue', 'late-title-second')

        events = subscribe_session_events()
        try:
            if clear:
                provisional = models.Session.load(sid).title
                callbacks[0]('Old conversation summary', 'llm')
                assert models.Session.load(sid).title == provisional
                assert events.empty()
            callbacks[-1]('Current conversation summary', 'llm')
            assert models.Session.load(sid).title == 'Current conversation summary'
            event = events.get_nowait()
            assert event['session_id'] == sid
            assert event['reason'] == 'agent_title'
        finally:
            unsubscribe_session_events(events)
    finally:
        config._evict_session_agent(sid)
