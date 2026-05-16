# Profiles & Workspaces Unified Section — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify Profiles and Workspaces into a single section with profile grid, creation wizard with templates, and per-profile workspace management.

**Architecture:** Single "Profiles" tab in sidebar/rail replaces both old tabs. Main panel renders a card grid (profiles) with expandable detail area containing workspaces. Backend gets a `template` field on profile creation that writes SOUL.md. Workspace-profile association via mapping file (non-breaking).

**Tech Stack:** Vanilla JS (panels.js), Python (api/profiles.py, api/workspace.py), CSS (style.css), i18n (i18n.js)

---

## File Structure

| File | Role |
|------|------|
| `static/index.html` | Remove workspaces panel HTML, remove workspaces nav buttons, update profiles panel structure |
| `static/panels.js` | Rewrite profile rendering (grid+detail+workspaces), add wizard, redirect workspace panel calls |
| `static/style.css` | Profile grid, card, avatar, wizard, workspace-in-detail styles |
| `static/i18n.js` | New translation keys for wizard, templates, empty states |
| `api/profiles.py` | Add `template` param to `create_profile_api()`, write SOUL.md |
| `api/workspace.py` | Add `load_workspaces_for_profile()` and `workspace_profile_mapping()` |
| `api/routes.py` | Add `GET /api/profile/<name>/workspaces`, update create endpoint |

---

## Task 1: Backend — Add template support to profile creation

**Files:**
- Modify: `api/profiles.py:570-626`
- Modify: `api/routes.py` (profile create handler)

- [ ] **Step 1: Add SOUL templates constant to profiles.py**

Add after line 27 (`_CLONE_CONFIG_FILES`):

```python
_SOUL_TEMPLATES = {
    'coder': """# Coder Profile

You are a software engineering assistant. You excel at:
- Writing clean, maintainable code
- Debugging complex issues
- Designing system architecture
- Code review and refactoring

Be precise, concise, and pragmatic. Prefer working solutions over theoretical perfection.
""",
    'researcher': """# Researcher Profile

You are a research assistant. You excel at:
- Gathering and synthesizing information from multiple sources
- Providing citations and references
- Analyzing data and identifying patterns
- Presenting findings clearly and objectively

Be thorough, cite sources, and distinguish facts from interpretations.
""",
    'writer': """# Writer Profile

You are a writing assistant. You excel at:
- Content creation across formats (articles, docs, emails, copy)
- Editing for clarity, tone, and structure
- Adapting voice and style to audience
- Organizing complex ideas into readable prose

Be creative yet precise. Match the user's intended tone and audience.
""",
}
```

- [ ] **Step 2: Update create_profile_api() signature and logic**

In `create_profile_api()` (line 570), add `template` parameter and write SOUL.md after creation:

```python
def create_profile_api(name: str, clone_from: str = None,
                       clone_config: bool = False,
                       base_url: str = None,
                       api_key: str = None,
                       template: str = None) -> dict:
```

After line 607 (`_write_endpoint_to_config(...)`), add:

```python
    # Write SOUL.md from template if requested (and not cloning which already copies SOUL.md)
    if template and template in _SOUL_TEMPLATES and not clone_config:
        soul_path = profile_path / 'SOUL.md'
        if not soul_path.exists():
            soul_path.write_text(_SOUL_TEMPLATES[template], encoding='utf-8')
```

- [ ] **Step 3: Update route handler to pass template field**

In `api/routes.py`, find the profile create POST handler. Add `template` extraction from body:

```python
template = body.get('template')  # optional: 'coder', 'researcher', 'writer'
```

Pass it to `create_profile_api(name, ..., template=template)`.

- [ ] **Step 4: Test manually**

```bash
curl -X POST http://localhost:5005/api/profile/create \
  -H "Content-Type: application/json" \
  -d '{"name":"testcoder","clone_config":false,"template":"coder"}'
```

Verify `~/.hermes/profiles/testcoder/SOUL.md` contains the coder template.

- [ ] **Step 5: Cleanup test profile**

```bash
curl -X POST http://localhost:5005/api/profile/delete \
  -H "Content-Type: application/json" \
  -d '{"name":"testcoder"}'
```


---

## Task 2: Backend — Workspace-profile association

**Files:**
- Modify: `api/workspace.py`
- Modify: `api/routes.py`

- [ ] **Step 1: Add workspace-profile mapping functions to workspace.py**

Add at end of `api/workspace.py`:

```python
def _workspace_profile_map_file() -> Path:
    """Return path to the workspace↔profile mapping file."""
    from api.profiles import _DEFAULT_HERMES_HOME
    state_dir = _DEFAULT_HERMES_HOME / 'webui_state'
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / 'workspace_profile_map.json'


def load_workspace_profile_map() -> dict:
    """Load {workspace_path: profile_name} mapping."""
    map_file = _workspace_profile_map_file()
    if map_file.exists():
        try:
            data = json.loads(map_file.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_workspace_profile_map(mapping: dict) -> None:
    """Save workspace↔profile mapping."""
    map_file = _workspace_profile_map_file()
    map_file.write_text(json.dumps(mapping, indent=2), encoding='utf-8')


def associate_workspace_to_profile(workspace_path: str, profile_name: str) -> None:
    """Associate a workspace path with a profile."""
    mapping = load_workspace_profile_map()
    mapping[workspace_path] = profile_name
    save_workspace_profile_map(mapping)


def workspaces_for_profile(profile_name: str) -> list:
    """Return workspaces associated with a given profile."""
    mapping = load_workspace_profile_map()
    all_ws = load_workspaces()
    associated_paths = [path for path, prof in mapping.items() if prof == profile_name]
    # If no explicit associations, show all workspaces for 'default' profile
    if not associated_paths and profile_name == 'default':
        return all_ws
    return [w for w in all_ws if w.get('path') in associated_paths]
```

- [ ] **Step 2: Add import for json at top of workspace.py (if not present)**

Check if `import json` exists. If not, add it.

- [ ] **Step 3: Add route for profile workspaces**

In `api/routes.py`, add handler for `GET /api/profile/<name>/workspaces`:

```python
    if parsed.path.startswith("/api/profile/") and parsed.path.endswith("/workspaces"):
        # GET /api/profile/<name>/workspaces
        parts = parsed.path.split("/")
        # parts = ['', 'api', 'profile', '<name>', 'workspaces']
        if len(parts) == 5:
            profile_name = parts[3]
            from api.workspace import workspaces_for_profile
            ws = workspaces_for_profile(profile_name)
            return _json_response(handler, {"workspaces": ws, "profile": profile_name})
```

- [ ] **Step 4: Update workspace add to auto-associate with active profile**

In `_handle_workspace_add()` in routes.py, after successfully adding workspace, associate it:

```python
    from api.workspace import associate_workspace_to_profile
    from api.profiles import get_active_profile_name
    associate_workspace_to_profile(str(resolved_path), get_active_profile_name())
```

- [ ] **Step 5: Test**

```bash
curl http://localhost:5005/api/profile/default/workspaces
```

Should return workspaces list for default profile.

---

## Task 3: Navigation — Remove Workspaces tab, unify under Profiles

**Files:**
- Modify: `static/index.html`
- Modify: `static/panels.js`

- [ ] **Step 1: Remove workspaces buttons from rail (index.html)**

Remove the workspaces button from the rail nav (line ~97):
```html
<!-- REMOVE THIS LINE: -->
<button class="rail-btn nav-tab" data-panel="workspaces" onclick="switchPanel('workspaces')" ...>
```

- [ ] **Step 2: Remove workspaces button from sidebar-nav (index.html)**

Remove the workspaces button from sidebar-nav (line ~111):
```html
<!-- REMOVE THIS LINE: -->
<button class="nav-tab" data-panel="workspaces" data-label="Spaces" onclick="switchPanel('workspaces')" ...>
```

- [ ] **Step 3: Remove panelWorkspaces div from sidebar (index.html)**

Remove lines 209-218 (the entire `<div class="panel-view" id="panelWorkspaces">` block).

- [ ] **Step 4: Add backwards-compat redirect in panels.js**

In `switchPanel()` function, add early redirect:

```javascript
  if (nextPanel === 'workspaces') { switchPanel('profiles'); return; }
```

- [ ] **Step 5: Remove workspace panel load from switchPanel()**

Find line ~210 in panels.js:
```javascript
  if (nextPanel === 'workspaces') await loadWorkspacesPanel();
```
Remove it (the redirect above handles it now).

- [ ] **Step 6: Verify sidebar still works**

Load the app. Profiles tab should be the only entry point. Clicking old workspace links should redirect to profiles.

---

## Task 4: Main Panel — Profile Grid with Cards

**Files:**
- Modify: `static/index.html`
- Modify: `static/panels.js`
- Modify: `static/style.css`

- [ ] **Step 1: Update profiles main panel HTML (index.html)**

Replace the current `#mainProfiles` content with:

```html
<div id="mainProfiles" class="main-view">
  <div class="main-view-header">
    <h2 data-i18n="tab_profiles">Profiles</h2>
    <button class="btn-primary btn-sm" onclick="openProfileWizard()" data-i18n="profile_new">+ New Profile</button>
  </div>
  <div id="profileGrid" class="profile-grid"></div>
  <div id="profileDetailSection" class="profile-detail-section" style="display:none">
    <div id="profileDetailContent"></div>
    <div id="profileWorkspacesSection" class="profile-workspaces-section"></div>
  </div>
  <div id="profileWizardSection" class="profile-wizard" style="display:none"></div>
</div>
```

- [ ] **Step 2: Add profile grid CSS to style.css**

```css
/* ── Profile Grid ── */
.profile-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
  padding: 16px;
}

.profile-grid-card {
  background: var(--card-bg, var(--surface));
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
  cursor: pointer;
  transition: border-color 0.15s, box-shadow 0.15s;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  text-align: center;
}

.profile-grid-card:hover {
  border-color: var(--link);
  box-shadow: 0 2px 8px rgba(0,0,0,0.15);
}

.profile-grid-card.active {
  border-color: var(--link);
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--link) 25%, transparent);
}

.profile-grid-card.selected {
  border-color: var(--accent);
}

.profile-avatar {
  width: 48px;
  height: 48px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 700;
  color: #fff;
  background: var(--link);
}

.profile-avatar.default-avatar {
  background: var(--accent);
}

.profile-grid-card-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--fg);
}

.profile-grid-card-meta {
  font-size: 11px;
  color: var(--muted);
}

.profile-grid-card-badges {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
  justify-content: center;
}

.profile-badge {
  font-size: 9px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--surface);
  border: 1px solid var(--border);
}

.profile-badge.active-badge {
  background: color-mix(in srgb, var(--link) 15%, transparent);
  border-color: var(--link);
  color: var(--link);
}

.profile-badge.gw-on {
  color: var(--ok, #4caf50);
  border-color: var(--ok, #4caf50);
}

/* ── Profile Detail Section ── */
.profile-detail-section {
  border-top: 1px solid var(--border);
  padding: 16px;
  margin-top: 8px;
}

.profile-workspaces-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
}

/* ── Mobile ── */
@media (max-width: 767px) {
  .profile-grid {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 3: Rewrite loadProfilesPanel() in panels.js for grid rendering**

Replace the current `loadProfilesPanel()` (lines 2041-2090) with new version that renders into `#profileGrid`:

```javascript
async function loadProfilesPanel() {
  const panel = $('profilesPanel');
  const grid = $('profileGrid');
  if (!grid) { if (panel) panel.innerHTML = ''; return; }

  try {
    const data = await api('/api/profiles');
    _profilesCache = data;
    grid.innerHTML = '';

    const profiles = data.profiles || [];
    const activeName = data.active || 'default';

    if (profiles.length <= 1) {
      // Empty state — only default exists
      grid.innerHTML = `
        <div class="profile-empty-state">
          <div class="profile-avatar default-avatar">N</div>
          <p>${esc(t('profile_empty_cta'))}</p>
          <button class="btn-primary btn-sm" onclick="openProfileWizard()">${esc(t('profile_new'))}</button>
          <div class="profile-empty-templates">
            <span>${esc(t('profile_templates_hint'))}</span>
          </div>
        </div>`;
    }

    for (const p of profiles) {
      const card = document.createElement('div');
      card.className = 'profile-grid-card';
      if (p.name === activeName) card.classList.add('active');
      if (_selectedProfile === p.name) card.classList.add('selected');

      const initial = p.name === 'default' ? 'N' : p.name.charAt(0).toUpperCase();
      const avatarClass = p.name === 'default' ? 'profile-avatar default-avatar' : 'profile-avatar';
      const meta = [];
      if (p.model) meta.push(p.model.split('/').pop());
      if (p.provider && p.provider !== 'auto') meta.push(p.provider);
      if (p.skill_count) meta.push(t('profile_skill_count', p.skill_count));

      const badges = [];
      if (p.name === activeName) badges.push(`<span class="profile-badge active-badge">${esc(t('profile_active'))}</span>`);
      if (p.gateway_running) badges.push(`<span class="profile-badge gw-on">${esc(t('profile_gateway_running'))}</span>`);

      card.innerHTML = `
        <div class="${avatarClass}">${esc(initial)}</div>
        <div class="profile-grid-card-name">${esc(p.name)}</div>
        <div class="profile-grid-card-meta">${esc(meta.join(' · ') || t('profile_no_configuration'))}</div>
        <div class="profile-grid-card-badges">${badges.join('')}</div>`;

      card.onclick = () => selectProfileCard(p.name);
      grid.appendChild(card);
    }

    // Also update sidebar compact list
    if (panel) renderProfilesSidebar(profiles, activeName);

  } catch (e) {
    grid.innerHTML = `<div style="color:var(--accent);padding:16px">${esc(e.message)}</div>`;
  }
}
```

- [ ] **Step 4: Add selectProfileCard() function**

```javascript
let _selectedProfile = null;

async function selectProfileCard(name) {
  _selectedProfile = name;
  // Re-render grid to update selected state
  const grid = $('profileGrid');
  if (grid) {
    grid.querySelectorAll('.profile-grid-card').forEach(c => c.classList.remove('selected'));
    grid.querySelector(`[data-profile="${name}"]`)?.classList.add('selected');
  }
  // Load detail + workspaces
  await loadProfileDetail(name);
}
```

- [ ] **Step 5: Add loadProfileDetail() function**

```javascript
async function loadProfileDetail(name) {
  const section = $('profileDetailSection');
  const content = $('profileDetailContent');
  const wsSection = $('profileWorkspacesSection');
  if (!section || !content) return;

  const profiles = (_profilesCache && _profilesCache.profiles) || [];
  const p = profiles.find(x => x.name === name);
  if (!p) { section.style.display = 'none'; return; }

  const activeName = (_profilesCache && _profilesCache.active) || 'default';
  const isActive = p.name === activeName;

  content.innerHTML = `
    <div class="detail-card">
      <div class="detail-card-title">${esc(p.name)}</div>
      <div class="detail-row"><span class="detail-row-label">Status</span><span>${isActive ? t('profile_active') : 'Inactive'}</span></div>
      <div class="detail-row"><span class="detail-row-label">Model</span><span>${esc(p.model || '-')}</span></div>
      <div class="detail-row"><span class="detail-row-label">Provider</span><span>${esc(p.provider || 'auto')}</span></div>
      <div class="detail-row"><span class="detail-row-label">Gateway</span><span>${p.gateway_running ? t('profile_gateway_running') : t('profile_gateway_stopped')}</span></div>
      <div class="detail-row"><span class="detail-row-label">Skills</span><span>${p.skill_count || 0}</span></div>
      <div style="margin-top:12px;display:flex;gap:8px">
        ${!isActive ? `<button class="btn-primary btn-sm" onclick="switchToProfile('${esc(p.name)}')">${esc(t('profile_switch'))}</button>` : ''}
        ${!p.is_default ? `<button class="btn-danger btn-sm" onclick="confirmDeleteProfile('${esc(p.name)}')">${esc(t('delete'))}</button>` : ''}
      </div>
    </div>`;

  // Load workspaces for this profile
  try {
    const wsData = await api('/api/profile/' + encodeURIComponent(name) + '/workspaces');
    renderProfileWorkspaces(wsSection, wsData.workspaces || [], name);
  } catch (e) {
    wsSection.innerHTML = `<div style="color:var(--muted);font-size:12px">${esc(e.message)}</div>`;
  }

  section.style.display = '';
}
```

- [ ] **Step 6: Add renderProfileWorkspaces() function**

```javascript
function renderProfileWorkspaces(container, workspaces, profileName) {
  if (!container) return;
  const header = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
    <strong style="font-size:12px">${esc(t('workspace_section_title'))} (${workspaces.length})</strong>
    <button class="btn-sm" onclick="openWorkspaceCreate()">${esc(t('workspace_add_title'))}</button>
  </div>`;

  if (!workspaces.length) {
    container.innerHTML = header + `<div style="color:var(--muted);font-size:12px">${esc(t('profile_no_workspaces'))}</div>`;
    return;
  }

  let rows = '';
  for (const w of workspaces) {
    rows += `<div class="ws-row" style="padding:6px 0;border-bottom:1px solid var(--border)">
      <div class="ws-row-info">
        <div class="ws-row-name">${esc(w.name || w.path.split('/').pop())}</div>
        <div class="ws-row-path">${esc(w.path)}</div>
      </div>
    </div>`;
  }
  container.innerHTML = header + rows;
}
```

- [ ] **Step 7: Verify grid renders correctly**

Load app, navigate to Profiles tab. Should see card grid with at least the default profile.


---

## Task 5: Profile Creation Wizard

**Files:**
- Modify: `static/panels.js`
- Modify: `static/style.css`

- [ ] **Step 1: Add wizard CSS to style.css**

```css
/* ── Profile Wizard ── */
.profile-wizard {
  padding: 16px;
  max-width: 480px;
  margin: 0 auto;
}

.wizard-step {
  display: none;
}

.wizard-step.active {
  display: block;
}

.wizard-step-title {
  font-size: 15px;
  font-weight: 600;
  margin-bottom: 12px;
}

.wizard-templates {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 8px;
  margin-bottom: 16px;
}

.wizard-template-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.15s;
}

.wizard-template-card:hover,
.wizard-template-card.selected {
  border-color: var(--link);
  background: color-mix(in srgb, var(--link) 8%, transparent);
}

.wizard-template-card-name {
  font-weight: 600;
  font-size: 12px;
  margin-top: 6px;
}

.wizard-template-card-desc {
  font-size: 10px;
  color: var(--muted);
  margin-top: 4px;
}

.wizard-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}

.wizard-summary {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  font-size: 12px;
}

.wizard-summary dt {
  color: var(--muted);
  font-size: 11px;
}

.wizard-summary dd {
  margin: 0 0 8px 0;
  font-weight: 500;
}
```

- [ ] **Step 2: Add openProfileWizard() function in panels.js**

```javascript
let _wizardState = { step: 1, template: null, name: '', cloneConfig: true };

function openProfileWizard() {
  _wizardState = { step: 1, template: null, name: '', cloneConfig: true };
  const wizard = $('profileWizardSection');
  const grid = $('profileGrid');
  const detail = $('profileDetailSection');
  if (wizard) wizard.style.display = '';
  if (grid) grid.style.display = 'none';
  if (detail) detail.style.display = 'none';
  renderWizardStep();
}

function closeProfileWizard() {
  const wizard = $('profileWizardSection');
  const grid = $('profileGrid');
  if (wizard) wizard.style.display = 'none';
  if (grid) grid.style.display = '';
  loadProfilesPanel();
}
```

- [ ] **Step 3: Add renderWizardStep() function**

```javascript
function renderWizardStep() {
  const wizard = $('profileWizardSection');
  if (!wizard) return;

  if (_wizardState.step === 1) {
    wizard.innerHTML = `
      <div class="wizard-step active">
        <div class="wizard-step-title">${esc(t('wizard_step1_title'))}</div>
        <div class="wizard-templates">
          <div class="wizard-template-card ${_wizardState.template === null ? 'selected' : ''}" onclick="selectWizardTemplate(null)">
            <div style="font-size:24px">📄</div>
            <div class="wizard-template-card-name">Blank</div>
            <div class="wizard-template-card-desc">${esc(t('wizard_blank_desc'))}</div>
          </div>
          <div class="wizard-template-card ${_wizardState.template === 'coder' ? 'selected' : ''}" onclick="selectWizardTemplate('coder')">
            <div style="font-size:24px">💻</div>
            <div class="wizard-template-card-name">Coder</div>
            <div class="wizard-template-card-desc">${esc(t('wizard_coder_desc'))}</div>
          </div>
          <div class="wizard-template-card ${_wizardState.template === 'researcher' ? 'selected' : ''}" onclick="selectWizardTemplate('researcher')">
            <div style="font-size:24px">🔬</div>
            <div class="wizard-template-card-name">Researcher</div>
            <div class="wizard-template-card-desc">${esc(t('wizard_researcher_desc'))}</div>
          </div>
          <div class="wizard-template-card ${_wizardState.template === 'writer' ? 'selected' : ''}" onclick="selectWizardTemplate('writer')">
            <div style="font-size:24px">✍️</div>
            <div class="wizard-template-card-name">Writer</div>
            <div class="wizard-template-card-desc">${esc(t('wizard_writer_desc'))}</div>
          </div>
        </div>
        <div class="wizard-actions">
          <button class="btn-primary btn-sm" onclick="wizardNext()">${esc(t('next'))}</button>
          <button class="btn-secondary btn-sm" onclick="closeProfileWizard()">${esc(t('cancel'))}</button>
        </div>
      </div>`;
  } else if (_wizardState.step === 2) {
    wizard.innerHTML = `
      <div class="wizard-step active">
        <div class="wizard-step-title">${esc(t('wizard_step2_title'))}</div>
        <div style="margin-bottom:12px">
          <label style="font-size:12px;color:var(--muted)">${esc(t('wizard_name_label'))}</label>
          <input type="text" id="wizardName" class="input" value="${esc(_wizardState.name)}"
            placeholder="my-agent" autocomplete="off"
            oninput="this.value=this.value.toLowerCase().replace(/[^a-z0-9_-]/g,''); _wizardState.name=this.value">
        </div>
        <label style="font-size:12px;display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="wizardClone" ${_wizardState.cloneConfig ? 'checked' : ''}
            onchange="_wizardState.cloneConfig=this.checked">
          <span>${esc(t('wizard_clone_keys'))}</span>
        </label>
        <div class="wizard-actions">
          <button class="btn-primary btn-sm" onclick="wizardNext()">${esc(t('next'))}</button>
          <button class="btn-secondary btn-sm" onclick="wizardBack()">${esc(t('back'))}</button>
        </div>
      </div>`;
    setTimeout(() => $('wizardName')?.focus(), 50);
  } else if (_wizardState.step === 3) {
    wizard.innerHTML = `
      <div class="wizard-step active">
        <div class="wizard-step-title">${esc(t('wizard_step3_title'))}</div>
        <dl class="wizard-summary">
          <dt>${esc(t('wizard_name_label'))}</dt>
          <dd>${esc(_wizardState.name)}</dd>
          <dt>Template</dt>
          <dd>${esc(_wizardState.template || 'Blank')}</dd>
          <dt>${esc(t('wizard_clone_keys'))}</dt>
          <dd>${_wizardState.cloneConfig ? 'Yes' : 'No'}</dd>
        </dl>
        <div class="wizard-actions">
          <button class="btn-primary btn-sm" id="wizardCreateBtn" onclick="wizardCreate()">${esc(t('create'))}</button>
          <button class="btn-secondary btn-sm" onclick="wizardBack()">${esc(t('back'))}</button>
        </div>
      </div>`;
  }
}
```

- [ ] **Step 4: Add wizard navigation and create functions**

```javascript
function selectWizardTemplate(tpl) {
  _wizardState.template = tpl;
  renderWizardStep();
}

function wizardNext() {
  if (_wizardState.step === 2 && !_wizardState.name.trim()) {
    showToast(t('wizard_name_required'), 'error');
    return;
  }
  _wizardState.step++;
  renderWizardStep();
}

function wizardBack() {
  _wizardState.step--;
  renderWizardStep();
}

async function wizardCreate() {
  const btn = $('wizardCreateBtn');
  if (btn) btn.disabled = true;
  try {
    const body = {
      name: _wizardState.name.trim(),
      clone_config: _wizardState.cloneConfig,
    };
    if (_wizardState.template) body.template = _wizardState.template;
    await api('/api/profile/create', { method: 'POST', body: JSON.stringify(body) });
    showToast(t('profile_created'));
    closeProfileWizard();
  } catch (e) {
    showToast(e.message || t('profile_create_failed'), 'error');
    if (btn) btn.disabled = false;
  }
}
```

- [ ] **Step 5: Test wizard flow**

Open app → Profiles → "+ New Profile" → select Coder template → enter name "test-coder" → Create. Verify profile appears in grid.

---

## Task 6: i18n — Add new translation keys

**Files:**
- Modify: `static/i18n.js`

- [ ] **Step 1: Add new keys to English locale in i18n.js**

Find the English locale object and add:

```javascript
    // Profile grid & wizard
    profile_new: '+ New Profile',
    profile_empty_cta: 'Create specialized profiles with their own personality, skills, and providers.',
    profile_templates_hint: 'Templates: Coder · Researcher · Writer',
    profile_switch: 'Switch to',
    profile_created: 'Profile created',
    profile_create_failed: 'Failed to create profile',
    profile_no_workspaces: 'No workspaces configured for this profile.',
    workspace_section_title: 'Workspaces',

    // Wizard
    wizard_step1_title: 'Choose a template',
    wizard_step2_title: 'Name & settings',
    wizard_step3_title: 'Confirm',
    wizard_name_label: 'Profile name',
    wizard_name_required: 'Profile name is required',
    wizard_clone_keys: 'Clone API keys from default',
    wizard_blank_desc: 'Start from scratch',
    wizard_coder_desc: 'Code & architecture',
    wizard_researcher_desc: 'Research & analysis',
    wizard_writer_desc: 'Content & editing',
    next: 'Next',
    back: 'Back',
    create: 'Create',
```

- [ ] **Step 2: Add Portuguese translations**

Find the pt-BR locale object and add equivalent keys:

```javascript
    profile_new: '+ Novo Perfil',
    profile_empty_cta: 'Crie perfis especializados com personalidade, skills e providers próprios.',
    profile_templates_hint: 'Templates: Coder · Researcher · Writer',
    profile_switch: 'Ativar',
    profile_created: 'Perfil criado',
    profile_create_failed: 'Falha ao criar perfil',
    profile_no_workspaces: 'Nenhum workspace configurado para este perfil.',
    workspace_section_title: 'Workspaces',

    wizard_step1_title: 'Escolha um template',
    wizard_step2_title: 'Nome & configurações',
    wizard_step3_title: 'Confirmar',
    wizard_name_label: 'Nome do perfil',
    wizard_name_required: 'Nome do perfil é obrigatório',
    wizard_clone_keys: 'Clonar chaves API do default',
    wizard_blank_desc: 'Começar do zero',
    wizard_coder_desc: 'Código & arquitetura',
    wizard_researcher_desc: 'Pesquisa & análise',
    wizard_writer_desc: 'Conteúdo & edição',
    next: 'Próximo',
    back: 'Voltar',
    create: 'Criar',
```

- [ ] **Step 3: Verify no missing keys**

Load app in both en and pt-BR. Check that wizard and grid show translated strings.

---

## Task 7: Sidebar compact list + renderProfilesSidebar()

**Files:**
- Modify: `static/panels.js`

- [ ] **Step 1: Add renderProfilesSidebar() function**

This replaces the old sidebar rendering. The sidebar panel (`#profilesPanel`) becomes a compact list for quick navigation:

```javascript
function renderProfilesSidebar(profiles, activeName) {
  const panel = $('profilesPanel');
  if (!panel) return;
  panel.innerHTML = '';

  for (const p of profiles) {
    const card = document.createElement('div');
    card.className = 'profile-card';
    if (p.name === activeName) card.classList.add('active');
    const meta = [];
    if (p.model) meta.push(p.model.split('/').pop());
    if (p.skill_count) meta.push(t('profile_skill_count', p.skill_count));
    const dot = p.name === activeName ? '●' : '○';
    card.innerHTML = `
      <div class="profile-card-header">
        <div style="min-width:0;flex:1">
          <div class="profile-card-name">${dot} ${esc(p.name)}</div>
          ${meta.length ? `<div class="profile-card-meta">${esc(meta.join(' · '))}</div>` : ''}
        </div>
      </div>`;
    card.onclick = () => selectProfileCard(p.name);
    panel.appendChild(card);
  }
}
```

- [ ] **Step 2: Verify sidebar and main panel stay in sync**

Click a profile in sidebar → main panel should show its detail. Click in grid → sidebar should highlight.

---

## Task 8: Integration testing & polish

**Files:**
- All modified files

- [ ] **Step 1: Full flow test — create profile with template**

1. Open app
2. Navigate to Profiles
3. Click "+ New Profile"
4. Select "Coder" template → Next
5. Enter name "dev-assistant" → Next
6. Confirm → Create
7. Verify card appears in grid
8. Verify SOUL.md exists on server

- [ ] **Step 2: Test workspace association**

1. Select the new profile card
2. Verify workspaces section shows (empty or with associated workspaces)
3. Add a workspace via composer dropdown
4. Return to profile detail — workspace should appear

- [ ] **Step 3: Test profile switch**

1. Click "Switch to" on a non-active profile
2. Verify active badge moves
3. Verify composer workspace dropdown updates

- [ ] **Step 4: Test delete**

1. Click Delete on a non-default profile
2. Confirm deletion
3. Verify card removed from grid

- [ ] **Step 5: Test mobile layout**

1. Resize to 375px width
2. Verify grid becomes single column
3. Verify wizard steps are full-width
4. Verify no horizontal overflow

- [ ] **Step 6: Test backwards compat**

1. Any old bookmark/link to `switchPanel('workspaces')` should redirect to profiles
2. Composer workspace dropdown still works independently

- [ ] **Step 7: Commit**

```bash
git add static/index.html static/panels.js static/style.css static/i18n.js api/profiles.py api/workspace.py api/routes.py
git commit -m "feat: unify profiles & workspaces into single section with wizard and templates"
```

