let _dashboardLoaded = false;

async function loadDashboard() {
  const root = document.getElementById('mainDashboard');
  if (!root) return;
  const now = new Date();
  const stamp = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  document.querySelectorAll('[data-dashboard-updated-at]').forEach(el => { el.textContent = stamp; });
  if (_dashboardLoaded) return;
  _dashboardLoaded = true;
}
