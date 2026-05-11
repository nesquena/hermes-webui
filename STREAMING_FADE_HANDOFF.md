# Streaming Fade Text Effect Handoff

## Summary

This branch adds an opt-in **Fade text effect** preference for HermesWebUI streaming assistant responses.

When enabled, newly streamed assistant words fade in instead of appearing via the default incremental markdown renderer. The goal is a ChatGPT/OpenWebUI-like animated streaming feel while still catching up to high-throughput model output.

The feature is **off by default** for performance.

## User-facing behavior

- New setting: **Settings → Preferences → Fade text effect**
- Runtime global: `window._fadeTextEffect`
- Default: `false`
- When enabled:
  - assistant stream uses a playout buffer rather than immediately rendering the full incoming chunk
  - visible text advances at adaptive speed based on live incoming word velocity, backlog, and stream age
  - new words are wrapped in spans and animated with opacity-only fade
  - high-speed output uses rolling phrase-sized waves instead of giant block pops
  - Hermes' bright live cursor is hidden during fade mode

## Main files changed

### `static/messages.js`

Core streaming implementation inside `attachLiveStream(...)`.

Added local fade state:

- `_streamFadeVisibleText`
- `_streamFadeWordCarry`
- `_streamFadeWordBornAt`
- `_streamFadeArrivalWps`
- `_streamFadeLastRevealCount`
- `_streamFadeLatestAnimationEndAt`

Key helpers:

- `_resetStreamFadeState()`
- `_cancelPendingStreamRender()`
- `_shouldUseStreamFade()`
- `_streamFadeWordCountOf(text)`
- `_streamFadeNextText(targetText)`
- `_renderStreamingFadeMarkdown(displayText)`
- `_wrapStreamingFadeWords(root)`
- `_drainStreamFadeBeforeDone(onDone)`

Important behavior:

- Fade mode renders at ~60fps (`16ms`) while default streaming remains ~15fps (`66ms`).
- Default SMD streaming path remains intact when fade mode is off.
- On `done`, fade mode drains remaining buffered text and waits for the final stagger/fade window before the final `renderMessages()` replacement.
- Prefix resets now call `_resetStreamFadeState()` so stale birth timestamps do not leak across markdown/tool-call rewrites.

### `static/style.css`

Adds opacity-only streaming fade CSS:

```css
.stream-fade-word.is-new {
  animation: stream-fade-word-in var(--stream-fade-ms,140ms) ease-out both;
}
@keyframes stream-fade-word-in { from { opacity:0; } to { opacity:1; } }
```

Also hides the live cursor during fade mode:

```css
[data-live-assistant="1"]:last-child .msg-body.stream-fade-active > :last-child::after,
[data-live-assistant="1"]:last-child .msg-body.stream-fade-active:not(:has(> *))::after {
  display:none;
  content:none;
}
```

### Settings plumbing

- `api/config.py`
  - adds `fade_text_effect` default and bool key
- `static/boot.js`
  - initializes `window._fadeTextEffect`
- `static/index.html`
  - adds Preferences checkbox
- `static/panels.js`
  - loads, autosaves, and saves the setting
- `static/i18n.js`
  - adds locale strings for all supported locales

### Tests

New file:

- `tests/test_smooth_text_fade.py`

Coverage includes:

- setting persistence/config plumbing
- Preferences UI plumbing
- i18n key presence
- fade helper presence
- executable Node regressions that invoke `_streamFadeNextText(...)`
- speed-ramp behavior
- high-speed rolling-wave behavior
- done-drain behavior
- CSS expectations
- cursor hiding

## Tunable constants

Defined near the top of `attachLiveStream(...)` in `static/messages.js`:

```js
const _STREAM_FADE_MS=140;
const _STREAM_FADE_WAVE_MS=320;
const _STREAM_FADE_MAX_STAGGER_MS=520;
```

Meaning:

- `_STREAM_FADE_MS`: base fade duration for normal streaming
- `_STREAM_FADE_WAVE_MS`: longer duration for high-speed multi-word waves
- `_STREAM_FADE_MAX_STAGGER_MS`: max stagger spread across newly inserted words

Adaptive playout speed currently uses:

```js
const baseWps = 30 + Math.min(streamAgeSeconds * 4, 35); // 30 → 65 wps
const arrivalWps = _streamFadeArrivalWps ? Math.min(_streamFadeArrivalWps * 2.4 + 20, 320) : 0;
const backlogWps = backlogWords > 0 ? Math.min(30 + backlogWords * 8, 420) : 0;
const wordsPerSecond = Math.min(420, Math.max(baseWps, arrivalWps, backlogWps));
```

Rolling burst floor:

```js
const burstFloor = backlogWords >= 120 ? 24
  : backlogWords >= 60 ? 18
  : backlogWords >= 30 ? 12
  : wordsPerSecond >= 300 ? 8
  : wordsPerSecond >= 220 ? 6
  : 0;
```

High-speed waves then use:

```js
const fadeMs = revealedThisFrame >= 8 ? _STREAM_FADE_WAVE_MS
  : revealedThisFrame >= 4 ? 240
  : _STREAM_FADE_MS;

const waveStepMs = revealedThisFrame >= 18 ? 18
  : revealedThisFrame >= 8 ? 22
  : revealedThisFrame >= 4 ? 16
  : 10;
```

## Design decisions and why

### Why not use only OpenWebUI's renderer?

A wholesale renderer transplant was avoided. Hermes keeps its existing streaming markdown path as default, and fade mode is a selective cosmetic layer.

### Why a playout buffer?

Hermes receives backend stream chunks that can arrive faster or more bursty than desired visually. Rendering each chunk immediately can pop large text blocks into the DOM. The playout buffer separates:

- text received from backend (`assistantText`)
- text currently visible (`_streamFadeVisibleText`)

### Why adaptive speed?

A fixed reveal rate felt robotic and lagged behind faster models. Earlier attempts using session-wide average arrival rate failed when the model spent time reasoning before writing because the denominator inflated and the ramp never triggered.

Current approach tracks **live target-word arrival velocity** using deltas:

```js
const instantArrivalWps = (targetWords - _streamFadeLastTargetWords) * 1000 / arrivalElapsedMs;
_streamFadeArrivalWps = _streamFadeArrivalWps
  ? (_streamFadeArrivalWps * 0.65 + instantArrivalWps * 0.35)
  : instantArrivalWps;
```

Then playout deliberately exceeds arrival velocity so it catches up.

### Why rolling waves?

At very high throughput, revealing too many words in one frame felt chunky and made the fade almost disappear. The current implementation reduces one-frame burst size and stretches/staggers high-speed waves across several hundred milliseconds.

This makes fast output feel more like animated text sweeping in rather than paragraph blocks appearing.

## Performance notes

Fade mode is more expensive than the default streaming path because it re-renders markdown and wraps visible text nodes during active streaming.

Mitigations:

- feature is opt-in and off by default
- default streaming-markdown path remains unchanged when disabled
- fade render cadence is capped at ~60fps
- skip wrapping inside `pre`, `code`, `script`, `style`, `textarea`, `svg`, and `math`
- animation is opacity-only, compositor-friendly

Expected impact:

- fine on modern desktop/Apple Silicon hardware
- higher CPU/battery use during long/high-speed responses
- users can disable it instantly from Preferences

## Verification performed

Commands run successfully:

```bash
cd /Users/agent/HermesWebUI
PY=/Users/agent/.hermes/hermes-agent/venv/bin/python
$PY -m pytest tests/test_smooth_text_fade.py tests/test_1003_preferences_autosave.py tests/test_streaming_markdown.py tests/test_chinese_locale.py tests/test_japanese_locale.py tests/test_korean_locale.py tests/test_russian_locale.py tests/test_spanish_locale.py -q
node --check static/messages.js static/panels.js static/boot.js static/i18n.js
$PY -m py_compile api/config.py
git diff --check
```

Latest result before writing this handoff:

```text
99 passed
```

Also performed:

- dead/debug scan over diff for `TODO`, `FIXME`, `console.log`, `debugger`, stale `100ms`, stale `220ms`, stale `48` burst constants
- review cleanup: blocked late `token` / `reasoning` / `interim_assistant` mutations during fade done-drain, moved fade wave calculations out of the per-word hot path, and made manual Settings save refresh `window._fadeTextEffect`
- HermesWebUI restart via launchctl
- live asset verification via `curl http://127.0.0.1:8787/static/messages.js`
- real chat/SSE smoke test: temp session, prompt `Reply with exactly: OK`, received `OK`, got `done`, deleted temp session

## Current service state when last verified

- HermesWebUI runs on port `8787`
- Restarted during validation
- Health endpoint returned OK

Useful checks:

```bash
curl -fsS http://127.0.0.1:8787/health
curl -fsS http://127.0.0.1:8787/static/messages.js | grep -E "_STREAM_FADE_WAVE_MS=320|_STREAM_FADE_MAX_STAGGER_MS=520|burstFloor=backlogWords>=120\?24"
curl -fsS http://127.0.0.1:8787/static/style.css | grep -E "var\(--stream-fade-ms,140ms\)|stream-fade-word-in"
```

## Known caveats

- LLM telemetry often reports **tokens/sec**, while the UI reveals visible words. These are not equivalent.
- The renderer cannot reveal text before complete visible text exists.
- If backend chunks arrive as very large bursts, the rolling-wave logic smooths them but may still require subjective tuning.
- The current visual is close, but final merge review should include manual browser testing with:
  - normal-speed model
  - high-throughput model (~100+ tok/s)
  - long markdown responses
  - code blocks
  - lists/tables
  - tool-call-heavy responses

## Suggested next review steps

1. Manually test in browser after hard refresh (`Cmd+Shift+R`).
2. Try a high-throughput long essay and tune only these constants if needed:
   - `_STREAM_FADE_WAVE_MS`
   - `_STREAM_FADE_MAX_STAGGER_MS`
   - burst floor thresholds
   - `waveStepMs`
3. Check the diff for whether the `done` handler reindent is acceptable for the PR. It is intentional because the original done body is now wrapped in `_finishDone` so fade mode can drain before final DOM replacement.
4. If submitting PR, mention the feature is opt-in/off-by-default and the default streaming markdown path remains unchanged.

## Files to include in PR

Expected modified/new files:

```text
api/config.py
static/boot.js
static/i18n.js
static/index.html
static/messages.js
static/panels.js
static/style.css
tests/test_smooth_text_fade.py
STREAMING_FADE_HANDOFF.md
```
