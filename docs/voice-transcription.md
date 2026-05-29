# Voice transcription module

Agent-facing reference for the Hermes WebUI **Voice** module: a left-sidebar
panel that records from the device microphone and transcribes speech through
user-configurable, OpenAI-compatible speech-to-text endpoints (for example a
local server hosting IBM Granite Speech). Read this before changing any voice
transcription code, config, or UI.

> Scope note: this is the WebUI-owned Voice module. It is **separate** from the
> composer dictation button (`#btnMic` → `POST /api/transcribe` →
> `tools.transcription_tools` in the Hermes agent venv). Do not conflate the
> two; they have different backends, endpoints, and configuration.

---

## 1. What it does

- Adds a **Voice** tab to the sidebar rail + mobile nav (mic icon, `data-panel="voice"`).
- Records audio in the browser with `getUserMedia` + `MediaRecorder` (webm/opus,
  ogg fallback) — using the microphone of the device that opened the WebUI.
- Uploads the clip to the WebUI server, which forwards it to the **selected**
  transcription endpoint and returns the transcript.
- Lets the user **switch models on the fly** from a dropdown in the panel.
- Lets the user **manage endpoints** in **Settings → Voice** (add/edit/remove
  label, base URL, model name, API key, timeout; pick a default).
- Keeps a persisted **transcript history** with copy, insert-to-chat, per-item
  delete, and clear-all.

The module is "configured" when at least one model exists (a user-defined model
or the environment-fallback model). When unconfigured, the panel shows a notice
linking to Settings → Voice and the record button is disabled.

---

## 2. File inventory

| File | Responsibility |
|---|---|
| `api/voice_transcription.py` | Core: multi-model config persistence + masking, model resolution, env fallback, `transcribe()`, transcript-history CRUD. **No heavy deps** — uses stdlib `urllib`. |
| `api/upload.py` → `handle_voice_transcribe()` | Multipart handler for `POST /api/voice/transcribe`. Reads optional `model_id`, calls `transcribe()`, persists the result to history. |
| `api/routes.py` | Route registration (see §4). |
| `static/index.html` | `#panelVoice` markup (model selector, record button, transcript box, history) + `#settingsPaneVoice` editor + nav buttons. |
| `static/panels.js` | Panel logic (`loadVoicePanel`, recording, `voiceSetActiveModel`) and settings-editor logic (`loadVoiceSettings`, `saveVoiceModels`, add/remove rows). |
| `static/style.css` | `.voice-*` styles. |
| `static/i18n.js` | English strings under the `voice_*` / `settings_tab_voice` keys (other locales fall back to English). |
| `tests/test_voice_granite_module.py` | Endpoint + config + history tests. |
| State: `~/.hermes/webui/voice_config.json` | Persisted model list + active id. |
| State: `~/.hermes/webui/voice_transcripts.json` | Transcript history (newest-first, capped at 100). |

---

## 3. Configuration

### 3.1 Persisted config (`voice_config.json`)

```json
{
  "active_id": "granite-2b",
  "models": [
    {
      "id": "granite-2b",
      "label": "Granite Speech 2B (local)",
      "base_url": "http://127.0.0.1:8000/v1",
      "model": "granite-speech-3.3-2b",
      "api_key": "",
      "timeout": 120
    }
  ]
}
```

Field rules (enforced in `_validate_incoming_model`):

- `id` — required, slug `^[a-z0-9][a-z0-9_-]{0,48}$`, unique. `env-granite` is reserved.
- `base_url` — required, must start with `http://` or `https://`. The
  `/audio/transcriptions` path is appended by the server.
- `model` — required; the model name sent in the multipart `model` field.
- `label` — display name (≤ 80 chars); defaults to `id`.
- `api_key` — optional bearer token. Sent as `Authorization: Bearer <key>`.
- `timeout` — request timeout in seconds, clamped to `[1, 600]`, default 120.
- Max 25 models.

`active_id` is always clamped to a present model id on read and write.

### 3.2 Environment fallback (zero-config / back-compat)

If `GRANITE_STT_BASE_URL` is set and no user model with id `env-granite` exists,
a **read-only** model is synthesized and appended:

| Env var | Meaning | Default |
|---|---|---|
| `GRANITE_STT_BASE_URL` | OpenAI-compatible base URL (enables the fallback) | — |
| `GRANITE_STT_MODEL` | Model name | `granite-speech-3.3-8b` |
| `GRANITE_STT_API_KEY` | Optional bearer token | — |
| `GRANITE_STT_TIMEOUT` | Timeout seconds | 120 |

The env model appears in selectors as “Granite (env)”, is **not** editable in
the Settings UI, and is never written to `voice_config.json`. It can be chosen
as the active/default model.

### 3.3 Secret handling

- `GET /api/voice/config` never returns raw API keys — each model exposes
  `has_api_key: bool` instead.
- On `POST /api/voice/config`, a model that **omits** `api_key` preserves the
  stored value (the UI omits it when the user did not edit the field); sending
  an explicit empty string clears it. The UI shows a `•••• (unchanged)`
  placeholder for models that already have a key.

---

## 4. HTTP API

| Method & path | Body / fields | Returns |
|---|---|---|
| `GET /api/voice/config` | — | `{ models: [{id,label,base_url,model,timeout,has_api_key,source}], active_id, configured }` (keys masked) |
| `POST /api/voice/config` | `{ models: [...], active_id }` | Saved public config. `400` with `{error}` on validation failure. |
| `POST /api/voice/active` | `{ id }` | Public config with the new `active_id`. `400` if id unknown. |
| `POST /api/voice/transcribe` | multipart: `file` (audio), optional `model_id` | `{ ok, transcript, entry, model_id }`. `503` if unconfigured/unreachable, `400` on other transcription errors. |
| `GET /api/voice/history` | — | `{ transcripts: [{id,text,created_at,model_id?}], configured }` |
| `POST /api/voice/history/delete` | `{ id }` or `{ all: true }` | `{ ok, transcripts }` |

`/api/voice/transcribe` is dispatched **before** `read_body()` in `routes.py`
(it consumes the raw multipart stream), alongside `/api/upload` and
`/api/transcribe`. Keep it there if you reorder routes.

---

## 5. Running a transcription backend

The WebUI only speaks the OpenAI `/audio/transcriptions` contract; you supply
the server. Options:

- **vLLM** serving `ibm-granite/granite-speech-3.3-8b` (GPU).
- **Native Python (transformers)** — load granite-speech and expose
  `/v1/audio/transcriptions`. The 2B variant (`granite-speech-3.3-2b`) runs on
  Apple Silicon (MPS) / CPU. Requires `transformers`, `torch`, `peft`,
  `soundfile`/`librosa`, and a matching `torchaudio`; decode browser webm/opus
  to 16 kHz mono with `ffmpeg` before inference.
- **Any OpenAI-compatible STT server** (e.g. a Whisper server) — point a model
  entry at it; the module is model-agnostic.

> Note: **llama.cpp does not run IBM granite-speech ASR** (whisper.cpp covers
> Whisper GGUF only). Use vLLM or transformers for Granite.

Minimum response contract: JSON `{"text": "..."}` (also accepts `{"transcript": ...}`
or a bare string body). Empty text is treated as an error.

---

## 6. Frontend flow

1. `switchPanel('voice')` → `loadVoicePanel()` → `_voiceLoadModels()` fetches
   `/api/voice/config`, fills `#voiceModelSelect`, sets `_voiceActiveModelId`,
   toggles the unconfigured notice.
2. Record button → `voiceToggleRecord()` → `MediaRecorder`. On stop,
   `_voiceTranscribe(blob)` POSTs the audio + `model_id` to
   `/api/voice/transcribe`, shows the transcript, refreshes history.
3. Changing the panel dropdown → `voiceSetActiveModel(id)` → `POST /api/voice/active`
   (sticky default) and updates `_voiceActiveModelId` for subsequent clips.
4. Leaving the panel aborts any in-progress recording (`_voiceStopRecording(true)`
   in `switchPanel`), so the mic does not stay live.
5. Settings → Voice → `loadVoiceSettings()` renders editable rows; Save →
   `saveVoiceModels()` POSTs to `/api/voice/config` (omitting unchanged keys).

Browsers only allow `getUserMedia` on **secure origins** — `https://` or
`localhost`/`127.0.0.1`. Over an SSH tunnel to `127.0.0.1` this is satisfied.

---

## 7. Testing

- `tests/test_voice_granite_module.py` covers: missing file (400), unconfigured
  (503), success + history persistence + masking, config CRUD/validation,
  `model_id` selection, active switching, and the env fallback. Tests monkeypatch
  `urllib.request.urlopen` and isolate `VOICE_CONFIG_FILE`/`VOICE_TRANSCRIPTS_FILE`
  to `tmp_path`. They never hit a real model server or the network.
- Run: `python3 -m pytest tests/test_voice_granite_module.py -q`.

---

## 8. Troubleshooting

| Symptom | Likely cause |
|---|---|
| Panel says “No transcription models configured” | No user model and `GRANITE_STT_BASE_URL` unset. Add one in Settings → Voice. |
| `503 Could not reach transcription endpoint` | The model server is down or the base URL is wrong. |
| `400 ... endpoint error 4xx/5xx` | The server rejected the request (bad model name, unsupported audio, auth). |
| Mic button does nothing / disabled | Insecure origin (not https/localhost), denied mic permission, or no configured model. |
| Always returns the same fixed sentence | You are pointed at a stub, not a real model server. |
| `Empty transcription response` | Server returned no `text` — often silent/too-short audio or an unsupported codec on the server side. |

---

## 9. Contract notes for agents

- Keep the WebUI dependency-free for this feature: forward audio with stdlib
  only. Heavy ML deps belong in the **separate** model server, not this repo.
- Never log or return raw `api_key` values. Preserve the mask-on-read /
  omit-to-preserve write contract in §3.3.
- The env-fallback model is read-only and must never be persisted to
  `voice_config.json`.
- If you add fields to a model entry, update: `_validate_incoming_model`,
  `_public_model`, the Settings editor (`_renderVoiceModelRows` /
  `_syncVoiceModelsFromDom`), this doc, and the tests.
- Update `CHANGELOG.md` for user-visible behavior changes.
