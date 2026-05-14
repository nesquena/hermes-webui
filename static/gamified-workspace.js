/**
 * Gamified 2D Workspace - Map-Style AI City
 * Transforms workspace zones into an interactive pixel city simulation
 */

class GamifiedWorkspace {
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
        uptime: '12h',
        position: { x: 0, y: 0 }
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
        uptime: '11h',
        position: { x: 0, y: 0 }
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
        uptime: '9h',
        position: { x: 0, y: 0 }
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
        uptime: '13h',
        position: { x: 0, y: 0 }
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
        uptime: '8h',
        position: { x: 0, y: 0 }
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
        uptime: '10h',
        position: { x: 0, y: 0 }
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
        uptime: '6h',
        position: { x: 0, y: 0 }
      }
    ];

    this.zoomLevel = 1;
    this.isPaused = false;
    this.selectedAgent = null;
    this.movingAgents = new Set();
    this.animationFrameId = null;
    
    this.init();
  }

  init() {
    console.log('[GamifiedWorkspace] Initializing gamified workspace...');
    this.setupEventListeners();
    this.renderAgents();
    this.startSimulation();
    this.updateWorkspaceStatus();
    this.startAnimationLoop();
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

    // Zone interactions
    document.querySelectorAll('.workspace-zone').forEach(zone => {
      zone.addEventListener('click', (e) => {
        if (e.target.closest('.workspace-agent')) return;
        this.onZoneClick(zone);
      });
      
      zone.addEventListener('mouseenter', () => {
        if (!zone.classList.contains('zone-highlight')) {
          zone.style.transform = 'translateY(-4px) scale(1.02)';
        }
      });
      
      zone.addEventListener('mouseleave', () => {
        if (!zone.classList.contains('zone-highlight')) {
          zone.style.transform = '';
        }
      });
    });

    // Mobile touch handling
    if ('ontouchstart' in window) {
      document.addEventListener('touchend', () => {
        // Clear any stuck hover states on mobile
        document.querySelectorAll('.workspace-zone').forEach(zone => {
          zone.style.transform = '';
        });
      });
    }

    // Keyboard navigation
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.selectedAgent) {
        this.closeAgentInfo();
      }
    });
  }

  renderAgents() {
    // Clear existing agents
    document.querySelectorAll('.agent-container').forEach(container => {
      container.innerHTML = '';
    });

    // Position and render each agent in their zone
    this.agents.forEach(agent => {
      const agentElement = this.createAgentElement(agent);
      const container = document.getElementById(`agents-${agent.zone}`);
      if (container) {
        this.positionAgentInZone(agent, container);
        container.appendChild(agentElement);
      }
    });

    console.log(`[GamifiedWorkspace] Rendered ${this.agents.length} agents`);
  }

  positionAgentInZone(agent, container) {
    // Get zone dimensions for absolute positioning
    const zone = container.closest('.workspace-zone');
    const zoneRect = zone.getBoundingClientRect();
    
    // Agent size
    const agentSize = 32;
    const padding = 15;
    
    // Calculate safe area (avoiding furniture)
    const safeWidth = zoneRect.width - (padding * 2) - agentSize;
    const safeHeight = zoneRect.height - (padding * 2) - agentSize - 50; // Account for header
    
    let attempts = 0;
    let validPosition = false;
    let x, y;

    while (!validPosition && attempts < 15) {
      // Generate random position within safe bounds
      x = padding + Math.random() * Math.max(0, safeWidth);
      y = 50 + padding + Math.random() * Math.max(0, safeHeight); // Start below header
      
      // Check for overlaps with other agents in the same zone
      const otherAgents = this.agents.filter(a => 
        a.id !== agent.id && 
        a.zone === agent.zone && 
        a.position
      );
      
      validPosition = otherAgents.every(other => {
        const distance = Math.sqrt(
          Math.pow(x - other.position.x, 2) + 
          Math.pow(y - other.position.y, 2)
        );
        return distance > agentSize + 8; // Minimum separation
      });
      
      attempts++;
    }

    // Fallback positioning if no valid position found
    if (!validPosition) {
      const agentsInZone = this.agents.filter(a => a.zone === agent.zone).length;
      const angle = (agentsInZone * 60) % 360; // Spread agents in circle
      const radius = Math.min(safeWidth, safeHeight) * 0.3;
      const centerX = zoneRect.width / 2;
      const centerY = (zoneRect.height / 2) + 25; // Offset for header
      
      x = centerX + Math.cos(angle * Math.PI / 180) * radius;
      y = centerY + Math.sin(angle * Math.PI / 180) * radius;
      
      // Ensure within bounds
      x = Math.max(padding, Math.min(x, zoneRect.width - agentSize - padding));
      y = Math.max(50 + padding, Math.min(y, zoneRect.height - agentSize - padding));
    }

    agent.position = { x, y };
  }

  createAgentElement(agent) {
    const agentDiv = document.createElement('div');
    agentDiv.className = `workspace-agent ${agent.role} status-${agent.status}`;
    
    // Use initials or emoji for avatar
    const avatarText = this.getAgentAvatar(agent);
    agentDiv.textContent = avatarText;
    agentDiv.title = `${agent.name} - ${agent.status}`;
    agentDiv.dataset.agentId = agent.id;
    agentDiv.dataset.name = agent.name; // For label display
    agentDiv.tabIndex = 0; // Make keyboard accessible

    // Position the agent
    if (agent.position) {
      agentDiv.style.position = 'absolute';
      agentDiv.style.left = `${agent.position.x}px`;
      agentDiv.style.top = `${agent.position.y}px`;
    }

    // Event handlers
    agentDiv.addEventListener('click', (e) => {
      e.stopPropagation();
      this.selectAgent(agent);
    });

    agentDiv.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        this.selectAgent(agent);
      }
    });

    // Enhanced touch handling for mobile
    if ('ontouchstart' in window) {
      let touchStartTime;
      let touchMoved = false;
      
      agentDiv.addEventListener('touchstart', (e) => {
        touchStartTime = Date.now();
        touchMoved = false;
        // Add visual feedback
        agentDiv.style.transform = 'scale(1.1)';
      });
      
      agentDiv.addEventListener('touchmove', () => {
        touchMoved = true;
        agentDiv.style.transform = '';
      });
      
      agentDiv.addEventListener('touchend', (e) => {
        agentDiv.style.transform = '';
        if (!touchMoved && Date.now() - touchStartTime < 300) { // Quick tap
          e.preventDefault();
          this.selectAgent(agent);
        }
      });
      
      agentDiv.addEventListener('touchcancel', () => {
        agentDiv.style.transform = '';
      });
    }

    return agentDiv;
  }

  getAgentAvatar(agent) {
    // Create more meaningful avatars based on role and name
    const roleEmojis = {
      coordinator: '👑',
      developer: '👨‍💻', 
      devops: '⚙️',
      researcher: '🔬',
      analyst: '📊',
      security: '🛡️'
    };

    // Use emoji if available, otherwise use initials
    if (roleEmojis[agent.role]) {
      return roleEmojis[agent.role];
    }

    // Fallback to initials
    return agent.name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2);
  }

  selectAgent(agent) {
    this.selectedAgent = agent;
    this.showAgentInfo(agent);
    
    // Visual selection feedback
    document.querySelectorAll('.workspace-agent').forEach(el => {
      el.classList.remove('selected');
    });
    
    const agentElement = document.querySelector(`[data-agent-id="${agent.id}"]`);
    if (agentElement) {
      agentElement.classList.add('selected');
      agentElement.focus();
    }
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
    
    // Ensure panel is visible on mobile
    if (window.innerWidth <= 768) {
      panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
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
    
    console.log(`[GamifiedWorkspace] Zone clicked: ${zoneName}, Agents: ${agentsInZone.length}`);
    
    // Add highlight animation
    zone.classList.add('zone-highlight');
    setTimeout(() => {
      zone.classList.remove('zone-highlight');
    }, 1000);
  }

  moveAgentToZone(agent, newZone) {
    if (this.movingAgents.has(agent.id)) return; // Already moving

    const oldZone = agent.zone;
    if (oldZone === newZone) return;

    this.movingAgents.add(agent.id);
    
    // Update agent data
    agent.zone = newZone;
    
    // Add movement animation class
    const agentElement = document.querySelector(`[data-agent-id="${agent.id}"]`);
    if (agentElement) {
      agentElement.classList.add('moving');
      
      // Enhanced trail effect for movement
      agentElement.classList.add('agent-trail');
      setTimeout(() => {
        agentElement.classList.remove('agent-trail');
      }, 1200);
    }

    // Smoother re-render with better timing
    setTimeout(() => {
      this.renderAgents();
      this.movingAgents.delete(agent.id);
      
      if (agentElement) {
        agentElement.classList.remove('moving');
      }
      
      console.log(`[GamifiedWorkspace] ${agent.name} moved from ${oldZone} to ${newZone}`);
    }, 1500);
  }

  // Enhanced zoom functionality with mobile considerations
  applyZoom() {
    const map = document.getElementById('workspaceMap');
    if (!map) return;

    // Disable zoom on mobile to prevent layout issues
    if (window.innerWidth <= 768) {
      map.style.transform = 'none';
      return;
    }

    map.style.transform = `scale(${this.zoomLevel})`;
    map.style.transformOrigin = 'center';
    
    // Adjust container scrolling if needed
    const container = map.closest('.workspace-2d-container');
    if (container && this.zoomLevel > 1) {
      container.style.overflow = 'auto';
    } else if (container) {
      container.style.overflow = 'hidden';
    }
  }

  startSimulation() {
    if (this.isPaused) return;

    // Agent status changes every 30 seconds
    setInterval(() => {
      if (this.isPaused) return;
      this.simulateAgentActivity();
    }, 30000);

    // Agent task updates every 2 minutes
    setInterval(() => {
      if (this.isPaused) return;
      this.updateAgentTasks();
    }, 120000);

    // Occasional agent movements every 5 minutes
    setInterval(() => {
      if (this.isPaused) return;
      this.simulateAgentMovement();
    }, 300000);
  }

  simulateAgentActivity() {
    // Randomly change some agent statuses
    const activeAgents = this.agents.filter(agent => 
      agent.status !== 'idle' && !this.movingAgents.has(agent.id)
    );
    
    if (activeAgents.length > 0) {
      const randomAgent = activeAgents[Math.floor(Math.random() * activeAgents.length)];
      const statuses = ['online', 'busy'];
      const newStatus = statuses[Math.random() > 0.5 ? 0 : 1];
      
      if (randomAgent.status !== newStatus) {
        randomAgent.status = newStatus;
        this.updateAgentDisplay(randomAgent);
        console.log(`[GamifiedWorkspace] ${randomAgent.name} status changed to ${newStatus}`);
      }
    }
  }

  simulateAgentMovement() {
    // Occasionally move an agent to meeting or lounge
    if (Math.random() > 0.7) {
      const workingAgents = this.agents.filter(agent => 
        agent.zone !== 'meeting' && 
        agent.zone !== 'lounge' && 
        !this.movingAgents.has(agent.id)
      );
      
      if (workingAgents.length === 0) return;

      const randomAgent = workingAgents[Math.floor(Math.random() * workingAgents.length)];
      const originalZone = randomAgent.zone;
      const destination = Math.random() > 0.5 ? 'meeting' : 'lounge';
      
      // Move to destination
      randomAgent.currentTask = destination === 'meeting' 
        ? 'Attending team meeting' 
        : 'Taking a break';
      randomAgent.status = 'busy';
      
      this.moveAgentToZone(randomAgent, destination);
      
      // Return after some time
      setTimeout(() => {
        if (!this.movingAgents.has(randomAgent.id)) {
          randomAgent.status = 'online';
          this.updateAgentTask(randomAgent);
          this.moveAgentToZone(randomAgent, originalZone);
        }
      }, 180000 + Math.random() * 120000); // 3-5 minutes
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

    this.updateWorkspaceStatus();
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
      if (this.selectedAgent && this.selectedAgent.id === agent.id) {
        agentElement.classList.add('selected');
      }
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

  startAnimationLoop() {
    const animate = () => {
      if (!this.isPaused) {
        // Add subtle animations and updates here if needed
        this.updateWorkspaceStatus();
      }
      
      this.animationFrameId = requestAnimationFrame(animate);
    };
    
    animate();
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
      map.style.transformOrigin = 'center';
      
      // Adjust container scrolling if needed
      const container = map.closest('.workspace-2d-container');
      if (container && this.zoomLevel > 1) {
        container.style.overflow = 'auto';
      } else if (container) {
        container.style.overflow = 'hidden';
      }
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

    console.log(`[GamifiedWorkspace] Simulation ${this.isPaused ? 'paused' : 'resumed'}`);
  }

  destroy() {
    if (this.animationFrameId) {
      cancelAnimationFrame(this.animationFrameId);
    }
    this.movingAgents.clear();
    console.log('[GamifiedWorkspace] Workspace destroyed');
  }
}

// Global function for closing agent info
function closeAgentInfo() {
  if (window.gamifiedWorkspace) {
    window.gamifiedWorkspace.closeAgentInfo();
  }
}

// Initialize the gamified workspace
function initGamifiedWorkspace() {
  console.log('[GamifiedWorkspace] Initializing...');
  
  // Clean up existing instance
  if (window.gamifiedWorkspace) {
    window.gamifiedWorkspace.destroy();
  }
  
  window.gamifiedWorkspace = new GamifiedWorkspace();
}

// Auto-initialize when panel is shown
function loadOffice() {
  console.log('[dashboard] Loading gamified office workspace...');
  
  // Small delay to ensure DOM is ready
  setTimeout(() => {
    initGamifiedWorkspace();
  }, 100);
}

// Export for manual initialization if needed
if (typeof module !== 'undefined' && module.exports) {
  module.exports = GamifiedWorkspace;
}