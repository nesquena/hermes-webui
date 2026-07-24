"""Settings UI, provider selection, and reversible migration contract."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TTS_JS = (ROOT / "static" / "tts.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is required")


def _node(body: str, api_impl: str):
    script = f"""
globalThis.window=globalThis;
globalThis.addEventListener=()=>{{}};
const stored=new Map([['hermes-tts-engine','edge'],['hermes-tts-voice','en-US-AriaNeural']]);
globalThis.localStorage={{getItem:key=>stored.has(key)?stored.get(key):null,setItem:(key,value)=>stored.set(key,String(value)),removeItem:key=>stored.delete(key)}};
globalThis.S={{profile:'default'}};
globalThis.api={api_impl};
globalThis.crypto={{randomUUID:()=> '12345678-1234-4678-9234-567812345678'}};
{TTS_JS}
{body}
"""
    result = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, cwd=ROOT
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _capability():
    return {
        "synthesis_supported": True,
        "provider_write_supported": True,
        "active_provider_available": True,
        "active_provider": "edge",
        "active_provider_name": "Microsoft Edge TTS",
        "config_fingerprint": "sha256:" + "0" * 64,
        "request_max_text_length": 4000,
        "providers": [
            {
                "name": "Microsoft Edge TTS",
                "provider_id": "edge",
                "available": True,
                "configured": True,
                "selectable": True,
                "active": True,
            }
        ],
    }


def test_only_browser_and_agent_are_selectable_in_static_markup():
    start = INDEX.index('id="settingsTtsEngine"')
    end = INDEX.index('</select>', start)
    options = INDEX[start:end]
    assert options.count('<option ') == 2
    assert 'value="browser"' in options
    assert 'value="agent"' in options
    for retired in ('edge', 'elevenlabs', 'openai', 'server'):
        assert f'value="{retired}"' not in options


def test_settings_surface_has_live_status_busy_and_provider_guidance():
    assert 'id="settingsTtsStatus" role="status" aria-live="polite"' in INDEX
    assert 'id="settingsTtsProvider"' in INDEX
    assert "const setupToken=Symbol('tts-settings')" in PANELS_JS
    assert "engineSelect._hermesTtsSetupToken=setupToken" in PANELS_JS
    assert "engineSelect._hermesTtsSetupToken===setupToken" in PANELS_JS
    assert "setAttribute('aria-busy','true')" in PANELS_JS
    assert "tts_provider_guidance" in PANELS_JS
    assert "option.disabled=!row.selectable" in PANELS_JS
    assert "inert.disabled=true" in PANELS_JS
    assert "getSettingsState(settings,{refresh:false})" in PANELS_JS


def test_settings_state_reuses_profile_scoped_capability_until_manual_refresh():
    capability = json.dumps(_capability())
    api_impl = f"""async(path)=>{{
  if(path==='/api/tts/capability'){{globalThis.capabilityCalls=(globalThis.capabilityCalls||0)+1;return {capability};}}
  throw new Error('unexpected '+path);
}}"""
    body = """
const settings={tts_engine:'agent',persisted_speech_keys:['tts_engine'],speech_settings_revision:1};
Promise.resolve()
  .then(()=>HermesTTS.getSettingsState(settings))
  .then(()=>HermesTTS.getSettingsState(settings))
  .then(state=>process.stdout.write(JSON.stringify({calls:capabilityCalls,provider:state.capability.active_provider})));
"""
    assert _node(body, api_impl) == {"calls": 1, "provider": "edge"}


def test_provider_save_updates_cached_capability_without_forced_reload():
    capability = _capability()
    capability["providers"].append(
        {
            "name": "Google Gemini TTS",
            "provider_id": "gemini",
            "available": True,
            "configured": True,
            "selectable": True,
            "active": False,
        }
    )
    capability_json = json.dumps(capability)
    fingerprint = "sha256:" + "1" * 64
    api_impl = f"""async(path,opts)=>{{
  globalThis.calls=(globalThis.calls||[]);calls.push(path);
  if(path==='/api/tts/capability')return {capability_json};
  if(path==='/api/tts/provider')return {{active_provider:'gemini',active_provider_name:'Google Gemini TTS',active_provider_available:true,synthesis_supported:true,provider_max_text_length:800,request_max_text_length:800,limit_source:'agent',config_fingerprint:'{fingerprint}'}};
  throw new Error('unexpected '+path);
}}"""
    body = """
const settings={tts_engine:'agent',persisted_speech_keys:['tts_engine'],speech_settings_revision:1};
HermesTTS.getSettingsState(settings,{refresh:true})
  .then(state=>HermesTTS.selectProvider('Google Gemini TTS',state.capability))
  .then(()=>HermesTTS.getSettingsState(settings))
  .then(state=>process.stdout.write(JSON.stringify({
    calls,
    active:state.capability.active_provider,
    synthesis:state.capability.synthesis_supported,
    requestMax:state.capability.request_max_text_length,
    rows:state.capability.providers.map(row=>[row.name,row.active,row.configured,row.selectable])
  })));
"""
    assert _node(body, api_impl) == {
        "calls": ["/api/tts/capability", "/api/tts/provider"],
        "active": "gemini",
        "synthesis": True,
        "requestMax": 800,
        "rows": [
            ["Microsoft Edge TTS", False, True, True],
            ["Google Gemini TTS", True, True, True],
        ],
    }


def test_timeout_reconciliation_cannot_apply_after_profile_switch():
    api_impl = """async(path,opts)=>{
  if(path==='/api/tts/capability')return {synthesis_supported:true,active_provider_available:true,provider_write_supported:true,config_fingerprint:'fp',providers:[{provider_id:'edge',name:'Edge',selectable:true}]};
  if(path==='/api/tts/provider')return await new Promise((resolve,reject)=>setTimeout(()=>{const error=new Error('timeout');error.name='TimeoutError';reject(error);},0));
  if(path==='/api/settings')return {tts_engine:'agent',persisted_speech_keys:['tts_engine'],speech_settings_revision:9};
  throw new Error('unexpected '+path);
}"""
    body = """
S={activeProfile:'alpha'};
const settings={tts_engine:'edge',persisted_speech_keys:['tts_engine'],speech_settings_revision:8};
HermesTTS.getSettingsState(settings,{refresh:true}).then(state=>{
  const pending=HermesTTS.migrateLegacyEngine(settings,state.capability).catch(error=>error.name);
  S.activeProfile='beta';HermesTTS.invalidateCapability();
  return pending.then(result=>process.stdout.write(JSON.stringify({result,engine:stored.get('hermes-tts-engine')})));
});
"""
    assert _node(body, api_impl) == {"result": "AbortError", "engine": "edge"}


def test_migration_success_commits_only_after_authoritative_response():
    capability = json.dumps(_capability())
    api_impl = f"""async(path,opts)=>{{
  if(path==='/api/tts/capability')return {capability};
  if(path==='/api/tts/provider'){{
    globalThis.posted=JSON.parse(opts.body);
    return {{speech_settings:{{revision:8,present_keys:['tts_engine','tts_voice'],values:{{tts_engine:'agent',tts_voice:'en-US-AriaNeural'}}}}}};
  }}
  throw new Error('unexpected '+path);
}}"""
    body = """
const settings={tts_engine:'edge',tts_voice:'en-US-AriaNeural',persisted_speech_keys:['tts_engine','tts_voice'],speech_settings_revision:7};
HermesTTS.getSettingsState(settings,{refresh:true}).then(state=>HermesTTS.migrateLegacyEngine(settings,state.capability)).then(result=>{
  process.stdout.write(JSON.stringify({persisted:stored.get('hermes-tts-engine'),voice:stored.get('hermes-tts-voice'),posted,result}));
});
"""
    result = _node(body, api_impl)
    assert result["persisted"] == "agent"
    assert result["voice"] == "en-US-AriaNeural"
    migration = result["posted"]["migration"]
    assert migration["legacy_engine_was_persisted"] is True
    assert migration["legacy_voice_was_persisted"] is True
    assert migration["legacy_edge_voice"] == "en-US-AriaNeural"
    assert result["posted"]["provider"] == "Microsoft Edge TTS"


def test_known_failure_preserves_exact_local_selection_and_voice():
    capability = json.dumps(_capability())
    api_impl = f"""async(path,opts)=>{{
  if(path==='/api/tts/capability')return {capability};
  const error=new Error('conflict');error.status=409;throw error;
}}"""
    body = """
const settings={tts_engine:'edge',tts_voice:'en-US-AriaNeural',persisted_speech_keys:['tts_engine','tts_voice'],speech_settings_revision:7};
HermesTTS.getSettingsState(settings,{refresh:true}).then(state=>HermesTTS.migrateLegacyEngine(settings,state.capability)).then(()=>process.exit(2)).catch(error=>{
  process.stdout.write(JSON.stringify({status:error.status,persisted:stored.get('hermes-tts-engine'),voice:stored.get('hermes-tts-voice'),suppressed:HermesTTS.isAutosaveSuppressed('tts_engine')}));
});
"""
    assert _node(body, api_impl) == {
        "status": 409,
        "persisted": "edge",
        "voice": "en-US-AriaNeural",
        "suppressed": False,
    }


def test_timeout_reconciles_authoritative_settings_instead_of_assuming_rollback():
    capability = json.dumps(_capability())
    api_impl = f"""async(path,opts)=>{{
  if(path==='/api/tts/capability')return {capability};
  if(path==='/api/tts/provider'){{const error=new Error('timeout');error.name='TimeoutError';throw error;}}
  if(path==='/api/settings')return {{tts_engine:'agent',persisted_speech_keys:['tts_engine'],speech_settings_revision:8}};
  throw new Error('unexpected');
}}"""
    body = """
const settings={tts_engine:'edge',persisted_speech_keys:['tts_engine'],speech_settings_revision:7};
HermesTTS.getSettingsState(settings,{refresh:true}).then(state=>HermesTTS.migrateLegacyEngine(settings,state.capability)).then(result=>{
  process.stdout.write(JSON.stringify({reconciled:result.reconciled,persisted:stored.get('hermes-tts-engine')}));
});
"""
    assert _node(body, api_impl) == {"reconciled": True, "persisted": "agent"}


def test_migration_suppresses_engine_voice_autosave_and_boot_mirror():
    assert "isAutosaveSuppressed(settingKey)" in PANELS_JS
    assert "shouldMirrorSetting(settingKey,generation)" in BOOT_JS
    assert "migrationPending&&(settingKey==='tts_engine'||settingKey==='tts_voice')" in TTS_JS


def test_provider_and_migration_posts_have_zero_retries():
    assert TTS_JS.count("retries:0") >= 4
    assert "retryTimeouts:false" in TTS_JS


def test_settings_generation_rejects_stale_mirrors():
    body = """
const generation=HermesTTS.captureSettingsGeneration();
HermesTTS.invalidateSettings();
process.stdout.write(JSON.stringify({
  stale:HermesTTS.shouldMirrorSetting('tts_engine',generation),
  current:HermesTTS.shouldMirrorSetting('tts_engine',HermesTTS.captureSettingsGeneration())
}));
"""
    assert _node(body, "async()=>({})") == {"stale": False, "current": True}


def test_provider_completion_after_profile_switch_is_aborted():
    capability = json.dumps(_capability())
    api_impl = "(path,opts)=>new Promise(resolve=>{globalThis.resolveProvider=resolve})"
    body = f"""
const pending=HermesTTS.selectProvider('Microsoft Edge TTS',{capability});
S.profile='other';
HermesTTS.invalidateCapability();
resolveProvider({{ok:true}});
pending.then(()=>process.exit(2)).catch(error=>{{
  process.stdout.write(JSON.stringify({{name:error.name,profile:S.profile}}));
}});
"""
    assert _node(body, api_impl) == {"name": "AbortError", "profile": "other"}


def test_voice_mode_deactivation_calls_controller_stop_directly():
    assert "window.HermesTTS.stop('voice-mode-deactivated')" in BOOT_JS
    assert "expected_speech_settings_revision=_settingsSpeechRevision" in PANELS_JS
    assert "await _setupTtsSettings(authoritative)" in PANELS_JS
