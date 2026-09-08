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
    item.innerHTML = `
      <div>
        <strong>${alert.message}</strong>
        <div class="alert-meta">${alert.type} • ${new Date(alert.timestamp).toLocaleString()}</div>
      </div>
      <span class="level-pill level-${alert.level || 'info'}">${alert.level || 'info'}</span>
    `;
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
