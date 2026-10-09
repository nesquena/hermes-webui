"""Recovered Stop copies keep the same Gateway prefix as real public GET."""
import json
import subprocess
import sys

import pytest

from tests.test_recovered_export_share_and_gateway_http import _request, _seed, TOKENS
from tests.test_recovered_surrogate_http import _new_server


@pytest.mark.parametrize('operation', ['branch-all', 'fork-prefix', 'duplicate'])
@pytest.mark.parametrize('gateway', ['before-restart', 'after-carrier'])
def test_recovered_stop_copy_keeps_gateway_display_context_and_private_ids(tmp_path, operation, gateway):
    root, env, sid, stream, journal, raw = _seed(tmp_path, 'stop', TOKENS[3], gateway)
    with _new_server(env, root, tmp_path/'copy-server.log') as base:
        status, body, _ = _request(base, '/api/session?session_id='+sid+'&msg_limit=all')
        assert status == 200
        visible = json.loads(body)['session']['messages']
        keep = len(visible)-2 if operation == 'fork-prefix' else len(visible)
        expected = [row.get('content') for row in visible[:keep]]
        assert 'LATER_GATEWAY_REQUEST' in expected and 'LATER_GATEWAY_ANSWER' in expected
        endpoint = '/api/session/duplicate' if operation == 'duplicate' else '/api/session/branch'
        payload = {'session_id': sid}
        if operation != 'duplicate':
            payload['keep_count'] = keep
        status, body, _ = _request(base, endpoint, payload)
        assert status == 200, body
        response = json.loads(body)
        copied_id = response['session']['session_id'] if operation == 'duplicate' else response['session_id']
        for _ in range(2):
            status, body, _ = _request(base, '/api/session?session_id='+copied_id+'&msg_limit=all')
            assert status == 200
            rows = json.loads(body)['session']['messages']
            assert [row.get('content') for row in rows] == expected
            assert not any(key.startswith('_state_db') or key == '_row_id' for row in rows for key in row)
    inspect = r'''
import json,sys
from api import models,session_ops
session=models.Session.load(sys.argv[1])
assert session is not None
print(json.dumps({'display':session.messages,'context':session.context_messages,
                 'regeneration':session_ops.regeneration_state(session,use_sidecar=True)}))
'''
    result = subprocess.run([sys.executable, '-c', inspect, copied_id], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    saved = json.loads(result.stdout)
    assert [row.get('content') for row in saved['display']] == expected
    for rows in [saved['context'], *saved['regeneration']]:
        text = [row.get('content') for row in rows]
        assert text.count('LATER_GATEWAY_REQUEST') == text.count('LATER_GATEWAY_ANSWER') == 1
        assert ('SECOND_GATEWAY_ANSWER' in text) == (operation != 'fork-prefix')
        assert 'CANCELLED_RUN_REPLAY' not in text and 'CANCELLED_TOOL_REPLAY' not in text
        gateway_rows = [row for row in rows if 'GATEWAY_' in str(row.get('content'))]
        assert all(row.get('_state_db_row_id', 0) > 0 for row in gateway_rows)
    assert journal.read_bytes() == raw


@pytest.mark.parametrize('warm_source', [False, True], ids=['cold-source', 'cached-source'])
def test_duplicate_rebases_exact_tool_owners_after_sqlite_image_successor_insertion(tmp_path, warm_source):
    root, env, sid, stream, journal, raw = _seed(tmp_path, 'stop', TOKENS[3], 'after-carrier')
    prepare = r'''
import json,sqlite3,sys
from tests.test_native_image_turn_display_context import IMAGE_A
from api import models
s=models.get_session(sys.argv[1])
assert models._cancelled_journal_turn_owner(s.messages)
with sqlite3.connect(models._active_state_db_path()) as db:
    stamp=db.execute("SELECT timestamp FROM messages WHERE session_id=? AND content='LATER_GATEWAY_REQUEST'",
                     (s.session_id,)).fetchone()[0]
    db.execute("UPDATE messages SET content='LATER_GATEWAY_REQUEST [screenshot]' "
               "WHERE session_id=? AND content='LATER_GATEWAY_REQUEST'", (s.session_id,))
token='settled-native-successor'
display={'role':'user','content':'LATER_GATEWAY_REQUEST','timestamp':stamp,
         '_active_turn_token':token,'_state_db_row_id':999}
rich=[{'type':'text','text':display['content']},
      {'type':'image_url','image_url':{'url':IMAGE_A}}]
context={**display,'content':rich,'api_content':json.dumps(rich),
         '_webui_trusted_agent_input_text':display['content']}
first={'role':'assistant','content':'SAME_TOOL_ANSWER','timestamp':stamp+1}
second={'role':'assistant','content':'SAME_TOOL_ANSWER','timestamp':stamp+2}
s.messages.extend([display,first,second])
s.context_messages.extend([context,dict(first),dict(second)])
owners=[len(s.messages)-2,len(s.messages)-1]
s.tool_calls.extend({'id':'copied-tool-'+str(i),'name':'terminal',
                     'arguments':{'command':'echo '+str(i)},'result':'tool-result-'+str(i),
                     'assistant_msg_idx':index} for i,index in enumerate(owners))
s.save()
print(json.dumps({'owners':owners}))
'''
    result = subprocess.run([sys.executable, '-c', prepare, sid], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    source_owners = json.loads(result.stdout)['owners']
    with _new_server(env, root, tmp_path/'tool-copy-server.log') as base:
        if warm_source:
            status, body, _ = _request(base, '/api/session?session_id='+sid+'&msg_limit=all')
            assert status == 200, body
        status, body, _ = _request(base, '/api/session/duplicate', {'session_id': sid})
        assert status == 200, body
        copied_id = json.loads(body)['session']['session_id']
        for _ in range(2):
            status, body, _ = _request(base, '/api/session?session_id='+copied_id+'&msg_limit=all')
            assert status == 200, body
            copied = json.loads(body)['session']
            owners = [i for i,row in enumerate(copied['messages']) if row.get('content') == 'SAME_TOOL_ANSWER']
            assert len(owners) == 2 and owners[0] > source_owners[0], 'SQLite successor must really shift ownership'
            cards = {card['id']:card for card in copied['tool_calls'] if card['id'].startswith('copied-tool-')}
            assert [cards['copied-tool-'+str(i)]['assistant_msg_idx'] for i in range(2)] == owners
            assert [cards['copied-tool-'+str(i)]['result'] for i in range(2)] == ['tool-result-0','tool-result-1']
    inspect = r'''
import json,sys
from api import models
source=models.Session.load(sys.argv[1]); copied=models.Session.load(sys.argv[2])
print(json.dumps({'source':source.tool_calls,'messages':copied.messages,'tools':copied.tool_calls}))
'''
    result = subprocess.run([sys.executable, '-c', inspect, sid, copied_id], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    saved = json.loads(result.stdout)
    owners = [i for i,row in enumerate(saved['messages']) if row.get('content') == 'SAME_TOOL_ANSWER']
    assert [card['assistant_msg_idx'] for card in saved['tools'] if card['id'].startswith('copied-tool-')] == owners
    assert [card['assistant_msg_idx'] for card in saved['source'] if card['id'].startswith('copied-tool-')] == source_owners
    assert journal.read_bytes() == raw
