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


/**
 * 2D Workspace Visualization
 * Handles agent positioning and interaction in the RimWorld-style office layout
 */

class Workspace2D {
  constructor() {
    this.agents = [
      {
        id: 'coordinator',
        name: 'Coordinator',
        role: 'coordinator',
        status: 'online',
        currentTask: 'Managing team workflows and coordinating tasks across zones',
        zone: 'operations',
        avatar: 'C',
        tasksCompleted: 45,
        uptime: '12h'
      },
      {
        id: 'developer-1',
        name: 'Developer Alpha',
        role: 'developer',
        status: 'busy',
        currentTask: 'Implementing new API endpoints and refactoring core modules',
        zone: 'engineering',
        avatar: 'D1',
        tasksCompleted: 38,
        uptime: '11h'
      },
      {
        id: 'developer-2',
        name: 'Developer Beta',
        role: 'developer',
        status: 'online',
        currentTask: 'Code review and bug fixes for the authentication system',
        zone: 'engineering',
        avatar: 'D2',
        tasksCompleted: 29,
        uptime: '9h'
      },
      {
        id: 'devops',
        name: 'DevOps Engineer',
        role: 'devops',
        status: 'online',
        currentTask: 'Monitoring server performance and deploying updates',
        zone: 'operations',
        avatar: 'Ops',
        tasksCompleted: 22,
        uptime: '13h'
      },
      {
        id: 'researcher',
        name: 'AI Researcher',
        role: 'researcher',
        status: 'online',
        currentTask: 'Analyzing user patterns and training new models',
        zone: 'research',
        avatar: 'R',
        tasksCompleted: 15,
        uptime: '8h'
      },
      {
        id: 'analyst',
        name: 'Data Analyst',
        role: 'analyst',
        status: 'busy',
        currentTask: 'Generating performance reports and metrics dashboards',
        zone: 'research',
        avatar: 'A',
        tasksCompleted: 31,
        uptime: '10h'
      },
      {
        id: 'security',
        name: 'Security Specialist',
        role: 'security',
        status: 'idle',
        currentTask: 'Running security audits and vulnerability assessments',
        zone: 'operations',
        avatar: 'S',
        tasksCompleted: 12,
        uptime: '6h'
      }
    ];

    this.zoomLevel = 1;
    this.isPaused = false;
    this.selectedAgent = null;
    
    this.init();
  }

  init() {
    console.log('[Workspace2D] Initializing 2D workspace...');
    this.renderAgents();
    this.setupEventListeners();
    this.startAgentSimulation();
    this.updateWorkspaceStatus();
  }

  setupEventListeners() {
    // Workspace controls
    const zoomInBtn = document.getElementById('zoomInBtn');
    const zoomOutBtn = document.getElementById('zoomOutBtn');
    const resetViewBtn = document.getElementById('resetViewBtn');
    const pauseBtn = document.getElementById('pauseBtn');

    if (zoomInBtn) zoomInBtn.addEventListener('click', () => this.zoomIn());
    if (zoomOutBtn) zoomOutBtn.addEventListener('click', () => this.zoomOut());
    if (resetViewBtn) resetViewBtn.addEventListener('click', () => this.resetView());
    if (pauseBtn) pauseBtn.addEventListener('click', () => this.togglePause());

    // Zone hover effects
    document.querySelectorAll('.workspace-zone').forEach(zone => {
      zone.addEventListener('click', (e) => {
        if (e.target.closest('.workspace-agent')) return;
        this.onZoneClick(zone);
      });
    });
  }

  renderAgents() {
    // Clear existing agents
    document.querySelectorAll('.agent-container').forEach(container => {
      container.innerHTML = '';
    });

    // Render agents in their assigned zones
    this.agents.forEach(agent => {
      const agentElement = this.createAgentElement(agent);
      const container = document.getElementById(`agents-${agent.zone}`);
      if (container) {
        container.appendChild(agentElement);
      }
    });

    console.log(`[Workspace2D] Rendered ${this.agents.length} agents`);
  }

  createAgentElement(agent) {
    const agentDiv = document.createElement('div');
    agentDiv.className = `workspace-agent ${agent.role} status-${agent.status}`;
    agentDiv.textContent = agent.avatar;
    agentDiv.title = `${agent.name} - ${agent.status}`;
    agentDiv.dataset.agentId = agent.id;

    agentDiv.addEventListener('click', (e) => {
      e.stopPropagation();
      this.selectAgent(agent);
    });

    return agentDiv;
  }

  selectAgent(agent) {
    this.selectedAgent = agent;
    this.showAgentInfo(agent);
    
    // Highlight selected agent
    document.querySelectorAll('.workspace-agent').forEach(el => {
      el.classList.remove('selected');
    });
    document.querySelector(`[data-agent-id="${agent.id}"]`)?.classList.add('selected');
  }

  showAgentInfo(agent) {
    const panel = document.getElementById('agentInfoPanel');
    const avatar = document.getElementById('selectedAgentAvatar');
    const name = document.getElementById('selectedAgentName');
    const role = document.getElementById('selectedAgentRole');
    const status = document.getElementById('selectedAgentStatus');
    const task = document.getElementById('selectedAgentTask');
    const tasks = document.getElementById('selectedAgentTasks');
    const uptime = document.getElementById('selectedAgentUptime');

    if (!panel) return;

    // Set avatar styling
    if (avatar) {
      avatar.textContent = agent.avatar;
      avatar.className = `agent-info-avatar ${agent.role}`;
    }

    // Set info text
    if (name) name.textContent = agent.name;
    if (role) role.textContent = agent.role.charAt(0).toUpperCase() + agent.role.slice(1);
    if (status) {
      status.textContent = agent.status.charAt(0).toUpperCase() + agent.status.slice(1);
      status.className = `agent-info-status ${agent.status}`;
    }
    if (task) task.textContent = agent.currentTask;
    if (tasks) tasks.textContent = agent.tasksCompleted;
    if (uptime) uptime.textContent = agent.uptime;

    panel.style.display = 'block';
  }

  closeAgentInfo() {
    const panel = document.getElementById('agentInfoPanel');
    if (panel) {
      panel.style.display = 'none';
    }
    
    // Remove selection highlight
    document.querySelectorAll('.workspace-agent').forEach(el => {
      el.classList.remove('selected');
    });
    
    this.selectedAgent = null;
  }

  onZoneClick(zone) {
    const zoneName = zone.id.replace('zone-', '');
    const agentsInZone = this.agents.filter(agent => agent.zone === zoneName);
    
    // Simple zone interaction - could expand this
    console.log(`[Workspace2D] Zone clicked: ${zoneName}, Agents: ${agentsInZone.length}`);
    
    // Briefly highlight the zone
    zone.style.transform = 'scale(1.02)';
    setTimeout(() => {
      zone.style.transform = '';
    }, 200);
  }

  startAgentSimulation() {
    if (this.isPaused) return;

    // Simulate agent status changes every 30 seconds
    setInterval(() => {
      if (this.isPaused) return;
      
      this.simulateAgentActivity();
    }, 30000);

    // Update agent tasks every 2 minutes
    setInterval(() => {
      if (this.isPaused) return;
      
      this.updateAgentTasks();
    }, 120000);
  }

  simulateAgentActivity() {
    // Randomly change some agent statuses
    const activeAgents = this.agents.filter(agent => agent.status !== 'idle');
    if (activeAgents.length > 0) {
      const randomAgent = activeAgents[Math.floor(Math.random() * activeAgents.length)];
      const statuses = ['online', 'busy'];
      const newStatus = statuses[Math.random() > 0.5 ? 0 : 1];
      
      if (randomAgent.status !== newStatus) {
        randomAgent.status = newStatus;
        this.updateAgentDisplay(randomAgent);
        console.log(`[Workspace2D] ${randomAgent.name} status changed to ${newStatus}`);
      }
    }

    // Occasionally move an agent to a different zone (meeting, lounge)
    if (Math.random() > 0.85) {
      this.moveAgentToMeeting();
    }
  }

  updateAgentTasks() {
    this.agents.forEach(agent => {
      if (agent.status === 'busy' || agent.status === 'online') {
        agent.tasksCompleted += Math.floor(Math.random() * 3);
        
        // Update uptime
        const currentUptime = parseInt(agent.uptime.replace('h', ''));
        agent.uptime = `${currentUptime + Math.floor(Math.random() * 2)}h`;
      }
    });

    // Update displayed info if an agent is selected
    if (this.selectedAgent) {
      this.showAgentInfo(this.selectedAgent);
    }
  }

  moveAgentToMeeting() {
    const workingAgents = this.agents.filter(agent => 
      agent.zone !== 'meeting' && agent.zone !== 'lounge'
    );
    
    if (workingAgents.length === 0) return;

    const randomAgent = workingAgents[Math.floor(Math.random() * workingAgents.length)];
    const originalZone = randomAgent.zone;
    
    // Move to meeting
    randomAgent.zone = 'meeting';
    randomAgent.currentTask = 'Attending team meeting';
    randomAgent.status = 'busy';
    
    this.renderAgents();
    console.log(`[Workspace2D] ${randomAgent.name} moved to meeting`);

    // Return after 5 minutes
    setTimeout(() => {
      randomAgent.zone = originalZone;
      randomAgent.status = 'online';
      this.updateAgentTask(randomAgent);
      this.renderAgents();
      console.log(`[Workspace2D] ${randomAgent.name} returned to ${originalZone}`);
    }, 300000); // 5 minutes
  }

  updateAgentTask(agent) {
    const tasks = {
      coordinator: ['Managing team workflows', 'Coordinating cross-zone activities', 'Planning sprint objectives'],
      developer: ['Implementing features', 'Code review and testing', 'Debugging system issues', 'Refactoring modules'],
      devops: ['Server maintenance', 'Deployment monitoring', 'Infrastructure scaling', 'Security updates'],
      researcher: ['Data analysis', 'Model training', 'Research documentation', 'Experiment design'],
      analyst: ['Performance metrics', 'Report generation', 'Data visualization', 'Trend analysis'],
      security: ['Security audit', 'Vulnerability scanning', 'Compliance checks', 'Threat monitoring']
    };

    const roleTasks = tasks[agent.role] || ['General tasks'];
    agent.currentTask = roleTasks[Math.floor(Math.random() * roleTasks.length)];
  }

  updateAgentDisplay(agent) {
    const agentElement = document.querySelector(`[data-agent-id="${agent.id}"]`);
    if (agentElement) {
      agentElement.className = `workspace-agent ${agent.role} status-${agent.status}`;
    }
  }

  updateWorkspaceStatus() {
    const statusElement = document.getElementById('workspaceStatus');
    if (!statusElement) return;

    const activeAgents = this.agents.filter(agent => agent.status !== 'idle').length;
    const totalAgents = this.agents.length;
    
    statusElement.textContent = `${activeAgents}/${totalAgents} AGENTS ACTIVE`;
    
    // Update zone statuses
    document.querySelectorAll('.workspace-zone').forEach(zone => {
      const zoneName = zone.id.replace('zone-', '');
      const zoneAgents = this.agents.filter(agent => agent.zone === zoneName);
      const activeInZone = zoneAgents.filter(agent => agent.status !== 'idle').length;
      
      const statusDot = zone.querySelector('.zone-status');
      if (statusDot) {
        statusDot.className = activeInZone > 0 ? 'zone-status active' : 'zone-status idle';
      }
    });
  }

  zoomIn() {
    this.zoomLevel = Math.min(this.zoomLevel + 0.1, 2);
    this.applyZoom();
  }

  zoomOut() {
    this.zoomLevel = Math.max(this.zoomLevel - 0.1, 0.5);
    this.applyZoom();
  }

  resetView() {
    this.zoomLevel = 1;
    this.applyZoom();
  }

  applyZoom() {
    const map = document.getElementById('workspaceMap');
    if (map) {
      map.style.transform = `scale(${this.zoomLevel})`;
      map.style.transformOrigin = 'top left';
    }
  }

  togglePause() {
    this.isPaused = !this.isPaused;
    const pauseBtn = document.getElementById('pauseBtn');
    
    if (pauseBtn) {
      if (this.isPaused) {
        pauseBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="5 3 19 12 5 21 5 3"/></svg>';
        pauseBtn.title = 'Resume Simulation';
      } else {
        pauseBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>';
        pauseBtn.title = 'Pause Simulation';
      }
    }

    console.log(`[Workspace2D] Simulation ${this.isPaused ? 'paused' : 'resumed'}`);
  }
}

// Global function for closing agent info (called from HTML)
function closeAgentInfo() {
  if (window.workspace2d) {
    window.workspace2d.closeAgentInfo();
  }
}

// Initialize gamified workspace when dashboard loads
function loadOffice() {
  console.log('[dashboard] Loading gamified office workspace...');
  
  // Clean up existing 2D workspace
  if (window.workspace2d) {
    window.workspace2d = null;
  }
  
  // Initialize the gamified workspace
  if (typeof initGamifiedWorkspace === 'function') {
    initGamifiedWorkspace();
  } else {
    console.warn('[dashboard] Gamified workspace not available, falling back to basic 2D');
    if (!window.workspace2d) {
      window.workspace2d = new Workspace2D();
    }
  }
}

// Load office when the panel is switched to
document.addEventListener('DOMContentLoaded', () => {
  // Hook into panel switching to initialize office
  const originalSwitchPanel = window.switchPanel;
  if (originalSwitchPanel) {
    window.switchPanel = function(panelName, options) {
      originalSwitchPanel(panelName, options);
      
      if (panelName === 'office') {
        setTimeout(() => loadOffice(), 100);
      }
    };
  }
});