/**
 * Hermes WebUI Service Worker
 * Minimal PWA service worker — enables "Add to Home Screen".
 * No offline caching of API responses (the UI requires a live backend).
 * Caches only static shell assets so the app shell loads fast on repeat visits.
 */

// Cache version is injected by the server at request time (routes.py /sw.js handler).
// Bumps automatically whenever the git commit changes — no manual edits needed.
const CACHE_NAME = 'hermes-shell-__WEBUI_VERSION__';

// Static assets that form the app shell.
//
// Versioned assets (CSS + JS) include `?v=__WEBUI_VERSION__` to match the
// query string the page sends — see index.html. Without the version query
// here, every cache lookup against `?v=...` URLs would miss and fall through
// to network, defeating the pre-cache.
//
// Do not pre-cache './' or login assets here: under password auth they can be
// either the authenticated app shell or login code, and stale cached responses
// can make valid password submits fail until the user clears browser cache.
// Navigations populate './' only after a successful non-redirect network load.
const VQ = '?v=__WEBUI_VERSION__';
const SHELL_ASSETS = [
  './static/style.css' + VQ,
  './static/pwa-startup.js' + VQ,
  './static/boot.js' + VQ,
  './static/assistant_turn_anchors.js' + VQ,
  './static/ui.js' + VQ,
  './static/messages.js' + VQ,
  './static/sessions.js' + VQ,
  './static/panels.js' + VQ,
  './static/commands.js' + VQ,
  './static/icons.js' + VQ,
  './static/i18n.js' + VQ,
  './static/workspace.js' + VQ,
  './static/terminal.js' + VQ,
  './static/onboarding.js' + VQ,
  './static/vendor/smd.min.js' + VQ,
  './static/vendor/katex/0.16.22/katex.min.css' + VQ,
  './static/vendor/katex/0.16.22/katex.min.js' + VQ,
  './static/favicon.svg',
  './static/favicon-32.png',
  './manifest.json',
];

function deleteOldShellCaches() {
  return caches.keys().then((keys) =>
    Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )
  );
}

// Install: prune old shell caches first, then pre-cache the app shell. Doing
// this before caches.open(CACHE_NAME) avoids a temporary double-cache window on
// quota-sensitive browsers during frequent version bumps.
self.addEventListener('install', (event) => {
  event.waitUntil(
    deleteOldShellCaches().then(() =>
      caches.open(CACHE_NAME).then((cache) => {
        return cache.addAll(SHELL_ASSETS).catch((err) => {
          // Non-fatal: if any asset fails, still activate
          console.warn('[sw] Shell pre-cache partial failure:', err);
        });
      })
    )
  );
  self.skipWaiting();
});

// Activate: keep the old-cache cleanup as a safety net in case install was
// interrupted or an older worker was already waiting.
self.addEventListener('activate', (event) => {
  event.waitUntil(deleteOldShellCaches());
  self.clients.claim();
});

// Fetch strategy:
// - API calls (/api/*, /stream) → always network (never cache)
// - Login assets → always network (never cache stale auth code)
// - Page navigations → network-first so auth redirects/cookies are honored
// - Shell assets → network-first with cache fallback
// - Everything else → network-only
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept cross-origin requests
  if (url.origin !== self.location.origin) return;

  // Never intercept the service worker script itself. Returning a cached sw.js
  // prevents the browser from seeing a new cache version after local patches.
  if (url.pathname.endsWith('/sw.js')) return;

  // Login assets must always hit the network. Older login.js builds have had
  // subpath-sensitive auth POST paths; if the service worker caches one, the
  // password can keep failing until the user manually clears browser cache.
  if (
    url.pathname.endsWith('/login') ||
    url.pathname.endsWith('/static/login.js')
  ) {
    return;
  }

  // API and streaming endpoints — always go to network.
  // The WebUI may be mounted under a subpath such as /hermes/, so API
  // requests can look like /hermes/api/sessions rather than /api/sessions.
  if (
    url.pathname.startsWith('/api/') ||
    url.pathname.includes('/api/') ||
    url.pathname.includes('/stream') ||
    url.pathname.startsWith('/health') ||
    url.pathname.includes('/health')
  ) {
    return; // let browser handle normally
  }

  // Page navigations must be network-first. A stale cached './' response can
  // otherwise hide the server's 302-to-login after auth expiry, or ignore a
  // freshly set login cookie until the user manually refreshes.
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(new Request(event.request, { cache: 'no-store' })).then((response) => {
        if (
          event.request.method === 'GET' &&
          response.status === 200 &&
          !response.redirected
        ) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put('./', clone));
        }
        return response;
      }).catch(() => {
        return caches.match('./').then((cached) => cached || new Response(
          '<html><body style="font-family:sans-serif;padding:2rem;background:#1a1a1a;color:#ccc">' +
          '<h2>You are offline</h2>' +
          '<p>Hermes requires a server connection. Please check your network and try again.</p>' +
          '</body></html>',
          { headers: { 'Content-Type': 'text/html' } }
        ));
      })
    );
    return;
  }

  // Only explicit shell assets are cached. Everything else should hit the
  // network so stale one-off files (especially auth/login scripts) do not get
  // trapped in CacheStorage until a manual cache clear.
  const scopePath = new URL(self.registration.scope).pathname;
  const relPath = url.pathname.startsWith(scopePath)
    ? url.pathname.slice(scopePath.length)
    : url.pathname.replace(/^\/+/, '');
  const shellPath = './' + relPath.replace(/^\/+/, '') + url.search;
  if (!SHELL_ASSETS.includes(shellPath)) return;

  // Shell assets: network-first with cache fallback. This keeps offline support
  // but avoids executing stale JS/CSS after a local hotfix when WEBUI_VERSION
  // has not changed yet (e.g. before a guarded restart updates the ?v token).
  event.respondWith(
    fetch(new Request(event.request, { cache: 'no-store' })).then((response) => {
      if (
        event.request.method === 'GET' &&
        response.status === 200
      ) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
      }
      return response;
    }).catch(() => caches.match(event.request).then((cached) => cached || new Response('Offline', {
      status: 503,
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    })))
  );
});

function _webPushScopeUrl(path) {
  const rel = String(path || '').replace(/^\/+/, '');
  return new URL(rel, self.registration.scope || './').href;
}

async function _webPushFetchJson(path, options = {}) {
  const {csrfToken = '', headers: extraHeaders = {}, ...fetchOptions} = options || {};
  const headers = {'Content-Type': 'application/json', ...extraHeaders};
  if (csrfToken) headers['X-Hermes-CSRF-Token'] = csrfToken;
  const response = await fetch(_webPushScopeUrl(path), {
    credentials: 'same-origin',
    headers,
    ...fetchOptions,
  });
  if (!response.ok) throw new Error(`Web Push route failed: ${response.status}`);
  const text = await response.text();
  return text ? JSON.parse(text) : {};
}

function _webPushUrlBase64ToUint8Array(base64String) {
  const normalized = String(base64String || '').replace(/-/g, '+').replace(/_/g, '/');
  const padding = '='.repeat((4 - normalized.length % 4) % 4);
  const raw = atob(normalized + padding);
  return Uint8Array.from(raw, (ch) => ch.charCodeAt(0));
}

function _normalizePushNotificationPayload(payload) {
  const normalized = payload && typeof payload === 'object' ? payload : {};
  const options = normalized.options && typeof normalized.options === 'object'
    ? { ...normalized.options }
    : {};
  if (!options.icon) options.icon = 'static/favicon-192.png';
  if (!options.badge) options.badge = 'static/favicon-32.png';
  if (!options.data || typeof options.data !== 'object') options.data = {};
  if (!options.data.url) options.data.url = './';
  return {
    title: normalized.title || 'Hermes',
    options,
  };
}

self.addEventListener('push', (event) => {
  const payload = (() => {
    if (!(event && event.data)) return _normalizePushNotificationPayload({});
    try {
      return _normalizePushNotificationPayload(event.data.json());
    } catch (_jsonErr) {
      return _normalizePushNotificationPayload({
        title: 'Hermes',
        options: { body: event.data.text() || '' },
      });
    }
  })();
  event.waitUntil((async () => {
    const scopePath = new URL(self.registration.scope || './').pathname;
    const clientList = self.clients && self.clients.matchAll
      ? await self.clients.matchAll({type: 'window', includeUncontrolled: true}).catch(() => [])
      : [];
    const hasVisibleClient = clientList.some((client) => {
      try {
        const clientUrl = new URL(client.url);
        return clientUrl.origin === self.location.origin &&
          clientUrl.pathname.startsWith(scopePath) &&
          (client.visibilityState === 'visible' || client.focused === true);
      } catch (_err) {
        return false;
      }
    });
    if (hasVisibleClient) return;
    return self.registration.showNotification(payload.title, payload.options);
  })());
});

self.addEventListener('pushsubscriptionchange', (event) => {
  event.waitUntil((async () => {
    try {
      const status = await _webPushFetchJson('/api/push/status');
      const csrfToken = typeof status.csrf_token === 'string' ? status.csrf_token : '';
      const oldEndpoint = event.oldSubscription && event.oldSubscription.endpoint;
      if (!status || !status.enabled) {
        if (oldEndpoint) {
          await _webPushFetchJson('/api/push/subscribe', {
            method: 'DELETE',
            csrfToken,
            body: JSON.stringify({ endpoint: oldEndpoint }),
          }).catch(() => {});
        }
        return;
      }
      let subscription = event.newSubscription || null;
      if (!subscription) {
        const keyData = await _webPushFetchJson('/api/push/vapid-public-key');
        subscription = await self.registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: _webPushUrlBase64ToUint8Array(keyData.public_key || ''),
        });
      }
      const payload = subscription && subscription.toJSON
        ? subscription.toJSON()
        : JSON.parse(JSON.stringify(subscription || {}));
      await _webPushFetchJson('/api/push/subscribe', {
        method: 'POST',
        csrfToken,
        body: JSON.stringify({ subscription: payload }),
      });
      if (oldEndpoint && payload.endpoint && oldEndpoint !== payload.endpoint) {
        await _webPushFetchJson('/api/push/subscribe', {
          method: 'DELETE',
          csrfToken,
          body: JSON.stringify({ endpoint: oldEndpoint }),
        }).catch(() => {});
      }
    } catch (_err) {}
  })());
});


self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const rawUrl = (event.notification.data && event.notification.data.url) || './';
  const targetUrl = new URL(rawUrl, self.registration.scope || './').href;
  const targetPath = new URL(targetUrl).pathname;
  const samePath = (clientUrl) => {
    try { return new URL(clientUrl).pathname === targetPath; } catch (_e) { return false; }
  };
  const sameOrigin = (clientUrl) => {
    try { return new URL(clientUrl).origin === self.location.origin; } catch (_e) { return false; }
  };
  event.waitUntil(
    self.clients.matchAll({type: 'window', includeUncontrolled: true}).then((clientList) => {
      // Match on pathname, not the full href: _sessionUrlForSid copies the
      // current page's query string + hash into the deep link, so an open tab
      // already on /session/<sid> would fail an exact-href match and spawn a
      // duplicate window.
      const targetClient = clientList.find((client) => samePath(client.url) && 'focus' in client);
      if (targetClient) return targetClient.focus();

      const openNotificationWindow = () => (
        self.clients.openWindow ? self.clients.openWindow(targetUrl) : undefined
      );
      const focusableClient = clientList.find((client) => sameOrigin(client.url) && 'focus' in client && 'navigate' in client);
      if (focusableClient && 'navigate' in focusableClient) {
        return focusableClient.navigate(targetUrl)
          .then((client) => (client && 'focus' in client ? client.focus() : focusableClient.focus()))
          .catch(() => focusableClient.focus());
      }
      return openNotificationWindow();
    })
  );
});
