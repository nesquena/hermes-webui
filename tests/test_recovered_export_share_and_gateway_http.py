"""Literal journal recovery -> HTTP consumers -> later SQLite turns, in two boots."""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.error
import urllib.request

import pytest

from tests.test_recovered_surrogate_http import _new_server


SEED = r'''
import json,sys
from pathlib import Path
from tests.test_cancel_restart_journal_recovery import _persist_recovery_boundary_turn
from tests.test_webui_state_db_reconciliation import _make_state_db
from api import models
from api.run_journal import RunJournalWriter
sid,stream,lifecycle,tokens = sys.argv[1:5]
models.SESSION_DIR.mkdir(parents=True,exist_ok=True)
_persist_recovery_boundary_turn(sid,stream,lifecycle)
writer=RunJournalWriter(sid,stream)
for text in json.loads(tokens): writer.append_sse_event('token',{'text':text})
writer.append_sse_event('stream_end',{})
if sys.argv[5].startswith('gateway'):
    saved=models.Session.load(sid)
    stamp=(11 if sys.argv[5]=='gateway-before-restart' else
           max(float(row.get('timestamp') or 0) for row in saved.messages)+10)
    db=Path(models._active_state_db_path())
    assert db.resolve().is_relative_to(Path(sys.argv[6]).resolve()), db
    db.parent.mkdir(parents=True,exist_ok=True)
    _make_state_db(db,sid,[
        {'role':'user','content':'Do the cancellable task.','timestamp':10},
        {'role':'assistant','content':'CANCELLED_RUN_REPLAY','timestamp':stamp},
        {'role':'tool','content':'CANCELLED_TOOL_REPLAY','timestamp':stamp+1},
        {'role':'user','content':'LATER_GATEWAY_REQUEST','timestamp':stamp+2},
        {'role':'assistant','content':'LATER_GATEWAY_ANSWER','timestamp':stamp+3},
        {'role':'user','content':'SECOND_GATEWAY_REQUEST','timestamp':stamp+4},
        {'role':'assistant','content':'SECOND_GATEWAY_ANSWER','timestamp':stamp+5},
    ])
'''

HISTORY = r'''
import json,sys
from api import models
from api.streaming import _new_turn_context_from_messages,_sanitize_messages_for_agent
session=models.get_session(sys.argv[1])
messages=models.reconciled_state_db_messages_for_session(session,prefer_context=True)
history=_sanitize_messages_for_agent(_new_turn_context_from_messages(messages,'NEXT_WEBUI_REQUEST'))
print(json.dumps(history))
'''


def _seed(tmp_path, lifecycle, tokens, gateway=None):
    root = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('AWS_', 'GH_', 'GITHUB_', 'OPENAI_', 'ANTHROPIC_'))
           and not k.endswith(('_API_KEY', '_AUTH_TOKEN', '_BOT_TOKEN', '_ACCESS_TOKEN'))}
    home, workspace = tmp_path/'home', tmp_path/'workspace'
    home.mkdir()
    workspace.mkdir()
    env.update(HERMES_HOME=str(home), HERMES_BASE_HOME=str(home),
               HERMES_CONFIG_PATH=str(home/'config.yaml'),
               HERMES_WEBUI_STATE_DIR=str(tmp_path/'state'),
               HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
               HERMES_WEBUI_TEST_NETWORK_BLOCK='1')
    sid, stream = 'http-recovered-flow', 'http-recovered-flow-run'
    result = subprocess.run([sys.executable, '-c', SEED, sid, stream, lifecycle,
                             json.dumps(tokens), 'gateway-'+gateway if gateway else 'plain', str(home)],
                            cwd=root, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    # Use the actual writer path rather than guessing its directory layout.
    journals = list((tmp_path/'state').rglob(stream+'.jsonl'))
    assert len(journals) == 1, journals
    journal = journals[0]
    return root, env, sid, stream, journal, journal.read_bytes()


def _request(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base+path, data=data,
                                     headers={'Content-Type': 'application/json'} if data else {})
    try:
        response = urllib.request.urlopen(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        assert int(response.headers.get('Content-Length', len(body))) == len(body)
        return response.status, body, response.headers


TOKENS = [['Durable prefix answer', ' emoji half \ud83d', ' reattached continuation'],
          ['Durable prefix answer', ' emoji half \udc00', ' reattached continuation'],
          ['Durable prefix answer', '\ud83d', '\ude42', ' reattached continuation'],
          ['Durable prefix answer', ' 普通🙂', ' reattached continuation']]


@pytest.mark.parametrize('lifecycle', ['crash', 'stop'])
@pytest.mark.parametrize('tokens', TOKENS, ids=['high', 'low', 'split-pair', 'unicode'])
@pytest.mark.parametrize('consumer', ['json-export', 'html-export', 'share'])
def test_recovered_text_remains_usable_through_two_real_server_boots(tmp_path, lifecycle, tokens, consumer):
    root, env, sid, stream, journal, raw = _seed(tmp_path, lifecycle, tokens)
    expected = json.loads(json.dumps(''.join(tokens)))
    token = None
    for boot in range(2):
        with _new_server(env, root, tmp_path/f'server-{boot}.log') as base:
            for _ in range(2):
                status, body, _ = _request(base, '/api/session?session_id='+sid)
                assert status == 200
                messages = json.loads(body)['session']['messages']
                assert any(row.get('content') == expected for row in messages)
            if consumer.endswith('export'):
                fmt = 'html' if consumer == 'html-export' else 'json'
                status, body, headers = _request(base, f'/api/session/export?session_id={sid}&format={fmt}')
                assert status == 200 and body
                assert 'attachment;' in headers['Content-Disposition']
                text = body.decode('utf-8')
                if fmt == 'json':
                    assert any(row.get('content') == expected for row in json.loads(text)['messages'])
                else:
                    assert 'Durable prefix answer' in text and 'reattached continuation' in text
                    if consumer == 'html-export' and tokens == TOKENS[3]:
                        assert '普通🙂' in text
            else:
                status, body, _ = _request(base, '/api/share/create', {'session_id': sid})
                assert status == 200, body
                created = json.loads(body)
                assert created['ok'] is True
                if token is not None:
                    assert created['share']['token'] == token
                token = created['share']['token']
                status, body, _ = _request(base, '/api/share/'+token)
                assert status == 200
                share = json.loads(body)['share']
                assert any(row.get('content') == expected for row in share['messages'])
                assert 'workspace' not in share and 'context_messages' not in share
                assert not list((tmp_path/'state'/'shares').glob('*.tmp'))
        assert journal.read_bytes() == raw
    history = subprocess.run([sys.executable, '-c', HISTORY, sid], cwd=root, env=env,
                             capture_output=True, text=True, timeout=30)
    assert history.returncode == 0, history.stderr
    assert any(row.get('content') == expected for row in json.loads(history.stdout))
    assert journal.read_bytes() == raw


@pytest.mark.parametrize('gateway', ['before-restart', 'after-carrier'])
def test_stop_recovery_keeps_later_gateway_turns_in_http_exports_share_and_next_send(tmp_path, gateway):
    root, env, sid, stream, journal, raw = _seed(tmp_path, 'stop', TOKENS[3], gateway=gateway)
    for boot in range(2):
        with _new_server(env, root, tmp_path/f'gateway-server-{boot}.log') as base:
            for path in ['/api/session?session_id='+sid,
                         '/api/session?session_id='+sid+'&msg_limit=4',
                         '/api/session?session_id='+sid+'&msg_limit=4',
                         '/api/session?session_id='+sid+'&msg_limit=4&msg_before=7',
                         '/api/session/export?session_id='+sid]:
                status, body, _ = _request(base, path)
                assert status == 200
                payload = json.loads(body)
                rows = payload.get('session', payload)['messages']
                text = [row.get('content') for row in rows]
                assert text[-4:] == ['LATER_GATEWAY_REQUEST', 'LATER_GATEWAY_ANSWER',
                                     'SECOND_GATEWAY_REQUEST', 'SECOND_GATEWAY_ANSWER']
                assert 'CANCELLED_RUN_REPLAY' not in text and 'CANCELLED_TOOL_REPLAY' not in text
            status, body, _ = _request(base, '/api/share/create', {'session_id': sid})
            assert status == 200
            status, body, _ = _request(base, '/api/share/'+json.loads(body)['share']['token'])
            assert status == 200
            text = [row.get('content') for row in json.loads(body)['share']['messages']]
            assert 'LATER_GATEWAY_REQUEST' in text and 'SECOND_GATEWAY_ANSWER' in text
        assert journal.read_bytes() == raw
    history = subprocess.run([sys.executable, '-c', HISTORY, sid], cwd=root, env=env,
                             capture_output=True, text=True, timeout=30)
    assert history.returncode == 0, history.stderr
    text = [row.get('content') for row in json.loads(history.stdout)]
    assert 'LATER_GATEWAY_REQUEST' in text and 'SECOND_GATEWAY_ANSWER' in text
    assert 'CANCELLED_RUN_REPLAY' not in text and 'CANCELLED_TOOL_REPLAY' not in text
    assert journal.read_bytes() == raw


@pytest.mark.parametrize('failure', ['fsync', 'replace'])
def test_share_snapshot_surrogate_fault_keeps_previous_file_and_cleans_temp(tmp_path, monkeypatch, failure):
    from api import shares
    path = tmp_path/'snapshot.json'
    original = b'{"previous": true}'
    path.write_bytes(original)

    def fail(*args):
        raise OSError('injected share commit failure')

    monkeypatch.setattr(shares.os, failure, fail)
    with pytest.raises(OSError, match='injected share commit failure'):
        shares._write_json_atomic(path, {'messages': [{'content': '\ud83d'}]})
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))


def test_get_restamped_cancel_successors_uses_ids_and_complete_owner_read(tmp_path):
    root, env, sid, stream, journal, raw = _seed(tmp_path, 'stop', TOKENS[3], 'before-restart')
    prepare = r"""
import sqlite3,sys
from api import models
session=models.get_session(sys.argv[1])
session.messages=models.reconciled_state_db_messages_for_session(session)
session.context_messages=models.reconciled_state_db_messages_for_session(session,prefer_context=True)
assert all(row.get('_state_db_row_id',0)>0 for row in session.messages[-4:])
session.save(touch_updated_at=False)
with sqlite3.connect(models._active_state_db_path()) as db:
    db.execute('UPDATE messages SET timestamp=timestamp+30 WHERE session_id=? AND id>3',(session.session_id,))
    db.executemany('INSERT INTO messages (session_id,role,content,timestamp) VALUES (?,?,?,?)',[
        (session.session_id,'user','NEW_GATEWAY_REQUEST',60),
        (session.session_id,'assistant','NEW_GATEWAY_ANSWER',61)])
"""
    result = subprocess.run([sys.executable, '-c', prepare, sid], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    with _new_server(env, root, tmp_path/'identity-get-server.log') as base:
        for limit in ['all', '30', '2']:
            status, body, _ = _request(base, '/api/session?session_id='+sid+'&msg_limit='+limit)
            assert status == 200
            payload = json.loads(body)['session']
            rows = payload['messages']
            contents = [row.get('content') for row in rows]
            assert contents[-2:] == ['NEW_GATEWAY_REQUEST', 'NEW_GATEWAY_ANSWER']
            if limit != '2':
                for text in ['LATER_GATEWAY_REQUEST','LATER_GATEWAY_ANSWER','SECOND_GATEWAY_REQUEST','SECOND_GATEWAY_ANSWER']:
                    assert contents.count(text) == 1
            assert not any('_state_db_row_id' in row for row in rows)
            assert 'CANCELLED_RUN_REPLAY' not in contents and 'CANCELLED_TOOL_REPLAY' not in contents
    assert journal.read_bytes() == raw
