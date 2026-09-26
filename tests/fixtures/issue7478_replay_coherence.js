async (scenario = 'user-only') => {
  const sid = S.session.session_id, stream = 'coherence-replay';
  const original = {
    EventSource: window.EventSource, setTimeout: window.setTimeout,
    clearTimeout: window.clearTimeout, save: window.saveInflightState, api: window.api,
  };
  let now = 0, id = 800000;
  const timers = new Map(), saved = [];
  const clone = value => JSON.parse(JSON.stringify(value));
  window.setTimeout = (fn, ms, ...args) => {
    timers.set(++id, {fn: () => fn(...args), at: now + Number(ms || 0), ms});
    return id;
  };
  window.clearTimeout = key => timers.delete(key);
  function advance(t) {
    now = t;
    for (let rounds = 0; rounds < 30; rounds++) {
      const due = [...timers].filter(([, v]) => v.at <= now).sort((a, b) => a[1].at - b[1].at);
      if (!due.length) return;
      for (const [key, timer] of due) if (timers.delete(key)) timer.fn();
    }
    throw new Error('Timer loop did not converge');
  }
  class FakeES {
    constructor(url) { this.url = String(url); this.readyState = 1; this.cbs = {}; }
    addEventListener(type, fn) { (this.cbs[type] ??= []).push(fn); }
    close() { this.readyState = 2; }
    emit(type, payload, seq) {
      if (!this.cbs[type]?.length) throw new Error('Unregistered stream event: ' + type);
      const event = {data: JSON.stringify(payload), lastEventId: stream + ':' + seq};
      for (const fn of [...this.cbs[type]]) fn(event);
    }
  }
  FakeES.OPEN = 1; FakeES.CLOSED = 2; FakeES.CONNECTING = 0;
  window.EventSource = FakeES;
  window.saveInflightState = (sessionId, data) => {
    original.save(sessionId, data);
    saved.push({at: now, data: clone(data), stored: loadInflightState(sessionId, stream)});
  };
  try {
    S.messages = scenario === 'historical'
      ? [{role: 'user', content: 'old prompt'}, {role: 'assistant', content: 'old answer'}, {role: 'user', content: 'current prompt'}]
      : [{role: 'user', content: 'current prompt'}];
    S.busy = true; S.activeStreamId = stream;
    // This synthetic turn bypasses send(): establish its sidebar owner too.
    // Otherwise the title event's cache render can purge INFLIGHT when the
    // fresh session's idle row has arrived, before replay is exercised.
    upsertActiveSessionForLocalTurn({messageCount: S.messages.length});
    INFLIGHT[sid] = {streamId: stream, messages: [...S.messages], toolCalls: [], uploaded: []};
    markInflight(sid, stream);
    attachLiveStream(sid, stream, [], {});
    const source = LIVE_STREAMS[sid].source;
    source.emit('title', {title: 'Replay coherence'}, 1);
    if (scenario === 'partial') {
      source.emit('token', {text: 'EARLY '}, 2);
      advance(40);
    }
    if (scenario === 'reasoning') source.emit('reasoning', {text: 'Current reasoning'}, 2);
    if (scenario === 'tool') {
      source.emit('tool', {name: 'terminal', tid: 'coherence-tool', args: {command: 'true'}}, 2);
      source.emit('tool_complete', {name: 'terminal', tid: 'coherence-tool', result: 'ok'}, 3);
    }
    advance(scenario === 'published' ? 1900 : 1999);
    const writesBeforeToken = saved.length;
    source.emit('token', {text: 'AUDIT_REPLAY_BODY'}, 206);
    const receipt = clone(INFLIGHT[sid]);
    const writesAfterToken = saved.length;
    advance(2000);
    const persisted = saved.filter(entry => entry.at === 2000).at(-1);
    if (!persisted) throw new Error('Expected the existing persistence deadline');
    const timersAtPersist = [...timers].map(([, v]) => ({at: v.at, ms: v.ms}));
    advance(2032);

    // Abrupt document-loss seam: discard live projection without orderly detach.
    // Recovery is loaded from real localStorage, not the in-memory capture.
    const late = clone(persisted.stored);
    source.close(); delete LIVE_STREAMS[sid]; timers.clear();
    document.querySelector('#liveAssistantTurn')?.remove();
    INFLIGHT[sid] = late;
    S.messages = late.messages.map(message => ({...message}));
    S.activeStreamId = stream; S.busy = true;
    window.api = async (url, ...rest) => String(url).includes('/api/chat/stream/status')
      ? {active: false, replay_available: true} : original.api(url, ...rest);
    attachLiveStream(sid, stream, [], {reconnecting: true});
    await Promise.resolve(); await Promise.resolve();
    const resumed = LIVE_STREAMS[sid].source;
    const replayUrl = resumed.url;
    resumed.emit('token', {text: ' SUFFIX'}, 207);
    advance(2065);
    const reconstructed = clone(INFLIGHT[sid]);
    // A queued callback from the retired source must not append into the new owner.
    source.emit('token', {text: 'STALE'}, 208);
    advance(2100);
    const afterStaleEvent = clone(INFLIGHT[sid]);
    return {scenario, receipt, persisted, timersAtPersist, writesBeforeToken,
      writesAfterToken, replayUrl, reconstructed, afterStaleEvent};
  } finally {
    try { closeLiveStream(sid, stream); } catch (_) { /* Browser context is isolated. */ }
    window.EventSource = original.EventSource;
    window.setTimeout = original.setTimeout;
    window.clearTimeout = original.clearTimeout;
    window.saveInflightState = original.save;
    window.api = original.api;
  }
}
