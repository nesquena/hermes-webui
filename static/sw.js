/**
 * SynthPulse Control Service Worker
 * Minimal PWA service worker — enables "Add to Home Screen".
 * No offline caching of API responses (the UI requires a live backend).
 * Caches only static shell assets so the app shell loads fast on repeat visits.
 */

// Cache version is injected by the server at request time (routes.py /sw.js handler).
// Bumps automatically whenever the git commit changes — no manual edits needed.
const CACHE_NAME = 'synthpulse-shell-__WEBUI_VERSION__';

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
  './static/workspace-clarity.css' + VQ,
  './static/workspace-shell.css' + VQ,
  './static/workspace-shell.js' + VQ,
  './static/bot-workbench.css' + VQ,
  './static/bot-workbench.js' + VQ,
  './static/conversation-workbench.js' + VQ,
  './static/conversation-workbench.css' + VQ,
  './static/pwa-startup.js' + VQ,
  './static/boot.js' + VQ,
  './static/assistant_turn_anchors.js' + VQ,
  './static/svg_visuals.js' + VQ,
  './static/svg_visuals.css' + VQ,
  './static/interactive-ui/kit-core.js' + VQ,
  './static/interactive-ui/kinds/compare.js' + VQ,
  './static/interactive-ui/kinds/flow.js' + VQ,
  './static/interactive-ui/kinds/funnel.js' + VQ,
  './static/interactive_ui.js' + VQ,
  './static/interactive_ui_map.js' + VQ,
  './static/interactive_ui.css' + VQ,
  './static/artifacts.js' + VQ,
  './static/artifacts.css' + VQ,
  './static/cron-notify.css' + VQ,
  './static/error-reporter.js' + VQ,
  './static/group-routing.css' + VQ,
  './static/help-content.js' + VQ,
  './static/help-tour.js' + VQ,
  './static/help-tour.css' + VQ,
  './static/help-guide.css' + VQ,
  './static/help-guide-loader.js' + VQ,
  './static/vendor/guida/0.2.0/guida.umd.js' + VQ,
  './static/memory_dashboard.js' + VQ,
  './static/memory_dashboard.css' + VQ,
  './static/ui.js' + VQ,
  './static/messages.js' + VQ,
  './static/sessions.js' + VQ,
  './static/sp-picker.js' + VQ,
  './static/panels.js' + VQ,
  './static/module-content.js' + VQ,
  './static/apps.js' + VQ,
  './static/apps.css' + VQ,
  './static/modules.js' + VQ,
  './static/modules.css' + VQ,
  './static/commands.js' + VQ,
  './static/icons.js' + VQ,
  './static/i18n.js' + VQ,
  './static/workspace.js' + VQ,
  './static/terminal.js' + VQ,
  './static/onboarding.js' + VQ,
  // Every deferred script index.html loads. Before this list only covered
  // the core bundle; the remaining panels (governance, projects, bots,
  // integrations, voice) fell through to the network on every PWA start.
  './static/skill_learning_activity.js' + VQ,
  './static/subagent-progress.js' + VQ,
  './static/tool-friendly.js' + VQ,
  './static/subagent-threads.js' + VQ,
  './static/sp-account.js' + VQ,
  './static/sp-ux.js' + VQ,
  './static/projects-folders.js' + VQ,
  './static/extension_settings.js' + VQ,
  './static/interaction_preferences.js' + VQ,
  './static/chat-bots.js' + VQ,
  './static/bot-knowledge.js' + VQ,
  './static/bot-builder.js' + VQ,
  './static/governance.js' + VQ,
  './static/integrations.js' + VQ,
  './static/projects.js' + VQ,
  './static/split.js' + VQ,
  './static/realtime_voice.js' + VQ,
  './static/outline.js' + VQ,
  './static/vendor/smd.min.js' + VQ,
  './static/vendor/katex/0.16.22/katex.min.css' + VQ,
  './static/vendor/katex/0.16.22/katex.min.js' + VQ,
  './static/synthwave-symbol.png',
  './static/favicon.svg',
  './static/favicon-32.png',
  './manifest.json',
];

// Optional catalogs are cached only after use, never during installation.
const LAZY_ASSETS=['help-features.js','help-msp-features.js','help-feature-targets.js','help-guide.js'].map(name=>'./static/'+name+VQ);

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
          '<p>SynthPulse requires a server connection. Please check your network and try again.</p>' +
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
  if (!SHELL_ASSETS.includes(shellPath) && !LAZY_ASSETS.includes(shellPath)) return;

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
