"""Real browser stream/persistence/reconnect handlers with deterministic time."""
import json
import os
from pathlib import Path

import pytest

from tests import browser_conversation_lifecycle as gate

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('virtual', [False, True])
@pytest.mark.parametrize('scenario', ['user-only', 'historical', 'partial', 'published', 'reasoning', 'tool'])
def test_persisted_replay_prefix_is_complete(tmp_path, virtual, scenario):
    from playwright.sync_api import sync_playwright

    agent = tmp_path / 'agent'
    agent.mkdir()
    (agent / 'run_agent.py').write_text('')
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.endswith('_API_KEY')}
    env.update(
        HERMES_WEBUI_HOST='127.0.0.1',
        HERMES_WEBUI_STATE_DIR=str(tmp_path / 'state'),
        HERMES_HOME=str(tmp_path / 'home'),
        HERMES_BASE_HOME=str(tmp_path / 'home'),
        HERMES_CONFIG_PATH=str(tmp_path / 'home/config.yaml'),
        HERMES_WEBUI_SKIP_ONBOARDING='1',
        HERMES_WEBUI_AGENT_DIR=str(agent),
        HERMES_WEBUI_DEFAULT_WORKSPACE=str(workspace),
        NO_PROXY='127.0.0.1,localhost',
        no_proxy='127.0.0.1,localhost',
    )
    for key in ['API_SERVER_KEY', 'HERMES_WEBUI_PASSWORD',
                'HERMES_WEBUI_EXTENSION_DIR', 'HERMES_WEBUI_EXTENSION_MANIFEST']:
        env.pop(key, None)
    gateway = gate.DeterministicGateway('normal')
    gateway.start()
    env.update(HERMES_WEBUI_CHAT_BACKEND='gateway', HERMES_WEBUI_GATEWAY_USE_RUNS_API='1', HERMES_WEBUI_GATEWAY_BASE_URL=gateway.base_url)
    proc = log = None
    try:
        proc, log, _, base = gate._start_webui_server(ROOT, env, tmp_path)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(base_url=base)
            page.goto('/', wait_until='domcontentloaded')
            page.wait_for_selector('#msg')
            page.evaluate('async()=>{await newSession();}')
            page.evaluate('v=>{window._virtualizeTranscript=v;window._chatActivityDisplayMode="transparent_stream";}', virtual)
            page.locator('#msg').fill(gate.PROMPT)
            page.locator('#btnSend').click()
            page.wait_for_function('()=>S.busy&&!!S.activeStreamId')
            gateway.release_reasoning.set()
            gateway.release_settle.set()
            gateway.release_terminal.set()
            page.wait_for_function('()=>!S.busy&&!S.activeStreamId')
            page.evaluate('async()=>{delete INFLIGHT[S.session.session_id];await newSession();}')
            result = page.evaluate((ROOT / 'tests/fixtures/issue7478_replay_coherence.js').read_text(), scenario)
            evidence = Path(os.environ.get('REPLAY_COHERENCE_EVIDENCE', str(tmp_path)))
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / f'replay-{scenario}-virtual-{virtual}.json').write_text(json.dumps(result, indent=2))
            browser.close()
        assert result['persisted']['data']['lastRunJournalSeq'] == 206
        assert result['writesAfterToken'] == result['writesBeforeToken']
        assert result['receipt'].get('lastAssistantText', '') == ('EARLY ' if scenario == 'partial' else '')
        assert result['afterStaleEvent'] == result['reconstructed']
        prefix = ('EARLY ' if scenario == 'partial' else '') + 'AUDIT_REPLAY_BODY'
        assert result['reconstructed']['lastAssistantText'] == prefix + ' SUFFIX'
        assert result['persisted']['stored']['lastAssistantText'] == prefix
        assert result['persisted']['stored']['lastRunJournalSeq'] == 206
        assert 'after_seq=206&' in result['replayUrl']
        assert result['reconstructed']['messages'][-1]['content'] == prefix + ' SUFFIX'
        if scenario == 'historical':
            assert result['reconstructed']['messages'][1]['content'] == 'old answer'
        if scenario == 'tool':
            assert len(result['reconstructed']['toolCalls']) == 1
            assert result['reconstructed']['toolCalls'][0]['done'] is True
        if scenario == 'reasoning':
            assert result['reconstructed']['lastReasoningText'] == 'Current reasoning'
    finally:
        gateway.close()
        gate._terminate_process(proc)
        if log:
            log.close()
