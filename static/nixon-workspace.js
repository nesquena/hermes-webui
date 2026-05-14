// NIXON Workspace - Interactive Dashboard JavaScript

class NixonWorkspace {
    constructor() {
        this.agents = [
            {
                id: 'coordinator',
                name: 'Coordinator',
                role: 'Coordinator',
                status: 'working',
                currentTask: 'Managing workflows',
                room: 'command-center',
                tasks: ['Workflow optimization', 'Team coordination', 'Performance monitoring'],
                avatar: 'C'
            },
            {
                id: 'developer',
                name: 'Developer',
                role: 'Developer',
                status: 'working',
                currentTask: 'Building features',
                room: 'developer-zone',
                tasks: ['API development', 'Feature implementation', 'Code review'],
                avatar: 'D'
            },
            {
                id: 'developer-2',
                name: 'Developer 2',
                role: 'Developer',
                status: 'working',
                currentTask: 'Bug fixing',
                room: 'developer-zone',
                tasks: ['Bug fixes', 'Testing', 'Documentation'],
                avatar: 'D2'
            },
            {
                id: 'devops',
                name: 'DevOps',
                role: 'DevOps',
                status: 'online',
                currentTask: 'Server maintenance',
                room: 'server-room',
                tasks: ['Infrastructure monitoring', 'Deployment automation', 'Security patches'],
                avatar: 'O'
            },
            {
                id: 'researcher',
                name: 'Researcher',
                role: 'Researcher',
                status: 'researching',
                currentTask: 'Data analysis',
                room: 'research-lab',
                tasks: ['Market research', 'Data mining', 'Trend analysis'],
                avatar: 'R'
            },
            {
                id: 'analyst',
                name: 'Analyst',
                role: 'Analyst',
                status: 'researching',
                currentTask: 'Performance review',
                room: 'research-lab',
                tasks: ['Performance metrics', 'Usage analytics', 'Report generation'],
                avatar: 'A'
            },
            {
                id: 'security',
                name: 'Security',
                role: 'Security',
                status: 'idle',
                currentTask: 'Security audit',
                room: 'deployment-hub',
                tasks: ['Security scanning', 'Vulnerability assessment', 'Compliance check'],
                avatar: 'S'
            }
        ];

        this.tasks = [
            {
                id: 'api-optimization',
                title: 'API Optimization',
                progress: 75,
                priority: 'high',
                assignedAgent: 'developer',
                stage: 'testing'
            },
            {
                id: 'security-audit',
                title: 'Security Audit',
                progress: 45,
                priority: 'medium',
                assignedAgent: 'security',
                stage: 'development'
            },
            {
                id: 'documentation-update',
                title: 'Documentation Update',
                progress: 20,
                priority: 'low',
                assignedAgent: 'developer-2',
                stage: 'development'
            }
        ];

        this.systemMetrics = {
            uptime: 98.5,
            responseTime: 42,
            memory: 64,
            activeAgents: 7,
            completedTasks: 156,
            activeProjects: 12
        };

        this.currentPanel = 'Task Pipeline';
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.startAgentSimulation();
        this.startMetricsUpdates();
        this.setupPanelSwitching();
        this.setupAgentInteractions();
        this.updateDashboard();
        
        console.log('NIXON Workspace initialized successfully');
    }

    setupEventListeners() {
        // Navigation items
        document.querySelectorAll('.nav-item').forEach(item => {
            item.addEventListener('click', () => this.handleNavigation(item));
        });

        // Control buttons
        document.querySelectorAll('.control-btn').forEach(btn => {
            btn.addEventListener('click', () => this.handleControlAction(btn));
        });

        // Search functionality
        const searchInput = document.querySelector('.search-bar input');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => this.handleSearch(e.target.value));
        }

        // Room interactions
        document.querySelectorAll('.office-room').forEach(room => {
            room.addEventListener('click', () => this.handleRoomClick(room));
        });

        // Agent avatars
        document.querySelectorAll('.agent-avatar').forEach(avatar => {
            avatar.addEventListener('click', () => this.handleAgentClick(avatar));
        });
    }

    handleNavigation(navItem) {
        // Remove active class from all nav items
        document.querySelectorAll('.nav-item').forEach(item => {
            item.classList.remove('active');
        });
        
        // Add active class to clicked item
        navItem.classList.add('active');
        
        const section = navItem.querySelector('span').textContent;
        this.showSection(section);
        
        // Add visual feedback
        this.showNotification(`Switched to ${section}`);
    }

    showSection(section) {
        // This would typically show/hide different views
        console.log(`Navigating to: ${section}`);
        
        // Update workspace header
        const workspaceHeader = document.querySelector('.workspace-header h1');
        if (workspaceHeader) {
            workspaceHeader.textContent = `${section} - AI Operations Center`;
        }
    }

    handleControlAction(btn) {
        const action = btn.querySelector('span') ? btn.querySelector('span').textContent : btn.textContent;
        
        switch(action.trim()) {
            case 'Full View':
                this.toggleFullView();
                break;
            case 'Pause':
                this.pauseSimulation();
                break;
            case 'Run All':
                this.runAllAgents();
                break;
        }
        
        this.showNotification(`Action: ${action}`);
    }

    toggleFullView() {
        const leftSidebar = document.querySelector('.left-sidebar');
        const rightSidebar = document.querySelector('.right-sidebar');
        const bottomPanels = document.querySelector('.bottom-panels');
        
        [leftSidebar, rightSidebar, bottomPanels].forEach(element => {
            if (element) {
                element.style.display = element.style.display === 'none' ? '' : 'none';
            }
        });
    }

    pauseSimulation() {
        this.simulationPaused = !this.simulationPaused;
        const btn = document.querySelector('.control-btn:nth-child(2)');
        if (btn) {
            const icon = btn.querySelector('i');
            const text = btn.querySelector('span');
            if (this.simulationPaused) {
                icon.className = 'fas fa-play';
                text.textContent = 'Resume';
            } else {
                icon.className = 'fas fa-pause';
                text.textContent = 'Pause';
            }
        }
    }

    runAllAgents() {
        this.agents.forEach(agent => {
            agent.status = 'busy';
            this.updateAgentStatus(agent.id, 'busy');
        });
        
        setTimeout(() => {
            this.agents.forEach(agent => {
                agent.status = 'online';
                this.updateAgentStatus(agent.id, 'online');
            });
        }, 3000);
    }

    handleSearch(query) {
        try {
            if (!query) return;
            
            const results = [
                ...this.agents.filter(agent => 
                    agent.name.toLowerCase().includes(query.toLowerCase()) ||
                    agent.currentTask.toLowerCase().includes(query.toLowerCase())
                ),
                ...this.tasks.filter(task => 
                    task.title.toLowerCase().includes(query.toLowerCase())
                )
            ];
            
            console.log('Search results:', results);
            this.showNotification(`Found ${results.length} results for "${query}"`);
        } catch (error) {
            console.error('Search error:', error);
            this.showNotification('Search failed. Please try again.');
        }
    }

    handleRoomClick(room) {
        const roomName = room.querySelector('.room-header h3').textContent;
        this.highlightRoom(room);
        this.showNotification(`Viewing ${roomName}`);
        
        // Show room details
        this.showRoomDetails(roomName);
    }

    highlightRoom(room) {
        // Remove highlight from all rooms
        document.querySelectorAll('.office-room').forEach(r => {
            r.style.boxShadow = '';
            r.style.borderColor = '';
        });
        
        // Highlight selected room
        room.style.boxShadow = '0 0 0 3px rgba(37, 99, 235, 0.2)';
        room.style.borderColor = 'var(--color-primary)';
    }

    showRoomDetails(roomName) {
        const roomAgents = this.agents.filter(agent => 
            agent.room === roomName.toLowerCase().replace(/\s+/g, '-')
        );
        
        console.log(`${roomName} has ${roomAgents.length} agents:`, roomAgents);
    }

    handleAgentClick(avatar) {
        const agentRole = avatar.getAttribute('data-role');
        const agent = this.agents.find(a => a.role === agentRole);
        
        if (agent) {
            this.showAgentDetails(agent);
        }
    }

    showAgentDetails(agent) {
        // Create a modal or tooltip with agent details
        this.showNotification(`${agent.name}: ${agent.currentTask}`);
        
        // Animate the agent
        const agentElement = document.querySelector(`[data-role="${agent.role}"]`);
        if (agentElement) {
            agentElement.style.transform = 'scale(1.2)';
            setTimeout(() => {
                agentElement.style.transform = '';
            }, 300);
        }
        
        console.log('Agent details:', agent);
    }

    startAgentSimulation() {
        this.simulationInterval = setInterval(() => {
            try {
                if (this.simulationPaused) return;
                
                // Randomly update agent tasks and statuses
                this.agents.forEach(agent => {
                    if (Math.random() < 0.1) { // 10% chance to change task
                        const newTask = agent.tasks[Math.floor(Math.random() * agent.tasks.length)];
                        agent.currentTask = newTask;
                        this.updateAgentInSidebar(agent);
                    }
                    
                    if (Math.random() < 0.05) { // 5% chance to change status
                        const statuses = ['working', 'researching', 'meeting', 'idle', 'online'];
                        agent.status = statuses[Math.floor(Math.random() * statuses.length)];
                        this.updateAgentStatus(agent.id, agent.status);
                    }
                });
                
                // Update 2D workspace with current agent data
                renderAgents(this.agents);
                
                // Update task progress
                this.tasks.forEach(task => {
                    if (Math.random() < 0.3) { // 30% chance to progress
                        task.progress = Math.min(100, task.progress + Math.floor(Math.random() * 5));
                        this.updateTaskProgress(task);
                    }
                });
                
            } catch (error) {
                console.error('Simulation error:', error);
            }
        }, 2000); // Update every 2 seconds
    }

    updateAgentStatus(agentId, status) {
        const agentElements = document.querySelectorAll(`[data-role*="${agentId}"], .${agentId}`);
        agentElements.forEach(element => {
            const statusIndicator = element.querySelector('.status-indicator') || element.querySelector('.status-dot');
            if (statusIndicator) {
                statusIndicator.className = statusIndicator.className.replace(/(online|busy|offline)/, status);
            }
        });
        
        // Update 2D workspace
        renderAgents(this.agents);
    }

    updateAgentInSidebar(agent) {
        const agentItems = document.querySelectorAll('.agent-item');
        agentItems.forEach(item => {
            const nameElement = item.querySelector('.agent-name');
            if (nameElement && nameElement.textContent === agent.name) {
                const taskElement = item.querySelector('.agent-task');
                if (taskElement) {
                    taskElement.textContent = agent.currentTask;
                }
            }
        });
    }

    updateTaskProgress(task) {
        const taskItems = document.querySelectorAll('.task-item');
        taskItems.forEach(item => {
            const titleElement = item.querySelector('.task-title');
            if (titleElement && titleElement.textContent === task.title) {
                const progressElement = item.querySelector('.task-progress');
                if (progressElement) {
                    progressElement.textContent = `${task.progress}% complete`;
                }
            }
        });
        
        // Update pipeline if task is there
        this.updatePipelineProgress(task);
    }

    updatePipelineProgress(task) {
        const pipelineTasks = document.querySelectorAll('.pipeline-task');
        pipelineTasks.forEach(pipelineTask => {
            if (pipelineTask.textContent.includes(task.title.split(' ')[0])) {
                if (task.progress >= 75) {
                    pipelineTask.classList.add('active');
                } else {
                    pipelineTask.classList.remove('active');
                }
            }
        });
    }

    startMetricsUpdates() {
        setInterval(() => {
            // Update system metrics with realistic variations
            this.systemMetrics.responseTime += Math.floor(Math.random() * 10) - 5;
            this.systemMetrics.responseTime = Math.max(20, Math.min(100, this.systemMetrics.responseTime));
            
            this.systemMetrics.memory += Math.floor(Math.random() * 4) - 2;
            this.systemMetrics.memory = Math.max(30, Math.min(90, this.systemMetrics.memory));
            
            this.updateMetricsDisplay();
        }, 5000); // Update every 5 seconds
    }

    updateMetricsDisplay() {
        const statusCards = document.querySelectorAll('.status-card');
        statusCards.forEach(card => {
            const label = card.querySelector('.status-label').textContent;
            const valueElement = card.querySelector('.status-value');
            
            switch(label) {
                case 'Response':
                    valueElement.textContent = `${this.systemMetrics.responseTime}ms`;
                    break;
                case 'Memory':
                    valueElement.textContent = `${this.systemMetrics.memory}%`;
                    break;
            }
        });
    }

    setupPanelSwitching() {
        const panelTabs = document.querySelectorAll('.panel-tab');
        panelTabs.forEach(tab => {
            tab.addEventListener('click', () => {
                // Remove active class from all tabs
                panelTabs.forEach(t => t.classList.remove('active'));
                // Add active class to clicked tab
                tab.classList.add('active');
                
                this.currentPanel = tab.textContent;
                this.updatePanelContent();
            });
        });
    }

    updatePanelContent() {
        const panelContent = document.querySelector('.panel-content');
        if (!panelContent) return;
        
        switch(this.currentPanel) {
            case 'Task Pipeline':
                panelContent.innerHTML = this.generatePipelineView();
                break;
            case 'Live Activity':
                panelContent.innerHTML = this.generateActivityView();
                break;
            case 'Server Health':
                panelContent.innerHTML = this.generateServerHealthView();
                break;
            case 'AI Workflow':
                panelContent.innerHTML = this.generateWorkflowView();
                break;
        }
    }

    generatePipelineView() {
        return `
            <div class="pipeline-view">
                <div class="pipeline-stage">
                    <div class="stage-header">Development</div>
                    <div class="stage-tasks">
                        <div class="pipeline-task">Feature Implementation</div>
                        <div class="pipeline-task">Code Review</div>
                    </div>
                </div>
                <div class="pipeline-arrow">→</div>
                <div class="pipeline-stage">
                    <div class="stage-header">Testing</div>
                    <div class="stage-tasks">
                        <div class="pipeline-task active">Unit Tests</div>
                        <div class="pipeline-task">Integration Tests</div>
                    </div>
                </div>
                <div class="pipeline-arrow">→</div>
                <div class="pipeline-stage">
                    <div class="stage-header">Deployment</div>
                    <div class="stage-tasks">
                        <div class="pipeline-task">Staging</div>
                        <div class="pipeline-task">Production</div>
                    </div>
                </div>
            </div>
        `;
    }

    generateActivityView() {
        const recentActivities = [
            '🔧 DevOps agent updated server configuration',
            '💡 Researcher completed market analysis',
            '🚀 Developer deployed new feature to staging',
            '🔍 Security agent completed vulnerability scan',
            '📊 Analyst generated performance report'
        ];

        return `
            <div class="activity-feed" style="padding: 20px;">
                <h3 style="margin-bottom: 20px; color: var(--text-primary);">Recent Activity</h3>
                ${recentActivities.map(activity => `
                    <div style="padding: 12px; margin-bottom: 8px; background: var(--bg-secondary); border-radius: 8px; font-size: 14px;">
                        ${activity}
                    </div>
                `).join('')}
            </div>
        `;
    }

    generateServerHealthView() {
        return `
            <div class="server-health" style="padding: 20px;">
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px;">
                    <div style="padding: 20px; background: var(--bg-secondary); border-radius: 12px;">
                        <h4 style="color: var(--text-primary); margin-bottom: 10px;">CPU Usage</h4>
                        <div style="font-size: 24px; font-weight: bold; color: var(--color-success);">34%</div>
                    </div>
                    <div style="padding: 20px; background: var(--bg-secondary); border-radius: 12px;">
                        <h4 style="color: var(--text-primary); margin-bottom: 10px;">Memory</h4>
                        <div style="font-size: 24px; font-weight: bold; color: var(--color-warning);">${this.systemMetrics.memory}%</div>
                    </div>
                    <div style="padding: 20px; background: var(--bg-secondary); border-radius: 12px;">
                        <h4 style="color: var(--text-primary); margin-bottom: 10px;">Network</h4>
                        <div style="font-size: 24px; font-weight: bold; color: var(--color-success);">Normal</div>
                    </div>
                </div>
            </div>
        `;
    }

    generateWorkflowView() {
        return `
            <div class="workflow-diagram" style="padding: 20px;">
                <h3 style="margin-bottom: 20px; color: var(--text-primary);">AI Workflow Overview</h3>
                <div style="display: flex; justify-content: center; align-items: center; gap: 30px;">
                    <div style="padding: 15px; background: var(--color-primary); color: white; border-radius: 8px; text-align: center;">
                        Input Processing
                    </div>
                    <div style="font-size: 20px;">→</div>
                    <div style="padding: 15px; background: var(--color-secondary); color: white; border-radius: 8px; text-align: center;">
                        Agent Assignment
                    </div>
                    <div style="font-size: 20px;">→</div>
                    <div style="padding: 15px; background: var(--color-success); color: white; border-radius: 8px; text-align: center;">
                        Task Execution
                    </div>
                    <div style="font-size: 20px;">→</div>
                    <div style="padding: 15px; background: var(--color-accent); color: white; border-radius: 8px; text-align: center;">
                        Output Delivery
                    </div>
                </div>
            </div>
        `;
    }

    setupAgentInteractions() {
        // Make agents move periodically
        setInterval(() => {
            if (this.simulationPaused) return;
            
            const agents = document.querySelectorAll('.agent-avatar');
            agents.forEach(agent => {
                if (Math.random() < 0.3) { // 30% chance to animate
                    this.animateAgent(agent);
                }
            });
        }, 3000);
    }

    animateAgent(agent) {
        const originalTransform = agent.style.transform;
        const movements = [
            'translateX(5px)',
            'translateX(-5px)',
            'translateY(5px)',
            'translateY(-5px)'
        ];
        
        const movement = movements[Math.floor(Math.random() * movements.length)];
        agent.style.transform = movement;
        agent.style.transition = 'transform 0.5s ease-in-out';
        
        setTimeout(() => {
            agent.style.transform = originalTransform;
        }, 500);
    }

    updateDashboard() {
        // Update counters in navigation
        document.querySelector('.nav-item:nth-child(3) .nav-badge').textContent = this.agents.length;
        document.querySelector('.nav-item:nth-child(4) .nav-badge').textContent = this.tasks.length;
        
        // Update notification badge
        const notificationBadge = document.querySelector('.notification-badge');
        if (notificationBadge) {
            notificationBadge.textContent = '3';
        }
        
        // Initial render of 2D workspace
        renderAgents(this.agents);
    }

    showNotification(message) {
        // Create a simple toast notification
        const notification = document.createElement('div');
        notification.style.cssText = `
            position: fixed;
            top: 20px;
            right: 20px;
            background: var(--color-primary);
            color: white;
            padding: 12px 20px;
            border-radius: 8px;
            box-shadow: var(--shadow-lg);
            z-index: 1000;
            font-size: 14px;
            opacity: 0;
            transition: opacity 0.3s ease;
        `;
        notification.textContent = message;
        
        document.body.appendChild(notification);
        
        // Fade in
        setTimeout(() => notification.style.opacity = '1', 10);
        
        // Remove after 3 seconds
        setTimeout(() => {
            notification.style.opacity = '0';
            setTimeout(() => document.body.removeChild(notification), 300);
        }, 3000);
    }

    destroy() {
        if (this.simulationInterval) {
            clearInterval(this.simulationInterval);
        }
    }
}

// 2D Workspace Functions
function mapStatusToZone(status) {
    if (status === 'researching') return 'zone-research';
    if (status === 'working') return 'zone-engineering';
    if (status === 'meeting') return 'zone-meeting';
    if (status === 'idle') return 'zone-lounge';
    return 'zone-operations';
}

function renderAgents(agents) {
    const map = document.getElementById('workspace-map');
    if (!map || !Array.isArray(agents)) return;

    agents.forEach(agent => {
        let el = document.querySelector(`[data-id="${agent.id}"]`);

        if (!el) {
            el = document.createElement('div');
            el.className = 'agent';
            el.dataset.id = agent.id;
            map.appendChild(el);
        }

        const zoneId = mapStatusToZone(agent.status);
        const zone = document.getElementById(zoneId);
        if (!zone) return;

        // Get zone position relative to map
        const mapRect = map.getBoundingClientRect();
        const zoneRect = zone.getBoundingClientRect();
        
        // Calculate relative position within the map
        const relativeX = zoneRect.left - mapRect.left + (zoneRect.width / 2) - 9; // Center agent (18px / 2)
        const relativeY = zoneRect.top - mapRect.top + (zoneRect.height / 2) - 9;

        el.style.left = relativeX + 'px';
        el.style.top = relativeY + 'px';
    });
}

// Initialize the workspace when DOM is loaded
document.addEventListener('DOMContentLoaded', () => {
    window.nixonWorkspace = new NixonWorkspace();
});

// Handle page unload
window.addEventListener('beforeunload', () => {
    if (window.nixonWorkspace) {
        window.nixonWorkspace.destroy();
    }
});