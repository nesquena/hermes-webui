"""Independent actual advisor oracles; synthetic backend state only."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import pytest
from api import models, run_journal
from tests.test_cancel_restart_journal_recovery import (
    _isolated_state,  # noqa: F401 -- disposable actual journal/session fixture
    _persist_recovery_boundary_turn,
    _simulate_restart,
)
from tests.test_webui_state_db_reconciliation import _make_state_db


CHILD_A = r'''
import errno,json,os,sys,time
from pathlib import Path
from api import run_journal as j
root,control=map(Path,sys.argv[1:3])
path=j._run_path('overlap-session','overlap-run',session_dir=root)
actual=os.fsync
def held_fail(fd):
    if os.fstat(fd).st_ino != path.stat().st_ino:
        return actual(fd)
    (control/'a-entered.json').write_text(json.dumps({'attempted_seq': json.loads(path.read_bytes().splitlines()[-1])['seq']}))
    deadline=time.monotonic()+12
    while not (control/'release-a').exists():
        if time.monotonic()>deadline: raise TimeoutError('own child A release deadline')
        time.sleep(.01)
    raise OSError(errno.EIO,'independent delayed fsync failure')
os.fsync=held_fail
try:
    j.RunJournalWriter('overlap-session','overlap-run',session_dir=root).append_sse_event('done',{'owner':'A'})
except OSError as exc:
    print(json.dumps({'failed': True, 'errno': exc.errno}))
else:
    raise AssertionError('fsync failure was not injected')
'''

CHILD_B = r'''
import json,os,sys
from pathlib import Path
from api import run_journal as j
root=Path(sys.argv[1]);path=j._run_path('overlap-session','overlap-run',session_dir=root)
actual=os.fsync;count=0
def observed(fd):
    global count
    if os.fstat(fd).st_ino==path.stat().st_ino: count+=1
    return actual(fd)
os.fsync=observed
row=j.RunJournalWriter('overlap-session','overlap-run',session_dir=root).append_sse_event('stream_end',{'owner':'B','text':'B_COMMITTED'})
assert count>=1
print(json.dumps({'committed_seq':row['seq'],'real_fsync_calls':count}))
'''



def test_failed_child_append_preserves_other_child_fsynced_row(tmp_path):
    root, control = tmp_path/'sessions', tmp_path/'control'
    control.mkdir()
    run_journal.RunJournalWriter('overlap-session','overlap-run',session_dir=root).append_sse_event('token',{'text':'PREFIX'})
    path = run_journal._run_path('overlap-session','overlap-run',session_dir=root)
    env = os.environ.copy()
    env['HERMES_WEBUI_RUN_JOURNAL_FSYNC'] = 'terminal-only'
    a = subprocess.Popen([sys.executable, '-c', CHILD_A, str(root), str(control)],
                         cwd=os.getcwd(),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    b = None
    try:
        deadline = time.monotonic()+8
        while not (control/'a-entered.json').exists():
            assert a.poll() is None, 'own child A exited before injected fsync'
            assert time.monotonic()<deadline
            time.sleep(.01)
        a_entered = json.loads((control/'a-entered.json').read_text())
        b = subprocess.Popen([sys.executable, '-c', CHILD_B, str(root)],
                             cwd=os.getcwd(),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        deadline = time.monotonic()+1
        while b.poll() is None and time.monotonic()<deadline:
            time.sleep(.01)
        peer_committed_before_release = b.poll() == 0
    finally:
        (control/'release-a').touch()
    a_out,a_err = a.communicate(timeout=15)
    assert a.returncode == 0, a_err
    assert b is not None
    b_out,b_err = b.communicate(timeout=15)
    assert b.returncode == 0, b_err
    rows = [json.loads(raw) for raw in path.read_bytes().splitlines()]
    assert json.loads(a_out)['failed'] is True
    assert json.loads(b_out)['real_fsync_calls'] >= 1
    assert a_entered['attempted_seq'] == 2
    assert not peer_committed_before_release, 'writer B must wait for A rollback'
    assert any(row.get('payload',{}).get('owner')=='B' for row in rows), 'failed A rollback deleted successful B fsynced row'


@pytest.mark.parametrize('kind', ['ordinary', 'cancel', 'untagged-control', 'live-partial-control'])
def test_real_sqlite_new_turn_after_recovered_journal(kind, tmp_path, monkeypatch):
    sid,stream = 'sqlite-recovered-'+kind, 'sqlite-recovered-run-'+kind
    db = tmp_path/'state.db'
    monkeypatch.setattr(models,'_active_state_db_path',lambda: db)
    _persist_recovery_boundary_turn(sid,stream,'stop' if kind=='cancel' else 'crash')
    run_journal.RunJournalWriter(sid,stream).append_sse_event('token',{'text':'RECOVERED_PREFIX'})
    _simulate_restart()
    session = models.get_session(sid)
    recovered = next(row for row in session.messages if row.get('content')=='RECOVERED_PREFIX')
    assert recovered.get('_recovered_from_run_journal') is True
    assert any(row.get('_error') for row in session.messages)
    if kind=='cancel':
        assert recovered.get('_recovered_from_cancel_journal') is True
    elif kind=='untagged-control':
        recovered.pop('_recovered_from_run_journal')
    elif kind=='live-partial-control':
        recovered['_partial']=True
    original = copy.deepcopy(session.messages)
    stamp = max(float(row.get('timestamp') or 0) for row in original)+10
    rows = [{'role':row['role'],'content':row.get('content',''),'timestamp':row.get('timestamp',10)}
            for row in original if not row.get('_error')]
    rows += [{'role':'user','content':'LATER_GATEWAY_REQUEST','timestamp':stamp},
             {'role':'assistant','content':'LATER_GATEWAY_ANSWER','timestamp':stamp+1}]
    _make_state_db(db,sid,rows)
    persisted = models.get_state_db_session_messages(sid)
    assert any(row.get('content')=='LATER_GATEWAY_ANSWER' for row in persisted)
    merged = models.reconciled_state_db_messages_for_session(session)
    assert session.messages == original
    if kind == 'live-partial-control':
        assert merged == original
    else:
        assert any(row.get('content')=='LATER_GATEWAY_REQUEST' for row in merged)
        assert any(row.get('content')=='LATER_GATEWAY_ANSWER' for row in merged)


def test_cached_writer_reseeds_after_other_interpreter_commits(tmp_path):
    root = tmp_path / 'sessions'
    writer = run_journal.RunJournalWriter('overlap-session', 'overlap-run', session_dir=root)
    writer.append_sse_event('token', {'text': 'PREFIX'})
    env = os.environ.copy()
    env['HERMES_WEBUI_RUN_JOURNAL_FSYNC'] = 'terminal-only'
    child = subprocess.run([sys.executable, '-c', CHILD_B, str(root)], cwd=os.getcwd(), env=env,
                           capture_output=True, text=True, check=True, timeout=15)
    assert json.loads(child.stdout)['committed_seq'] == 2
    assert writer.append_sse_event('token', {'text': 'AFTER PEER'})['seq'] == 3
    rows = run_journal.read_run_events('overlap-session', 'overlap-run', session_dir=root, validated_recovery=True)
    assert not rows['malformed']
    assert [row['seq'] for row in rows['events']] == [1, 2, 3]


def test_recovery_reader_waits_for_other_process_failed_terminal_settlement(tmp_path):
    root, control = tmp_path / 'sessions', tmp_path / 'control'
    control.mkdir()
    run_journal.RunJournalWriter('overlap-session', 'overlap-run', session_dir=root).append_sse_event('token', {'text': 'PREFIX'})
    child = subprocess.Popen([sys.executable, '-c', CHILD_A, str(root), str(control)], cwd=os.getcwd(),
                             env=os.environ.copy(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        deadline = time.monotonic() + 8
        while not (control / 'a-entered.json').exists():
            assert child.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        pending = pool.submit(run_journal.read_run_events, 'overlap-session', 'overlap-run',
                              session_dir=root, validated_recovery=True)
        with pytest.raises(FutureTimeout):
            pending.result(timeout=.2)
    finally:
        (control / 'release-a').touch()
        output, error = child.communicate(timeout=15)
        pool.shutdown(wait=True)
    assert child.returncode == 0, error
    assert json.loads(output)['failed'] is True
    settled = pending.result(timeout=5)
    assert [row['seq'] for row in settled['events']] == [1]
    assert not settled['malformed']


def test_native_windows_lock_contract_releases_on_exception(tmp_path, monkeypatch):
    path = tmp_path / 'windows.jsonl'
    path.touch()
    calls = []

    class WindowsLock:
        LK_LOCK, LK_UNLCK = 1, 2

        @staticmethod
        def locking(fd, mode, count):
            assert os.lseek(fd, 0, os.SEEK_CUR) == 0
            assert os.fstat(fd).st_size == 1
            calls.append((mode, count))

    monkeypatch.setattr(run_journal, '_fcntl', None)
    monkeypatch.setattr(run_journal, '_msvcrt', WindowsLock)
    with path.open('rb') as journal:
        with pytest.raises(ValueError, match='injected guarded read'):
            with run_journal._journal_process_lock(journal.fileno(), path, shared=True):
                raise ValueError('injected guarded read')
    assert calls == [(WindowsLock.LK_LOCK, 1), (WindowsLock.LK_UNLCK, 1)]
    assert Path(str(path) + '.lock').read_bytes() == b'\0'
    assert path.read_bytes() == b''


def test_missing_process_lock_backend_cannot_publish_row(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, '_fcntl', None)
    monkeypatch.setattr(run_journal, '_msvcrt', None)
    with pytest.raises(OSError, match='locking is unavailable'):
        run_journal.RunJournalWriter('unsupported', 'run', session_dir=tmp_path).append_sse_event('token', {'text': 'NEVER'})
    path = run_journal._run_path('unsupported', 'run', session_dir=tmp_path)
    assert not path.read_bytes()
    assert str(path) not in run_journal._SEQ_CACHE
