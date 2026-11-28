"""Mounted draft handler + real client recovery after four checkpoint conflicts.

Synthetic stores only; Node fresh realms share localStorage. Request transport is
line-oriented IPC, not authenticated HTTP or a physical browser.
"""
import json
from pathlib import Path
import selectors
import shutil
import subprocess

import pytest

from tests.test_composer_draft_profile_races import env as env


@pytest.mark.parametrize('skip_index', [False, True], ids=['indexed', 'skip-index'])
def test_client_recovers_pending_draft_after_checkpoint_budget(env, monkeypatch, skip_index):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for real client realm regression')
    assert node is not None
    e = env
    owner = e.owner(composer_draft={'text': 'last durable draft', 'files': []},
                    active_stream_id='synthetic-stream', pending_user_message='synthetic pending')
    e.m.SESSIONS[owner.session_id] = owner
    real_fsync = e.m.os.fsync
    publications = []
    publishing = False

    def fsync(fd):
        nonlocal publishing
        real_fsync(fd)
        if not publishing and len(publications) < 4:
            publishing = True
            try:
                owner.save(touch_updated_at=False, skip_index=skip_index)
                publications.append(True)
            finally:
                publishing = False

    monkeypatch.setattr(e.m.os, 'fsync', fsync)
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.Popen([node, str(Path(__file__).with_name('probe_composer_draft_durability.js')),
                             str(root / 'static/sessions.js'), json.dumps({'bridge': 2})],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    statuses = []
    output = []
    assert proc.stdout is not None and proc.stdin is not None and proc.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            assert selector.select(20), 'client IPC deadline exceeded'
            line = proc.stdout.readline()
            if not line:
                break
            output.append(line)
            if not line.startswith('{"request":'):
                continue
            body = json.loads(line)['request']
            sid = body.pop('session_id')
            status, payload = e.call(sid=sid, **body)
            statuses.append(status)
            proc.stdin.write(json.dumps({'status': status, 'body': payload}) + '\n')
            proc.stdin.flush()
        assert proc.wait(timeout=5) == 0, ''.join(output) + proc.stderr.read()
    finally:
        selector.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()
    assert statuses == [409, 200]
    assert len(publications) == 4
    assert e.m.get_session_for_draft(owner.session_id).composer_draft['text'] == 'typed while streaming'
    assert not list(e.folder.rglob('*.tmp.*'))
