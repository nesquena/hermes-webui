/* >>> hermes-fork: iframe bearer-from-hash shim (HermesOS Cloud)
 *
 * Allows the WebUI to be embedded in an iframe inside dashboard.hermesos.cloud
 * without relying on third-party cookies (Chrome M125+ partitions / blocks).
 *
 * The dashboard mints `https://<vm>/#iframe_token=<bearer>` and points the
 * iframe at it. On first paint THIS script:
 *
 *   1. Reads `#iframe_token=` from location.hash
 *   2. Stores the bearer in sessionStorage (survives in-app navigation)
 *   3. Wipes the hash so the bearer doesn't sit in the URL bar / history
 *   4. Monkey-patches the three transports the WebUI uses so every same-origin
 *      request carries the bearer:
 *        - `fetch()` — adds `Authorization: Bearer <token>` header
 *        - `XMLHttpRequest.open` — same header on `.send()`
 *        - `EventSource` — appends `?token=<bearer>` to the URL (EventSource
 *          can't set headers; the WebUI's Caddy MUST have a `@authQueryToken
 *          query token=<bearer>` matcher for SSE auth to land)
 *        - `WebSocket` — same `?token=<bearer>` URL approach
 *
 * Idempotent: re-running the shim (e.g. if the dashboard re-mints a token)
 * picks up the new value and keeps the patched transports.
 *
 * MUST be the FIRST executable script in index.html — every other JS on the
 * page is allowed to issue fetch/XHR/SSE/WS calls during its own init.
 *
 * Direct-URL users (typing the gateway URL into a tab, no iframe parent) do
 * not have a hash token and are unaffected: the shim no-ops out, leaving the
 * cookie-based / forward_auth paths to handle them.
 */
(function () {
  if (window.__hermesIframeShimInstalled) {
    // Already installed (e.g. shim shipped twice). Refresh token if a new hash
    // arrived but keep the existing wrappers in place.
    var existingMatch = (window.location.hash || '').match(/iframe_token=([^&]+)/);
    if (existingMatch) {
      try {
        sessionStorage.setItem('hermes-iframe-token', decodeURIComponent(existingMatch[1]));
        window.history.replaceState(null, '', window.location.pathname + window.location.search);
      } catch (e) { /* ignore */ }
    }
    return;
  }
  window.__hermesIframeShimInstalled = true;

  // Token lifecycle ----------------------------------------------------------
  var STORAGE_KEY = 'hermes-iframe-token';
  function readTokenFromHash() {
    try {
      var hash = window.location.hash || '';
      var m = hash.match(/iframe_token=([^&]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    } catch (e) { return null; }
  }
  function readToken() {
    try {
      return sessionStorage.getItem(STORAGE_KEY) || null;
    } catch (e) { return null; }
  }
  function persistToken(token) {
    try { sessionStorage.setItem(STORAGE_KEY, token); } catch (e) { /* ignore */ }
  }
  function wipeHash() {
    try { window.history.replaceState(null, '', window.location.pathname + window.location.search); } catch (e) { /* ignore */ }
  }

  var hashToken = readTokenFromHash();
  if (hashToken) {
    persistToken(hashToken);
    wipeHash();
  }

  // Re-read every call so token rotations (e.g. dashboard mints fresh URL,
  // reloads the iframe) take effect without needing to re-install the shim.
  function currentToken() { return readToken(); }

  // Same-origin guard --------------------------------------------------------
  // The shim only attaches the bearer to requests landing on this WebUI's own
  // origin. Cross-origin requests (e.g. CDN font fetches, analytics) must NOT
  // see the bearer.
  function isSameOrigin(url) {
    if (!url) return false;
    try {
      // Relative URLs ("/api/x", "./x", "x") are always same-origin.
      if (typeof url === 'string') {
        if (url[0] === '/' && url[1] !== '/') return true;
        if (!/^[a-z][a-z0-9+.-]*:/i.test(url)) return true;
      }
      var parsed = (typeof url === 'string') ? new URL(url, window.location.origin) : url;
      return parsed.origin === window.location.origin;
    } catch (e) { return false; }
  }

  function appendQueryToken(rawUrl, token) {
    try {
      var u = new URL(rawUrl, window.location.origin);
      if (!u.searchParams.has('token')) {
        u.searchParams.set('token', token);
      }
      return u.toString();
    } catch (e) {
      // Fallback string concatenation if URL parsing fails.
      var sep = rawUrl.indexOf('?') >= 0 ? '&' : '?';
      return rawUrl + sep + 'token=' + encodeURIComponent(token);
    }
  }

  // fetch() patch ------------------------------------------------------------
  if (typeof window.fetch === 'function') {
    var origFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      var token = currentToken();
      var urlForCheck = (typeof input === 'string') ? input : (input && input.url) || '';
      if (token && isSameOrigin(urlForCheck)) {
        init = init || {};
        var headers = new Headers(init.headers || (input && input.headers) || undefined);
        if (!headers.has('Authorization')) {
          headers.set('Authorization', 'Bearer ' + token);
        }
        init.headers = headers;
      }
      return origFetch(input, init);
    };
  }

  // XMLHttpRequest.open patch -----------------------------------------------
  if (typeof window.XMLHttpRequest === 'function') {
    var XhrProto = window.XMLHttpRequest.prototype;
    var origOpen = XhrProto.open;
    var origSend = XhrProto.send;
    XhrProto.open = function (method, url) {
      this.__hermesShimUrl = url;
      return origOpen.apply(this, arguments);
    };
    XhrProto.send = function () {
      var token = currentToken();
      if (token && isSameOrigin(this.__hermesShimUrl)) {
        try { this.setRequestHeader('Authorization', 'Bearer ' + token); } catch (e) { /* opened-but-already-sent */ }
      }
      return origSend.apply(this, arguments);
    };
  }

  // EventSource patch --------------------------------------------------------
  // EventSource has no header API, so for same-origin SSE we append the token
  // as a `?token=` query param. The WebUI's Caddy must accept that via an
  // `@authQueryToken query token=<bearer>` matcher.
  if (typeof window.EventSource === 'function') {
    var OrigEventSource = window.EventSource;
    function PatchedEventSource(url, config) {
      var token = currentToken();
      var finalUrl = (token && isSameOrigin(url)) ? appendQueryToken(url, token) : url;
      return new OrigEventSource(finalUrl, config);
    }
    PatchedEventSource.prototype = OrigEventSource.prototype;
    PatchedEventSource.CONNECTING = OrigEventSource.CONNECTING;
    PatchedEventSource.OPEN = OrigEventSource.OPEN;
    PatchedEventSource.CLOSED = OrigEventSource.CLOSED;
    window.EventSource = PatchedEventSource;
  }

  // WebSocket patch ----------------------------------------------------------
  // Same story as EventSource — no header API in the constructor. Patch the
  // URL with ?token= for ws:/wss: targets on the same host.
  if (typeof window.WebSocket === 'function') {
    var OrigWebSocket = window.WebSocket;
    function PatchedWebSocket(url, protocols) {
      var token = currentToken();
      var finalUrl = url;
      try {
        var u = new URL(url, window.location.origin);
        var sameHost = u.host === window.location.host;
        if (token && sameHost && !u.searchParams.has('token')) {
          u.searchParams.set('token', token);
          finalUrl = u.toString();
        }
      } catch (e) { /* ignore — pass raw url through */ }
      return protocols ? new OrigWebSocket(finalUrl, protocols) : new OrigWebSocket(finalUrl);
    }
    PatchedWebSocket.prototype = OrigWebSocket.prototype;
    PatchedWebSocket.CONNECTING = OrigWebSocket.CONNECTING;
    PatchedWebSocket.OPEN = OrigWebSocket.OPEN;
    PatchedWebSocket.CLOSING = OrigWebSocket.CLOSING;
    PatchedWebSocket.CLOSED = OrigWebSocket.CLOSED;
    window.WebSocket = PatchedWebSocket;
  }
})();
/* <<< hermes-fork */
