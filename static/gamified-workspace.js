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

    // Responsive positioning on window resize
    let resizeTimeout;
    window.addEventListener('resize', () => {
      clearTimeout(resizeTimeout);
      resizeTimeout = setTimeout(() => {
        this.renderAgents(); // Re-position agents on resize
        this.applyZoom(); // Adjust zoom for new screen size
      }, 150);
    });
  }

  renderAgents() {
    // Clear existing agents from workspace-map
    const workspaceMap = document.getElementById('workspaceMap');
    if (!workspaceMap) return;
    
    // Remove any existing agent elements
    workspaceMap.querySelectorAll('.workspace-agent').forEach(el => el.remove());

    // Position and render each agent directly in workspace-map
    this.agents.forEach(agent => {
      const agentElement = this.createAgentElement(agent);
      this.positionAgentInWorkspace(agent, agentElement);
      workspaceMap.appendChild(agentElement);
    });

    console.log(`[GamifiedWorkspace] Rendered ${this.agents.length} agents in workspace-map`);
  }

  positionAgentInWorkspace(agent, agentElement) {
    // Get workspace map dimensions for responsive positioning
    const workspaceMap = document.getElementById('workspaceMap');
    if (!workspaceMap) return;

    const mapRect = workspaceMap.getBoundingClientRect();
    const mapWidth = mapRect.width;
    const mapHeight = mapRect.height;

    // Position agents directly in workspace-map based on their zone
    // with small random offsets inside room boundaries
    
    const zonePositions = {
      engineering: { 
        baseX: 80 + 50, // room left + offset from wall
        baseY: 80 + 60, // room top + header + offset
        maxX: 80 + 340 - 50, // room left + width - offset
        maxY: 80 + 260 - 50  // room top + height - offset
      },
      research: { 
        baseX: Math.max(mapWidth - 80 - 340 + 50, 450), // right side
        baseY: 80 + 60,
        maxX: Math.max(mapWidth - 80 - 50, 500),
        maxY: 80 + 260 - 50
      },
      operations: { 
        baseX: 80 + 50,
        baseY: Math.max(mapHeight - 200 - 260 + 60, 400), // bottom side
        maxX: 80 + 340 - 50,
        maxY: Math.max(mapHeight - 200 - 50, 450)
      },
      meeting: { 
        baseX: Math.max(mapWidth - 80 - 340 + 50, 450),
        baseY: Math.max(mapHeight - 200 - 260 + 60, 400),
        maxX: Math.max(mapWidth - 80 - 50, 500),
        maxY: Math.max(mapHeight - 200 - 50, 450)
      },
      lounge: { 
        baseX: Math.max((mapWidth / 2) - 210 + 50, 150), // center - half width + offset
        baseY: Math.max(mapHeight - 80 - 120 + 40, 500),
        maxX: Math.max((mapWidth / 2) + 210 - 50, 300),
        maxY: Math.max(mapHeight - 80 - 40, 550)
      }
    };

    const zonePos = zonePositions[agent.zone];
    if (!zonePos) {
      console.warn(`No position defined for zone: ${agent.zone}`);
      return;
    }

    // Add small random offset within room boundaries (±20px)
    const randomOffsetX = (Math.random() - 0.5) * 40; // -20 to +20
    const randomOffsetY = (Math.random() - 0.5) * 40; // -20 to +20
    
    // Calculate final position
    let finalX = zonePos.baseX + randomOffsetX;
    let finalY = zonePos.baseY + randomOffsetY;
    
    // Ensure agent stays within room boundaries
    finalX = Math.max(zonePos.baseX, Math.min(finalX, zonePos.maxX - 32));
    finalY = Math.max(zonePos.baseY, Math.min(finalY, zonePos.maxY - 32));

    // Apply positioning with CSS attributes for sprite rendering
    agentElement.style.position = 'absolute';
    agentElement.style.left = `${finalX}px`;
    agentElement.style.top = `${finalY}px`;
    agentElement.setAttribute('data-zone', agent.zone);

    // Store position for future reference
    agent.position = { x: finalX, y: finalY };
  }

  createAgentElement(agent) {
    const agentDiv = document.createElement('div');
    agentDiv.className = `workspace-agent ${agent.role} status-${agent.status}`;
    
    // Use sprite-based rendering - text will be hidden by CSS for sprite roles
    const avatarText = this.getAgentAvatar(agent);
    agentDiv.textContent = avatarText;
    agentDiv.title = `${agent.name} - ${agent.status}`;
    agentDiv.dataset.agentId = agent.id;
    agentDiv.dataset.name = agent.name; // For label display
    agentDiv.dataset.zone = agent.zone; // For CSS positioning
    agentDiv.tabIndex = 0; // Make keyboard accessible

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
    // For sprite-based agents, we still need fallback text
    // The CSS will handle showing sprites and hiding text
    const roleEmojis = {
      coordinator: '👑',
      developer: '💻', 
      devops: '⚙️',
      researcher: '🔬',
      analyst: '📊',
      security: '🛡️'
    };

    // Use emoji if available, otherwise use meaningful initials
    if (roleEmojis[agent.role]) {
      return roleEmojis[agent.role];
    }

    // Enhanced fallback to role-based initials
    const roleInitials = {
      coordinator: 'CO',
      developer: agent.name.includes('Alpha') ? 'D1' : 'D2',
      devops: 'DO',
      researcher: 'RS',
      analyst: 'AN',
      security: 'SC'
    };

    return roleInitials[agent.role] || agent.name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2);
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
    
    // Get agent element
    const agentElement = document.querySelector(`[data-agent-id="${agent.id}"]`);
    if (agentElement) {
      agentElement.classList.add('moving');
      
      // Enhanced trail effect for movement
      agentElement.classList.add('agent-trail');
      setTimeout(() => {
        agentElement.classList.remove('agent-trail');
      }, 1200);
    }

    // Re-position agent with smooth transition
    setTimeout(() => {
      if (agentElement) {
        // Calculate new position
        this.positionAgentInWorkspace(agent, agentElement);
        
        // Update zone data attribute
        agentElement.setAttribute('data-zone', newZone);
        
        this.movingAgents.delete(agent.id);
        agentElement.classList.remove('moving');
        
        console.log(`[GamifiedWorkspace] ${agent.name} moved from ${oldZone} to ${newZone}`);
      }
    }, 600);
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