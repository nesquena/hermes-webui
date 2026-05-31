import json
import threading
from api.helpers import j

_lock = threading.Lock()
_pending = None
_last = None

def _read_body(handler):
    l = int(handler.headers.get('content-length', 0))
    b = handler.rfile.read(l).decode('utf-8')
    return json.loads(b)

def handle_ask_push(handler, parsed):
    global _pending
    try:
        data = _read_body(handler)
        q = data.get('question')
        o = data.get('options')
        if not isinstance(q, str) or not isinstance(o, list):
            return j(handler, {'error': 'Invalid request'}, status=400)
        with _lock:
            _pending = {'question': q, 'options': o}
        return j(handler, {'status': 'queued'})
    except Exception as e:
        return j(handler, {'error': str(e)}, status=500)

def handle_ask_pending(handler, parsed):
    global _pending
    with _lock:
        if _pending is not None:
            p = _pending
            _pending = None
            return j(handler, p)
    return j(handler, None)

def handle_ask_answer(handler, parsed):
    global _last
    try:
        data = _read_body(handler)
        q = data.get('question')
        a = data.get('answer')
        if not isinstance(q, str) or not isinstance(a, str):
            return j(handler, {'error': 'Invalid request'}, status=400)
        with _lock:
            _last = {'question': q, 'answer': a}
        return j(handler, {'status': 'received'})
    except Exception as e:
        return j(handler, {'error': str(e)}, status=500)

def handle_ask_last_answer(handler, parsed):
    global _last
    with _lock:
        if _last is not None:
            ans = _last
            _last = None
            return j(handler, ans)
    return j(handler, None)