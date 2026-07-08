// MiniMax TTS engine for hermes-webui (v1, non-streaming).
//
// Registers via window.registerHermesTtsEngine — see static/boot.js for the
// contract. synthesize(text, opts) returns Promise<ArrayBuffer>. The actual
// HTTP call to MiniMax is proxied server-side by /api/tts so the
// MINIMAX_API_KEY stays off the browser.
//
// v1 surface:
//   - engine: 'minimax'  (registered here)
//   - default voice: English_expressive_narrator (configurable via tts.minimax.voice_id)
//   - audio: mp3, 32 kHz mono, ~128 kbps (MiniMax defaults)
//   - streaming: NOT YET — every utterance is one POST + one audio buffer.
//     v2 will reuse the Edge/voice-mode chunking infra to speak sentence-by-sentence
//     as Hermes streams tokens.
(function () {
  function _register() {
    if (typeof window.registerHermesTtsEngine !== 'function') {
      console.warn('[MiniMax TTS] registerHermesTtsEngine not defined yet — engine will NOT register. Load order is broken.');
      return false;
    }
    var ok = window.registerHermesTtsEngine({
      id: 'minimax',
      label: 'MiniMax',
      synthesize: async function synthesize(text, opts) {
        if (!text || typeof text !== 'string') {
          throw new Error('MiniMax TTS: text is required');
        }
        var body = { engine: 'minimax', text: text };
        // Optional per-utterance override; falls back to server-side tts.minimax.voice_id.
        if (opts && typeof opts.voice_id === 'string' && opts.voice_id) {
          body.voice_id = opts.voice_id;
        }
        var resp = await fetch('/api/tts', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!resp.ok) {
          var detail = '';
          try {
            var j = await resp.json();
            detail = (j && j.error) ? ': ' + j.error : '';
          } catch (_e) { /* ignore */ }
          throw new Error('MiniMax TTS failed (' + resp.status + ')' + detail);
        }
        return await resp.arrayBuffer();
      },
    });
    if (ok) {
      console.info('[MiniMax TTS] engine registered.');
    } else {
      console.warn('[MiniMax TTS] engine registration was rejected (likely a reserved id or invalid id).');
    }
    return ok;
  }

  // Try immediately (works when this script loads AFTER boot.js, which
  // is the normal ordering — boot.js defines window.registerHermesTtsEngine).
  if (!_register()) {
    // Fallback: defer until boot.js has had a chance to define the API.
    // If we still fail after a few seconds, give up with a clear error so
    // the user can see what went wrong instead of silent failure.
    var attempts = 0;
    var interval = setInterval(function () {
      attempts++;
      if (_register()) {
        clearInterval(interval);
      } else if (attempts >= 20) {
        clearInterval(interval);
        console.error('[MiniMax TTS] gave up after 10s — boot.js never defined registerHermesTtsEngine. Check script load order in index.html.');
      }
    }, 500);
  }
})();