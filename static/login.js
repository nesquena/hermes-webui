/* Login page — external script, no inline handlers.
 * Loaded by the /login route. Reads data attributes from the form for
 * i18n strings so the server does not need to inject JS literals.
 */
document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('login-form');
  var input = document.getElementById('pw');
  var passkeyBtn = document.getElementById('passkey-login');

  if (!form || !input) return;

  var invalidPw = form.getAttribute('data-invalid-pw') || 'Invalid password';
  var connFailed = form.getAttribute('data-conn-failed') || 'Connection failed';

  function showErr(msg) {
    var err = document.getElementById('err');
    if (err) { err.textContent = msg; err.style.display = 'block'; }
  }

  function hideErr() {
    var err = document.getElementById('err');
    if (err) { err.style.display = 'none'; }
  }

  // Return the ?next= redirect path if present and safe, otherwise './'
  // Guards against open-redirect: rejects protocol-relative (//evil.com),
  // absolute URLs, backslash variants, and control characters.
  function _safeNextPath() {
    try {
      var raw = new URL(window.location.href).searchParams.get('next');
      if (!raw) return './';
      if (raw.charAt(0) !== '/') return './';             // must be path-absolute
      if (raw.charAt(1) === '/' || raw.charAt(1) === '\\') return './'; // reject // and \\
      if (/[\x00-\x1f\x7f\s]/.test(raw)) return './';  // reject control chars / whitespace
      // #5578: never redirect back to the login page — that self-referential
      // chain is what grows the URL exponentially on repeated expired-auth
      // bounces. Detect the login route even through nested percent-encoding
      // (a nested chain looks like `/session/login%3Fnext%3D...`, where the `?`
      // is encoded so a plain split('?') wouldn't isolate the path). Decode a
      // few levels and check the leading PATH. Only collapse login-route chains
      // — a legitimate non-login path that merely carries its own `next=` query
      // key must still round-trip.
      if (raw.length > 2048) return './';
      var probe = raw;
      var stabilized = false;
      for (var i = 0; i < 8; i++) {
        var pathOnly = probe.split('?')[0].split('#')[0].split('&')[0].replace(/\/+$/, '');
        if (pathOnly === '/login' || /\/login$/.test(pathOnly)) return './';
        var decoded;
        try { decoded = decodeURIComponent(probe); } catch (_) { stabilized = true; break; }
        if (decoded === probe) { stabilized = true; break; }
        probe = decoded;
      }
      // If still decoding at the cap (pathologically deep encoding), fail closed.
      if (!stabilized) return './';
      return raw;
    } catch (_) { return './'; }
  }

  async function doLogin(e) {
    e.preventDefault();
    var pw = input.value;
    hideErr();
    try {
      var res = await fetch('api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw }),
        credentials: 'include',
      });
      var data = {};
      try { data = await res.json(); } catch (_) {}
      if (res.ok && data.ok) {
        window.location.href = _safeNextPath();
      } else {
        showErr(data.error || invalidPw);
      }
    } catch (ex) {
      showErr(connFailed);
    }
  }

  form.addEventListener('submit', doLogin);

  function b64uToBytes(s) {
    s = String(s || '').replace(/-/g, '+').replace(/_/g, '/');
    while (s.length % 4) s += '=';
    var bin = atob(s);
    var out = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function bytesToB64u(buf) {
    var bytes = new Uint8Array(buf);
    var bin = '';
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }

  async function doPasskeyLogin() {
    if (!window.PublicKeyCredential || !navigator.credentials) return;
    hideErr();
    try {
      passkeyBtn.disabled = true;
      var optRes = await fetch('api/auth/passkey/options', { method: 'POST', body: '{}', credentials: 'include' });
      var optData = await optRes.json();
      if (!optRes.ok || !optData.publicKey) throw new Error(optData.error || 'Passkey unavailable');
      var pk = optData.publicKey;
      pk.challenge = b64uToBytes(pk.challenge);
      if (Array.isArray(pk.allowCredentials)) {
        pk.allowCredentials = pk.allowCredentials.map(function (c) { return Object.assign({}, c, { id: b64uToBytes(c.id) }); });
      }
      var cred = await navigator.credentials.get({ publicKey: pk });
      if (!cred) throw new Error('Passkey sign-in cancelled');
      var payload = {
        id: cred.id,
        rawId: bytesToB64u(cred.rawId),
        type: cred.type,
        response: {
          authenticatorData: bytesToB64u(cred.response.authenticatorData),
          clientDataJSON: bytesToB64u(cred.response.clientDataJSON),
          signature: bytesToB64u(cred.response.signature),
          userHandle: cred.response.userHandle ? bytesToB64u(cred.response.userHandle) : null,
        },
      };
      var res = await fetch('api/auth/passkey/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload), credentials: 'include',
      });
      var data = {};
      try { data = await res.json(); } catch (_) {}
      if (res.ok && data.ok) window.location.href = _safeNextPath();
      else showErr(data.error || invalidPw);
    } catch (ex) {
      showErr(ex && ex.message ? ex.message : connFailed);
    } finally {
      passkeyBtn.disabled = false;
    }
  }

  if (passkeyBtn && window.PublicKeyCredential && navigator.credentials) {
    fetch('api/auth/status', { credentials: 'include' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (s) { if (s && s.passkeys_enabled) passkeyBtn.style.display = 'block'; })
      .catch(function () {});
    passkeyBtn.addEventListener('click', doPasskeyLogin);
  }

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      doLogin(e);
    }
  });

  // On page load, probe the server so we can distinguish "can't reach server"
  // (Tailscale off, wrong network) from "session expired / need to log in".
  // Uses /health — public for WebUI auth, but deployment access proxies may
  // require same-origin cookies before the request reaches WebUI.
  // If unreachable, retries every 3 s and auto-reloads once the server is back.
  (function checkConnectivity() {
    var retryTimer = null;

    function setFormDisabled(disabled) {
      if (input) input.disabled = disabled;
      var btn = form.querySelector('button');
      if (btn) btn.disabled = disabled;
    }

    function probe() {
      fetch('health', { method: 'GET', credentials: 'same-origin' })
        .then(function (r) {
          if (r.ok) {
            // Server is reachable — if we were in retry mode, reload so the
            // page reflects the correct auth state (expired session, etc.).
            if (retryTimer !== null) {
              clearInterval(retryTimer);
              retryTimer = null;
              window.location.reload();
            }
          } else {
            showErr(connFailed + ' (server error ' + r.status + ')');
          }
        })
        .catch(function () {
          showErr('Cannot reach server — check your VPN / Tailscale connection.');
          setFormDisabled(true);
          // Keep retrying so the page auto-recovers once the network is back.
          if (retryTimer === null) {
            retryTimer = setInterval(probe, 3000);
          }
        });
    }

    probe();
  })();
});
