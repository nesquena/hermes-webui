// ── i18n: locale bundles and t() helper ──────────────────────────────────────
// To add a new language: add an entry to LOCALES below with all keys translated.
// The language code must match a valid BCP 47 tag (used for speech recognition).
// Keys missing in a non-English locale fall back to English automatically.

// LOCALES is assembled from per-language bundle files under static/i18n/, loaded
// on demand: en (fallback) + the active language load synchronously via a
// document.write shim in index.html; other languages load lazily on switch.
// To add/edit a language: edit static/i18n/<code>.js (source strings live there).
const LOCALES = (window.__I18N = window.__I18N || {});
const AVAILABLE_LOCALES = ["en","it","ja","ru","es","de","zh","zh-Hant","pt","ko","fr","tr","pl","vi"];
// Self-names for the language picker — the full set is available without
// loading every bundle (bundles load lazily). Prefer a loaded bundle's _label.
const LOCALE_LABELS = {"en":"English","it":"Italiano","ja":"日本語","ru":"Русский","es":"Español","de":"Deutsch","zh":"简体中文","zh-Hant":"繁體中文","pt":"Português","ko":"한국어","fr":"Français","tr":"Türkçe","pl":"Polski","vi":"Tiếng Việt"};
const _I18N_VER = (function () {
  try {
    const cs = document.currentScript || document.querySelector('script[src*="i18n.js?v="]');
    const m = cs && cs.src && cs.src.match(/[?&]v=([^&]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  } catch (_) { return ''; }
})();
function _loadLocaleBundle(lang, cb) {
  if (LOCALES[lang]) { if (cb) cb(); return; }
  const s = document.createElement('script');
  s.src = 'static/i18n/' + lang + '.js' + (_I18N_VER ? ('?v=' + _I18N_VER) : '');
  s.onload = () => { if (cb) cb(); };
  s.onerror = () => { if (cb) cb(); }; // missing bundle -> t() falls back to en
  document.head.appendChild(s);
}

let _locale = LOCALES.en;

/**
 * Resolve an incoming locale tag to a known LOCALES key.
 * Supports exact keys, case-insensitive matches, and a few common aliases
 * (e.g. zh-CN -> zh, zh-TW -> zh-Hant). Returns null when unresolved.
 * @param {string} lang
 * @returns {string|null}
 */
function resolveLocale(lang) {
  if (typeof lang !== 'string') return null;
  const raw = lang.trim();
  if (!raw) return null;
  if (LOCALES[raw]) return raw;

  const lower = raw.toLowerCase().replace(/_/g, '-');

  // Case-insensitive direct match first.
  const direct = AVAILABLE_LOCALES.find((k) => k.toLowerCase() === lower);
  if (direct) return direct;

  // Common Chinese variants.
  if (lower === 'zh' || lower.startsWith('zh-cn') || lower.startsWith('zh-sg') || lower.startsWith('zh-hans')) {
    return AVAILABLE_LOCALES.indexOf('zh')>=0 ? 'zh' : null;
  }
  if (lower.startsWith('zh-tw') || lower.startsWith('zh-hk') || lower.startsWith('zh-mo') || lower.startsWith('zh-hant')) {
    return AVAILABLE_LOCALES.indexOf('zh-Hant')>=0 ? 'zh-Hant' : null;
  }

  // Fallback to base language subtag (e.g. en-US -> en).
  const base = lower.split('-')[0];
  const baseMatch = AVAILABLE_LOCALES.find((k) => k.toLowerCase() === base);
  return baseMatch || null;
}

/**
 * Resolve locale with precedence:
 * 1) primary (typically server setting)
 * 2) fallback (typically localStorage)
 * 3) English
 * @param {string} primary
 * @param {string} fallback
 * @returns {string}
 */
function resolvePreferredLocale(primary, fallback) {
  return resolveLocale(primary) || resolveLocale(fallback) || 'en';
}

/**
 * Translate a key. Falls back to English if the key is missing in the active locale.
 * Supports function values (for interpolated strings): call t('key', arg).
 * @param {string} key
 * @param {...*} args - forwarded to function-valued translations
 * @returns {string}
 */
function t(key, ...args) {
  const val = _locale[key] ?? LOCALES.en[key];
  if (val === undefined) return key;  // final fallback: return key itself
  if (typeof val === 'function') return val(...args);
  if (args.length) {
    // Locale strings can use numbered placeholders like {0} and {1}.
    return String(val).replace(/\{(\d+)\}/g, (match, idx) => (
      Object.prototype.hasOwnProperty.call(args, idx) ? String(args[idx]) : match
    ));
  }
  return val;
}

/**
 * Switch locale by language code (e.g. 'en', 'zh').
 * Persists to localStorage and updates the <html lang> attribute.
 * @param {string} lang
 */
function setLocale(lang) {
  const resolved = resolveLocale(lang) || 'en';
  try { localStorage.setItem('hermes-lang', resolved); } catch (_) {}
  const apply = () => {
    _locale = LOCALES[resolved] || LOCALES.en || {};
    document.documentElement.lang = (_locale && _locale._speech) || resolved;
  };
  if (LOCALES[resolved] || resolved === 'en') {
    apply();
  } else {
    // Runtime switch to a not-yet-loaded language: fetch its bundle, then
    // apply and re-stamp the DOM (the caller already applied the old locale).
    _loadLocaleBundle(resolved, () => {
      apply();
      if (typeof applyLocaleToDOM === 'function') applyLocaleToDOM();
    });
  }
}

/**
 * Load locale from localStorage (called once at boot, before DOMContentLoaded).
 * Server-persisted preference is applied later in loadSettingsPanel().
 */
function loadLocale() {
  let stored = null;
  try { stored = localStorage.getItem('hermes-lang'); } catch (_) {}
  setLocale(resolvePreferredLocale(null, stored));
}

/**
 * Re-stamp all [data-i18n] elements in the DOM with the current locale.
 * Safe to call at any time — missing keys fall back to English.
 * Call after setLocale() to make static HTML text update without a reload.
 */
function applyLocaleToDOM() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    const key = el.getAttribute('data-i18n');
    const val = t(key);
    if (val && val !== key) el.textContent = val;
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    const key = el.getAttribute('data-i18n-title');
    const val = t(key);
    if (!val || val === key) return;
    if (el.hasAttribute('data-tooltip')) {
      // Custom CSS tooltip is in use (#1775) — sync it and explicitly clear
      // the native `title` attribute so the slow ~1.5s browser tooltip never
      // co-fires alongside the fast custom tooltip.
      el.setAttribute('data-tooltip', val);
      if (el.hasAttribute('title')) el.removeAttribute('title');
    } else {
      // Element opted out of custom tooltips — fall back to the native title.
      el.title = val;
    }
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    const key = el.getAttribute('data-i18n-placeholder');
    const val = t(key);
    if (val && val !== key) el.placeholder = val;
  });
  document.querySelectorAll('[data-i18n-aria-label]').forEach(el => {
    const key = el.getAttribute('data-i18n-aria-label');
    const val = t(key);
    if (val && val !== key) el.setAttribute('aria-label', val);
  });
  if (typeof syncAppTitlebar === 'function') syncAppTitlebar();
}

// Apply saved locale immediately so there's no flash of English on reload.
loadLocale();
