// Password-manager unlock control.
// Shows a small lock button only when the active profile has an external vault
// manager (Bitwarden / 1Password) that needs unlocking. The master password is
// sent once to /api/vault/unlock and is not kept in the page.
(function () {
  'use strict';
  var tr = function (k, fb) { return (typeof t === 'function' && t(k) !== k) ? t(k) : fb; };

  function api(path, body) {
    var opts = { credentials: 'same-origin' };
    if (body) {
      opts.method = 'POST';
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (r) { return r.json(); });
  }

  var btn = null, panel = null, backends = [], profile = null, seq = 0;

  function closePanel() { if (panel) { panel.remove(); panel = null; } }

  function refresh() {
    var mine = ++seq;
    return api('api/vault/status').then(function (s) {
      if (mine !== seq) return;  // a newer status request superseded this one
      var p = (s && s.profile) || null;
      if (p !== profile) closePanel();  // panel rows belong to the old profile
      profile = p;
      backends = (s && s.backends) || [];
      btn.hidden = !backends.length;
      var allOpen = backends.length && backends.every(function (b) { return b.unlocked; });
      btn.textContent = allOpen ? '🔓' : '🔒';
      var label = backends.map(function (b) {
        return b.display_name + ': ' + (b.unlocked ? tr('vault_unlocked', 'unlocked') : tr('vault_locked', 'locked'));
      }).join('\n');
      btn.title = label;
      btn.setAttribute('aria-label', tr('vault_unlock_title', 'Password manager') + (label ? ' — ' + label : ''));
    }).catch(function () { btn.hidden = true; });
  }

  function row(b) {
    var el = document.createElement('div');
    el.className = 'vault-unlock-row';
    var name = document.createElement('strong');
    name.textContent = b.display_name;
    el.appendChild(name);
    if (b.unlocked) {
      var lockBtn = document.createElement('button');
      lockBtn.type = 'button';
      lockBtn.textContent = tr('vault_lock_btn', 'Lock');
      lockBtn.onclick = function () {
        api('api/vault/lock', { backend: b.name, profile: profile }).then(function () { closePanel(); refresh(); },
          function () { closePanel(); refresh(); });
      };
      el.appendChild(lockBtn);
      return el;
    }
    var form = document.createElement('form');
    var input = document.createElement('input');
    input.type = 'password';
    input.autocomplete = 'off';
    input.placeholder = tr('vault_master_password', 'Master password');
    var submit = document.createElement('button');
    submit.type = 'submit';
    submit.textContent = tr('vault_unlock_btn', 'Unlock');
    var msg = document.createElement('div');
    msg.className = 'vault-unlock-msg';
    msg.setAttribute('role', 'status');
    form.appendChild(input); form.appendChild(submit);
    form.onsubmit = function (e) {
      e.preventDefault();
      var pw = input.value;
      input.value = '';
      if (!pw) return;
      submit.disabled = true;
      msg.textContent = tr('vault_unlocking', 'Unlocking…');
      api('api/vault/unlock', { backend: b.name, master_password: pw, profile: profile }).then(function (r) {
        if (r && r.success) { closePanel(); refresh(); return; }
        if (r && r.error && /profile changed/i.test(r.error)) { closePanel(); refresh(); return; }
        msg.textContent = (r && r.error) || tr('vault_unlock_failed', 'Unlock failed');
      }).catch(function () {
        msg.textContent = tr('vault_unlock_failed', 'Unlock failed');
      }).then(function () { submit.disabled = false; });
      pw = '';
    };
    el.appendChild(form); el.appendChild(msg);
    setTimeout(function () { input.focus(); }, 0);
    return el;
  }

  function togglePanel() {
    if (panel) { closePanel(); return; }
    // Re-read status first so the rows always match the current profile.
    refresh().then(openPanel);
  }

  function openPanel() {
    if (panel || !backends.length) return;
    panel = document.createElement('div');
    panel.className = 'vault-unlock-panel';
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-label', tr('vault_unlock_title', 'Password manager'));
    backends.forEach(function (b) { panel.appendChild(row(b)); });
    document.body.appendChild(panel);
  }

  function init() {
    btn = document.createElement('button');
    btn.type = 'button';
    // Reuse the titlebar icon-button look; fall back to a floating button if
    // the titlebar is absent (e.g. a stripped-down embed).
    var reload = document.getElementById('btnReload');
    btn.className = reload ? 'app-titlebar-reload vault-unlock-btn' : 'vault-unlock-btn vault-unlock-btn--floating';
    btn.id = 'btnVaultUnlock';
    btn.hidden = true;
    btn.onclick = togglePanel;
    if (reload) reload.parentNode.insertBefore(btn, reload);
    else document.body.appendChild(btn);
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePanel(); });
    // Profile switches do not reload the page. Watch the app's active
    // profile and drop the panel as soon as it changes; the server also
    // rejects actions carrying a stale profile (409).
    window.addEventListener('focus', refresh);
    setInterval(function () {
      var cur = (typeof S !== 'undefined' && S && S.activeProfile) || null;
      if (cur && profile && cur !== profile) { closePanel(); refresh(); }
    }, 1000);
    refresh();
    setInterval(refresh, 60000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
