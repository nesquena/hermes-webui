import json
import threading
import time
import os
import random
import math
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


# Mock server supports configurable simulated latency and canned responses via environment variables:
# - MOCK_LATENCY_MS: fixed latency in milliseconds
# - MOCK_MIN_LATENCY_MS / MOCK_MAX_LATENCY_MS: range for random latency (overrides MOCK_LATENCY_MS if both set, uniform)
# - MOCK_LATENCY_DISTRIBUTION: 'uniform' (default) or 'normal'
# - MOCK_LATENCY_MEAN_MS / MOCK_LATENCY_STD_MS: for normal distribution
# - MOCK_RESPONSE_MODE: 'echo' (default) or 'canned'
# - MOCK_CANNED_TEXT: text to return when MOCK_RESPONSE_MODE='canned'
# - MOCK_CANNED_RESPONSES: JSON mapping of question substring -> canned text
# - MOCK_MODEL_RESPONSES: JSON mapping of model name -> canned text
# - MOCK_VERBOSE: if set to '1', the server will print a bit more logging to stdout
# - STREAM: supports streaming when request includes "stream": true in JSON payload or query param stream=1


def _get_latency_seconds() -> float:
    dist = os.environ.get("MOCK_LATENCY_DISTRIBUTION", "uniform").lower()
    if dist == "normal":
        mean = int(os.environ.get("MOCK_LATENCY_MEAN_MS", "50"))
        std = int(os.environ.get("MOCK_LATENCY_STD_MS", "20"))
        # generate normal, clamp at 0
        val = int(random.gauss(mean, std))
        if val < 0:
            val = 0
        return val / 1000.0

    # uniform / default
    min_ms = os.environ.get("MOCK_MIN_LATENCY_MS")
    max_ms = os.environ.get("MOCK_MAX_LATENCY_MS")
    if min_ms is not None and max_ms is not None:
        try:
            lo = int(min_ms)
            hi = int(max_ms)
            if lo < 0:
                lo = 0
            if hi < lo:
                hi = lo
            ms = random.randint(lo, hi)
            return ms / 1000.0
        except Exception:
            pass
    fixed = os.environ.get("MOCK_LATENCY_MS")
    if fixed is not None:
        try:
            ms = int(fixed)
            if ms < 0:
                ms = 0
            return ms / 1000.0
        except Exception:
            pass
    return 0.0


def _load_json_env(name: str):
    v = os.environ.get(name)
    if not v:
        return {}
    try:
        return json.loads(v)
    except Exception:
        try:
            # tolerate simple key=value;key2=value2 format
            out = {}
            for part in v.split(';'):
                if not part:
                    continue
                k, sep, val = part.partition('=')
                if sep:
                    out[k.strip()] = val.strip()
            return out
        except Exception:
            return {}


CANNED_MAP = _load_json_env('MOCK_CANNED_RESPONSES')
MODEL_MAP = _load_json_env('MOCK_MODEL_RESPONSES')


def _match_canned(question: str, model: str | None) -> str | None:
    # Priority: exact model map -> question substring map
    if model and MODEL_MAP:
        if model in MODEL_MAP:
            return MODEL_MAP[model]
    if CANNED_MAP:
        for k, v in CANNED_MAP.items():
            if k.lower() in question.lower():
                return v
    return None


class MockHandler(BaseHTTPRequestHandler):
    def _set_json(self, status=200, content_type='application/json'):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        # do not set Content-Length to enable streaming/chunked style behavior
        self.end_headers()

    def _maybe_sleep(self):
        s = _get_latency_seconds()
        if s > 0:
            if os.environ.get("MOCK_VERBOSE") == "1":
                print(f"[mock] sleeping for {s:.3f}s to simulate latency")
            time.sleep(s)

    def _write_json(self, obj):
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        # Support either /v1/models or /models (preflight may call /models on the base URL)
        path = self.path.rstrip('/')
        if path.endswith('/v1/models') or path.endswith('/models') or path.startswith('/v1/models'):
            self._maybe_sleep()
            self._set_json(200)
            body = {"data": [{"id": "mock-model", "object": "model"}]}
            self._write_json(body)
            return
        self._set_json(404)
        self._write_json({"error": "not found"})

    def do_POST(self):
        # Chat/completions endpoints
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        stream_q = qs.get('stream', ['0'])[0] == '1'

        if self.path.startswith('/v1/chat/completions') or self.path.startswith('/v1/completions') or self.path.startswith('/chat/completions'):
            length = int(self.headers.get('content-length', 0))
            data = self.rfile.read(length) if length else b'{}'
            try:
                payload = json.loads(data.decode())
            except Exception:
                payload = {}

            # Find most recent user message or prompt
            content = None
            if isinstance(payload.get('messages'), list):
                for m in reversed(payload['messages']):
                    if m.get('role') == 'user':
                        content = m.get('content')
                        break
            if not content:
                content = payload.get('prompt') or 'Hello from mock'

            model = payload.get('model')
            # Match canned per-model or per-question
            canned = _match_canned(content or '', model)

            mode = os.environ.get('MOCK_RESPONSE_MODE', 'echo').lower()
            if mode == 'canned' and not canned:
                reply_text = os.environ.get('MOCK_CANNED_TEXT', 'This is a canned mock response.')
            elif canned:
                reply_text = canned
            else:
                reply_text = f"Mock reply: {content}"

            # Determine if streaming requested via payload
            stream_payload = bool(payload.get('stream'))
            stream = stream_q or stream_payload

            if stream:
                # Stream responses as server-sent events compatible with OpenAI-style 'data: {...}\n\n' chunks
                self._set_json(200, content_type='text/event-stream')
                # Simulate chunking: break reply into sentences and send with delays
                sentences = [s.strip() for s in reply_text.split('.') if s.strip()]
                if not sentences:
                    sentences = [reply_text]
                try:
                    for idx, part in enumerate(sentences):
                        chunk_obj = {
                            'id': f"mock-{int(time.time())}",
                            'object': 'chat.completion.chunk',
                            'created': int(time.time()),
                            'model': model or 'mock-model',
                            'choices': [
                                {
                                    'delta': {'role': 'assistant', 'content': (part + ('.' if idx < len(sentences)-1 else ''))},
                                    'index': 0,
                                    'finish_reason': None,
                                }
                            ],
                        }
                        # Send in OpenAI stream format
                        data_line = 'data: ' + json.dumps(chunk_obj) + '\n\n'
                        self.wfile.write(data_line.encode())
                        self.wfile.flush()
                        # Sleep between chunks to simulate streaming
                        time.sleep(0.05 + _get_latency_seconds())
                    # final DONE message
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                except BrokenPipeError:
                    # client disconnected
                    pass
                return

            # Non-streaming path: simulate latency then return full JSON
            self._maybe_sleep()
            resp = {
                "id": "mock-" + str(int(time.time())),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model or 'mock-model',
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply_text},
                        "finish_reason": "stop",
                    }
                ],
            }
            self._set_json(200)
            self._write_json(resp)
            return

        self._set_json(404)
        self._write_json({"error": "not found"})

    def log_message(self, format, *args):
        # Silence default logging to keep output clean unless verbose requested
        if os.environ.get("MOCK_VERBOSE") == "1":
            super().log_message(format, *args)
        return


def start_mock_server(port: int = 5080, host: str = '127.0.0.1'):
    """Start the mock OpenAI-compatible server in a background thread.

    Returns (server, thread).
    """
    server = HTTPServer((host, port), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if os.environ.get("MOCK_VERBOSE") == "1":
        print(f"[mock] started on http://{host}:{port}")
    return server, thread


if __name__ == '__main__':
    srv, thr = start_mock_server(5080)
    print('Mock OpenAI server running on http://127.0.0.1:5080 (Ctrl-C to stop)')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.shutdown()
        srv.server_close()
