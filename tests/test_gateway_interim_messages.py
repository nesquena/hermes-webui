"""Gateway commentary boundaries, replay, and display-only persistence."""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
from api import gateway_chat as g, config, models, run_journal
import pytest
from unittest.mock import MagicMock


@pytest.mark.parametrize('real_disk', [False, True])
def test_success_writeback_keeps_commentary_outside_model_context(monkeypatch, tmp_path, real_disk):
    monkeypatch.setattr(models, 'SESSION_DIR', tmp_path)
    session = models.Session(session_id='sid', workspace=str(ROOT)) if real_disk else MagicMock()
    if real_disk:
        save = models.Session.save
        monkeypatch.setattr(models.Session, 'save', lambda self,*a,**k: save(self,*a,skip_index=True,**k))
    session.session_id = 'sid'
    session.profile = None
    session.active_stream_id = 'stream'
    session.workspace = str(ROOT)
    session.messages = []
    session.context_messages = []
    session.tool_calls = []
    session.process_wakeup_pause = None
    session.pending_user_message = 'question'
    session.pending_user_source = 'webui'
    session.pending_started_at = time.time()
    session.pending_attachments = []
    monkeypatch.setitem(g.STREAMS, 'stream', config.create_stream_channel())
    monkeypatch.setattr(g, 'get_session', lambda *a: session)
    monkeypatch.setattr('api.gateway_delegation_wakeup.settle_reservation', lambda *a,**k: None)
    monkeypatch.setattr(g, '_stream_writeback_is_current', lambda *a: True)
    monkeypatch.setattr(g, 'gateway_supports_approval', lambda *a: True)
    monkeypatch.setattr(g, '_gateway_use_runs_api_enabled', lambda *a: True)
    monkeypatch.setattr(g, '_gateway_endpoint_for_profile', lambda *a: ('http://unused',''))
    monkeypatch.setattr(g, '_gateway_config_for_profile', lambda *a: {'model':{}})
    monkeypatch.setattr(g, '_gateway_reasoning_effort_for_request', lambda *a,**k: None)
    from api import streaming
    monkeypatch.setattr(streaming, '_load_webui_prefill_context', lambda *a: {})
    monkeypatch.setattr(streaming, '_webui_ephemeral_system_prompt', lambda *a,**k: '')
    rows = []
    if not real_disk:
        monkeypatch.setattr(run_journal, 'read_run_events', lambda *a,**k: {'events':rows})
    class Journal:
        def __init__(self,*a,**k): pass
        def append_sse_event(self,event,data):
            rows.append({'event':event,'payload':data,'created_at':time.time()})
        def __getattr__(self,name): return lambda *a,**k: None
    if not real_disk:
        monkeypatch.setattr(g, 'RunJournalWriter', Journal)
    def run(*a,put_gateway_event, cancel_event, **k):
        return g._relay_gateway_run_events([
            frame('message.delta',1,delta='Checking.'),
            frame('message.interim',2,text='Checking.',already_streamed=True),
            frame('message.interim',3,text='Confirmed.',already_streamed=False),
            frame('message.delta',4,delta='Final.'),
            frame('run.completed',5,output='Final.'),
        ],'sid','stream','run','http://unused','',put_gateway_event=put_gateway_event,cancel_event=cancel_event)[:2]
    monkeypatch.setattr(g, '_run_gateway_runs_api_streaming', run)
    g._run_gateway_chat_streaming('sid','question','model',str(ROOT),'stream')
    assert [(m['role'],m['content']) for m in session.messages] == [('user','question'),('assistant','Checking.'),('assistant','Confirmed.'),('assistant','Final.')]
    assert [(m['role'],m['content']) for m in session.context_messages] == [('user','question'),('assistant','Final.')]
    if not real_disk:
        assert session.save.called
    timestamps = [m['timestamp'] for m in session.messages]
    assert timestamps == sorted(timestamps)
    if real_disk:
        saved = json.loads(session.path.read_text())
        assert [m['content'] for m in saved['messages']] == ['question','Checking.','Confirmed.','Final.']
        assert [m['content'] for m in saved['context_messages']] == ['question','Final.']
        rows = run_journal.read_run_events('sid','stream')['events']
        assert len([row for row in rows if row['event'] == 'interim_assistant']) == 2
        g.STREAM_PARTIAL_TEXT['stream'] = ''
        assert g._restore_relayed_gateway_state('sid','stream') == 4
        assert g.STREAM_PARTIAL_TEXT['stream'] == 'Final.'

@pytest.mark.parametrize('text', [None, '', '   ', 0, False, [], {}, ['bad']])
def test_invalid_interim_does_not_reset_answer(text):
    result, events, seqs = relay([frame('message.delta',1,delta='Final'),frame('message.interim',2,text=text),frame('run.completed',3,output='Final')])
    assert [e for e,d in events] == ['token']
    assert result[0] == g.STREAM_PARTIAL_TEXT['stream'] == 'Final'
    assert seqs == [1,2,3]

def test_interim_without_cursor_fails_closed_to_status_polling(monkeypatch):
    monkeypatch.setattr(run_journal,'read_run_events',lambda *a,**k:{'events':[{'event':'interim_assistant','payload':{'text':'Checking'}}]})
    assert g._restore_relayed_gateway_state('sid','stream') is None

def test_journal_commentary_recovery_is_idempotent_and_final_free(monkeypatch):
    rows = [
        {'event':'token','payload':{'text':'Checking.'}},
        {'event':'interim_assistant','payload':{'text':'Checking.','already_streamed':True}},
        {'event':'interim_assistant','payload':{'text':'Confirmed.','already_streamed':False}},
        {'event':'token','payload':{'text':'Final.'}},
    ]
    monkeypatch.setattr(run_journal,'read_run_events',lambda *a,**k:{'events':rows})
    session = SimpleNamespace(session_id='sid', messages=[{'role':'user','content':'question'}],tool_calls=[])
    for _ in range(2):
        models._append_journaled_partial_output(session,'stream',dedupe_existing=True,dedupe_min_index=1,append_context=False,display_only=True,through_last_interim=True)
    assert [m['content'] for m in session.messages] == ['question','Checking.','Confirmed.']
    assert all(m.get('_recovered_display_only') for m in session.messages[1:])

def test_sealed_tool_keeps_later_completion(monkeypatch):
    rows = [
        {'event':'tool','payload':{'name':'terminal','tid':'t1'}},
        {'event':'interim_assistant','payload':{'text':'Checking.','already_streamed':False}},
        {'event':'tool_complete','payload':{'name':'terminal','tid':'t1'}},
        {'event':'token','payload':{'text':'Final.'}},
    ]
    monkeypatch.setattr(run_journal,'read_run_events',lambda *a,**k:{'events':rows})
    session = SimpleNamespace(session_id='sid',messages=[],tool_calls=[])
    models._append_journaled_partial_output(session,'stream',append_context=False,display_only=True,through_last_interim=True)
    assert session.tool_calls[0]['done'] is True
    assert [m['content'] for m in session.messages] == ['', 'Checking.']

@pytest.fixture(autouse=True)
def isolation(monkeypatch, tmp_path):
    monkeypatch.setattr(g, '_iter_sse_lines_cancellable', lambda resp, cancel: iter(resp))
    monkeypatch.setattr('api.route_approvals.settle_gateway_pending_run', lambda *a, **k: None)
    monkeypatch.setattr(g, 'update_active_run', lambda *a, **k: None)
    monkeypatch.setitem(g.STREAM_PARTIAL_TEXT, 'stream', '')
    monkeypatch.setitem(g.STREAM_REASONING_TEXT, 'stream', '')
    monkeypatch.setitem(g.STREAM_LIVE_TOOL_CALLS, 'stream', [])

def frame(event, seq, **data):
    return ('data: ' + json.dumps(dict(event=event, seq=seq, **data))).encode()

def relay(frames, **kwargs):
    events, seqs = [], []
    result = g._relay_gateway_run_events(frames, 'sid', 'stream', 'run', 'http://unused', '',
        put_gateway_event=lambda e,d: events.append((e,d)), cancel_event=threading.Event(),
        on_seq=seqs.append, **kwargs)
    return result, events, seqs

def test_live_interim_order_and_final_separation():
    result, events, seqs = relay([
        frame('message.interim', 1, text='  Checking files.  ', already_streamed=False),
        frame('tool.started', 2, name='terminal', args={'command':'pwd'}, tool_call_id='t1'),
        frame('message.delta', 3, delta='Final answer.'),
        frame('run.completed', 4, output='Final answer.'),
    ])
    assert events[0] == ('interim_assistant', {'text':'Checking files.', 'already_streamed':False, 'gateway_seq':1})
    assert [e for e,d in events] == ['interim_assistant','tool','token']
    assert result[0] == g.STREAM_PARTIAL_TEXT['stream'] == 'Final answer.'
    assert seqs == [1,2,3,4]

def test_streamed_interim_seals_tokens_without_repeating_final():
    result, events, seqs = relay([
        frame('message.delta', 1, delta='Checking files.'),
        frame('message.interim', 2, text='Checking files.', already_streamed=True),
        frame('message.delta', 3, delta='Answer.'),
        frame('run.completed', 4, output='Answer.'),
    ])
    assert events[1] == ('interim_assistant', {'text':'Checking files.', 'already_streamed':True, 'gateway_seq':2})
    assert result[0] == g.STREAM_PARTIAL_TEXT['stream'] == 'Answer.'

def test_restart_restores_only_open_answer_segment(monkeypatch):
    rows = [
        {'event':'token','payload':{'text':'Checking files.','gateway_seq':1}},
        {'event':'interim_assistant','payload':{'text':'Checking files.','already_streamed':True,'gateway_seq':2}},
        {'event':'token','payload':{'text':'Ans','gateway_seq':3}},
    ]
    monkeypatch.setattr(run_journal, 'read_run_events', lambda *a,**k: {'events':rows})
    assert g._restore_relayed_gateway_state('sid','stream') == 3
    assert g.STREAM_PARTIAL_TEXT['stream'] == 'Ans'
    result, events, seqs = relay([frame('message.delta',4,delta='wer.'),frame('run.completed',5,output='Answer.')],final_text=g._seed_gateway_stream_text('stream'))
    assert result[0] == g.STREAM_PARTIAL_TEXT['stream'] == 'Answer.'
    assert [d['text'] for e,d in events if e == 'token'] == ['wer.']
