/* Login page — external script, no inline handlers.
 * Loaded by the /login route. Reads data attributes from the form for
 * i18n strings so the server does not need to inject JS literals.
 */
document.addEventListener('DOMContentLoaded', function () {
  var form = document.getElementById('login-form');
  var input = document.getElementById('pw');

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

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      doLogin(e);
    }
  });

  // On page load, probe the server so we can distinguish "can't reach server"
  // (Tailscale off, wrong network) from "session expired / need to log in".
  // Uses /health — a public endpoint, no auth required.
  // If unreachable, retries every 3 s and auto-reloads once the server is back.
  (function checkConnectivity() {
    var retryTimer = null;

    function setFormDisabled(disabled) {
      if (input) input.disabled = disabled;
      var btn = form.querySelector('button');
      if (btn) btn.disabled = disabled;
    }

    function probe() {
      fetch('health', { method: 'GET', credentials: 'omit' })
        .then(function (r) {
          if (r.ok) {
            // Server is reachable — if we were in retry mode, reload so the
            // page reflects the correct auth state (expired session, etc.).
            if (retryTimer !== null) {
              clearTimeout(retryTimer);
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
