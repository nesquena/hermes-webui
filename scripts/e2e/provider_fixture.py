"""Loopback deterministic provider for real chat and governance action reviews."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
import time


def stdio_mcp(state):
    """Actual local MCP JSON-RPC transport; effects confined to one QA ledger."""
    state = Path(state).resolve()
    if state.name != 'e2e-state' or not (state / 'workspace').is_dir():
        raise ValueError('MCP fixture requires private QA state')
    tools = [{'name': 'qa_mark', 'description': 'Record a synthetic marker locally.',
              'inputSchema': {'type': 'object', 'properties': {
                  'marker': {'type': 'string', 'description': 'Synthetic QA marker'}},
                  'required': ['marker'], 'additionalProperties': False}}]
    tools += [{'name': 'qa_lookup_' + str(i), 'description': 'Synthetic catalog entry ' + str(i),
               'inputSchema': {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                               'required': ['value']}} for i in range(1, 8)]
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        if method == 'initialize':
            result = {'protocolVersion': request.get('params', {}).get('protocolVersion', '2024-11-05'),
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'qa-stdio', 'version': '1.0'}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': tools}
        elif method == 'tools/call':
            params = request.get('params', {})
            marker = str(params.get('arguments', {}).get('marker', ''))
            if params.get('name') != 'qa_mark' or not re.fullmatch(r'QA_MCP_[A-Z0-9_]+', marker):
                result = {'content': [{'type': 'text', 'text': 'Unsupported synthetic invocation'}], 'isError': True}
            else:
                with (state / 'workspace' / 'qa-mcp-effects.jsonl').open('a') as record:
                    record.write(json.dumps({'marker': marker, 'pid': os.getpid()}) + '\n')
                result = {'content': [{'type': 'text', 'text': 'QA_MCP_EXECUTED:' + marker}]}
        else:
            print(json.dumps({'jsonrpc': '2.0', 'id': request['id'],
                              'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
            continue
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)


if len(sys.argv) > 1 and sys.argv[1] == '--mcp':
    stdio_mcp(sys.argv[2])
    raise SystemExit(0)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        self.send_json({'object':'list','data':[{'id':'qa-deterministic','object':'model','owned_by':'qa'}]})
    def send_json(self, data):
        body=json.dumps(data).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        data=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
        messages=data.get('messages',[])
        last_index=next((i for i in range(len(messages)-1,-1,-1) if messages[i].get('role')=='user'),-1)
        last=messages[last_index].get('content','') if last_index>=0 else ''
        if not isinstance(last,str): last='QA message'
        system_text=str([m.get('content','') for m in messages if m.get('role')=='system'])
        tool_calls=None
        is_review='You review one already-permitted tool action for an administrator.' in system_text
        review_decision=None
        clarify_marker = None
        clarify_phase = None
        clarify_answers = None
        clarify_origin = next((m['content'] for m in messages if m.get('role') == 'user'
            and isinstance(m.get('content'), str) and 'QA_CLARIFY_FLOW|' in m['content']), '')
        kanban_origin = any('QA_KANBAN_WORKER_FLOW' in str(m.get('content', '')) for m in messages)
        kanban_start = any(re.fullmatch(r'work kanban task t_[a-f0-9]+', str(m.get('content', '')))
                           for m in messages if m.get('role') == 'user')
        continuation_origin = next((m['content'] for m in messages if m.get('role') == 'user'
            and isinstance(m.get('content'), str) and 'QA_CONTINUATION_PARENT|' in m['content']), '')
        if not is_review and 'QA_SLOW_CHILD' in last and 'QA_DELEGATE_SLOW_PARENT' not in last:
            time.sleep(5)
        if not is_review and 'QA_STEER_WINDOW' in last and data.get('tools'):
            with (Path(os.environ['QA_STATE'])/'provider-evidence.jsonl').open('a') as record:
                record.write(json.dumps({'phase':'request_started','marker':'QA_STEER_WINDOW'})+'\n')
        if not is_review and 'QA_SLOW_RESPONSE' in last and data.get('tools'):
            time.sleep(5)
        if is_review:
            review=json.loads(last)
            rules=str(review.get('administrator_rules',''))
            review_decision='approve' if 'QA_ALLOW_ONLY' in rules else 'deny' if 'QA_DENY_ONLY' in rules else 'manual'
            answer=json.dumps({'decision':review_decision,'reason':'Deterministic QA review for this synthetic action.','confidence':.99})
        elif 'QA_BOT_HANDOFF_RECEIVER' in last:
            valid_profile = 'QA_BOT_RESEARCH' in system_text
            valid_history = any('QA_BOT_HANDOFF_START' in str(m.get('content', '')) for m in messages)
            answer = 'QA_BOT_HANDOFF_DONE' if valid_profile and valid_history else 'QA_BOT_HANDOFF_CONTEXT_ERROR'
        elif 'QA_BOT_HANDOFF_START' in last:
            results = [m.get('content', '') for m in messages[last_index+1:] if m.get('role') == 'tool']
            if data.get('tools') and ('Bot team directory for this turn' not in system_text or 'Research bot for QA' not in system_text):
                answer = 'QA_BOT_AWARENESS_MISSING'
            elif results:
                answer = 'QA_BOT_HANDOFF_QUEUED: ' + str(results[-1])
            elif 'delegate_to_bot' in json.dumps(data.get('tools', [])):
                direct = any(t.get('function', {}).get('name') == 'delegate_to_bot' for t in data.get('tools', []))
                arguments = {'action': 'delegate', 'bot': 'qa-research',
                             'task': 'QA_BOT_HANDOFF_RECEIVER: report a short result here.'}
                if not direct:
                    arguments = {'name': 'delegate_to_bot', 'arguments': arguments}
                tool_calls = [{'id': 'qa-bot-handoff-' + str(time.time_ns()), 'type': 'function',
                    'function': {'name': 'delegate_to_bot' if direct else 'tool_call',
                                 'arguments': json.dumps(arguments)}}]
                answer = ''
            else:
                answer = 'QA_BOT_HANDOFF_TOOL_MISSING: ' + json.dumps([t.get('function', {}).get('name') for t in data.get('tools', [])])
        elif clarify_origin and any(tool.get('function', {}).get('name') == 'clarify' for tool in data.get('tools', [])):
            clarify_marker = clarify_origin.split('QA_CLARIFY_FLOW|', 1)[1].splitlines()[0].strip()
            if not re.fullmatch(r'[a-z0-9-]+', clarify_marker):
                raise ValueError('Unsupported clarify fixture marker')
            # Some provider adapters omit name from tool-result messages;
            # correlate them to the actual preceding clarify call IDs.
            call_ids = {c['id'] for m in messages for c in m.get('tool_calls', [])
                        if c.get('function', {}).get('name') == 'clarify'}
            results = [m.get('content', '') for m in messages if m.get('role') == 'tool'
                       and (m.get('name') == 'clarify' or m.get('tool_call_id') in call_ids)]
            clarify_phase = len(results)
            if len(results) < 2:
                question = {'question': ('QA clarification choice ' if not results else 'QA clarification follow-up ') + clarify_marker}
                if not results:
                    question['choices'] = ['Alpha', 'Beta']
                tool_calls = [{'id': 'qa-clarify-' + str(time.time_ns()), 'type': 'function',
                    'function': {'name': 'clarify', 'arguments': json.dumps({'questions': [question]})}}]
                answer = None
            else:
                clarify_answers = [json.loads(value) for value in results]
                answer = 'QA_CLARIFY_COMPLETED:' + clarify_marker + ':' + json.dumps(clarify_answers)
        elif (kanban_origin or kanban_start) and any(tool.get('function', {}).get('name') == 'kanban_show' for tool in data.get('tools', [])):
            call_ids = {c['id'] for m in messages for c in m.get('tool_calls', [])
                        if c.get('function', {}).get('name') == 'kanban_complete'}
            results = [m.get('content', '') for m in messages if m.get('role') == 'tool'
                       and (m.get('name') == 'kanban_complete' or m.get('tool_call_id') in call_ids)]
            if results:
                answer = 'QA_KANBAN_WORKER_FINISHED'
            elif not kanban_origin:
                # Read the real task through its dispatcher-owned worker.
                tool_calls = [{'id': 'qa-kanban-show-' + str(time.time_ns()), 'type': 'function',
                    'function': {'name': 'kanban_show', 'arguments': '{}'}}]
                answer = None
            else:
                # A real dispatcher-owned worker remains active long enough
                # to exercise claim fences, then completes with its own token.
                time.sleep(12)
                tool_calls = [{'id': 'qa-kanban-complete-' + str(time.time_ns()), 'type': 'function',
                    'function': {'name': 'kanban_complete', 'arguments': json.dumps({
                        'summary': 'QA_KANBAN_WORKER_DONE', 'metadata': {'qa_fixture': True}})}}]
                answer = None
        elif 'QA_SUPPLEMENT_CLI|' in last or 'QA_SUPPLEMENT_MCP|' in last:
            results = [m.get('content', '') for m in messages[last_index+1:] if m.get('role') == 'tool']
            if results:
                answer = 'QA_SUPPLEMENT_TOOL_RESULT: ' + str(results[-1])
            else:
                cli = 'QA_SUPPLEMENT_CLI|' in last
                value = last.split('QA_SUPPLEMENT_CLI|' if cli else 'QA_SUPPLEMENT_MCP|', 1)[1].splitlines()[0].strip()
                if cli:
                    if not re.fullmatch(r'qa-cli-[a-z0-9-]+\.txt', value):
                        raise ValueError('Unsupported CLI fixture target')
                    arguments = {'command': '/usr/bin/touch ' + value,
                                 'workdir': str(Path(os.environ['QA_STATE']) / 'workspace')}
                else:
                    # MCP tools are deferred behind the engine's real tool_call
                    # bridge. Invoking that bridge preserves schema validation
                    # and the underlying tool's governance/action approval.
                    arguments = {'name': 'mcp__qa_stdio__qa_mark', 'arguments': {'marker': value}}
                tool_calls = [{'id': 'qa-supplement-' + str(time.time_ns()), 'type': 'function',
                               'function': {'name': 'terminal' if cli else 'tool_call',
                                            'arguments': json.dumps(arguments)}}]
                answer = None
        elif 'QA_STEER_WINDOW' in last and data.get('tools') and not any(m.get('role')=='tool' for m in messages[last_index+1:]):
            time.sleep(5)
            target=str(Path(os.environ['QA_STATE'])/'workspace'/'qa-evidence.txt')
            tool_calls=[{'id':'qa-steer-'+str(time.time_ns()),'type':'function','function':{'name':'read_file','arguments':json.dumps({'path':target})}}]
            answer=None
        elif continuation_origin:
            results = [m.get('content', '') for m in messages[last_index+1:] if m.get('role') == 'tool']
            if results:
                answer = 'QA_CONTINUATION_RESULT: ' + str(results[-1])
            elif 'ASYNC DELEGATION BATCH COMPLETE' in last:
                target = continuation_origin.split('QA_CONTINUATION_PARENT|', 1)[1].splitlines()[0].strip()
                tool_calls = [{'id': 'qa-continuation-write-' + str(time.time_ns()), 'type': 'function',
                    'function': {'name': 'write_file', 'arguments': json.dumps({'path': target,
                        'content': 'QA_FORBIDDEN_CONTINUATION_EFFECT'})}}]
                answer = None
            elif 'QA_CONTINUATION_PARENT|' in last:
                tool_calls = [{'id': 'qa-continuation-delegate-' + str(time.time_ns()), 'type': 'function',
                    'function': {'name': 'delegate_task', 'arguments': json.dumps({'tasks': [
                        {'goal': 'QA_SLOW_CHILD. Return exactly QA_CHILD_ONE_DONE. No tools required.'},
                        {'goal': 'QA_SLOW_CHILD. Return exactly QA_CHILD_TWO_DONE. No tools required.'}]})}}]
                answer = None
            else:
                answer = 'QA_CONTINUATION_WAITING_FOR_CHILDREN'
        elif ('QA_DELEGATE_PARENT' in last or 'QA_DELEGATE_SLOW_PARENT' in last) and (data.get('tools') or any(m.get('role')=='tool' for m in messages[last_index+1:])):
            results=[m.get('content','') for m in messages[last_index+1:] if m.get('role')=='tool']
            if results:
                answer='QA_DELEGATE_RESULT: '+str(results[-1])
            else:
                slow = 'QA_SLOW_CHILD. ' if 'QA_DELEGATE_SLOW_PARENT' in last else ''
                tool_calls=[{'id':'qa-delegate-'+str(time.time_ns()),'type':'function','function':{'name':'delegate_task','arguments':json.dumps({'tasks':[{'goal':slow+'Return exactly QA_CHILD_ONE_DONE. This is a synthetic QA task; no tools required.'},{'goal':slow+'Return exactly QA_CHILD_TWO_DONE. This is a synthetic QA task; no tools required.'}]})}}]
                answer=None
        elif 'QA_TODO|' in last and (data.get('tools') or any(m.get('role')=='tool' for m in messages[last_index+1:])):
            results=[m.get('content','') for m in messages[last_index+1:] if m.get('role')=='tool']
            if results:
                answer='QA_TODO_RESULT: '+str(results[-1])
            else:
                action=last.split('QA_TODO|',1)[1].splitlines()[0].strip()
                todos=[] if action=='clear' else [{'id':'qa-first','content':'QA todo updated' if action=='update' else 'QA todo created','status':'completed' if action=='update' else 'in_progress'},{'id':'qa-second','content':'QA next task','status':'pending'}]
                tool_calls=[{'id':'qa-todo-'+str(time.time_ns()),'type':'function','function':{'name':'todo','arguments':json.dumps({'todos':todos})}}]
                answer=None
        elif ('QA_GOV_READ|' in last or 'QA_GOV_WRITE|' in last) and (data.get('tools') or any(m.get('role')=='tool' for m in messages[last_index+1:])):
            results=[m.get('content','') for m in messages[last_index+1:] if m.get('role')=='tool']
            if results:
                answer='QA_GOV_TOOL_RESULT: '+str(results[-1])
            else:
                write='QA_GOV_WRITE|' in last
                target=last.split('QA_GOV_WRITE|' if write else 'QA_GOV_READ|',1)[1].splitlines()[0].strip()
                arguments={'path':target}
                if write: arguments['content']='QA_GOVERNANCE_EXECUTED'
                tool_calls=[{'id':'qa-gov-action-'+str(time.time_ns()),'type':'function','function':{'name':'write_file' if write else 'read_file','arguments':json.dumps(arguments)}}]
                answer=None
        elif 'QA_STEP_OVERVIEW|' in last:
            results=[m for m in messages[last_index+1:] if m.get('role')=='tool']
            target=last.split('QA_STEP_OVERVIEW|',1)[1].splitlines()[0].strip()
            targets=[target,target,target+'.missing']
            if len(results)<len(targets):
                tool_calls=[{'id':'qa-step-'+str(len(results))+'-'+str(time.time_ns()),'type':'function','function':{'name':'read_file','arguments':json.dumps({'path':targets[len(results)]})}}]
                answer=None
            else:
                answer='QA_STEPS_COMPLETE: Two reads finished. The optional file was not found.'
        elif 'QA_READING_SURFACE' in last:
            answer="""## A clear place to start

Keep the launch focused on **one useful outcome**. Bring the people, files and next steps together in the same project.

### Your next steps
1. Confirm the audience and the result you want to deliver.
2. Ask the research bot to gather the relevant material.
3. Review the draft together before sharing it.

| Step | Owner | Status |
| --- | --- | --- |
| Gather context | Research bot | Ready |
| Review the draft | Project team | Next |

```text
project: Customer onboarding
next_step: Review the welcome guide
```

You can ask a follow-up here, or open the project to continue with your team.
"""
        else:
            markers=re.findall(r'(?:ALICE_GROUP_QA|BOB_GROUP_QA|QA_[A-Z_]+)',last)
            if any('QA_ACTUAL_STEER' in str(m.get('content','')) for m in messages):
                markers=['QA_ACTUAL_STEER']
            answer=('RESEARCH ' if 'QA_BOT_RESEARCH' in system_text else 'DEFAULT ')+'QA_REPLY: '+(markers[-1] if markers else last.splitlines()[0][:80] if last else 'QA')
        evidence=Path(os.environ.get('QA_STATE',str(Path(__file__).parent)))/'provider-evidence.jsonl'
        with evidence.open('a') as record:
            record.write(json.dumps({'review':is_review,'review_decision':review_decision,'request_model':data.get('model'),'last_user_text':last[:500],'clarify_marker':clarify_marker,'clarify_phase':clarify_phase,'clarify_answers':clarify_answers,'observed_steer':any('QA_ACTUAL_STEER' in str(m.get('content','')) for m in messages),'issued_tools':[c['function']['name'] for c in tool_calls or []],'tools_offered':[t.get('function',{}).get('name') for t in data.get('tools',[])]})+'\n')
        base={'id':'chatcmpl-qa','created':int(time.time()),'model':data.get('model') or 'qa-deterministic'}
        message={'role':'assistant','content':answer}
        if tool_calls: message['tool_calls']=tool_calls
        finish='tool_calls' if tool_calls else 'stop'
        usage={'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}
        if data.get('stream'):
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            delta={'role':'assistant'}
            if tool_calls:delta['tool_calls']=[dict(c,index=i) for i,c in enumerate(tool_calls)]
            else:delta['content']=answer
            chunk={**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':None}]}
            self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode());self.wfile.flush()
            final={**base,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':usage}
            self.wfile.write(('data: '+json.dumps(final)+'\n\ndata: [DONE]\n\n').encode())
        else:self.send_json({**base,'object':'chat.completion','choices':[{'index':0,'message':message,'finish_reason':finish}],'usage':usage})

ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1]) if len(sys.argv)>1 else 19087),Handler).serve_forever()
