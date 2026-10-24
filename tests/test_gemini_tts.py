"""Gemini read-aloud contract; all upstream calls and credentials are isolated."""
import base64
import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import api.auth as auth
import api.config as config
import api.routes as routes
from tests.js_source_extract import extract_function
from tests.test_issue7391_tts_default_engine import _FakeHandler

ROOT = Path(__file__).resolve().parents[1]
WAV = b'RIFF\x04\x00\x00\x00WAVE'


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def audio_response(data=None):
    return {'steps': [{'type': 'model_output', 'content': [
        {'type': 'audio', 'data': base64.b64encode(WAV).decode() if data is None else data}
    ]}]}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    import api.profiles as profiles
    real_get_active_home = profiles.get_active_hermes_home
    monkeypatch.setattr(profiles, 'get_active_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(profiles, '_loaded_profile_env_keys', set())
    monkeypatch.setattr(auth, 'is_auth_enabled', lambda: False)
    monkeypatch.setattr(config, 'get_config', lambda: {})
    monkeypatch.setattr(routes, 'load_settings', lambda: {'tts_engine': 'gemini'})
    (tmp_path / '.env').write_text('GEMINI_API_KEY=test-key-not-secret\n')
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
    monkeypatch.delattr(routes._handle_tts, '_tts_limiter', raising=False)
    yield real_get_active_home
    monkeypatch.delattr(routes._handle_tts, '_tts_limiter', raising=False)


def post(body=None):
    h = _FakeHandler(json.dumps(body or {'text': 'Hello', 'engine': 'gemini'}).encode())
    routes._handle_tts(h, None)
    return h


@pytest.mark.parametrize('engine', ['gemini', '', None])
def test_gemini_interactions_contract_and_wav(monkeypatch, engine):
    calls = []
    def upstream(req, **kwargs):
        calls.append((req, kwargs))
        return Response(json.dumps(audio_response()).encode())
    monkeypatch.setattr(routes, '_tts_open', upstream)
    body = {'text': 'Read exactly: Ελληνικά.\nDo not paraphrase.'}
    if engine is not None:
        body['engine'] = engine
    h = post(body)
    assert h.status == 200
    assert h.wfile.getvalue() == WAV
    assert h.sent_headers['Content-Type'] == 'audio/wav'
    assert h.sent_headers['Content-Length'] == str(len(WAV))
    assert h.sent_headers['Cache-Control'] == 'no-store'
    req, kwargs = calls[0]
    assert req.full_url == 'https://generativelanguage.googleapis.com/v1beta/interactions'
    assert req.get_header('X-goog-api-key') == 'test-key-not-secret'
    assert kwargs['timeout'] == 60
    opener = kwargs['opener_factory']()
    assert any(isinstance(x, routes._NoRedirectTtsHandler) for x in opener.handlers)
    assert json.loads(req.data) == {
        'model': 'gemini-3.8-flash-lite-tts',
        'input': [{'type': 'user_input', 'content': [{'type': 'text', 'text': body['text']}]}],
        'response_format': {'type': 'audio'},
        'generation_config': {'speech_config': [{'voice': 'Kore'}]},
        'store': False,
    }


def test_gemini_config_and_google_env_file_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    (tmp_path / '.env').write_text('GOOGLE_API_KEY=test-google-key\n')
    monkeypatch.setattr(config, 'get_config', lambda: {'tts': {'provider': 'xai', 'gemini': {'model': 'custom-tts', 'voice': 'Puck'}}})
    def upstream(req, **kwargs):
        assert req.get_header('X-goog-api-key') == 'test-google-key'
        payload = json.loads(req.data)
        assert payload['model'] == 'custom-tts'
        assert payload['generation_config']['speech_config'] == [{'voice': 'Puck'}]
        return Response(json.dumps(audio_response()).encode())
    monkeypatch.setattr(routes, '_tts_open', upstream)
    assert post().status == 200


def test_gemini_missing_key_is_503_not_edge_fallback(monkeypatch, tmp_path):
    (tmp_path / '.env').write_text('')
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: pytest.fail('unexpected network'))
    assert post().status == 503


@pytest.mark.parametrize('payload', [{}, audio_response('%%%'), audio_response(''), {'steps': None}, {'steps': [{'type': 'model_output', 'content': [{'type': 'audio', 'data': base64.b64encode(b'not wav').decode()}]}]}])
def test_gemini_rejects_malformed_or_missing_audio(monkeypatch, payload):
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: Response(json.dumps(payload).encode()))
    h = post()
    assert h.status == 502
    assert h.payload()['error'] == 'Gemini TTS generation failed'


def test_gemini_upstream_exception_does_not_leak_key(monkeypatch, caplog):
    def upstream(*a, **k):
        raise RuntimeError('provider echoed test-key-not-secret')
    monkeypatch.setattr(routes, '_tts_open', upstream)
    h = post()
    assert h.status == 502
    assert 'test-key-not-secret' not in h.wfile.getvalue().decode() + caplog.text


def test_gemini_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(routes, '_TTS_PROXY_MAX_BYTES', 20)
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: Response(b'x' * 100))
    assert post().status == 502


@pytest.mark.parametrize('text,status', [('', 400), ('x' * 5001, 400)])
def test_gemini_shared_text_guards_no_network(monkeypatch, text, status):
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: pytest.fail('unexpected network'))
    assert post({'text': text, 'engine': 'gemini'}).status == status


def test_gemini_auth_guard_no_network(monkeypatch):
    monkeypatch.setattr(auth, 'is_auth_enabled', lambda: True)
    monkeypatch.setattr(auth, 'parse_cookie', lambda h: None)
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: pytest.fail('unexpected network'))
    assert post().status == 401


def test_gemini_rate_limit(monkeypatch):
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: Response(json.dumps(audio_response()).encode()))
    assert post().status == 200
    assert post().status == 429


def test_gemini_settings_option_and_reserved_engine():
    assert '<option value="gemini">Gemini 3.8 Flash-Lite TTS</option>' in (ROOT / 'static/index.html').read_text()
    assert 'gemini:1' in (ROOT / 'static/boot.js').read_text()
    assert "engine==='gemini'" in (ROOT / 'static/panels.js').read_text()


@pytest.mark.parametrize('entry', ['speakMessage(btn)', 'autoReadLastAssistant()'])
@pytest.mark.parametrize('fail', [False, True])
def test_gemini_frontend_chunking_error_and_no_browser_fallback(entry, fail):
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    src = (ROOT / 'static/ui.js').read_text()
    fns = '\n'.join(extract_function(src, name) for name in [
        '_stripForTTS', '_splitForTTS', '_playGeminiTtsChunked', 'speakMessage', 'autoReadLastAssistant', 'stopTTS', '_stopActivePlaybackAudio',
        '_beginTtsPlayback', '_ownsTtsPlayback', '_sendTtsRequest', '_acquireTtsRequestSlot', '_ttsRequestWaitMs', '_noteTtsRequestSent'
    ])
    harness = r'''
const requests=[], toasts=[], revoked=[];
let _ttsSpeaking=false, _playingEdgeAudio=null, _ttsCurrentUtterance=null,
_ttsChunkQueue=[], _ttsChunkIndex=0, _ttsActiveBtn=null, _ttsGeneration=0;
let _ttsRequestMinGapMs=0, _ttsLastRequestTs=0;
const S={activeProfile:'default'};
globalThis.window=globalThis;
const row={dataset:{rawText:'Hello. '.repeat(1000)}};
const btn={dataset:{speaking:'0'},closest:()=>row};
globalThis.localStorage={getItem:k=>k==='hermes-tts-engine'?'gemini':k==='hermes-tts-auto-read'?'true':null};
globalThis.document={baseURI:'http://localhost/',querySelectorAll:s=>s.includes('assistant')?[row]:[btn]};
globalThis.speechSynthesis={cancel(){},speak(){throw Error('browser fallback forbidden');}};
globalThis.showToast=m=>toasts.push(m);
URL.createObjectURL=()=> 'blob:test'; URL.revokeObjectURL=u=>revoked.push(u);
globalThis.Audio=class{play(){queueMicrotask(()=>this.onended());return Promise.resolve();}pause(){}};
globalThis.fetch=async(url,opts)=>{requests.push(JSON.parse(opts.body));return {
 ok:!FAIL,status:502,json:async()=>({error:'Gemini TTS generation failed'}),arrayBuffer:async()=>new ArrayBuffer(12),headers:{get:()=> 'audio/wav'}};};
FNS
ENTRY;
setImmediate(()=>console.log(JSON.stringify({requests,toasts,revoked,speaking:_ttsSpeaking,button:btn.dataset.speaking})));
'''
    script = harness.replace('FAIL', str(fail).lower()).replace('FNS', fns).replace('ENTRY', entry)
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed['requests']
    assert all(r['engine'] == 'gemini' and len(r['text']) <= 5000 for r in observed['requests'])
    assert observed['speaking'] is False
    if fail:
        assert len(observed['requests']) == 1
        assert observed['toasts'] == ['Gemini TTS generation failed']
    else:
        assert len(observed['requests']) > 1
        assert len(observed['revoked']) == len(observed['requests'])


def run_js(script):
    node = shutil.which('node')
    if node is None:
        pytest.skip('node unavailable')
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_gemini_voice_setting_populates_without_browser_speech():
    src = (ROOT / 'static/panels.js').read_text()
    block = src.split('// Populate voice selector based on engine', 1)[1].split('// TTS rate/pitch sliders', 1)[0]
    observed = run_js('''
const sel={innerHTML:''}; globalThis.window=globalThis;
const $=()=>sel; const localStorage={getItem:()=> 'gemini'};
function _speechSetting(){return '';}
function _syncSpeechPreferenceCache(){};
''' + block + '\nconsole.log(JSON.stringify(sel.innerHTML));')
    assert 'Gemini voice (server-configured; default Kore)' in observed


@pytest.mark.parametrize('phase', ['pending', 'playing'])
def test_gemini_stop_prevents_late_audio_and_cleans_active_blob(phase):
    src = (ROOT / 'static/ui.js').read_text()
    fns = '\n'.join(extract_function(src, n) for n in ['_splitForTTS', '_playGeminiTtsChunked', 'stopTTS', '_stopActivePlaybackAudio',
        '_beginTtsPlayback', '_ownsTtsPlayback', '_sendTtsRequest', '_acquireTtsRequestSlot', '_ttsRequestWaitMs', '_noteTtsRequestSent'])
    script = r'''
let _ttsSpeaking=false,_playingEdgeAudio=null,_ttsGeneration=0,
_ttsCurrentUtterance=null,_ttsChunkQueue=[],_ttsChunkIndex=0,_ttsActiveBtn=null;
let _ttsRequestMinGapMs=0, _ttsLastRequestTs=0;
const S={activeProfile:'default'};
globalThis.window=globalThis; globalThis.document={baseURI:'http://localhost/',querySelectorAll:()=>[btn]};
const btn={dataset:{speaking:'0'}};const revoked=[],played=[];
URL.createObjectURL=()=> 'blob:test';URL.revokeObjectURL=u=>revoked.push(u);
globalThis.Audio=class{play(){played.push(1);return Promise.resolve();}pause(){}};
let reply;globalThis.fetch=()=>new Promise(r=>reply=r);
FNS
const done=_playGeminiTtsChunked('Test.',btn);
(async()=>{
 await new Promise(r=>setImmediate(r));
 if(PHASE==='pending'){
   stopTTS();reply({ok:true,arrayBuffer:async()=>new ArrayBuffer(12)});
 }else{
   reply({ok:true,arrayBuffer:async()=>new ArrayBuffer(12)});
   await new Promise(r=>setImmediate(r));stopTTS();
 }
 await done;
 console.log(JSON.stringify({played,revoked,speaking:_ttsSpeaking,button:btn.dataset.speaking}));
})();
'''.replace('FNS', fns).replace('PHASE', json.dumps(phase))
    observed = run_js(script)
    assert observed['speaking'] is False
    assert observed['button'] == '0'
    assert len(observed['played']) == (phase == 'playing')
    assert len(observed['revoked']) == (phase == 'playing')


def test_gemini_voice_mode_uses_shared_gemini_player():
    src = (ROOT / 'static/boot.js').read_text()
    fn = extract_function(src, '_speakResponse')
    observed = run_js('''
const calls=[];const _voiceModeActive=true;let _voiceModeThinkingSid=null;
let _ttsGeneration=0, _voiceTtsGenStart=0, _browserTtsSuppressNextErrorRearm=false;
function _clearBrowserTtsRecovery(){};function _clearVoiceMicRearm(){};
const S={session:{session_id:'test'}};function _setState(){};function _startListening(){};
const document={querySelectorAll:()=>[{dataset:{rawText:'Read this verbatim.'}}]};
const localStorage={getItem:()=> 'gemini'};
function _playGeminiTtsChunked(text,btn){calls.push({text,btn});return new Promise(()=>{});}
''' + fn + '\n_speakResponse();console.log(JSON.stringify(calls));')
    assert observed == [{'text': 'Read this verbatim.', 'btn': None}]


@pytest.mark.parametrize('loaded_key', ['GEMINI_API_KEY', 'GOOGLE_API_KEY'])
@pytest.mark.parametrize('request_key', ['GEMINI_API_KEY', 'GOOGLE_API_KEY', None])
def test_gemini_two_profile_credential_isolation(monkeypatch, tmp_path, isolated,
                                                loaded_key, request_key):
    import os
    import threading
    import api.profiles as profiles

    # Real dotenv reload and request-local resolution; only transport is fake.
    monkeypatch.setattr(profiles, 'get_active_hermes_home', isolated)
    monkeypatch.setattr(profiles, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(profiles, '_is_isolated_profile_mode', lambda: False)
    monkeypatch.setattr(profiles, '_tls', threading.local())
    for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    first = tmp_path / 'profiles' / 'first'
    second = tmp_path / 'profiles' / 'second'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / '.env').write_text(f'{loaded_key}=fake-first-profile-key\n')
    (second / '.env').write_text(
        f'{request_key}=fake-second-profile-key\n' if request_key else '')
    profiles._reload_dotenv(first)
    assert os.environ[loaded_key] == 'fake-first-profile-key'
    assert loaded_key in profiles._loaded_profile_env_keys
    calls = []

    def upstream(req, **kwargs):
        calls.append(req)
        return Response(json.dumps(audio_response()).encode())

    monkeypatch.setattr(routes, '_tts_open', upstream)
    profiles.set_request_profile('second')
    try:
        assert profiles.get_active_hermes_home() == second
        h = post({'text': 'Private second profile text', 'engine': 'gemini',
                  'profile': 'second'})
    finally:
        profiles.clear_request_profile()
    if request_key:
        assert h.status == 200
        assert len(calls) == 1
        assert calls[0].get_header('X-goog-api-key') == 'fake-second-profile-key'
        assert json.loads(calls[0].data)['input'][0]['content'][0]['text'] == 'Private second profile text'
    else:
        assert h.status == 503
        assert h.payload()['error'] == 'Gemini API key not configured'
        assert calls == []


@pytest.mark.parametrize('deployment_key', ['GEMINI_API_KEY', 'GOOGLE_API_KEY'])
@pytest.mark.parametrize('profile_key', ['GEMINI_API_KEY', 'GOOGLE_API_KEY'])
def test_gemini_profile_key_precedes_deployment_key(monkeypatch, tmp_path,
                                                    deployment_key, profile_key):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setenv(deployment_key, 'fake-deployment-key')
    (tmp_path / '.env').write_text(f'{profile_key}=fake-profile-key\n')

    def upstream(req, **kwargs):
        assert req.get_header('X-goog-api-key') == 'fake-profile-key'
        return Response(json.dumps(audio_response()).encode())

    monkeypatch.setattr(routes, '_tts_open', upstream)
    assert post().status == 200


@pytest.mark.parametrize('key', ['GEMINI_API_KEY', 'GOOGLE_API_KEY'])
def test_gemini_process_environment_keys_are_not_supported(monkeypatch, tmp_path, key):
    (tmp_path / '.env').write_text('')
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.setenv(key, 'fake-process-key')
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: pytest.fail('unexpected network'))
    h = post()
    assert h.status == 503
    assert h.payload()['error'] == 'Gemini API key not configured'


@pytest.mark.parametrize('loaded_key', ['GEMINI_API_KEY', 'GOOGLE_API_KEY'])
@pytest.mark.parametrize('request_key', ['GEMINI_API_KEY', 'GOOGLE_API_KEY', None])
@pytest.mark.parametrize('reload_state', ['publication', 'partial_failure'])
def test_gemini_reload_cannot_supply_another_requests_credentials(
        monkeypatch, tmp_path, isolated, loaded_key, request_key, reload_state):
    import os
    import threading
    from collections.abc import MutableMapping
    import api.profiles as profiles

    monkeypatch.setattr(profiles, 'get_active_hermes_home', isolated)
    monkeypatch.setattr(profiles, '_DEFAULT_HERMES_HOME', tmp_path)
    monkeypatch.setattr(profiles, '_is_isolated_profile_mode', lambda: False)
    monkeypatch.setattr(profiles, '_tls', threading.local())
    for key in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        monkeypatch.delenv(key, raising=False)
    first = tmp_path / 'profiles' / 'first'
    second = tmp_path / 'profiles' / 'second'
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / '.env').write_text(f'{loaded_key}=fake-first-profile-key\n' +
                              ('BAD\x00KEY=value\n' if reload_state == 'partial_failure' else ''))
    (second / '.env').write_text(
        f'{request_key}=fake-second-profile-key\n' if request_key else '')
    profiles.set_request_profile('second')
    assert profiles.get_active_hermes_home() == second
    calls = []

    def upstream(req, **kwargs):
        calls.append(req)
        return Response(json.dumps(audio_response()).encode())

    monkeypatch.setattr(routes, '_tts_open', upstream)
    published, release = threading.Event(), threading.Event()
    original = os.environ

    class PausedEnvironment(MutableMapping):
        def __getitem__(self, key):
            return original[key]

        def __delitem__(self, key):
            del original[key]

        def __iter__(self):
            return iter(original)

        def __len__(self):
            return len(original)

        def __setitem__(self, key, value):
            original[key] = value
            if key == loaded_key:
                published.set()
                assert release.wait(10), 'dotenv publication barrier timed out'

    worker = None
    try:
        if reload_state == 'publication':
            monkeypatch.setattr(os, 'environ', PausedEnvironment())
            worker = threading.Thread(target=profiles._reload_dotenv, args=(first,))
            worker.start()
            assert published.wait(10), 'dotenv key was not published'
        else:
            profiles._reload_dotenv(first)
        # Both failure windows leave a real key without published provenance.
        assert original[loaded_key] == 'fake-first-profile-key'
        assert loaded_key not in profiles._loaded_profile_env_keys
        h = post({'text': 'Private second profile text', 'engine': 'gemini',
                  'profile': 'second'})
    finally:
        release.set()
        if worker is not None:
            worker.join(10)
        profiles.clear_request_profile()
        original.pop(loaded_key, None)
    assert worker is None or not worker.is_alive()
    if request_key:
        assert h.status == 200
        assert len(calls) == 1
        assert calls[0].get_header('X-goog-api-key') == 'fake-second-profile-key'
        assert json.loads(calls[0].data)['input'][0]['content'][0]['text'] == 'Private second profile text'
    else:
        assert h.status == 503
        assert h.payload()['error'] == 'Gemini API key not configured'
        assert calls == []


@pytest.mark.parametrize('failure', ['home', 'dotenv'])
def test_gemini_profile_resolution_unavailable_fails_closed(monkeypatch, failure):
    import api.onboarding as onboarding
    import api.profiles as profiles

    def unavailable(*args):
        raise OSError('synthetic profile resolution failure')

    if failure == 'home':
        monkeypatch.setattr(profiles, 'get_active_hermes_home', unavailable)
    else:
        monkeypatch.setattr(onboarding, '_load_env_file', unavailable)
    monkeypatch.setenv('GEMINI_API_KEY', 'fake-deployment-key')
    monkeypatch.setenv('GOOGLE_API_KEY', 'fake-other-deployment-key')
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: pytest.fail('unexpected network'))
    assert post().status == 503


def test_gemini_5000_char_boundary_and_outer_whitespace_verbatim(monkeypatch):
    text = ' ' + 'x' * 4998 + '\n'
    def upstream(req, **kwargs):
        assert json.loads(req.data)['input'][0]['content'][0]['text'] == text
        return Response(json.dumps(audio_response()).encode())
    monkeypatch.setattr(routes, '_tts_open', upstream)
    assert post({'text': text, 'engine': 'gemini'}).status == 200


@pytest.mark.parametrize('gemini_config', [{'voice': ['Kore']}, {'model': 'bad\nmodel'}, {'model': 7}])
def test_gemini_invalid_config_rejected_before_network(monkeypatch, gemini_config):
    monkeypatch.setattr(config, 'get_config', lambda: {'tts': {'gemini': gemini_config}})
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: pytest.fail('unexpected network'))
    assert post().status == 400


def test_gemini_saved_engine_round_trip(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'SETTINGS_FILE', tmp_path / 'settings.json')
    config.save_settings({'tts_engine': 'gemini'})
    assert config.load_settings()['tts_engine'] == 'gemini'


def test_gemini_profile_mismatch_precedes_credentials_and_quota(monkeypatch):
    import api.profiles as profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'second')
    monkeypatch.setattr(config, 'get_config', lambda: pytest.fail('config read'))
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: pytest.fail('network'))
    assert post({'text': 'Private first profile text', 'engine': 'gemini', 'profile': 'first'}).status == 409
    assert not hasattr(routes._handle_tts, '_tts_limiter')


@pytest.mark.parametrize('kind', ['ambiguous', 'decoded_size'])
def test_gemini_rejects_ambiguous_and_oversized_decoded_audio(monkeypatch, kind):
    payload = audio_response()
    if kind == 'ambiguous':
        payload['steps'][0]['content'] *= 2
    else:
        monkeypatch.setattr(routes, '_TTS_PROXY_MAX_BYTES', 1000)
        payload = audio_response(base64.b64encode(WAV + b'x' * 1000).decode())
    monkeypatch.setattr(routes, '_tts_open', lambda *a, **k: Response(json.dumps(payload).encode()))
    assert post().status == 502


def gemini_lifecycle_functions():
    src = (ROOT / 'static/ui.js').read_text()
    return '\n'.join(extract_function(src, n) for n in [
        '_splitForTTS', '_playGeminiTtsChunked', 'stopTTS', '_stopActivePlaybackAudio',
        '_beginTtsPlayback', '_ownsTtsPlayback', '_sendTtsRequest', '_acquireTtsRequestSlot',
        '_ttsRequestWaitMs', '_noteTtsRequestSent', '_playEdgeTtsChunked'])


LIFECYCLE_SETUP = r'''
let _ttsSpeaking=false,_playingEdgeAudio=null,_ttsGeneration=0,
_ttsCurrentUtterance=null,_ttsChunkQueue=[],_ttsChunkIndex=0,_ttsActiveBtn=null;
let _ttsRequestMinGapMs=2000, _ttsLastRequestTs=0, clock=10000;
Date.now=()=>clock;
const timers=[];globalThis.setTimeout=(fn,ms)=>{const t={fn,at:clock+ms};timers.push(t);return t;};
globalThis.clearTimeout=t=>{const i=timers.indexOf(t);if(i>=0) timers.splice(i,1);};
const S={activeProfile:'first'}, requests=[], audio=[], revoked=[], toasts=[];
const btn={dataset:{speaking:'0'}};
globalThis.window=globalThis;
globalThis.document={baseURI:'http://localhost/',querySelectorAll:()=>[btn]};
globalThis.localStorage={getItem:()=>null};
globalThis.showToast=m=>toasts.push(m);
URL.createObjectURL=()=> 'blob:'+audio.length;
URL.revokeObjectURL=u=>revoked.push(u);
globalThis.Audio=class{
 constructor(url){this.url=url;audio.push(this);}
 play(){this.played=true;return Promise.resolve();}
 pause(){this.paused=true;}
};
globalThis.fetch=(url,init)=>new Promise(resolve=>requests.push({body:JSON.parse(init.body),resolve,at:clock}));
const response={ok:true,status:200,arrayBuffer:async()=>new ArrayBuffer(12),headers:{get:()=> 'audio/wav'}};
const flush=()=>new Promise(r=>setImmediate(r));
async function advance(ms){clock+=ms;const due=timers.filter(t=>t.at<=clock);due.forEach(t=>timers.splice(timers.indexOf(t),1));due.forEach(t=>t.fn());await flush();}
'''


@pytest.mark.parametrize('phase', ['fetch', 'playing'])
def test_gemini_replacement_cannot_clobber_new_owner(phase):
    observed = run_js(LIFECYCLE_SETUP + gemini_lifecycle_functions() + r'''
(async()=>{
 const old=_playGeminiTtsChunked('Old. '.repeat(1500),btn);await flush();
 if(PHASE==='playing'){requests[0].resolve(response);await flush();}
 stopTTS();const fresh=_playGeminiTtsChunked('New.',btn);await flush();
 if(PHASE==='fetch'){requests[0].resolve(response);await flush();}
 await advance(2000);requests[1].resolve(response);await flush();
 const owner=_playingEdgeAudio;
 if(PHASE==='playing'){audio[0].onended();await flush();}
 const kept=_playingEdgeAudio===owner&&_ttsSpeaking&&btn.dataset.speaking==='1';
 owner.onended();await fresh;await old;
 console.log(JSON.stringify({kept,requests:requests.map(r=>r.body),played:audio.length,revoked,speaking:_ttsSpeaking,button:btn.dataset.speaking}));
})();
'''.replace('PHASE', json.dumps(phase)))
    assert observed['kept'] is True
    assert len(observed['requests']) == 2
    assert observed['played'] == (2 if phase == 'playing' else 1)
    assert len(observed['revoked']) == observed['played']
    assert observed['speaking'] is False and observed['button'] == '0'


@pytest.mark.parametrize('direction', ['edge_first', 'gemini_first'])
def test_gemini_and_edge_share_pacing_and_captured_profile(direction):
    observed = run_js(LIFECYCLE_SETUP + gemini_lifecycle_functions() + r'''
(async()=>{
 if(DIRECTION==='edge_first') _playEdgeTtsChunked('Edge.',btn);
 else _playGeminiTtsChunked('Gemini.',btn);
 await flush();stopTTS();
 if(DIRECTION==='edge_first') _playGeminiTtsChunked('Gemini. '.repeat(1500),btn);
 else _playEdgeTtsChunked('Edge.',btn);
 S.activeProfile='second';await flush();
 await advance(1999);const before=requests.length;
 await advance(1);requests[1].resolve(response);await flush();
 if(DIRECTION==='edge_first'){
   audio[0].onended();await flush();await advance(2000);
 }
 const result={before,requests:requests.map(r=>({body:r.body,at:r.at}))};
 stopTTS();requests[0].resolve(response);await flush();
 console.log(JSON.stringify(result));
})();
'''.replace('DIRECTION', json.dumps(direction)))
    assert observed['before'] == 1
    assert observed['requests'][1]['at'] - observed['requests'][0]['at'] == 2000
    assert all(r['body']['profile'] == 'first' for r in observed['requests'])
    assert observed['requests'][1]['body']['engine'] == ('gemini' if direction == 'edge_first' else 'edge')
    if direction == 'edge_first':
        assert len(observed['requests']) == 3


@pytest.mark.parametrize('cancel', [False, True])
def test_gemini_429_retry_is_bounded_paced_and_cancellable(cancel):
    observed = run_js(LIFECYCLE_SETUP + gemini_lifecycle_functions() + r'''
(async()=>{
 const done=_playGeminiTtsChunked('Hello.',btn);await flush();
 const limited={ok:false,status:429,json:async()=>({error:'rate limited'})};
 requests[0].resolve(limited);await flush();
 if(CANCEL) stopTTS();
 for(let i=1;i<=3;i++){
   await advance(2000);
   if(requests[i]){requests[i].resolve(limited);await flush();}
 }
 await done;
 console.log(JSON.stringify({at:requests.map(r=>r.at),toasts,speaking:_ttsSpeaking,button:btn.dataset.speaking}));
})();
'''.replace('CANCEL', str(cancel).lower()))
    assert observed['at'] == ([10000] if cancel else [10000, 12000, 14000, 16000])
    assert observed['toasts'] == ([] if cancel else ['rate limited'])
    assert observed['speaking'] is False and observed['button'] == '0'


@pytest.mark.parametrize('failure', ['constructor', 'play', 'error'])
def test_gemini_audio_failures_release_url_and_settle(failure):
    observed = run_js(LIFECYCLE_SETUP + gemini_lifecycle_functions() + r'''
(async()=>{
 if(FAILURE==='constructor') globalThis.Audio=class{constructor(){throw Error('construct failed');}};
 if(FAILURE==='play') globalThis.Audio=class{play(){return Promise.reject(Error('play failed'));}pause(){}};
 const done=_playGeminiTtsChunked('Hello. '.repeat(1500),btn);await flush();
 requests[0].resolve(response);await flush();
 if(FAILURE==='error') audio[0].onerror();
 await done;
 console.log(JSON.stringify({requests:requests.length,revoked,toasts,speaking:_ttsSpeaking,button:btn.dataset.speaking}));
})();
'''.replace('FAILURE', json.dumps(failure)))
    assert observed['requests'] == 1
    assert len(observed['revoked']) == 1
    assert len(observed['toasts']) == 1
    assert observed['speaking'] is False and observed['button'] == '0'


@pytest.mark.parametrize('outcome', ['success', 'failure', 'cancel', 'replace'])
def test_gemini_voice_mode_rearms_only_when_current_audio_is_quiet(outcome):
    boot = (ROOT / 'static/boot.js').read_text()
    functions = '\n'.join(extract_function(boot, n) for n in [
        '_speakResponse', '_scheduleVoiceMicRearm', '_clearVoiceMicRearm'])
    closure = boot.split('window._hermesTtsVoiceClosure=function(){', 1)[1].split('};', 1)[0]
    observed = run_js(LIFECYCLE_SETUP + gemini_lifecycle_functions() + functions + r'''
let _voiceModeActive=true,_voiceModeState='idle',_voiceModeThinkingSid=null,
_voiceTtsGenStart=0,_voiceMicRearmTimer=null,_browserTtsSuppressNextErrorRearm=false;
let listened=0;
function _startListening(){listened++;_voiceModeState='listening';}
function _setState(state){_voiceModeState=state;}
function _clearBrowserTtsRecovery(){}
window._hermesTtsVoiceClosure=function(){CLOSURE};
localStorage.getItem=()=> 'gemini';
document.querySelectorAll=s=>s.includes('assistant')?[{dataset:{rawText:'Voice reply.'}}]:[btn];
(async()=>{
 _speakResponse();await flush();
 if(OUTCOME==='cancel'){stopTTS();requests[0].resolve(response);await flush();}
 else{
   requests[0].resolve(OUTCOME==='failure'?{ok:false,status:502,json:async()=>({error:'failure'})}:response);
   await flush();
   if(OUTCOME!=='failure') audio[0].onended();await flush();
 }
 let during=0;
 if(OUTCOME==='replace'){
   _playGeminiTtsChunked('Replacement.',btn);await flush();
   await advance(500);during=listened;
   await advance(1500);requests[1].resolve(response);await flush();
   await advance(500);during+=listened;
   audio[1].onended();await flush();
 }
 await advance(500);
 console.log(JSON.stringify({during,listened,state:_voiceModeState,speaking:_ttsSpeaking,requests:requests.map(r=>r.body)}));
})();
'''.replace('CLOSURE', closure).replace('OUTCOME', json.dumps(outcome)))
    assert observed['during'] == 0
    assert observed['listened'] == 1
    assert observed['state'] == 'listening'
    assert observed['speaking'] is False
    assert all(r['engine'] == 'gemini' and r['profile'] == 'first' for r in observed['requests'])
