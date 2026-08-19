/* model_router.js -- Model scheduler frontend integration (opt-in, off by default).

 * Provides two toggles:
 *   1. Settings-level master switch (`model_scheduler_enabled` in settings.json),
 *      surfaced in the Settings panel. When off, nothing here runs.
 *   2. Composer auto/manual switch next to the model chip. When on (and master
 *      on), the composer auto-applies a recommendation before sending.
 *
 * The backend endpoints (api/model_router.py + routes) expose:
 *   GET  /api/model-router/status    -> {enabled, schedule, quota_window_hours}
 *   GET  /api/model-router/policy    -> {enabled, schedule, models, quota_window_hours}
 *   GET  /api/model-router/recommend -> {model, provider, reason, difficulty, ...}
 *   POST /api/model-router/policy    -> merge updates into model-policy.json
 *   POST /api/model-router/failure   -> record upstream failure (cooldown)
 *
 * Model values in this UI use the WebUI's `provider/model` form (e.g.
 * `openai/gpt-5.4-mini`), while the scheduler returns `model` + `provider`
 * separately. `_mrToSelectValue` joins them so a recommendation can be
 * applied by setting `#modelSelect`.
 */
(function () {
  'use strict';

  // ── state ──────────────────────────────────────────────────────────────
  let _masterOn = false;      // settings-level master switch
  let _composerAuto = true;   // composer auto/manual (only relevant if master on)
  let _initialized = false;
  let _lastRecommend = null;  // {key, model, provider, reason, ts}

  const LS_COMPOSER_AUTO = 'hermes-webui-model-router-auto';
  const TTL_MS = 60 * 1000; // recommend cache TTL (avoid spamming /recommend per keystroke)

  // ── helpers ────────────────────────────────────────────────────────────
  function _el(id) {
    return document.getElementById(id);
  }

  function _mrToSelectValue(rec) {
    if (!rec || !rec.model) return '';
    if (rec.provider) return rec.provider + '/' + rec.model;
    return rec.model;
  }

  function _currentSessionId() {
    return (typeof S !== 'undefined' && S.session && S.session.session_id) || '';
  }

  // Cache key must isolate by text + session so a recommendation made for one
  // message (or one session) is never reused for a different one within TTL.
  function _cacheKey(text) {
    return _currentSessionId() + '\n' + String(text || '').slice(0, 4000);
  }

  function _applyRecommendation(rec) {
    if (!rec || !rec.model) return false;
    const sel = _el('modelSelect');
    if (!sel) return false;
    const target = _mrToSelectValue(rec);
    if (!target) return false;
    // Build option list if the recommended model isn't present yet (e.g. the
    // scheduler profile lists models the current provider dropdown lacks).
    let exists = false;
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === target) { exists = true; break; }
    }
    if (!exists) {
      const opt = document.createElement('option');
      opt.value = target;
      opt.dataset.model = rec.model || '';
      if (rec.provider) opt.dataset.provider = rec.provider;
      opt.textContent = (rec.provider ? rec.provider + ' ' : '') + rec.model;
      sel.appendChild(opt);
    }
    if (sel.value !== target) {
      sel.value = target;
      // Fire the same onchange path the user would trigger (persists to
      // session + model chip sync).
      if (typeof sel.onchange === 'function') {
        try { sel.onchange(); } catch (_e) { /* non-fatal */ }
      } else {
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
      return true;
    }
    return false;
  }

  async function _recommend(text) {
    if (!_masterOn) return null;
    const key = _cacheKey(text);
    if (_lastRecommend && _lastRecommend._key === key && (Date.now() - _lastRecommend.ts) < TTL_MS) {
      return _lastRecommend;
    }
    try {
      const qs = new URLSearchParams({ text: String(text || '').slice(0, 4000) });
      const data = await api('/api/model-router/recommend?' + qs.toString(), { timeoutMs: 5000 });
      if (data && data.model) {
        _lastRecommend = Object.assign({}, data, { ts: Date.now(), _key: key });
        return _lastRecommend;
      }
    } catch (_e) { /* network/5xx: keep current model, never block sending */ }
    return null;
  }

  // Called from send() (messages.js) right before the payload is built.
  // Returns a Promise; send() awaits it but continues even if it fails.
  async function beforeSend(text) {
    if (!_masterOn || !_composerAuto) return;
    if (!text || !text.trim()) return;
    const ownerSid = _currentSessionId();
    const rec = await _recommend(text);
    // Greptile P1: 仅当用户仍停留在发起 send 的会话时才应用推荐，避免
    // /recommend 返回时用户已切换会话，导致迟到推荐覆盖错误会话的模型。
    if (rec && ownerSid === _currentSessionId()) _applyRecommendation(rec);
  }

  // ── master switch (settings) ──────────────────────────────────────────
  async function refreshMaster() {
    try {
      const st = await api('/api/model-router/status', { timeoutMs: 5000 });
      _masterOn = !!(st && st.enabled);
    } catch (_e) {
      _masterOn = false;
    }
    _syncComposerToggleVisibility();
  }

  function setMaster(on) {
    _masterOn = !!on;
    if (!_masterOn) {
      _composerAuto = false;
      _lastRecommend = null; // never reuse a stale recommendation after disable
    }
    _syncComposerToggleVisibility();
  }

  // ── composer auto/manual toggle ────────────────────────────────────────
  function _syncComposerToggleVisibility() {
    const wrap = _el('modelRouterComposerWrap');
    if (!wrap) return;
    // Keep inline-flex so the toggle stays on the same line as the model chip.
    wrap.style.display = _masterOn ? 'inline-flex' : 'none';
    const cb = _el('modelRouterComposerAuto');
    if (cb) cb.checked = _composerAuto;
  }

  function _onComposerAutoChange() {
    const cb = _el('modelRouterComposerAuto');
    _composerAuto = !!(cb && cb.checked);
    try { localStorage.setItem(LS_COMPOSER_AUTO, _composerAuto ? '1' : '0'); } catch (_e) {}
  }

  // ── init: inject composer toggle next to the model chip ───────────────
  function _injectComposerToggle() {
    if (_el('modelRouterComposerWrap')) return;
    const modelWrap = document.querySelector('.composer-model-wrap');
    if (!modelWrap) return;
    const wrap = document.createElement('div');
    wrap.id = 'modelRouterComposerWrap';
    wrap.className = 'composer-model-router-toggle';
    wrap.style.cssText = 'display:inline-flex;align-items:center;gap:4px;margin-left:6px;font-size:12px;color:var(--text,#888);white-space:nowrap;';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = 'modelRouterComposerAuto';
    cb.style.cssText = 'width:13px;height:13px;accent-color:var(--accent,#4f8cff);cursor:pointer;';
    cb.title = 'Auto-switch model via scheduler';
    cb.addEventListener('change', _onComposerAutoChange);
    const label = document.createElement('span');
    label.textContent = 'Auto';
    label.style.cursor = 'pointer';
    label.addEventListener('click', function () { cb.checked = !cb.checked; _onComposerAutoChange(); });
    wrap.appendChild(cb);
    wrap.appendChild(label);
    modelWrap.parentNode.insertBefore(wrap, modelWrap.nextSibling);
    _syncComposerToggleVisibility();
  }

  function init() {
    if (_initialized) return;
    _initialized = true;
    try { _composerAuto = localStorage.getItem(LS_COMPOSER_AUTO) !== '0'; } catch (_e) {}
    _injectComposerToggle();
    refreshMaster();
  }

  // Expose a tiny API for messages.js send() hook.
  window.ModelRouter = {
    init: init,
    beforeSend: beforeSend,
    setMaster: setMaster,
    get enabled() { return _masterOn && _composerAuto; },
  };

  // Auto-init once DOM is ready (scripts are deferred, so DOM is available).
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
