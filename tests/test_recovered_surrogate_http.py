"""Persist in another interpreter, then serve recovered output over real HTTP."""
import json
import os
import subprocess
import socket
import time
from contextlib import contextmanager
import sys
import urllib.request
from pathlib import Path

import pytest

SEED = r'''
import json, sys
from tests.test_cancel_restart_journal_recovery import _persist_recovery_boundary_turn
from api.run_journal import RunJournalWriter
from api import models
models.SESSION_DIR.mkdir(parents=True, exist_ok=True)
sid, stream, tokens = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
_persist_recovery_boundary_turn(sid, stream, "crash")
writer = RunJournalWriter(sid, stream)
for text in tokens:
    writer.append_sse_event("token", {"text": text})
writer.append_sse_event("stream_end", {})
'''


@contextmanager
def _new_server(env, root, log_path):
    # Start only after the seed interpreter has exited: this is a real new
    # WebUI process reading durable state, never a client/service restart.
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    env.update(HERMES_WEBUI_PORT=str(port), HERMES_WEBUI_HOST='127.0.0.1',
               HERMES_WEBUI_PASSWORD='', HERMES_WEBUI_TEST_NETWORK_BLOCK='1')
    base_url = f'http://127.0.0.1:{port}'
    with log_path.open('w', encoding='utf-8') as log:
        proc = subprocess.Popen([sys.executable, str(root / 'server.py')], cwd=root, env=env,
                                stdout=log, stderr=subprocess.STDOUT,
                                **({'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}))
        try:
            deadline = time.monotonic() + 30
            while True:
                assert proc.poll() is None, log_path.read_text(encoding='utf-8')
                try:
                    with urllib.request.urlopen(base_url + '/health', timeout=.5) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                assert time.monotonic() < deadline, 'own isolated server startup timeout'
                time.sleep(.03)
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.parametrize("tokens", [["prefix", "\ud83d", "after"], ["prefix", "\udc00", "after"],
                                   ["prefix", "\ud83d", "\ude42", "after"], ["prefix", "普通🙂", "after"]])
@pytest.mark.parametrize("wire", ["json", "sse"])
def test_recovered_surrogate_after_seed_interpreter_exit_is_served(tmp_path, tokens, wire):
    sid = f"http-surrogate-{ord(tokens[1][0])}-{len(tokens)}-{wire}"
    stream = sid + "-run"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('AWS_', 'GH_', 'GITHUB_', 'OPENAI_', 'ANTHROPIC_'))
           and not key.endswith(('_API_KEY', '_AUTH_TOKEN', '_BOT_TOKEN', '_ACCESS_TOKEN'))}
    state = tmp_path / 'state'
    home = tmp_path / 'home'
    workspace = tmp_path / 'workspace'
    home.mkdir()
    workspace.mkdir()
    env.update(HERMES_WEBUI_STATE_DIR=str(state), HERMES_HOME=str(home), HERMES_BASE_HOME=str(home),
               HERMES_CONFIG_PATH=str(home / 'config.yaml'), HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
               HERMES_WEBUI_TEST_NETWORK_BLOCK='1')
    root = Path(__file__).resolve().parents[1]
    seeded = subprocess.run([sys.executable, '-c', SEED, sid, stream, json.dumps(tokens)], env=env,
                            cwd=root, timeout=30, capture_output=True, text=True)
    assert seeded.returncode == 0, seeded.stderr
    with _new_server(env, root, tmp_path / 'server.log') as base_url:
        if wire == "json":
            expected = json.loads(json.dumps("".join(tokens)))
            for _ in range(2):
                with urllib.request.urlopen(base_url + '/api/session?session_id=' + sid, timeout=15) as response:
                    assert response.status == 200
                    payload = json.load(response)
                assert any(row.get('content') == expected for row in payload['session']['messages'])
            persisted = json.loads((Path(env['HERMES_WEBUI_STATE_DIR']) / 'sessions' / (sid + '.json')).read_text())
            assert any(row.get('content') == expected for row in persisted['messages'])
        else:
            with urllib.request.urlopen(base_url + '/api/chat/stream?stream_id=' + stream, timeout=15) as response:
                assert response.status == 200
                body = response.read().decode('utf-8')
            token_payloads = []
            for block in body.split('\n\n'):
                if 'event: token\n' in block:
                    token_payloads.append(json.loads(next(line[6:] for line in block.splitlines() if line.startswith('data: '))))
            assert [row['text'] for row in token_payloads] == tokens
            assert all('id: ' + stream + ':' + str(i + 1) in body for i in range(len(tokens)))


@pytest.mark.parametrize('pretty', [False, True])
@pytest.mark.parametrize('text', ['\ud83d', '\udc00', '普通🙂'])
def test_compact_and_pretty_json_transport_preserve_nested_provider_text(pretty, text):
    from api.helpers import _json_response_body
    payload = {'messages': [{'content': text}], 'metadata': {'nested': text}, 'count': 1}
    body = _json_response_body(payload, pretty=pretty)
    assert json.loads(body.decode('utf-8')) == payload
    if text == '普通🙂':
        assert text.encode('utf-8') in body
