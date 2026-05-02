let _dashboardLoaded = false;

function _getGreetingKey() {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return 'greeting_good_morning';
  if (h >= 12 && h < 18) return 'greeting_good_afternoon';
  return 'greeting_good_evening';
}

function _t(key) {
  if (typeof t === 'function') return t(key);
  const lang = (localStorage.getItem('hermes-lang') || 'en');
  if (typeof TRANSLATIONS === 'object' && TRANSLATIONS[lang]) return TRANSLATIONS[lang][key] || key;
  return key;
}

async function loadDashboard() {
  const root = document.getElementById('mainDashboard');
  if (!root) return;

  const now = new Date();
  const stamp = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  document.querySelectorAll('[data-dashboard-updated-at]').forEach(el => { el.textContent = stamp; });

  const greetingEl = document.getElementById('heroGreetingTime');
  if (greetingEl) greetingEl.textContent = _t(_getGreetingKey());

  if (_dashboardLoaded) return;
  _dashboardLoaded = true;
}
