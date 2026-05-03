/**
 * Hermes WebUI Service Worker
 * Minimal PWA service worker — enables "Add to Home Screen".
 * No offline caching of API responses (the UI requires a live backend).
 * Caches only static shell assets so the app shell loads fast on repeat visits.
 */

// Cache version is injected by the server at request time (routes.py /sw.js handler).
// Bumps automatically whenever the git commit changes — no manual edits needed.
const CACHE_NAME = 'hermes-shell-__CACHE_VERSION__';

// Static assets that form the app shell.
//
// Versioned assets (CSS + JS) include `?v=__CACHE_VERSION__` to match the
// query string the page sends — see index.html. Without the version query
// here, every cache lookup against `?v=...` URLs would miss and fall through
// to network, defeating the pre-cache.
//
// Unversioned assets (`./`, manifest.json, favicons) are referenced from
// index.html without a cache-bust query, so they stay unversioned here too.
const VQ = '?v=__CACHE_VERSION__';
const SHELL_ASSETS = [
  './',
  './static/style.css' + VQ,
  './static/boot.js' + VQ,
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
  './static/favicon.svg',
  './static/favicon-32.png',
  './manifest.json',
];

// Install: pre-cache the app shell
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(SHELL_ASSETS).catch((err) => {
        // Non-fatal: if any asset fails, still activate
        console.warn('[sw] Shell pre-cache partial failure:', err);
      });
    })
  );
  self.skipWaiting();
});

// Activate: clean up old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// Fetch strategy:
// - API calls (/api/*, /stream) → always network (never cache)
// - Shell assets → cache-first with network fallback
// - Everything else → network-first, fall back to offline page
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never intercept cross-origin requests
  if (url.origin !== self.location.origin) return;

  // Never intercept the service worker script itself. Returning a cached sw.js
  // prevents the browser from seeing a new cache version after local patches.
  if (url.pathname.endsWith('/sw.js')) return;

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

  // Shell assets: cache-first
  event.respondWith(
    caches.match(event.request).then((cached) => {
      if (cached) return cached;
      return fetch(event.request).then((response) => {
        // Cache successful GET responses for shell assets
        if (
          event.request.method === 'GET' &&
          response.status === 200
        ) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => {
        // Offline fallback for navigation requests.
        // Note: caches.match() returns a Promise (always truthy in a `||` check),
        // so we must await/then to unwrap it — otherwise the `new Response(...)`
        // branch is dead code and the browser falls back to its default offline page.
        if (event.request.mode === 'navigate') {
          return caches.match('./').then((cached) => cached || new Response(
            '<html><body style="font-family:sans-serif;padding:2rem;background:#1a1a1a;color:#ccc">' +
            '<h2>You are offline</h2>' +
            '<p>Hermes requires a server connection. Please check your network and try again.</p>' +
            '</body></html>',
            { headers: { 'Content-Type': 'text/html' } }
          ));
        }
      });
    })
  );
});
