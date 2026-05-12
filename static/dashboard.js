/**
 * Dashboard functionality for Hermes WebUI
 * Handles agent status monitoring, activity feed, and dashboard data management
 */

// Mock agent data for demonstration (replace with actual API calls)
const MOCK_AGENTS = [
  {
    id: 'agent-1',
    name: 'Nexus Prime',
    role: 'NEXUS',
    status: 'active',
    lastMessage: 'Processing workflow automation tasks',
    lastActive: '2 minutes ago',
    currentTask: 'Automating deployment pipeline'
  },
  {
    id: 'agent-2', 
    name: 'Forge Alpha',
    role: 'FORGE',
    status: 'idle',
    lastMessage: 'Code generation complete',
    lastActive: '15 minutes ago',
    currentTask: 'Standby'
  },
  {
    id: 'agent-3',
    name: 'Pixel Beta',
    role: 'PIXEL',
    status: 'active',
    lastMessage: 'Optimizing UI components',
    lastActive: '1 minute ago',
    currentTask: 'Dashboard theme refinements'
  },
  {
    id: 'agent-4',
    name: 'Scout Gamma',
    role: 'SCOUT',
    status: 'active',
    lastMessage: 'Repository analysis in progress',
    lastActive: '30 seconds ago',
    currentTask: 'Scanning codebase patterns'
  },
  {
    id: 'agent-5',
    name: 'Sage Delta',
    role: 'SAGE',
    status: 'error',
    lastMessage: 'Connection timeout detected',
    lastActive: '45 minutes ago',
    currentTask: 'Recovery mode'
  }
];

const MOCK_ACTIVITY = [
  { time: '14:32', text: 'Nexus Prime completed workflow automation' },
  { time: '14:30', text: 'Scout Gamma started repository scan' },
  { time: '14:28', text: 'Pixel Beta deployed UI updates' },
  { time: '14:25', text: 'Forge Alpha generated new components' },
  { time: '14:20', text: 'System health check passed' },
  { time: '14:15', text: 'Sage Delta connection restored' }
];

const MOCK_LOGS = [
  { level: 'info', time: '14:35:42', message: '[NEXUS] Task execution completed successfully' },
  { level: 'debug', time: '14:35:38', message: '[SCOUT] Analyzing file: src/dashboard.js' },
  { level: 'warning', time: '14:35:30', message: '[SAGE] Retry attempt 3/5 for connection' },
  { level: 'info', time: '14:35:25', message: '[PIXEL] UI theme applied: claw3d-dark' },
  { level: 'error', time: '14:35:20', message: '[SAGE] Connection timeout after 30s' },
  { level: 'info', time: '14:35:15', message: '[FORGE] Code generation module loaded' },
  { level: 'debug', time: '14:35:10', message: '[NEXUS] Workflow queue: 3 pending tasks' }
];

/**
 * Load dashboard overview with agent status grid and activity feed
 */
async function loadDashboard() {
  console.log('[dashboard] Loading dashboard overview...');
  await Promise.all([
    loadAgentGrid(),
    loadActivityFeed()
  ]);
}

/**
 * Populate the agent status grid
 */
function loadAgentGrid() {
  const grid = document.getElementById('agentGrid');
  if (!grid) return;

  grid.innerHTML = MOCK_AGENTS.map(agent => `
    <div class="agent-card role-${agent.role.toLowerCase()}">
      <div class="agent-card-header">
        <div class="agent-name">${agent.name}</div>
        <div class="agent-role ${agent.role.toLowerCase()}">${agent.role}</div>
      </div>
      <div class="agent-status">
        <div class="status-dot ${agent.status}"></div>
        <div class="status-text">${agent.status.toUpperCase()}</div>
      </div>
      <div class="agent-message">${agent.lastMessage}</div>
    </div>
  `).join('');
}

/**
 * Populate the activity feed
 */
function loadActivityFeed() {
  const activityList = document.getElementById('activityList');
  if (!activityList) return;

  activityList.innerHTML = MOCK_ACTIVITY.map(item => `
    <div class="activity-item">
      <div class="activity-time">${item.time}</div>
      <div class="activity-text">${item.text}</div>
    </div>
  `).join('');
}

/**
 * Load agents list view with detailed information
 */
async function loadAgents() {
  console.log('[dashboard] Loading agents list...');
  const agentsList = document.getElementById('agentsList');
  const agentCount = document.getElementById('agentCount');
  
  if (!agentsList) return;

  const activeCount = MOCK_AGENTS.filter(agent => agent.status === 'active').length;
  
  if (agentCount) {
    agentCount.textContent = `${activeCount}/${MOCK_AGENTS.length} ACTIVE`;
  }

  agentsList.innerHTML = MOCK_AGENTS.map(agent => `
    <div class="agent-list-item role-${agent.role.toLowerCase()}">
      <div class="status-dot ${agent.status}"></div>
      <div class="agent-info">
        <div class="agent-card-header">
          <div class="agent-name">${agent.name}</div>
          <div class="agent-role ${agent.role.toLowerCase()}">${agent.role}</div>
        </div>
        <div class="agent-meta">
          <span>Last active: ${agent.lastActive}</span>
          <span>•</span>
          <span>Status: ${agent.status.toUpperCase()}</span>
        </div>
        <div class="agent-task">Current: ${agent.currentTask}</div>
      </div>
    </div>
  `).join('');
}

/**
 * Load dashboard logs with color-coded severity
 */
async function loadDashboardLogs() {
  console.log('[dashboard] Loading dashboard logs...');
  const logsContent = document.getElementById('dashboardLogsContent');
  if (!logsContent) return;

  logsContent.innerHTML = MOCK_LOGS.map(log => `
    <div class="log-line ${log.level}">
      <span class="log-time">${log.time}</span>
      <span class="log-message">${log.message}</span>
    </div>
  `).join('');

  // Auto-scroll to bottom
  logsContent.scrollTop = logsContent.scrollHeight;
}

/**
 * Refresh dashboard logs
 */
function refreshDashboardLogs() {
  console.log('[dashboard] Refreshing logs...');
  loadDashboardLogs();
}

/**
 * Clear dashboard logs
 */
function clearDashboardLogs() {
  const logsContent = document.getElementById('dashboardLogsContent');
  if (logsContent) {
    logsContent.innerHTML = '<div class="log-line info">Logs cleared</div>';
  }
}

/**
 * Toggle log streaming pause/resume
 */
let logsPaused = false;
function toggleLogsPause() {
  logsPaused = !logsPaused;
  const button = document.querySelector('.logs-btn:last-child');
  if (button) {
    button.textContent = logsPaused ? 'RESUME' : 'PAUSE';
  }
  console.log(`[dashboard] Logs ${logsPaused ? 'paused' : 'resumed'}`);
}

/**
 * Apply dashboard theme to sidebar when in dashboard mode
 */
function applyDashboardTheme() {
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) {
    sidebar.classList.add('dashboard-mode');
  }
}

/**
 * Remove dashboard theme from sidebar
 */
function removeDashboardTheme() {
  const sidebar = document.querySelector('.sidebar');
  if (sidebar) {
    sidebar.classList.remove('dashboard-mode');
  }
}

/**
 * Check if current panel is a dashboard panel
 */
function isDashboardPanel(panelName) {
  return ['dashboard', 'agents', 'office', 'dashboardLogs'].includes(panelName);
}

// Export functions for global access
window.loadDashboard = loadDashboard;
window.loadAgents = loadAgents;
window.loadDashboardLogs = loadDashboardLogs;
window.refreshDashboardLogs = refreshDashboardLogs;
window.clearDashboardLogs = clearDashboardLogs;
window.toggleLogsPause = toggleLogsPause;
window.applyDashboardTheme = applyDashboardTheme;
window.removeDashboardTheme = removeDashboardTheme;
window.isDashboardPanel = isDashboardPanel;