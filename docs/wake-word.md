# Wake word — hands-free dictation start

Opt-in feature: say "Hey Jarvis" and dictation starts, hands-free. This
document covers usage, tuning, privacy, and the evaluation behind the
shipped defaults. Feature issue: kanban t_c70e5bf1; implementation: PR #8339.

## Enable / disable

- **Enable**: Settings → Voice → **Wake word** checkbox. First enable lazily
  fetches `static/wakeword.js` and the vendored ONNX runtime + models (same
  origin, no CDN), then opens the microphone.
- **Disable**: same checkbox. The detector stops the microphone tracks,
  closes the AudioContext, and releases all three ONNX sessions. A disable
  during startup cancels the pending start (nothing keeps listening).
- The preference persists in `localStorage` (`hermes-wake-word-enabled`).
  On reload with the setting on, detection re-arms after your first
  interaction with the page (browsers require a user activation for
  microphone access in most setups; where permission was already granted it
  resumes immediately).

## How it works

```
mic → AudioWorklet tap (16 kHz mono) → melspectrogram.onnx
     → embedding_model.onnx → hey_jarvis_v0.1.onnx → score
     → 3 consecutive frames ≥ threshold + 2 s refractory → trigger
     → start dictation (same path as tapping the mic button)
```

- Everything runs **in the page** via onnxruntime-web WebAssembly. No audio
  or scores leave the browser; the server is not involved in detection.
- The wake word is a **start-only** signal: it never stops dictation, and it
  is suppressed while dictation is active.
- While enabled, expect ~5% of one CPU core (single-threaded wasm) and the
  usual microphone-in-use indicator from your OS/browser.

## Tuning the threshold

The score threshold is stored in `localStorage`
(`hermes-wake-word-score`, 0–100 integer → raw 0.20–0.95; default 55 → 0.55).
Higher = fewer false triggers, lower = catches quieter/faster speech.

Evaluation (see below) measured **0 false positives per hour at every
threshold 0.3–0.8**, so the default 0.55 sits in a wide safe band. Raise it
only if you see spurious triggers in your environment (e.g. TV running all
day); lower it if detection feels sluggish in noise.

## Privacy & lifecycle

- Off by default; nothing is fetched and no mic is opened until enabled.
- Detection is fully local; the only network involved is dictation itself
  (Web Speech API or the existing `/api/transcribe` path), which behaves
  exactly as when you tap the mic button.
- Disabling (or closing the tab) releases the microphone and all model
  memory; repeated toggles do not accumulate sessions.

## Evaluation summary (2026-10-17, corpus v2)

Engine choice: **openWakeWord** (MIT, pure-ONNX chain, browser-runnable) over
Porcupine (proprietary, requires access-key activation) — details in the eval
report artifact of the kanban card.

- Corpus: 1 h of real other-content speech negatives (two Piper TTS voices,
  news-style sentences + babble/music/TV-mix/fan/keyboard classes) and 72
  positives ("hey jarvis" at SNR −3…20 dB, babble & fan, 3 positions).
- Trigger rule (same as shipped): ≥3 consecutive 80 ms frames over
  threshold + 2 s refractory.
- Results (hey_jarvis model): 71/72 detections, **0 FP/hour at every
  threshold 0.3–0.8**; control models (alexa, hey_mycroft, hey_rhasspy)
  0/72 — corpus honesty check.
- CPU: RTF 0.022 server-side / ≈0.05 in-browser (wasm, 1 thread).
- The one miss is a −3 dB SNR clip in fan noise — below usable SNR for the
  phrase.

## Testing

- `tests/test_wakeword_static.py` — static gates (vendored assets present,
  CSP wasm allowance, lazy-load wiring, no auto-start on load).
- `tests/browser_wakeword_e2e.py` — real-browser E2E. Phase A drives the
  real mic→AudioWorklet path with Chromium's
  `--use-file-for-fake-audio-capture` playing the vendored positive fixture
  and asserts dictation starts hands-free; Phase B pushes 60 s of negative
  real speech through the full model pipeline and asserts zero triggers.
  Fixtures ship in `tests/fixtures/wakeword/` (hermetic).

## Roadmap

- Custom "hey hermes" model trained via openWakeWord's training pipeline
  (child card of the feature card) — drop-in replacement model file plus
  updated locale strings.
