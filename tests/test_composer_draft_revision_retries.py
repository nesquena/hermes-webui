"""Draft create-only publication and same-profile checkpoint recovery."""
import json
from pathlib import Path
import shutil
import subprocess
import threading

import pytest

from tests.test_composer_draft_profile_races import PRIVATE, env as env


def test_first_shell_does_not_replace_new_foreign_owner(env, monkeypatch):
    e = env
    original = e.m.Session(session_id='probe-owner', profile='default')
    e.m.SESSIONS[original.session_id] = original
    foreign = e.m.Session(session_id=original.session_id, profile='bravo',
                          composer_draft=PRIVATE)
    ready, published = threading.Event(), threading.Event()
    errors, intermediate = [], []
    caller = threading.current_thread()
    real_fsync = e.m.os.fsync

    def fsync(fd):
        real_fsync(fd)
        if threading.current_thread() is caller and not published.is_set():
            ready.set()
            assert published.wait(5), 'foreign skip-index save blocked by first-shell I/O'
            assert not errors
            intermediate.append(json.loads(foreign.path.read_text())['profile'])

    def publish():
        try:
            assert ready.wait(5)
            foreign.save(touch_updated_at=False, skip_index=True)
        except BaseException as exc:
            errors.append(exc)
        finally:
            published.set()

    monkeypatch.setattr(e.m.os, 'fsync', fsync)
    thread = threading.Thread(target=publish)
    thread.start()
    try:
        status, payload = e.call(text='first default draft')
    finally:
        ready.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    final = json.loads(original.path.read_text())
    actual = dict(status=status, payload=payload, intermediate_owners=intermediate,
                  final_owner=final['profile'], draft=e.m._load_session_draft(original.session_id))
    print('FIRST_SHELL_RESULT', json.dumps(actual, sort_keys=True))
    assert intermediate == ['bravo']
    assert final['profile'] == 'bravo', actual
    assert status == 409 and 'draft' not in payload


@pytest.mark.parametrize('skip_index', [True, False])
def test_same_profile_save_conflict_recovery(env, monkeypatch, skip_index):
    e = env
    original = e.owner(composer_draft={'text': 'last durable draft', 'files': []},
                       active_stream_id='synthetic-stream', pending_user_message='synthetic pending')
    e.m.SESSIONS[original.session_id] = original
    ready, published = threading.Event(), threading.Event()
    errors = []
    caller = threading.current_thread()
    real_fsync = e.m.os.fsync

    def fsync(fd):
        real_fsync(fd)
        if threading.current_thread() is caller:
            ready.set()
            assert published.wait(5), 'Session.save blocked on draft fsync'
            assert not errors

    def publish():
        try:
            assert ready.wait(5)
            # The same Session.save operation used by the streaming checkpoint.
            original.save(skip_index=skip_index)
        except BaseException as exc:
            errors.append(exc)
        finally:
            published.set()

    monkeypatch.setattr(e.m.os, 'fsync', fsync)
    thread = threading.Thread(target=publish)
    thread.start()
    try:
        status, payload = e.call(text='typed while streaming')
    finally:
        ready.set()
        thread.join(6)
    assert not thread.is_alive() and not errors
    assert status == 200 and payload['draft']['text'] == 'typed while streaming'
    durable = e.m.get_session_for_draft(original.session_id).composer_draft
    assert durable['text'] == 'typed while streaming'
    if shutil.which('node') is None:
        pytest.skip('Backend recovery verified; Node is required for the reload probe')
    data = {'status': status, 'body': payload, 'durable': durable}
    script = Path(__file__).with_name('probe_draft_client.js')
    result = subprocess.run(['node', str(script), str(Path.cwd() / 'static/sessions.js'), json.dumps(data)],
                            capture_output=True, text=True, timeout=20)
    print('SAME_PROFILE_CLIENT_RESULT', result.stdout, result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
