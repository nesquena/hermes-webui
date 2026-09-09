async function loadStats() {
  const response = await fetch('/api/stats');
  const stats = await response.json();

  document.getElementById('stat-pairs').textContent = stats.monitoredPairs;
  document.getElementById('stat-proposals').textContent = stats.activeProposals;
  document.getElementById('stat-alerts').textContent = stats.alertsToday;
  document.getElementById('stat-rpc').textContent = stats.rpcStatus;
}

async function loadAlerts() {
  const response = await fetch('/api/alerts');
  const data = await response.json();
  const list = document.getElementById('alert-list');
  list.innerHTML = '';

  data.alerts.slice(0, 5).forEach((alert) => {
    const item = document.createElement('li');
    item.className = 'alert-item';
    // Build alert DOM safely using textContent to avoid stored XSS
    const container = document.createElement('div');

    const strong = document.createElement('strong');
    strong.textContent = alert.message;
    container.appendChild(strong);

    const meta = document.createElement('div');
    meta.className = 'alert-meta';
    meta.textContent = `${alert.type} • ${new Date(alert.timestamp).toLocaleString()}`;
    container.appendChild(meta);

    const level = alert.level || 'info';
    const pill = document.createElement('span');
    pill.className = `level-pill level-${level}`;
    pill.textContent = level;

    item.appendChild(container);
    item.appendChild(pill);
    list.appendChild(item);
  });
}

(async () => {
  await loadStats();
  await loadAlerts();
  setInterval(async () => {
    await loadStats();
    await loadAlerts();
  }, 15000);
})();
