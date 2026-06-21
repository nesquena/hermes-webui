/**
 * Hermes WebUI — Offline Conversation Cache
 *
 * Uses IndexedDB to cache:
 *   1. The session list (/api/sessions) — the sidebar data
 *   2. Individual session conversations (/api/session?session_id=X&messages=1)
 *
 * Cache is warmed proactively for the 5 most recent sessions after every
 * successful sidebar load, so "today's conversations" are always available
 * when the connection drops or is too weak to load.
 *
 * On GET failures, api() falls back to this cache. A visual banner tells
 * the user they're reading cached data with a relative timestamp.
 *
 * PRIVACY MODEL (review #4435):
 *   - Profile-scoped: each profile gets its own IndexedDB database.
 *     Profile B can NEVER read Profile A's cached data.
 *   - Opt-in: transcript caching is disabled by default. The user must
 *     explicitly enable it via the settings panel. When disabled, no data
 *     is cached and existing cache is wiped.
 *   - Logout-safe: nukeCache() is called on sign-out and profile switch.
 *
 * Storage strategy:
 *   - Sessions larger than 2MB (after media stripping) are not cached
 *   - Base64 data URIs recursively stripped from ALL string fields
 *   - Tool result fields truncated; attachment/thumbnail fields dropped
 *   - Cache is version-tagged; mismatches nuke the store on load
 */
(function () {
  'use strict';

  // ── Constants ─────────────────────────────────────────────────────────
  var DB_PREFIX = 'hermes-offline-cache';
  var LEGACY_DB_NAME = 'hermes-offline-cache'; // pre-scoping DB name (no profile suffix)
  var DB_VERSION = 1;
  var STORE_SESSION_LIST = 'sessionList'; // single record, key='current'
  var STORE_SESSIONS = 'sessions'; // keyed by session_id
  var MAX_SESSION_BYTES = 2 * 1024 * 1024; // skip caching sessions > 2MB
  var MAX_CACHED_SESSIONS = 20; // evict oldest beyond this
  var WARM_COUNT = 5; // number of recent sessions to proactively cache
  var SETTING_KEY = 'hermes-offline-cache-enabled';

  // Matches data:*;base64,... sequences of 100+ chars (real embedded media,
  // not short inline tokens)
  var DATA_URI_RE = /data:[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]{100,}/g;

  // ── State ─────────────────────────────────────────────────────────────
  var _db = null;
  var _dbInitFailed = false;
  var _scope = null; // profile name — must be set before any DB operation
  var _legacyWiped = false; // tracks one-time legacy DB cleanup per page load

  // ── Opt-in ────────────────────────────────────────────────────────────
  function _isEnabled() {
    try {
      return localStorage.getItem(SETTING_KEY) === 'true';
    } catch (_) {
      return false;
    }
  }

  function isEnabled() {
    return _isEnabled();
  }

  function setEnabled(on) {
    try {
      localStorage.setItem(SETTING_KEY, on ? 'true' : 'false');
    } catch (_) {}
    if (!on) {
      // Wipe existing cache immediately when disabled
      nukeCache();
    }
  }

  // ── Profile scope ─────────────────────────────────────────────────────
  // Each profile gets its own IndexedDB database: hermes-offline-cache:<profile>
  // This guarantees Profile B can never read Profile A's cached transcripts.
  function _dbName() {
    var safe = (_scope || '').replace(/[^a-zA-Z0-9_-]/g, '').slice(0, 64);
    return DB_PREFIX + ':' + (safe || 'default');
  }

  /**
   * Set the active profile scope. Must be called before any cache operation.
   * Switching profiles closes the current DB handle and opens the new
   * profile's DB on the next operation. Also wipes the legacy unscoped DB
   * on first call.
   */
  async function setScope(profileName) {
    var newScope = profileName || 'default';
    if (_scope === newScope && _db) return;

    // Close current DB connection so it doesn't linger
    if (_db) {
      try { _db.close(); } catch (_) {}
      _db = null;
    }
    _dbInitFailed = false; // reset — different DB, different init
    _scope = newScope;

    // One-time legacy DB cleanup: delete the old unscoped DB
    if (!_legacyWiped) {
      _legacyWiped = true;
      _deleteLegacyDB();
    }
  }

  function _deleteLegacyDB() {
    try {
      var req = indexedDB.deleteDatabase(LEGACY_DB_NAME);
      req.onsuccess = function () {};
      req.onerror = function () {};
      req.onblocked = function () {};
    } catch (_) {}
  }

  // ── Version tracking ──────────────────────────────────────────────────
  function _appVersion() {
    try {
      return (
        (typeof window !== 'undefined' &&
          window.__WEBUI_VERSION__) ||
        document.documentElement.getAttribute('data-version') ||
        'unknown'
      );
    } catch (_) {
      return 'unknown';
    }
  }

  // ── DB open ───────────────────────────────────────────────────────────
  function _openDB() {
    if (!_scope) return Promise.reject(new Error('No cache scope set'));
    if (_db) return Promise.resolve(_db);
    if (_dbInitFailed) return Promise.reject(new Error('IndexedDB unavailable'));
    return new Promise(function (resolve, reject) {
      try {
        var req = indexedDB.open(_dbName(), DB_VERSION);
        req.onupgradeneeded = function (e) {
          var db = e.target.result;
          if (!db.objectStoreNames.contains(STORE_SESSION_LIST)) {
            db.createObjectStore(STORE_SESSION_LIST);
          }
          if (!db.objectStoreNames.contains(STORE_SESSIONS)) {
            db.createObjectStore(STORE_SESSIONS);
          }
        };
        req.onsuccess = function (e) {
          _db = e.target.result;
          // Handle unexpected close (e.g. another tab triggers version change)
          _db.onclose = function () { _db = null; };
          _db.onversionchange = function () {
            try { _db.close(); } catch (_) {}
            _db = null;
          };
          _checkVersion().then(function () { resolve(_db); });
        };
        req.onerror = function () {
          _dbInitFailed = true;
          reject(req.error || new Error('IndexedDB open failed'));
        };
      } catch (e) {
        _dbInitFailed = true;
        reject(e);
      }
    });
  }

  // Check if cached version matches current app version; nuke if mismatch.
  async function _checkVersion() {
    if (!_db) return;
    try {
      var tx = _db.transaction(STORE_SESSION_LIST, 'readonly');
      var store = tx.objectStore(STORE_SESSION_LIST);
      var record = await _txGet(store, '__version__');
      var current = _appVersion();
      if (record && record !== current) {
        // Version mismatch — nuke everything
        await nukeCache();
        await _putSessionListMeta('__version__', current);
      } else if (!record) {
        await _putSessionListMeta('__version__', current);
      }
    } catch (_) {
      // non-fatal
    }
  }

  // ── Transaction helpers ───────────────────────────────────────────────
  function _txGet(store, key) {
    return new Promise(function (resolve, reject) {
      var req = store.get(key);
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function _txPut(store, key, value) {
    return new Promise(function (resolve, reject) {
      var req = store.put(value, key);
      req.onsuccess = function () { resolve(); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function _txDelete(store, key) {
    return new Promise(function (resolve, reject) {
      var req = store.delete(key);
      req.onsuccess = function () { resolve(); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function _txKeys(store) {
    return new Promise(function (resolve, reject) {
      var req = store.getAllKeys();
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }

  function _txClear(store) {
    return new Promise(function (resolve, reject) {
      var req = store.clear();
      req.onsuccess = function () { resolve(); };
      req.onerror = function () { reject(req.error); };
    });
  }

  // ── Media stripping (recursive, fail-closed) ──────────────────────────
  // Deep-clones the session data while sanitizing all fields that could
  // contain embedded media, large tool outputs, or sensitive attachments.
  // Returns null if cloning fails (fail-closed: skip caching entirely).

  // Keys whose values are dropped entirely (replaced with a placeholder)
  var DROP_KEYS = {
    attachment: true, attachments: true,
    thumbnail: true, thumbnail_url: true,
    preview: true, preview_url: true,
    file_data: true, blob: true
  };

  // Keys whose string values are truncated beyond 5KB
  var TRUNCATE_KEYS = {
    result: true, output: true, snippet: true
  };

  function _sanitizeValue(val, depth) {
    if (depth > 10) return '[nested — truncated]';

    if (typeof val === 'string') {
      // Strip embedded base64 data URIs
      if (val.length > 200 && DATA_URI_RE.test(val)) {
        DATA_URI_RE.lastIndex = 0; // reset global regex state
        val = val.replace(DATA_URI_RE, '[base64 omitted]');
      }
      return val;
    }

    if (Array.isArray(val)) {
      var arr = [];
      for (var i = 0; i < val.length; i++) {
        arr.push(_sanitizeValue(val[i], depth + 1));
      }
      return arr;
    }

    if (val && typeof val === 'object') {
      // Handle image_url structures (OpenAI format)
      if (val.type === 'image_url' || (val.image_url && typeof val.image_url === 'object')) {
        return { type: 'text', text: '[cached image omitted]' };
      }

      var out = {};
      for (var key in val) {
        if (!Object.prototype.hasOwnProperty.call(val, key)) continue;
        var v = val[key];

        // Drop known binary/attachment fields
        if (DROP_KEYS[key]) {
          out[key] = '[omitted]';
          continue;
        }

        // Truncate large tool-result / output fields
        if (TRUNCATE_KEYS[key] && typeof v === 'string' && v.length > 5000) {
          out[key] = v.slice(0, 200) +
            '\n…[cached tool output truncated, ' +
            Math.round(v.length / 1024) + 'KB]';
          continue;
        }

        // Replace raw image_url string values
        if (key === 'image_url' && typeof v === 'string' && v.length > 200) {
          out[key] = '[cached image omitted]';
          continue;
        }

        out[key] = _sanitizeValue(v, depth + 1);
      }
      return out;
    }

    return val;
  }

  function _sanitizeForCache(sessionData) {
    if (!sessionData || typeof sessionData !== 'object') return null;
    try {
      // The recursive sanitizer builds a new object tree, so this doubles
      // as a deep clone — no separate JSON.parse(JSON.stringify()) needed.
      return _sanitizeValue(sessionData, 0);
    } catch (_) {
      // FAIL CLOSED: don't cache if sanitization fails
      return null;
    }
  }

  // ── Size check ────────────────────────────────────────────────────────
  function _byteLength(obj) {
    try {
      return JSON.stringify(obj).length * 2; // rough UTF-16 estimate
    } catch (_) {
      return MAX_SESSION_BYTES + 1; // can't measure, skip caching
    }
  }

  // ── Public API ────────────────────────────────────────────────────────

  async function _putSessionListMeta(key, value) {
    var db = await _openDB();
    var tx = db.transaction(STORE_SESSION_LIST, 'readwrite');
    await _txPut(tx.objectStore(STORE_SESSION_LIST), key, value);
  }

  /**
   * Cache the session list response from /api/sessions.
   * No-op when caching is disabled (opt-in default-off).
   */
  async function cacheSessionList(data) {
    if (!_isEnabled()) return;
    try {
      var db = await _openDB();
      var payload = {
        data: data,
        cachedAt: Date.now(),
      };
      var tx = db.transaction(STORE_SESSION_LIST, 'readwrite');
      await _txPut(tx.objectStore(STORE_SESSION_LIST), 'current', payload);
    } catch (_) {
      // non-fatal — caching is best-effort
    }
  }

  /**
   * Retrieve the cached session list.
   * Returns { data, cachedAt } or null.
   */
  async function getCachedSessionList() {
    if (!_isEnabled()) return null;
    try {
      var db = await _openDB();
      var tx = db.transaction(STORE_SESSION_LIST, 'readonly');
      return await _txGet(tx.objectStore(STORE_SESSION_LIST), 'current');
    } catch (_) {
      return null;
    }
  }

  /**
   * Cache a single session's conversation data.
   * Sanitizes media (fail-closed) and skips sessions that are too large.
   * No-op when caching is disabled (opt-in default-off).
   */
  async function cacheSession(sessionId, data) {
    if (!_isEnabled()) return;
    if (!sessionId || !data) return;
    try {
      var stripped = _sanitizeForCache(data);
      if (!stripped) return; // sanitization failed — fail closed
      if (_byteLength(stripped) > MAX_SESSION_BYTES) return;

      var db = await _openDB();
      var payload = {
        data: stripped,
        cachedAt: Date.now(),
      };
      var tx = db.transaction(STORE_SESSIONS, 'readwrite');
      await _txPut(tx.objectStore(STORE_SESSIONS), sessionId, payload);

      // Evict oldest sessions if over limit
      await _evictExcess(db);
    } catch (_) {
      // non-fatal
    }
  }

  /**
   * Retrieve a cached session by ID.
   * Returns { data, cachedAt } or null.
   */
  async function getCachedSession(sessionId) {
    if (!_isEnabled()) return null;
    if (!sessionId) return null;
    try {
      var db = await _openDB();
      var tx = db.transaction(STORE_SESSIONS, 'readonly');
      return await _txGet(tx.objectStore(STORE_SESSIONS), sessionId);
    } catch (_) {
      return null;
    }
  }

  /**
   * Get a list of all cached session IDs.
   */
  async function getCachedSessionIds() {
    if (!_isEnabled()) return [];
    try {
      var db = await _openDB();
      var tx = db.transaction(STORE_SESSIONS, 'readonly');
      return await _txKeys(tx.objectStore(STORE_SESSIONS));
    } catch (_) {
      return [];
    }
  }

  /**
   * Evict oldest cached sessions beyond MAX_CACHED_SESSIONS.
   * Uses cachedAt timestamp to determine age.
   * Runs entirely within a single readwrite transaction to avoid
   * auto-commit between the key scan and the delete loop.
   */
  async function _evictExcess(db) {
    try {
      var tx = db.transaction(STORE_SESSIONS, 'readwrite');
      var store = tx.objectStore(STORE_SESSIONS);

      // Collect all entries via cursor — keeps transaction alive
      var entries = [];
      await new Promise(function (resolve, reject) {
        var req = store.openCursor();
        req.onsuccess = function () {
          var cursor = req.result;
          if (cursor) {
            var val = cursor.value;
            if (val && val.cachedAt) {
              entries.push({ key: cursor.key, cachedAt: val.cachedAt });
            }
            cursor.continue();
          } else {
            resolve();
          }
        };
        req.onerror = function () { reject(req.error); };
      });

      if (entries.length <= MAX_CACHED_SESSIONS) return;

      // Sort newest-first, evict the tail
      entries.sort(function (a, b) { return b.cachedAt - a.cachedAt; });
      var toEvict = entries.slice(MAX_CACHED_SESSIONS);
      // Queue ALL delete requests synchronously before awaiting any of them.
      // IndexedDB transactions auto-commit when the call stack returns to the
      // event loop with no pending requests — sequential awaits between deletes
      // cause TransactionInactiveError on strict implementations (iOS Safari).
      var deletePromises = [];
      for (var i = 0; i < toEvict.length; i++) {
        deletePromises.push(_txDelete(store, toEvict[i].key));
      }
      await Promise.all(deletePromises);
    } catch (_) {
      // non-fatal
    }
  }

  /**
   * Clear the entire cache for the current profile scope.
   * Also deletes the legacy unscoped DB if it still exists.
   */
  async function nukeCache() {
    try {
      // Close and reopen to ensure we're hitting the scoped DB
      var db = await _openDB();
      var tx1 = db.transaction(STORE_SESSION_LIST, 'readwrite');
      await _txClear(tx1.objectStore(STORE_SESSION_LIST));
      var tx2 = db.transaction(STORE_SESSIONS, 'readwrite');
      await _txClear(tx2.objectStore(STORE_SESSIONS));
    } catch (_) {
      // non-fatal
    }
    // Also wipe legacy DB if present
    _deleteLegacyDB();
  }

  /**
   * Background-warm the N most recent sessions.
   * Called after a successful sidebar load. Fetches and caches the
   * conversation data for the newest sessions so they're available offline.
   * Silent — no UI feedback, no error toasts.
   * No-op when caching is disabled (opt-in default-off).
   */
  async function warmRecentSessions(sessionsList) {
    if (!_isEnabled()) return;
    if (!Array.isArray(sessionsList) || !sessionsList.length) return;
    try {
      // Pick the N most recent by updated_at (or fallback to first N)
      var sorted = sessionsList
        .filter(function (s) { return s && s.session_id; })
        .sort(function (a, b) {
          var ta = new Date(a.updated_at || a.created_at || 0).getTime();
          var tb = new Date(b.updated_at || b.created_at || 0).getTime();
          return tb - ta;
        })
        .slice(0, WARM_COUNT);

      // Fetch each session's conversation data silently
      for (var i = 0; i < sorted.length; i++) {
        try {
          var session = sorted[i];
          var rel = 'api/session?session_id=' +
            encodeURIComponent(session.session_id) +
            '&messages=1&resolve_model=0';
          var url = new URL(rel, document.baseURI || location.href);
          var res = await fetch(url.href, { credentials: 'include' });
          if (res.ok) {
            var data = await res.json();
            await cacheSession(session.session_id, data);
          }
        } catch (_) {
          // skip this session
        }
      }
    } catch (_) {
      // non-fatal
    }
  }

  /**
   * Format a relative time string for the cache timestamp.
   */
  function formatCacheAge(cachedAt) {
    if (!cachedAt) return 'earlier';
    var ageMs = Date.now() - cachedAt;
    var mins = Math.floor(ageMs / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + 'h ago';
    var days = Math.floor(hours / 24);
    return days + 'd ago';
  }

  // ── Offline banner ────────────────────────────────────────────────────
  var _bannerEl = null;
  var _bannerTimer = null;

  function _ensureBanner() {
    if (_bannerEl) return _bannerEl;
    _bannerEl = document.getElementById('offline-cache-banner');
    if (!_bannerEl) {
      _bannerEl = document.createElement('div');
      _bannerEl.id = 'offline-cache-banner';
      _bannerEl.style.cssText =
        'display:none;position:fixed;top:0;left:0;right:0;z-index:10000;' +
        'background:#3a2a0a;color:#f0c040;font-size:13px;padding:6px 16px;' +
        'text-align:center;font-family:system-ui,sans-serif;' +
        'border-bottom:1px solid #5a4a1a;pointer-events:none;' +
        'transition:opacity 0.3s;';
      document.body.appendChild(_bannerEl);
    }
    return _bannerEl;
  }

  function showOfflineBanner(cachedAt) {
    var banner = _ensureBanner();
    banner.textContent =
      'Offline — showing cached conversations (updated ' +
      formatCacheAge(cachedAt) +
      ')';
    banner.style.display = 'block';
    banner.style.opacity = '1';

    // Auto-hide after 5 seconds (stays in DOM, just fades)
    if (_bannerTimer) clearTimeout(_bannerTimer);
    _bannerTimer = setTimeout(function () {
      banner.style.opacity = '0';
      setTimeout(function () {
        if (banner.style.opacity === '0') banner.style.display = 'none';
      }, 300);
    }, 5000);
  }

  function hideOfflineBanner() {
    if (_bannerEl) {
      _bannerEl.style.opacity = '0';
      setTimeout(function () {
        if (_bannerEl && _bannerEl.style.opacity === '0')
          _bannerEl.style.display = 'none';
      }, 300);
    }
  }

  // Listen for connection state changes — hide banner when back online
  if (typeof window !== 'undefined') {
    window.addEventListener('online', function () {
      hideOfflineBanner();
    });
    window.addEventListener('hermes:pwa-connection-change', function (e) {
      if (e && e.detail && e.detail.online) {
        hideOfflineBanner();
      }
    });
  }

  // ── Export ────────────────────────────────────────────────────────────
  window.HermesOfflineCache = {
    cacheSessionList: cacheSessionList,
    getCachedSessionList: getCachedSessionList,
    cacheSession: cacheSession,
    getCachedSession: getCachedSession,
    getCachedSessionIds: getCachedSessionIds,
    warmRecentSessions: warmRecentSessions,
    nukeCache: nukeCache,
    setScope: setScope,
    setEnabled: setEnabled,
    isEnabled: isEnabled,
    showOfflineBanner: showOfflineBanner,
    hideOfflineBanner: hideOfflineBanner,
    formatCacheAge: formatCacheAge,
    WARM_COUNT: WARM_COUNT,
  };
})();
