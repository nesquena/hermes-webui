"""Client-neutral chat attachment handoff, through real request preparation."""
import copy
from types import SimpleNamespace

import pytest

from api import routes
from api.models import Session
from api.streaming import _build_native_multimodal_message


@pytest.fixture
def intake(monkeypatch, tmp_path):
    real_start = routes._start_run
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    inbox = tmp_path / 'inbox'
    inbox.mkdir()
    monkeypatch.setenv('HERMES_WEBUI_ATTACHMENT_DIR', str(inbox))
    session = Session(session_id='attachment-test', workspace=str(workspace),
                      model='test-model', model_provider='test-provider', profile='default')
    calls = []
    monkeypatch.setattr(routes, '_agent_runtime_barrier_response', lambda **kw: None)
    monkeypatch.setattr(routes, '_get_or_materialize_session', lambda *a, **kw: session)
    monkeypatch.setattr(routes, '_resolve_chat_workspace_with_recovery', lambda *a: str(workspace))
    monkeypatch.setattr(routes, '_resolve_chat_workspace_for_regeneration', lambda *a: str(workspace))
    monkeypatch.setattr(routes, '_read_profile_model_config', lambda *a: (None, None, {}))
    monkeypatch.setattr(routes, '_resolve_compatible_session_model_state',
                        lambda *a, **kw: ('test-model', 'test-provider', False))
    monkeypatch.setattr(routes, '_repair_foreign_session_model_provider',
                        lambda *a, **kw: 'test-provider')
    monkeypatch.setattr(routes, 'get_config_snapshot', lambda: {})
    monkeypatch.setattr(routes, 'webui_gateway_chat_enabled', lambda cfg: False)
    monkeypatch.setattr(routes, 'j', lambda h, payload, status=200: (status, payload))
    monkeypatch.setattr(routes, 'bad', lambda h, error, status=400: (status, {'error': error}))

    def start(s, **kw):
        calls.append(copy.deepcopy(kw))
        return {'stream_id': 'test-stream'}

    monkeypatch.setattr(routes, '_start_run', start)

    def submit(text='Describe this', attachments=None, **kw):
        return routes._handle_chat_start(None, dict(
            session_id=session.session_id, message=text,
            attachments=attachments or [], **kw))

    def file(name='photo.jpg', where=None):
        target = (where or workspace) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'\xff\xd8\xffexample-image')
        return {'name': name, 'path': str(target), 'mime': 'image/jpeg',
                'size': target.stat().st_size, 'is_image': True}

    return SimpleNamespace(submit=submit, file=file, calls=calls, session=session,
                           workspace=workspace, inbox=inbox, real_start=real_start)


@pytest.mark.parametrize('gateway', [False, True])
@pytest.mark.parametrize('save_mode', ['eager', 'deferred'])
def test_request_journal_and_worker_receive_the_same_turn(intake, monkeypatch, tmp_path, gateway, save_mode):
    from api import models, turn_journal, gateway_chat
    import threading

    monkeypatch.setattr(routes, '_start_run', intake.real_start)
    monkeypatch.setattr('api.runtime_adapter.runtime_adapter_enabled', lambda: False)
    monkeypatch.setattr('api.runtime_adapter.runtime_adapter_runner_enabled', lambda: False)
    monkeypatch.setattr(routes, 'webui_gateway_chat_enabled', lambda cfg: gateway)
    monkeypatch.setattr(routes, 'get_webui_session_save_mode', lambda: save_mode)
    monkeypatch.setattr(routes, '_active_run_stream_for_session', lambda sid: None)
    monkeypatch.setattr(routes, 'set_last_workspace', lambda path: None)
    monkeypatch.setattr(routes, 'publish_session_list_changed', lambda *a, **kw: None)
    monkeypatch.setattr(gateway_chat, '_mark_gateway_run_starting', lambda sid: None)
    monkeypatch.setattr(turn_journal, '_default_session_dir', lambda: tmp_path / 'journal')
    monkeypatch.setattr(models, 'SESSION_DIR', tmp_path / 'sessions')
    (tmp_path / 'sessions').mkdir()
    workers = []

    class Worker:
        def __init__(self, *, target, args, kwargs, daemon):
            self.target, self.args = target, args

        def start(self):
            events = turn_journal.read_turn_journal(intake.session.session_id)['events']
            # The submitted row must be durable before the worker is started.
            workers.append((self.target, self.args, events[-1]))

    monkeypatch.setattr(routes, 'threading', SimpleNamespace(
        Thread=Worker, Lock=threading.Lock, RLock=threading.RLock))
    att = intake.file()
    status, body = intake.submit('Read', [att])
    assert status == 200, body
    assert len(workers) == 1
    target, args, event = workers[0]
    expected = f'Read\n\n[Attached files: {att["path"]}]'
    assert target is (routes._run_gateway_chat_streaming if gateway else routes._run_agent_streaming)
    assert args[1] == event['content'] == intake.session.pending_user_message == expected
    assert args[5] == event['attachments'] == intake.session.pending_attachments
    assert event['event'] == 'submitted'
    # Worker launch is intercepted, not the production intake/persistence path.
    import json
    saved = json.loads((tmp_path / 'sessions' / f'{intake.session.session_id}.json').read_text())
    assert saved['pending_user_message'] == expected
    assert saved['pending_attachments'] == args[5]
    stream_id = body['stream_id']
    routes.STREAMS.pop(stream_id, None)
    routes.unregister_stream_owner(stream_id)


@pytest.mark.parametrize('save_mode', ['eager', 'deferred'])
def test_canonical_turn_survives_checkpoint_recovery_and_regeneration(intake, monkeypatch, tmp_path, save_mode):
    import json
    from api.models import _append_recovered_pending_turn
    from api.session_ops import plan_regeneration
    from api.streaming import _context_messages_for_new_turn
    from api.turn_journal import append_turn_journal_event, read_turn_journal

    att = intake.file()
    assert intake.submit('Read this', [att])[0] == 200
    turn = intake.calls[-1]
    expected = f'Read this\n\n[Attached files: {att["path"]}]'
    assert turn['msg'] == expected
    monkeypatch.setattr(routes, 'get_webui_session_save_mode', lambda: save_mode)
    routes._prepare_chat_start_session_for_stream(
        intake.session, msg=turn['msg'], attachments=turn['attachments'],
        workspace=turn['workspace'], model=turn['model'], model_provider=turn['model_provider'],
        stream_id='checkpoint-stream', started_at=123, defer_save=True)
    assert intake.session.pending_user_message == expected
    if save_mode == 'eager':
        assert intake.session.messages[0]['content'] == expected
    event = append_turn_journal_event(intake.session.session_id, {
        'event': 'submitted', 'stream_id': 'checkpoint-stream',
        'content': intake.session.pending_user_message,
        'attachments': intake.session.pending_attachments,
    }, session_dir=tmp_path)
    events = read_turn_journal(intake.session.session_id, session_dir=tmp_path)['events']
    assert events[-1]['content'] == expected
    assert events[-1]['turn_id'] == event['turn_id']
    # JSON boundary, followed by the real pending-turn recovery and context builder.
    restored = Session(session_id='restored', workspace=str(intake.workspace),
                       pending_user_message=json.loads(json.dumps(expected)),
                       pending_attachments=json.loads(json.dumps(turn['attachments'])))
    _append_recovered_pending_turn(restored, timestamp=123)
    _append_recovered_pending_turn(restored, timestamp=123)
    context = _context_messages_for_new_turn(restored, 'Follow up')
    assert len([m for m in context if m.get('content') == expected]) == 1
    # Regeneration is only admitted after the recovered turn has settled.
    restored.pending_user_message = None
    restored.pending_attachments = []
    restored.messages.append({'role': 'assistant', 'content': 'Done'})
    restored.context_messages = copy.deepcopy(restored.messages)
    plan = plan_regeneration(restored)
    assert plan.turn.message_text == expected
    assert plan.turn.message_text.count('[Attached files:') == 1


def test_regeneration_does_not_reformat_retained_turn(intake, monkeypatch):
    att = intake.file()
    expected = f'Read\n\n[Attached files: {att["path"]}]'
    retained = SimpleNamespace(turn=SimpleNamespace(message_text=expected, attachments=[att]))
    monkeypatch.setattr('api.session_ops.plan_regeneration', lambda *a, **kw: retained)
    monkeypatch.setattr('api.runtime_adapter.runtime_adapter_runner_enabled', lambda: False)
    status, _ = routes._handle_chat_start(None, {
        'session_id': intake.session.session_id, 'regenerate': True,
        'regeneration_revision': 'revision'})
    assert status == 200
    assert intake.calls[-1]['msg'] == expected


@pytest.mark.parametrize('gateway', [False, True])
@pytest.mark.parametrize('text', ['Describe this', ''])
def test_structured_attachment_reaches_model_without_client_suffix(intake, monkeypatch, gateway, text):
    monkeypatch.setattr(routes, 'webui_gateway_chat_enabled', lambda cfg: gateway)
    att = intake.file(where=intake.inbox / intake.session.session_id)
    status, body = intake.submit(text, [att])
    assert status == 200, body
    turn = intake.calls[-1]
    expected = (f'{text}\n\n[Attached files: {att["path"]}]' if text else
                f"Uploaded: photo.jpg\n\n[Attached files: {att['path']}]")
    assert turn['msg'] == expected
    assert turn['attachments'][0]['path'] == att['path']
    assert turn['gateway_chat_enabled'] is gateway
    content = _build_native_multimodal_message('', turn['msg'], turn['attachments'],
                                              str(intake.workspace),
                                              cfg={'agent': {'image_input_mode': 'text'}})
    assert content == expected


def test_text_only_and_empty_admission(intake):
    assert intake.submit('plain')[0] == 200
    assert intake.calls[-1]['msg'] == 'plain'
    intake.calls.clear()
    assert intake.submit('')[0] == 400
    assert not intake.calls


def test_multiple_files_and_extracted_directory_keep_order(intake):
    first = intake.file('résumé 1.txt')
    directory = intake.workspace / 'archive'
    directory.mkdir()
    second = {'name': 'archive', 'path': str(directory), 'extracted': 2}
    assert intake.submit('Read these', [first, second, first])[0] == 200
    assert intake.calls[-1]['msg'] == (
        f'Read these\n\n[Attached files: {first["path"]}, {directory}, {first["path"]}]')


@pytest.mark.parametrize('invalid', ['missing', 'outside', 'symlink', 'empty', 'malformed'])
def test_invalid_attachment_rejects_entire_turn(intake, tmp_path, invalid):
    valid = intake.file()
    if invalid == 'missing':
        bad = {'path': str(intake.workspace / 'missing.jpg')}
    elif invalid == 'outside':
        bad = intake.file('private.jpg', where=tmp_path)
    elif invalid == 'symlink':
        outside = intake.file('private.jpg', where=tmp_path)
        link = intake.workspace / 'escape.jpg'
        link.symlink_to(outside['path'])
        bad = {'path': str(link)}
    elif invalid == 'empty':
        bad = {'name': 'missing.jpg', 'path': ''}
    else:
        bad = {'path': {'not': 'a path'}}
    status, _ = intake.submit('Read both', [valid, bad])
    assert status == 400
    assert not intake.calls
    assert not intake.session.pending_user_message


@pytest.mark.parametrize('value', [123, ['photo.jpg'], {'name': 'photo.jpg'}, True])
def test_non_string_path_is_rejected_even_when_stringified_file_exists(intake, value):
    intake.file(str(value))
    status, _ = intake.submit('Read this', [{'path': value}])
    assert status == 400
    assert not intake.calls


def test_filename_alias_and_attachment_cap(intake):
    att = intake.file()
    att['filename'] = att.pop('name')
    assert intake.submit('Read', [att] * 21)[0] == 200
    assert len(intake.calls[-1]['attachments']) == 20
    assert intake.calls[-1]['msg'].count(att['path']) == 20


def test_mixed_native_image_and_document_keep_references(intake):
    image = intake.file()
    document = intake.file('report.pdf')
    document.update(mime='application/pdf', is_image=False)
    assert intake.submit('Compare', [image, document])[0] == 200
    turn = intake.calls[-1]
    content = _build_native_multimodal_message(
        '', turn['msg'], turn['attachments'], str(intake.workspace),
        cfg={'agent': {'image_input_mode': 'native'}})
    assert content[0] == {'type': 'text', 'text': turn['msg']}
    assert image['path'] in content[0]['text']
    assert document['path'] in content[0]['text']
    assert len(content) == 2
    assert content[1]['type'] == 'image_url'
    assert content[1]['image_url']['url'].startswith('data:image/jpeg;base64,')


def test_relative_workspace_reference_is_canonical_for_text_and_embedding(intake):
    att = intake.file('photo [1].jpg')
    absolute = att['path']
    att['path'] = att['name']
    assert intake.submit('Read', [att])[0] == 200
    turn = intake.calls[-1]
    assert turn['attachments'][0]['path'] == absolute
    assert turn['msg'] == f'Read\n\n[Attached files: {absolute}]'


def test_rejected_request_does_not_modify_existing_pending_turn(intake):
    intake.session.pending_user_message = 'Original turn'
    intake.session.pending_attachments = [{'path': '/original/file.jpg'}]
    before = copy.deepcopy(intake.session.__dict__)
    assert intake.submit('New turn', [{'path': '/missing/file.jpg'}])[0] == 400
    assert intake.session.__dict__ == before
    assert not intake.calls


def test_new_turn_does_not_reuse_previous_attachments(intake):
    att = intake.file()
    assert intake.submit('First', [att])[0] == 200
    assert intake.submit('Second')[0] == 200
    assert att['path'] in intake.calls[0]['msg']
    assert intake.calls[1]['msg'] == 'Second'
    assert intake.calls[1]['attachments'] == []
