"""Shared deterministic transport for the loaded-window browser gates.

Only transport is replaced: the page's production listeners, pagination,
reconciliation and renderer still execute. Never connect to a provider.
"""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

INIT = r"""
window.fixtureSources = [];
class FixtureEventSource extends EventTarget {
  static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
  constructor(url) {
    super(); this.url = String(url); this.readyState = 1;
    fixtureSources.push(this);
    queueMicrotask(() => this.dispatchEvent(new Event('open')));
  }
  close() { this.readyState = 2; }
  emit(type, data, id='') {
    const event = new MessageEvent(type, {data:JSON.stringify(data),lastEventId:id});
    this.dispatchEvent(event);
    if(typeof this['on'+type] === 'function') this['on'+type](event);
  }
}
window.EventSource = FixtureEventSource;
"""


def fixture(tool_count=0):
    return {
        'stream_id': 'run-fixture', 'last_seq': 10,
        'messages': [{'role': 'assistant', 'content': '', 'reasoning': 'Inspecting fixture'}],
        'tool_calls': [
            {'name': 'terminal', 'tid': f'fixture-tool-{i}',
             'args': {'command': 'true'}, 'preview': 'OK', 'done': True}
            for i in range(tool_count)
        ],
    }


def session_route(session, sid, workspace):
    def respond(route):
        params = parse_qs(urlsplit(route.request.url).query)
        if params.get('session_id', [''])[0] != sid:
            route.fulfill(status=404, json={'error': 'fixture session not found'})
            return
        payload = deepcopy(session)
        payload.setdefault('workspace', workspace)
        messages = payload.get('messages', [])
        payload['message_count'] = len(messages)
        if params.get('messages', ['1'])[0] == '0':
            payload['messages'] = []
        else:
            end = min(len(messages), int(params.get('msg_before', [len(messages)])[0]))
            limit = int(params.get('msg_limit', [end])[0])
            start = max(0, end - limit)
            payload.update(messages=messages[start:end], _messages_offset=start,
                           _messages_truncated=start > 0)
        route.fulfill(json={'session': payload})
    return respond
