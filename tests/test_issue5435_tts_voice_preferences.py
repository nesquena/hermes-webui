"""Regression checks for #5435 TTS and voice preference persistence."""

import json
import importlib
import pathlib
import urllib.error
import urllib.request

from tests._pytest_port import BASE

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")

SPEECH_DEFAULTS = {
    "tts_enabled": False,
    "tts_auto_read": False,
    "tts_engine": "browser",
    "tts_voice": "",
    "tts_rate": 1.0,
    "tts_pitch": 1.0,
    "voice_mode_button": False,
    "voice_continuous": False,
    "voice_silence_ms": 1800,
    "raw_audio_mode": False,
}
PERSISTED_SPEECH_KEYS_FIELD = "persisted_speech_keys"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.loads(response.read()), response.status


def post(path, body=None):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read()), response.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def _extract_balanced_block(src, marker):
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    end = None
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    assert end is not None, f"Unbalanced block for {marker!r}"
    return src[start:end]


def _settings_file_snapshot():
    cfg = importlib.import_module("api.config")
    path = cfg.SETTINGS_FILE
    original = path.read_text(encoding="utf-8") if path.exists() else None
    return path, original


def _restore_settings_file(path, original):
    if original is None:
        if path.exists():
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original, encoding="utf-8")


def test_settings_api_exposes_tts_voice_and_raw_audio_defaults():
    data, status = get("/api/settings")

    assert status == 200
    assert data[PERSISTED_SPEECH_KEYS_FIELD] == []
    for key, value in SPEECH_DEFAULTS.items():
        assert data[key] == value


def test_settings_api_round_trips_speech_preferences():
    path, original = _settings_file_snapshot()
    payload = {
        "tts_enabled": True,
        "tts_auto_read": True,
        "tts_engine": "voicevox_local",
        "tts_voice": "en-US-AriaNeural",
        "tts_rate": "1.4",
        "tts_pitch": "0",
        "voice_mode_button": True,
        "voice_continuous": True,
        "voice_silence_ms": "2400",
        "raw_audio_mode": True,
    }
    try:
        saved, status = post("/api/settings", payload)
        reloaded, reload_status = get("/api/settings")

        assert status == 200
        assert reload_status == 200
        assert saved["tts_enabled"] is True
        assert saved["tts_auto_read"] is True
        assert saved["tts_engine"] == "voicevox_local"
        assert saved["tts_voice"] == "en-US-AriaNeural"
        assert saved["tts_rate"] == 1.4
        assert saved["tts_pitch"] == 0.0
        assert saved["voice_mode_button"] is True
        assert saved["voice_continuous"] is True
        assert saved["voice_silence_ms"] == 2400
        assert saved["raw_audio_mode"] is True
        assert saved[PERSISTED_SPEECH_KEYS_FIELD] == sorted(payload)
        for key in payload:
            expected = saved[key]
            assert reloaded[key] == expected
        assert reloaded[PERSISTED_SPEECH_KEYS_FIELD] == sorted(payload)
    finally:
        _restore_settings_file(path, original)


def test_settings_api_reports_only_raw_persisted_speech_keys():
    path, original = _settings_file_snapshot()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "show_tps": True,
                    "tts_pitch": 0.0,
                    "voice_mode_button": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        data, status = get("/api/settings")

        assert status == 200
        assert data[PERSISTED_SPEECH_KEYS_FIELD] == [
            "tts_pitch",
            "voice_mode_button",
        ]
        assert data["tts_pitch"] == 0.0
        assert data["voice_mode_button"] is False
        assert data["tts_enabled"] is False
    finally:
        _restore_settings_file(path, original)


def test_unrelated_settings_save_does_not_materialize_absent_speech_defaults():
    path, original = _settings_file_snapshot()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"show_tps": False}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        saved, status = post("/api/settings", {"show_tps": True})

        assert status == 200
        assert saved["show_tps"] is True
        assert saved[PERSISTED_SPEECH_KEYS_FIELD] == []

        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["show_tps"] is True
        for key in SPEECH_DEFAULTS:
            assert key not in raw
    finally:
        _restore_settings_file(path, original)


def test_startup_workspace_repair_write_drops_merged_speech_defaults():
    path, original = _settings_file_snapshot()
    cfg = importlib.import_module("api.config")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "show_tps": False,
                    "tts_pitch": 0.0,
                    "default_workspace": "C:/stale/workspace",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        merged = cfg.load_settings()
        merged["default_workspace"] = "C:/fixed/workspace"
        persisted = cfg._settings_payload_for_write(
            merged, cfg._extract_persisted_speech_keys(cfg._read_raw_settings_file())
        )

        assert persisted["show_tps"] is False
        assert persisted["tts_pitch"] == 0.0
        assert persisted["default_workspace"] == "C:/fixed/workspace"
        assert PERSISTED_SPEECH_KEYS_FIELD not in persisted
        for key in SPEECH_DEFAULTS:
            if key != "tts_pitch":
                assert key not in persisted
    finally:
        _restore_settings_file(path, original)


def test_invalid_speech_settings_preserve_previous_values_and_unrelated_settings():
    path, original = _settings_file_snapshot()
    data, status = get("/api/settings")
    original_show_tps = bool(data.get("show_tps"))
    valid = {
        "tts_engine": "edge",
        "tts_voice": "zh-CN-XiaoxiaoNeural",
        "tts_rate": 1.2,
        "tts_pitch": 1.1,
        "voice_silence_ms": 2200,
    }
    try:
        saved, status = post("/api/settings", valid)
        assert status == 200
        assert saved["tts_engine"] == "edge"

        invalid, status = post(
            "/api/settings",
            {
                "tts_engine": "",
                "tts_voice": "x" * 201,
                "tts_rate": "nan",
                "tts_pitch": 3,
                "voice_silence_ms": 199,
                "show_tps": not original_show_tps,
            },
        )

        assert status == 200
        for key, value in valid.items():
            assert invalid[key] == value
        assert invalid["show_tps"] is (not original_show_tps)
    finally:
        _restore_settings_file(path, original)


def test_backend_schema_contains_typed_speech_validation():
    for key in SPEECH_DEFAULTS:
        assert f'"{key}"' in CONFIG_PY
    assert '"voice_silence_ms": (200, 60000)' in CONFIG_PY
    assert '"tts_rate": (0.5, 2.0)' in CONFIG_PY
    assert '"tts_pitch": (0.0, 2.0)' in CONFIG_PY
    assert "_SETTINGS_TTS_ENGINE_RE" in CONFIG_PY
    assert 'k == "tts_voice"' in CONFIG_PY


def test_boot_mirrors_server_settings_before_tts_apply_and_preserves_failure_fallback():
    mirror_idx = BOOT_JS.index("function _mirrorSpeechSettingsFromServer")
    success_call_idx = BOOT_JS.index("_mirrorSpeechSettingsFromServer(s);", mirror_idx)
    apply_idx = BOOT_JS.index("_applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true')", success_call_idx)
    catch_idx = BOOT_JS.index("}catch(e){", success_call_idx)
    failure_apply_idx = BOOT_JS.index("_applyTtsEnabled(localStorage.getItem('hermes-tts-enabled')==='true')", catch_idx)

    assert success_call_idx < apply_idx
    assert catch_idx < failure_apply_idx
    assert "const defaults={" in BOOT_JS
    assert "Array.isArray(s.persisted_speech_keys) ? s.persisted_speech_keys : []" in BOOT_JS
    assert "if(!hasServerValue(settingKey)&&cached!==null)" in BOOT_JS
    for storage_key in [
        "hermes-tts-enabled",
        "hermes-tts-auto-read",
        "hermes-tts-engine",
        "hermes-tts-voice",
        "hermes-tts-rate",
        "hermes-tts-pitch",
        "hermes-voice-mode-button",
        "hermes-voice-continuous",
        "hermes-voice-silence-ms",
        "hermes-raw-audio-mode",
    ]:
        assert storage_key in BOOT_JS
    assert "window._applyRawAudioModePreference" in BOOT_JS


def test_persisted_speech_key_metadata_controls_boot_and_panel_precedence():
    mirror_fn = _extract_balanced_block(BOOT_JS, "function _mirrorSpeechSettingsFromServer")
    speech_helpers_start = PANELS_JS.index("const _SETTINGS_SPEECH_STORAGE_KEYS=")
    speech_helpers_end = PANELS_JS.index("function _preferencesPayloadFromUi", speech_helpers_start)
    speech_helpers_block = PANELS_JS[speech_helpers_start:speech_helpers_end].strip()
    speech_setting_start = PANELS_JS.index("const persistedSpeechKeys = new Set(")
    speech_setting_end = PANELS_JS.index("const _speechBool=function", speech_setting_start)
    speech_setting_block = PANELS_JS[speech_setting_start:speech_setting_end].strip()
    script = f"""
const assert = require('assert');
const localStorage = {{
  store: new Map([
    ['hermes-tts-enabled', 'true'],
    ['hermes-tts-pitch', '0.8'],
  ]),
  getItem(key) {{
    return this.store.has(key) ? this.store.get(key) : null;
  }},
  setItem(key, value) {{
    this.store.set(key, String(value));
  }},
}};
const window = {{}};
{speech_helpers_block}
{mirror_fn}
_mirrorSpeechSettingsFromServer({{tts_enabled: false, tts_pitch: 1, persisted_speech_keys: []}});
assert.strictEqual(localStorage.getItem('hermes-tts-enabled'), 'true');
assert.strictEqual(localStorage.getItem('hermes-tts-pitch'), '0.8');
{{
  let settings = {{tts_enabled: false, tts_pitch: 1, persisted_speech_keys: []}};
  {speech_setting_block}
  assert.strictEqual(_speechSetting('tts_enabled', 'hermes-tts-enabled', false, 'bool'), 'true');
  assert.strictEqual(_speechSetting('tts_pitch', 'hermes-tts-pitch', 1), '0.8');
}}
_mirrorSpeechSettingsFromServer({{tts_enabled: false, tts_pitch: 1, persisted_speech_keys: ['tts_enabled', 'tts_pitch']}});
assert.strictEqual(localStorage.getItem('hermes-tts-enabled'), 'false');
assert.strictEqual(localStorage.getItem('hermes-tts-pitch'), '1');
{{
  let settings = {{tts_enabled: false, tts_pitch: 1, persisted_speech_keys: ['tts_enabled', 'tts_pitch']}};
  {speech_setting_block}
  assert.strictEqual(_speechSetting('tts_enabled', 'hermes-tts-enabled', false, 'bool'), false);
  assert.strictEqual(_speechSetting('tts_pitch', 'hermes-tts-pitch', 1), 1);
}}
"""

    import subprocess

    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_settings_panel_speech_payload_is_sparse_by_ownership():
    speech_helpers_start = PANELS_JS.index("const _SETTINGS_SPEECH_STORAGE_KEYS=")
    speech_helpers_end = PANELS_JS.index("function _setPreferencesAutosaveStatus", speech_helpers_start)
    speech_helpers_block = PANELS_JS[speech_helpers_start:speech_helpers_end].strip()
    script = f"""
const assert = require('assert');
const localStorage = {{
  store: new Map(),
  getItem(key) {{
    return this.store.has(key) ? this.store.get(key) : null;
  }},
  setItem(key, value) {{
    this.store.set(key, String(value));
  }},
  clear() {{
    this.store.clear();
  }},
}};
const controls = {{
  settingsTtsEnabled: {{checked: false}},
  settingsTtsAutoRead: {{checked: false}},
  settingsTtsEngine: {{value: 'browser'}},
  settingsTtsVoice: {{value: ''}},
  settingsTtsRate: {{value: '1'}},
  settingsTtsPitch: {{value: '1'}},
  settingsVoiceModeEnabled: {{checked: false}},
  settingsRawAudio: {{checked: false}},
}};
function $(id) {{ return controls[id] || null; }}
{speech_helpers_block}
_captureSpeechPreferenceOwnership({{persisted_speech_keys: []}});
assert.deepStrictEqual(_speechPreferencesPayloadFromUi(), {{}});

_captureSpeechPreferenceOwnership({{persisted_speech_keys: ['tts_enabled']}});
assert.deepStrictEqual(_speechPreferencesPayloadFromUi(), {{tts_enabled: false}});

localStorage.clear();
localStorage.setItem('hermes-tts-pitch', '0.8');
controls.settingsTtsPitch.value = '0.8';
_captureSpeechPreferenceOwnership({{persisted_speech_keys: []}});
assert.deepStrictEqual(_speechPreferencesPayloadFromUi(), {{tts_pitch: 0.8}});

localStorage.clear();
controls.settingsTtsRate.value = '1.4';
_captureSpeechPreferenceOwnership({{persisted_speech_keys: []}});
_markSpeechPreferenceChanged('tts_rate');
assert.deepStrictEqual(_speechPreferencesPayloadFromUi(), {{tts_rate: 1.4}});
"""

    import subprocess

    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


def test_settings_panel_persists_speech_fields_and_keeps_immediate_cache_writes():
    payload_idx = PANELS_JS.index("function _preferencesPayloadFromUi")
    payload_end = PANELS_JS.index("function _setPreferencesAutosaveStatus", payload_idx)
    payload_block = PANELS_JS[payload_idx:payload_end]
    panel_idx = PANELS_JS.index("TTS settings use /api/settings as the durable source")
    panel_end = PANELS_JS.index("const notifCb=$('settingsNotificationsEnabled')", panel_idx)
    panel_block = PANELS_JS[panel_idx:panel_end]

    for field in SPEECH_DEFAULTS:
        assert f"_setOwnedSpeechPayload(payload,'{field}'" in payload_block
    for storage_key in [
        "hermes-tts-enabled",
        "hermes-tts-auto-read",
        "hermes-tts-engine",
        "hermes-tts-voice",
        "hermes-tts-rate",
        "hermes-tts-pitch",
        "hermes-voice-mode-button",
        "hermes-voice-continuous",
        "hermes-voice-silence-ms",
        "hermes-raw-audio-mode",
    ]:
        assert storage_key in panel_block or storage_key in payload_block
    assert "_speechSetting('tts_engine','hermes-tts-engine','browser')" in panel_block
    assert "function _speechPreferencesPayloadFromUi()" in PANELS_JS
    assert "savedRate||'1'" not in panel_block
    assert "savedPitch||'1'" not in panel_block
    assert "ttsRateSlider.value=(savedRate===null||savedRate===undefined)?'1':String(savedRate)" in panel_block
    assert "ttsPitchSlider.value=(savedPitch===null||savedPitch===undefined)?'1':String(savedPitch)" in panel_block
    assert "if(settings&&persistedSpeechKeys.has(key)) return settings[key];" in PANELS_JS
    assert "function _captureSpeechPreferenceOwnership(settings)" in PANELS_JS
    assert "function _speechPreferenceIsOwned(settingKey)" in PANELS_JS
    assert "_markSpeechPreferenceChanged('tts_rate')" in panel_block
    assert "_syncSpeechPreferenceCache('tts_rate',ttsRateSlider.value)" in panel_block
    assert "_syncSpeechPreferenceCache('tts_pitch',ttsPitchSlider.value)" in panel_block
    assert "Object.assign(payload,_speechPreferencesPayloadFromUi());" in payload_block
    assert "Object.assign(body,_speechPreferencesPayloadFromUi());" in PANELS_JS
    assert "_schedulePreferencesAutosave()" in panel_block
    assert "_applyVoiceModePref" in panel_block
    assert "_populateTtsVoices" in panel_block
