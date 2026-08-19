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
 * Model values in this UI use the WebUI's selector value form. The scheduler
 * returns `model` + `provider` separately. When the recommended model is
 * missing from `#modelSelect`, _applyRecommendation synthesizes an
 * `@provider:model` route hint (same shape as ui.js
 * `_ensureModelOptionInDropdown`) so the backend routes through the selected
 * provider instead of misreading a `provider/model` slash value as an
 * OpenRouter ID.
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
    const model = String(rec.model).trim();
    const provider = String(rec.provider || '').trim();
    if (!provider) return model;
    const explicitPrefix = '@' + provider + ':';
    // 已带 @provider: 前缀的模型直接沿用，避免二次包裹。
    if (model.toLowerCase().startsWith(explicitPrefix.toLowerCase())) return model;
    return explicitPrefix + model;
  }

  // 在现有 <select> 中按 model + provider 精确匹配，避免把 OpenRouter
  // 组里同名的 `openai/gpt-...` 斜杠项误选为直连 OpenAI 的推荐。
  function _mrFindOption(sel, rec) {
    const model = String(rec.model || '').trim();
    const provider = String(rec.provider || '').trim();
    if (!model) return '';
    const injected = _mrToSelectValue(rec);
    const legacy = provider ? provider + '/' + model : model;
    for (let i = 0; i < sel.options.length; i++) {
      const opt = sel.options[i];
      const value = String(opt.value || '');
      if (value !== injected && value !== legacy && value !== model) continue;
      const optProvider = String(
        (typeof _getOptionProviderId === 'function' ? _getOptionProviderId(opt) : '') || ''
      ).trim();
      // 选项有明确 provider 归属时必须与推荐一致；静态兜底选项没有
      // data-provider，保留旧的 provider/model 值匹配行为。
      if (optProvider && provider && optProvider.toLowerCase() !== provider.toLowerCase()) continue;
      if (provider && !optProvider && value !== legacy && value !== model) continue;
      return value;
    }
    return '';
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
    const model = String(rec.model || '').trim();
    const provider = String(rec.provider || '').trim();
    if (!model) return false;
    // 优先选择目录中已存在且 provider 归属一致的选项（例如 active provider
    // 的裸 model 选项，或非 active provider 的 @provider:model 选项）。
    let target = _mrFindOption(sel, rec);
    if (!target) {
      // 目录里没有该模型：合成 @provider:model 路由提示项，与 ui.js
      // _ensureModelOptionInDropdown 完全一致。不能用 provider/model 斜杠值——
      // 当配置 provider 为 openai-codex 时，后端会把 openai/gpt-... 当成
      // OpenRouter 标识（Greptile P1）。
      const opt = document.createElement('option');
      target = _mrToSelectValue(rec);
      opt.value = target;
      opt.dataset.model = model;
      if (provider) opt.dataset.provider = provider;
      opt.dataset.custom = '1';
      opt.textContent = (provider ? provider + ' ' : '') + model;
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
