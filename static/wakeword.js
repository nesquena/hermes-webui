/* Hermes WebUI — wake-word detector (browser-side openWakeWord port).
 *
 * Hands-free dictation start: an always-on (opt-in) AudioWorklet tap feeds
 * 16 kHz mono PCM through the vendored openWakeWord ONNX models
 * (melspectrogram -> speech_embedding -> wake model) using onnxruntime-web,
 * mirroring openwakeword's AudioFeatures streaming logic:
 *
 *   - every 80 ms frame (1280 samples) the melspectrogram of
 *     [480 past samples + the 1280 frame samples] is appended to a
 *     capped (970, 32) mel-row buffer          (python: [-n-160*3:])
 *   - one 96-d embedding row is computed from the last 76 mel rows
 *     (input [1,76,32,1]) per frame (python: windows stepping by 8 rows)
 *   - the wake model scores the last 16 feature rows ([1,16,96])
 *   - mel transform x/10 + 2 applied after the graph, like python's
 *     default melspec_transform
 *
 * Trigger rule (matches the shipped eval harness): >= N consecutive frames
 * over threshold + refractory cooldown. On trigger we call the same
 * window._toggleMicCapture() path the mic button uses. While dictation is
 * active the detector keeps listening but suppresses triggers (the wake
 * word is a START word, not a stop word).
 *
 * Loaded lazily from boot.js only when the user enables the feature in
 * Settings; nothing is fetched and no mic is opened otherwise.
 */
(function(){
  'use strict';
  if (window.HermesWakeWord) return; // double-load guard

  const SR = 16000;
  const FRAME = 1280;            // 80 ms
  const CTX = 480;               // 30 ms past context for the mel window
  const MEL_MAX = 970;           // ~10 s of mel rows (python: 10*97)
  const FEAT_MAX = 120;          // ~10 s of feature rows
  // Page-relative, matching how index.html/boot.js reference static assets.
  const VENDOR = 'static/vendor/';

  function log(...a){ try{ console.debug('[wakeword]', ...a); }catch(_){} }

  // Local track-stopper (start() cancellation paths must not depend on boot.js).
  function _stopTracksShim(stream){
    try{ stream && stream.getTracks().forEach(t=>t.stop()); }catch(_){}
  }

  class WakeWordDetector {
    constructor(opts){
      opts = opts || {};
      this.modelBase = opts.modelBase || (VENDOR + 'openwakeword/');
      this.wakeModelFile = opts.wakeModelFile || 'hey_jarvis_v0.1.onnx';
      this.threshold = opts.threshold != null ? opts.threshold : 0.5;
      this.consecutive = opts.consecutive || 3;
      this.refractoryMs = opts.refractoryMs != null ? opts.refractoryMs : 2000;
      this.onTrigger = opts.onTrigger || function(){};
      this.enabled = opts.enabled !== false;
      this._state = 'idle'; // idle|loading|running|error
      this._gen = 0;                 // bumped by stop(); cancels in-flight start()
      this._scores = [];
      this._resetStream();
    }

    _resetStream(){
      this.pend = new Float32Array(0); // unconsumed samples (FIFO)
      this.hist = new Float32Array(0); // last CTX consumed samples
      this.melRows = [];               // Float32Array(32) each
      this.features = [];              // number[96] each
      this._run = 0;
      this._lastHit = 0;
      this._triggers = 0;
      this._warm = 0;                  // python zeroes the first 5 predictions
      this._chain = Promise.resolve(); // serializes ONNX calls
    }

    get state(){ return this._state; }
    get score(){ return this._scores.length ? this._scores[this._scores.length-1] : 0; }
    get triggers(){ return this._triggers; }

    async start(){
      if (this._state === 'running' || this._state === 'loading') return;
      this._state = 'loading';
      const gen = ++this._gen; // invalidated by stop(): cancel in-flight startup
      const stale = ()=> gen !== this._gen;
      try{
        if (!window.ort) throw new Error('onnxruntime-web not loaded');
        const ort = window.ort;
        ort.env.wasm.numThreads = 1;  // server sends no COOP/COEP headers
        ort.env.wasm.simd = true;
        const t0 = performance.now();
        const sopts = { executionProviders: ['wasm'], graphOptimizationLevel: 'all' };
        this.melModel = await ort.InferenceSession.create(this.modelBase + 'melspectrogram.onnx', sopts);
        if (stale()){ this._releaseSessions([this.melModel]); this.melModel = null; return; }
        this.embModel = await ort.InferenceSession.create(this.modelBase + 'embedding_model.onnx', sopts);
        if (stale()){ this._releaseSessions([this.melModel, this.embModel]); this.melModel = this.embModel = null; return; }
        this.wakeModel = await ort.InferenceSession.create(this.modelBase + this.wakeModelFile, sopts);
        if (stale()){ this._releaseSessions([this.melModel, this.embModel, this.wakeModel]); this.melModel = this.embModel = this.wakeModel = null; return; }
        log('models loaded in', Math.round(performance.now() - t0), 'ms');

        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true }
        });
        if (stale()){ _stopTracksShim(stream); return; }
        this.stream = stream;
        this.ctx = new AudioContext({ sampleRate: SR });
        await this.ctx.resume();
        if (stale()){ this.ctx.close().catch(()=>{}); _stopTracksShim(stream); this.stream = null; this.ctx = null; return; }
        const src = this.ctx.createMediaStreamSource(stream);

        const workletCode = `
          class TapProcessor extends AudioWorkletProcessor {
            process(inputs){
              const ch = inputs[0] && inputs[0][0];
              if (ch) this.port.postMessage(ch.slice(0));
              return true;
            }
          }
          registerProcessor('ww-tap', TapProcessor);
        `;
        const url = URL.createObjectURL(new Blob([workletCode], { type: 'application/javascript' }));
        try{
          await this.ctx.audioWorklet.addModule(url);
          if (stale()){ URL.revokeObjectURL(url); return; }
          this.node = new AudioWorkletNode(this.ctx, 'ww-tap');
        } finally {
          URL.revokeObjectURL(url);
        }
        this.node.port.onmessage = (e)=>{ this._onAudio(e.data); };
        src.connect(this.node);
        if (stale()){ // raced with stop() between addModule and connect
          this.stop();
          return;
        }
        // Not connected to destination: analysis only, nothing plays out.

        this._state = 'running';
        log('running (phrase model:', this.wakeModelFile + ')');
      }catch(err){
        this._state = 'error';
        this.stop();
        throw err;
      }
    }

    /** Release ONNX sessions after pending inference finishes (findings #2/#3). */
    async _releaseSessions(models){
      const list = (models || []).filter(Boolean);
      if (!list.length) return;
      // Serialize behind in-flight _drain() work so we never release a
      // session a queued frame is about to run.
      this._chain = this._chain.catch(()=>{}).then(async ()=>{
        for (const m of list){ try{ await m.release(); }catch(_){} }
      });
      return this._chain;
    }

    stop(){
      this._gen = (this._gen || 0) + 1; // cancels any in-flight start()
      try{ if (this.node && this.node.port) this.node.port.onmessage = null; }catch(_){}
      try{ this.node && this.node.disconnect(); }catch(_){}
      this.node = null;
      try{ this.stream && this.stream.getTracks().forEach(t=>t.stop()); }catch(_){}
      this.stream = null;
      try{ this.ctx && this.ctx.close(); }catch(_){}
      this.ctx = null;
      // Fully release model resources — repeated toggles must not accumulate
      // ONNX sessions (Greptile finding #3).
      const models = [this.melModel, this.embModel, this.wakeModel];
      this.melModel = this.embModel = this.wakeModel = null;
      this._releaseSessions(models);
      if (this._state !== 'error') this._state = 'idle';
    }

    /** Feed a block of Float32 samples (any length). Serialized internally. */
    _onAudio(chunk){
      if (this._state !== 'running' && this._state !== 'feeding') return;
      const merged = new Float32Array(this.pend.length + chunk.length);
      merged.set(this.pend, 0);
      merged.set(chunk, this.pend.length);
      this.pend = merged;
      this._chain = this._chain.then(()=>this._drain());
    }

    async _drain(){
      while (this.pend.length >= FRAME){
        if (!this.melModel || !this.embModel || !this.wakeModel) return; // stopped mid-drain
        const frame = this.pend.subarray(0, FRAME);
        const windowSamples = new Float32Array(CTX + FRAME);
        windowSamples.set(this.hist, 0);
        windowSamples.set(frame, CTX);
        // consume now; hist = last CTX samples of this frame
        this.hist = frame.slice(FRAME - CTX);
        this.pend = this.pend.slice(FRAME);
        try{
          await this._processFrame(windowSamples);
        }catch(err){
          if (this.melModel) log('frame error', err); // real error only if still live
          return;
        }
      }
    }

    async _processFrame(windowSamples){
      const ort = window.ort;
      // 1) mel rows for this 80 ms window
      const melIn = new ort.Tensor('float32', windowSamples, [1, windowSamples.length]);
      const melOut = await this.melModel.run({ input: melIn });
      const melData = melOut.output.data;
      const nRows = Math.floor(melData.length / 32);
      for (let r = 0; r < nRows; r++){
        const row = new Float32Array(32);
        for (let c = 0; c < 32; c++) row[c] = melData[r * 32 + c] / 10 + 2;
        this.melRows.push(row);
      }
      if (this.melRows.length > MEL_MAX) this.melRows = this.melRows.slice(-MEL_MAX);
      if (!nRows) return;

      // 2) one embedding row from the last 76 mel rows
      if (this.melRows.length >= 76){
        const win = this.melRows.slice(-76);
        const flat = new Float32Array(76 * 32);
        for (let i = 0; i < 76; i++) flat.set(win[i], i * 32);
        const embIn = new ort.Tensor('float32', flat, [1, 76, 32, 1]);
        const embOut = await this.embModel.run({ input_1: embIn });
        const embKey = Object.keys(embOut)[0];
        this.features.push(Array.from(embOut[embKey].data));
        if (this.features.length > FEAT_MAX) this.features = this.features.slice(-FEAT_MAX);
      } else {
        return;
      }

      // 3) wake model on the last 16 feature rows
      if (this.features.length >= 16){
        const winF = this.features.slice(-16);
        const flatF = new Float32Array(16 * 96);
        for (let i = 0; i < 16; i++) for (let j = 0; j < 96; j++) flatF[i * 96 + j] = winF[i][j];
        const wakeIn = new ort.Tensor('float32', flatF, [1, 16, 96]);
        const wakeOut = await this.wakeModel.run({ 'x.1': wakeIn });
        const wakeKey = Object.keys(wakeOut)[0];
        let score = wakeOut[wakeKey].data[0];
        this._warm++;
        if (this._warm <= 5) score = 0; // python: zero first 5 predictions
        this._scores.push(score);
        if (this._scores.length > 50) this._scores.shift();
        this._handleScore(score);
      }
    }

    _handleScore(score){
      const now = performance.now();
      if (score >= this.threshold){
        this._run++;
        if (this._run >= this.consecutive && now - this._lastHit >= this.refractoryMs){
          this._run = 0;
          this._lastHit = now;
          this._triggers++;
          log('TRIGGER', score.toFixed(3), '#' + this._triggers);
          if (this.enabled){
            try{ this.onTrigger(score); }catch(err){ log('onTrigger error', err); }
          }
        }
      } else {
        this._run = 0;
      }
    }
  }

  // Expose for boot.js and tests. No auto-start, no mic access on load.
  window.HermesWakeWord = { WakeWordDetector, SR, FRAME };
})();
