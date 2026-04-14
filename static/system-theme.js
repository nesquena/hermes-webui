/* system-theme.js — "System (auto)" theme support for prefers-color-scheme */
(function () {
  'use strict';

  const STORAGE_KEY = 'hermes-theme';
  const MQ = window.matchMedia('(prefers-color-scheme: dark)');

  /** Resolve a theme name to an effective light/dark value. */
  function resolveTheme(name) {
    if (name === 'system') return MQ.matches ? 'dark' : 'light';
    return name;
  }

  /** Apply a theme to the DOM and persist it. */
  function applyTheme(name) {
    localStorage.setItem(STORAGE_KEY, name);
    document.documentElement.dataset.theme = resolveTheme(name);
    // Persist to server
    fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: name })
    }).catch(function () {});
  }

  // Expose globally so the settings <select onchange="applyTheme(this.value)"> works
  window.applyTheme = applyTheme;

  // Listen for OS-level theme changes and update if the user chose "system"
  MQ.addEventListener('change', function () {
    var stored = localStorage.getItem(STORAGE_KEY);
    if (stored === 'system') {
      document.documentElement.dataset.theme = resolveTheme('system');
    }
  });
})();
