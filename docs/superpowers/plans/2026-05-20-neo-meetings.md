# Neo Meetings — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Meetings panel to NeoWebUI that lets Júnior create Jitsi rooms on demand, embed them via iframe, and generate post-meeting material (summary, decisions, tasks).

**Architecture:** New `meetings` panel following existing panel pattern (register in panels.js, HTML div, dedicated JS file). Backend CRUD via `api/meetings.py` with JSON persistence at `STATE_DIR/meetings.json`. Jitsi room URL built from configurable base + slug + timestamp + random suffix. Post-meeting flow triggers a prompt to Neo for structured output.

**Tech Stack:** Vanilla JS (frontend), Python 3.12 stdlib (backend), Jitsi IFrame API (CDN), JSON file storage.

---

## File Structure

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `static/meetings.js` | Panel rendering, Jitsi iframe mount, form logic, post-meeting flow |
| Create | `api/meetings.py` | CRUD operations, JSON persistence, room URL generation |
| Create | `tests/test_neo_meetings.py` | Backend + frontend integration tests |
| Modify | `static/panels.js:17-37` | Register `meetings` in panel maps |
| Modify | `static/panels.js:168-225` | Add lazy-load hook in `switchPanel` |
| Modify | `static/index.html:92-98` | Add sidebar rail button |
| Modify | `static/index.html:106-115` | Add mobile bottom nav button |
| Modify | `static/index.html:127-131` | Add dashboard menu item |
| Modify | `static/index.html:330-355` | Replace `new_component` quick action with `new_meeting` |
| Modify | `static/index.html` (after line ~1105) | Add `<div id="mainMeetings">` |
| Modify | `static/dashboard.js:15-21` | Replace `new_component` prompt with `new_meeting` handler |
| Modify | `static/dashboard.js:347-367` | Add `new_meeting` action → `switchPanel('meetings')` |
| Modify | `static/i18n.js` (en ~429, pt ~5905) | Add meeting-related i18n keys |
| Modify | `static/style.css` | Add `.meetings-*` classes |
| Modify | `api/routes.py` | Wire GET/POST `/api/meetings*` |
| Modify | `api/config.py` | Add `MEETINGS_FILE` path constant |

---

## Task 1: Backend — Persistence Layer (`api/meetings.py`)

**Files:**
- Create: `api/meetings.py`
- Modify: `api/config.py:51` (add `MEETINGS_FILE`)
- Test: `tests/test_neo_meetings.py`

- [ ] **Step 1: Add MEETINGS_FILE to config**

In `api/config.py`, after line 51 (`PROJECTS_FILE = STATE_DIR / "projects.json"`), add:

```python
MEETINGS_FILE = STATE_DIR / "meetings.json"
```

- [ ] **Step 2: Write failing test for meetings store**

Create `tests/test_neo_meetings.py`:

```python
"""Tests for Neo Meetings backend."""
import json
import time
from pathlib import Path

import pytest

# Ensure api package is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def isolate_meetings(tmp_path, monkeypatch):
    """Point MEETINGS_FILE to a temp file so tests don't touch real data."""
    fake = tmp_path / "meetings.json"
    monkeypatch.setattr("api.config.MEETINGS_FILE", fake)
    import api.meetings as m
    monkeypatch.setattr(m, "MEETINGS_FILE", fake)
    yield fake


class TestMeetingsStore:
    def test_load_empty(self):
        from api.meetings import load_meetings
        result = load_meetings()
        assert result == []

    def test_create_meeting(self):
        from api.meetings import create_meeting, load_meetings
        meeting = create_meeting(
            title="Sprint Review",
            project="obreiro",
            objective="alinhamento",
            participants=["junior", "cliente"],
        )
        assert meeting["id"]
        assert meeting["title"] == "Sprint Review"
        assert meeting["status"] == "planned"
        assert meeting["room_url"].startswith("https://")
        stored = load_meetings()
        assert len(stored) == 1
        assert stored[0]["id"] == meeting["id"]

    def test_finish_meeting(self):
        from api.meetings import create_meeting, finish_meeting, load_meetings
        m = create_meeting(title="Test", project="test", objective="briefing")
        result = finish_meeting(m["id"])
        assert result["status"] == "finished"
        stored = load_meetings()
        assert stored[0]["status"] == "finished"

    def test_finish_nonexistent(self):
        from api.meetings import finish_meeting
        result = finish_meeting("nonexistent-id")
        assert result is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd /home/jrmelo/Projetos/neo-webui && python -m pytest tests/test_neo_meetings.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.meetings'`

- [ ] **Step 4: Implement `api/meetings.py`**

```python
"""Neo Meetings — local-first meeting storage and room URL generation."""

import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from api.config import MEETINGS_FILE

_LOCK = threading.RLock()

MEETING_STATUS_VALUES = {"planned", "active", "finished", "processed"}
OBJECTIVE_VALUES = {"alinhamento", "homologacao", "fechamento_sprint", "briefing", "suporte", "outro"}

# Configurable via env; defaults to public Jitsi instance for dev.
import os
MEET_BASE_URL = os.getenv("NEO_MEET_BASE_URL", "https://meet.jit.si")


def _now() -> float:
    return time.time()


def _generate_room_slug(project: str, title: str) -> str:
    ts = time.strftime("%Y%m%d-%H%M")
    suffix = secrets.token_hex(3)
    slug = f"{project}-{ts}-{suffix}".lower()
    # Sanitize: only alphanumeric, hyphens
    return "".join(c if c.isalnum() or c == "-" else "-" for c in slug)


def _load_store() -> list[dict]:
    if not MEETINGS_FILE.exists():
        return []
    try:
        data = json.loads(MEETINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_store(meetings: list[dict]) -> None:
    MEETINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEETINGS_FILE.write_text(json.dumps(meetings, ensure_ascii=False, indent=2), encoding="utf-8")


def load_meetings() -> list[dict]:
    with _LOCK:
        return _load_store()


def create_meeting(
    title: str,
    project: str,
    objective: str = "alinhamento",
    participants: list[str] | None = None,
) -> dict:
    room_slug = _generate_room_slug(project, title)
    meeting = {
        "id": str(uuid.uuid4()),
        "title": title.strip(),
        "project": project.strip(),
        "objective": objective if objective in OBJECTIVE_VALUES else "outro",
        "participants": participants or [],
        "room_slug": room_slug,
        "room_url": f"{MEET_BASE_URL}/{room_slug}",
        "status": "planned",
        "created_at": _now(),
        "started_at": None,
        "finished_at": None,
        "summary": None,
    }
    with _LOCK:
        store = _load_store()
        store.insert(0, meeting)
        _save_store(store)
    return meeting


def get_meeting(meeting_id: str) -> dict | None:
    with _LOCK:
        for m in _load_store():
            if m["id"] == meeting_id:
                return m
    return None


def start_meeting(meeting_id: str) -> dict | None:
    with _LOCK:
        store = _load_store()
        for m in store:
            if m["id"] == meeting_id:
                m["status"] = "active"
                m["started_at"] = _now()
                _save_store(store)
                return m
    return None


def finish_meeting(meeting_id: str) -> dict | None:
    with _LOCK:
        store = _load_store()
        for m in store:
            if m["id"] == meeting_id:
                m["status"] = "finished"
                m["finished_at"] = _now()
                _save_store(store)
                return m
    return None


def update_summary(meeting_id: str, summary: dict[str, Any]) -> dict | None:
    with _LOCK:
        store = _load_store()
        for m in store:
            if m["id"] == meeting_id:
                m["summary"] = summary
                m["status"] = "processed"
                _save_store(store)
                return m
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/jrmelo/Projetos/neo-webui && python -m pytest tests/test_neo_meetings.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add api/meetings.py api/config.py tests/test_neo_meetings.py
git commit -m "feat(meetings): add backend persistence layer with JSON store"
```

---

## Task 2: Backend — API Routes

**Files:**
- Modify: `api/routes.py:497` (add import)
- Modify: `api/routes.py` inside `handle_get` (add GET `/api/meetings`)
- Modify: `api/routes.py` inside `handle_post` (add POST routes)
- Test: `tests/test_neo_meetings.py` (extend)

- [ ] **Step 1: Write failing test for API endpoints**

Append to `tests/test_neo_meetings.py`:

```python
class TestMeetingsAPI:
    """Integration tests hitting the HTTP server."""

    @pytest.fixture(autouse=True)
    def server(self, live_server):
        self.base = live_server
        yield

    def _get(self, path):
        import urllib.request
        req = urllib.request.Request(f"{self.base}{path}")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def _post(self, path, data):
        import urllib.request
        body = json.dumps(data).encode()
        req = urllib.request.Request(f"{self.base}{path}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def test_get_meetings_empty(self):
        result = self._get("/api/meetings")
        assert result == {"meetings": []}

    def test_create_and_list(self):
        created = self._post("/api/meetings/create", {
            "title": "Sprint Review",
            "project": "obreiro",
            "objective": "alinhamento",
        })
        assert created["ok"] is True
        assert created["meeting"]["status"] == "planned"

        listed = self._get("/api/meetings")
        assert len(listed["meetings"]) == 1

    def test_start_and_finish(self):
        created = self._post("/api/meetings/create", {
            "title": "Test", "project": "test",
        })
        mid = created["meeting"]["id"]

        started = self._post(f"/api/meetings/{mid}/start", {})
        assert started["meeting"]["status"] == "active"

        finished = self._post(f"/api/meetings/{mid}/finish", {})
        assert finished["meeting"]["status"] == "finished"
```

Note: This test class uses the project's existing `live_server` fixture from `conftest.py`. If the fixture isn't available, these tests can be run manually against a dev server on port 8788.

- [ ] **Step 2: Add import in routes.py**

At `api/routes.py:498`, after `from api import jira as neo_jira`, add:

```python
from api import meetings as neo_meetings
```

- [ ] **Step 3: Add GET route in handle_get**

In `api/routes.py`, inside `handle_get`, after the workspaces block (~line 1297), add:

```python
    if parsed.path == "/api/meetings":
        return j(handler, {"meetings": neo_meetings.load_meetings()})

    if parsed.path.startswith("/api/meetings/") and not parsed.path.endswith("/start") and not parsed.path.endswith("/finish"):
        meeting_id = parsed.path[len("/api/meetings/"):]
        meeting = neo_meetings.get_meeting(meeting_id)
        if meeting:
            return j(handler, {"meeting": meeting})
        return j(handler, {"error": "not_found"}, status=404)
```

- [ ] **Step 4: Add POST routes in handle_post**

In `api/routes.py`, inside `handle_post`, after the projects/create block (~line 2185), add:

```python
    if parsed.path == "/api/meetings/create":
        title = body.get("title", "").strip()
        project = body.get("project", "").strip()
        if not title or not project:
            return j(handler, {"ok": False, "error": "title and project required"}, status=400)
        meeting = neo_meetings.create_meeting(
            title=title,
            project=project,
            objective=body.get("objective", "alinhamento"),
            participants=body.get("participants"),
        )
        return j(handler, {"ok": True, "meeting": meeting})

    if parsed.path.startswith("/api/meetings/") and parsed.path.endswith("/start"):
        meeting_id = parsed.path[len("/api/meetings/"):-len("/start")]
        result = neo_meetings.start_meeting(meeting_id)
        if result:
            return j(handler, {"ok": True, "meeting": result})
        return j(handler, {"ok": False, "error": "not_found"}, status=404)

    if parsed.path.startswith("/api/meetings/") and parsed.path.endswith("/finish"):
        meeting_id = parsed.path[len("/api/meetings/"):-len("/finish")]
        result = neo_meetings.finish_meeting(meeting_id)
        if result:
            return j(handler, {"ok": True, "meeting": result})
        return j(handler, {"ok": False, "error": "not_found"}, status=404)

    if parsed.path.startswith("/api/meetings/") and parsed.path.endswith("/summary"):
        meeting_id = parsed.path[len("/api/meetings/"):-len("/summary")]
        summary = body.get("summary")
        if not summary:
            return j(handler, {"ok": False, "error": "summary required"}, status=400)
        result = neo_meetings.update_summary(meeting_id, summary)
        if result:
            return j(handler, {"ok": True, "meeting": result})
        return j(handler, {"ok": False, "error": "not_found"}, status=404)
```

- [ ] **Step 5: Run tests**

Run: `cd /home/jrmelo/Projetos/neo-webui && python -m pytest tests/test_neo_meetings.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add api/routes.py tests/test_neo_meetings.py
git commit -m "feat(meetings): wire API routes GET/POST for meetings CRUD"
```

---

## Task 3: Frontend — Panel Registration (`panels.js`)

**Files:**
- Modify: `static/panels.js:17-24` (APP_TITLEBAR_KEYS, NEO_SHELL_PANELS)
- Modify: `static/panels.js:26-38` (MAIN_VIEW_CLASS_BY_PANEL)
- Modify: `static/panels.js:168-225` (switchPanel lazy-load)

- [ ] **Step 1: Register meetings in APP_TITLEBAR_KEYS**

In `static/panels.js:17-22`, change:

```javascript
const APP_TITLEBAR_KEYS = {
  chat: 'tab_conversations', dashboard: 'tab_dashboard', tasks: 'tab_automation', skills: 'tab_skills',
  memory: 'tab_memory', workspaces: 'tab_workspaces',
  profiles: 'tab_profiles', todos: 'tab_todos', settings: 'tab_settings',
  projects: 'tab_projects', agents: 'tab_agents',
};
```

To:

```javascript
const APP_TITLEBAR_KEYS = {
  chat: 'tab_conversations', dashboard: 'tab_dashboard', tasks: 'tab_automation', skills: 'tab_skills',
  memory: 'tab_memory', workspaces: 'tab_workspaces',
  profiles: 'tab_profiles', todos: 'tab_todos', settings: 'tab_settings',
  projects: 'tab_projects', agents: 'tab_agents', meetings: 'tab_meetings',
};
```

- [ ] **Step 2: Add meetings to NEO_SHELL_PANELS**

In `static/panels.js:24`, change:

```javascript
const NEO_SHELL_PANELS = new Set(['dashboard', 'chat', 'projects', 'profiles', 'agents', 'settings', 'skills', 'tasks']);
```

To:

```javascript
const NEO_SHELL_PANELS = new Set(['dashboard', 'chat', 'projects', 'profiles', 'agents', 'settings', 'skills', 'tasks', 'meetings']);
```

- [ ] **Step 3: Add meetings to MAIN_VIEW_CLASS_BY_PANEL**

In `static/panels.js:26-38`, add `meetings: 'showing-meetings'` after `profiles`:

```javascript
const MAIN_VIEW_CLASS_BY_PANEL = {
  dashboard: 'showing-dashboard',
  chat: 'showing-chat',
  projects: 'showing-projects',
  todos: 'showing-todos',
  agents: 'showing-agents',
  settings: 'showing-settings',
  skills: 'showing-skills',
  memory: 'showing-memory',
  tasks: 'showing-tasks',
  workspaces: 'showing-workspaces',
  profiles: 'showing-profiles',
  meetings: 'showing-meetings',
};
```

- [ ] **Step 4: Add lazy-load hook in switchPanel**

In `static/panels.js`, inside `switchPanel`, after the `projects` block (line ~215):

```javascript
  if (nextPanel === 'projects' && typeof loadProjectsCommandCenter === 'function') await loadProjectsCommandCenter();
```

Add:

```javascript
  if (nextPanel === 'meetings' && typeof loadMeetingsPanel === 'function') await loadMeetingsPanel();
```

- [ ] **Step 5: Commit**

```bash
git add static/panels.js
git commit -m "feat(meetings): register meetings panel in shell navigation"
```

---

## Task 4: Frontend — i18n Keys

**Files:**
- Modify: `static/i18n.js` (en section ~line 429, pt section ~line 5905)

- [ ] **Step 1: Add English keys**

In `static/i18n.js`, after `tab_settings: 'Settings',` (line ~444), add:

```javascript
    tab_meetings: 'Meetings',
```

After `action_deploy_project: 'Deploy Project',` (line ~853), add:

```javascript
    action_new_meeting: 'New Meeting',
    meetings_title: 'Meetings',
    meetings_subtitle: 'Create and manage project meetings',
    meetings_new: 'New Meeting',
    meetings_project: 'Project / Client',
    meetings_objective: 'Objective',
    meetings_participants: 'Participants',
    meetings_generate_room: 'Generate Room',
    meetings_open_tab: 'Open in new tab',
    meetings_end: 'End Meeting',
    meetings_status_planned: 'Planned',
    meetings_status_active: 'In progress',
    meetings_status_finished: 'Finished',
    meetings_status_processed: 'Processed',
    meetings_post_title: 'Post-Meeting',
    meetings_post_summary: 'Generate Summary',
    meetings_post_obsidian: 'Save to Obsidian',
    meetings_post_jira: 'Create Jira Task',
    meetings_empty: 'No meetings yet. Create your first one!',
    meetings_obj_alinhamento: 'Alignment',
    meetings_obj_homologacao: 'Homologation',
    meetings_obj_fechamento_sprint: 'Sprint Closing',
    meetings_obj_briefing: 'Briefing',
    meetings_obj_suporte: 'Support',
    meetings_obj_outro: 'Other',
```

- [ ] **Step 2: Add Portuguese keys**

In `static/i18n.js`, in the `pt:` section, after `tab_settings: 'Configurações',` (line ~5920), add:

```javascript
    tab_meetings: 'Reuniões',
```

And in the appropriate area of the pt section (after action keys), add:

```javascript
    action_new_meeting: 'Nova Reunião',
    meetings_title: 'Reuniões',
    meetings_subtitle: 'Crie e gerencie reuniões de projetos',
    meetings_new: 'Nova Reunião',
    meetings_project: 'Projeto / Cliente',
    meetings_objective: 'Objetivo',
    meetings_participants: 'Participantes',
    meetings_generate_room: 'Gerar Sala',
    meetings_open_tab: 'Abrir em nova aba',
    meetings_end: 'Encerrar Reunião',
    meetings_status_planned: 'Planejada',
    meetings_status_active: 'Em andamento',
    meetings_status_finished: 'Finalizada',
    meetings_status_processed: 'Processada',
    meetings_post_title: 'Pós-Reunião',
    meetings_post_summary: 'Gerar Resumo',
    meetings_post_obsidian: 'Salvar no Obsidian',
    meetings_post_jira: 'Criar Tarefa Jira',
    meetings_empty: 'Nenhuma reunião ainda. Crie a primeira!',
    meetings_obj_alinhamento: 'Alinhamento',
    meetings_obj_homologacao: 'Homologação',
    meetings_obj_fechamento_sprint: 'Fechamento de Sprint',
    meetings_obj_briefing: 'Briefing',
    meetings_obj_suporte: 'Suporte',
    meetings_obj_outro: 'Outro',
```

- [ ] **Step 3: Commit**

```bash
git add static/i18n.js
git commit -m "feat(meetings): add i18n keys for meetings panel (en + pt-BR)"
```

---

## Task 5: Frontend — HTML Structure (sidebar + main view div)

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: Add rail button (desktop sidebar)**

In `static/index.html`, after the `profiles` rail button (line ~98), before the settings button (line ~101), add:

```html
    <button class="rail-btn nav-tab" data-panel="meetings" onclick="switchPanel('meetings')" title="Meetings" data-i18n-title="tab_meetings" aria-label="Meetings"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 10l5-3v10l-5-3V10z"/><rect x="3" y="7" width="12" height="10" rx="2"/></svg></button>
```

- [ ] **Step 2: Add mobile bottom nav button**

In `static/index.html`, after the `profiles` mobile nav button (line ~112), before settings (line ~115), add:

```html
      <button class="nav-tab" data-panel="meetings" data-label="Meetings" onclick="switchPanel('meetings')" title="Meetings" data-i18n-title="tab_meetings"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 10l5-3v10l-5-3V10z"/><rect x="3" y="7" width="12" height="10" rx="2"/></svg></button>
```

- [ ] **Step 3: Add dashboard menu item**

In `static/index.html`, after the `profiles` dashboard menu item (line ~131), add:

```html
        <button class="neo-dashboard-menu-item" data-neo-menu-item data-panel="meetings" onclick="switchPanel('meetings')"><span aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M15 10l5-3v10l-5-3V10z"/><rect x="3" y="7" width="12" height="10" rx="2"/></svg></span><span data-i18n="tab_meetings">Reuniões</span></button>
```

- [ ] **Step 4: Replace `new_component` quick action with `new_meeting`**

In `static/index.html`, replace the `new_component` button (lines ~342-345):

```html
                  <button class="dashboard-quick-action" type="button" data-dashboard-action="new_component" onclick="handleDashboardQuickAction('new_component')">
                    <span class="dashboard-quick-action-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m12 2 8 4v12l-8 4-8-4V6z"/><path d="M12 22V12M20 6l-8 6-8-6"/></svg></span>
                    <span data-i18n="action_new_component">Novo Componente</span>
```

With:

```html
                  <button class="dashboard-quick-action" type="button" data-dashboard-action="new_meeting" onclick="handleDashboardQuickAction('new_meeting')">
                    <span class="dashboard-quick-action-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M15 10l5-3v10l-5-3V10z"/><rect x="3" y="7" width="12" height="10" rx="2"/></svg></span>
                    <span data-i18n="action_new_meeting">Nova Reunião</span>
```

- [ ] **Step 5: Add mainMeetings div**

In `static/index.html`, after `<div id="mainWorkspaces" class="main-view">` and its closing `</div>` (after line ~1105), add:

```html
    <div id="mainMeetings" class="main-view">
      <div class="meetings-panel" id="meetingsPanel">
        <header class="meetings-header">
          <div>
            <h2 data-i18n="meetings_title">Reuniões</h2>
            <p class="meetings-subtitle" data-i18n="meetings_subtitle">Crie e gerencie reuniões de projetos</p>
          </div>
          <button class="btn btn-primary meetings-new-btn" onclick="showMeetingForm()" data-i18n="meetings_new">Nova Reunião</button>
        </header>
        <div id="meetingsContent" class="meetings-content"></div>
      </div>
    </div>
```

- [ ] **Step 6: Add script tag for meetings.js**

In `static/index.html`, near the other script tags (after `dashboard.js`), add:

```html
  <script src="/static/meetings.js?v=__WEBUI_VERSION__"></script>
```

- [ ] **Step 7: Commit**

```bash
git add static/index.html
git commit -m "feat(meetings): add HTML structure — sidebar, nav, main view, quick action"
```

---

## Task 6: Frontend — Dashboard Quick Action Handler

**Files:**
- Modify: `static/dashboard.js:15-21` (DASHBOARD_QUICK_ACTION_PROMPTS)
- Modify: `static/dashboard.js:347-367` (handleDashboardQuickAction)

- [ ] **Step 1: Replace new_component in DASHBOARD_QUICK_ACTION_PROMPTS**

In `static/dashboard.js:18`, change:

```javascript
  new_component: 'Quero criar um novo componente. Me ajude a definir escopo, estados e comportamento esperado.',
```

To:

```javascript
  new_meeting: null,
```

(null signals that this action doesn't use the chat prompt flow)

- [ ] **Step 2: Add new_meeting handler in handleDashboardQuickAction**

In `static/dashboard.js:347`, change:

```javascript
function handleDashboardQuickAction(action) {
  if (action === 'open_terminal') {
```

To:

```javascript
function handleDashboardQuickAction(action) {
  if (action === 'new_meeting') {
    switchPanel('meetings');
    return;
  }
  if (action === 'open_terminal') {
```

- [ ] **Step 3: Commit**

```bash
git add static/dashboard.js
git commit -m "feat(meetings): wire new_meeting quick action to meetings panel"
```

---

## Task 7: Frontend — Meetings Panel Logic (`static/meetings.js`)

**Files:**
- Create: `static/meetings.js`

- [ ] **Step 1: Create meetings.js with panel loader and renderer**

Create `static/meetings.js`:

```javascript
/* Neo Meetings panel — room creation, Jitsi embed, post-meeting flow. */

let _meetingsLoaded = false;
let _meetingsData = [];
let _activeMeeting = null;

async function loadMeetingsPanel() {
  const container = document.getElementById('meetingsContent');
  if (!container) return;
  try {
    const resp = await fetch('/api/meetings');
    const data = await resp.json();
    _meetingsData = data.meetings || [];
  } catch (e) {
    _meetingsData = [];
  }
  _meetingsLoaded = true;
  renderMeetingsPanel();
}

function renderMeetingsPanel() {
  const container = document.getElementById('meetingsContent');
  if (!container) return;

  if (_activeMeeting && _activeMeeting.status === 'active') {
    renderActiveMeeting(container);
    return;
  }

  if (_activeMeeting && _activeMeeting.status === 'finished') {
    renderPostMeeting(container);
    return;
  }

  let html = '';

  // Creation form
  html += renderMeetingForm();

  // Meeting list
  if (_meetingsData.length === 0) {
    html += `<p class="meetings-empty" data-i18n="meetings_empty">${t('meetings_empty')}</p>`;
  } else {
    html += '<div class="meetings-list">';
    for (const m of _meetingsData) {
      html += renderMeetingCard(m);
    }
    html += '</div>';
  }

  container.innerHTML = html;
}

function renderMeetingForm() {
  return `
    <div class="meetings-form" id="meetingsForm">
      <div class="meetings-form-row">
        <label for="meetingTitle">${t('title') || 'Title'}</label>
        <input type="text" id="meetingTitle" class="input" placeholder="Sprint Review, Briefing..." />
      </div>
      <div class="meetings-form-row">
        <label for="meetingProject" data-i18n="meetings_project">${t('meetings_project')}</label>
        <input type="text" id="meetingProject" class="input" placeholder="obreiro, brabus, 300..." />
      </div>
      <div class="meetings-form-row">
        <label for="meetingObjective" data-i18n="meetings_objective">${t('meetings_objective')}</label>
        <select id="meetingObjective" class="input">
          <option value="alinhamento">${t('meetings_obj_alinhamento')}</option>
          <option value="homologacao">${t('meetings_obj_homologacao')}</option>
          <option value="fechamento_sprint">${t('meetings_obj_fechamento_sprint')}</option>
          <option value="briefing">${t('meetings_obj_briefing')}</option>
          <option value="suporte">${t('meetings_obj_suporte')}</option>
          <option value="outro">${t('meetings_obj_outro')}</option>
        </select>
      </div>
      <div class="meetings-form-row">
        <label for="meetingParticipants" data-i18n="meetings_participants">${t('meetings_participants')}</label>
        <input type="text" id="meetingParticipants" class="input" placeholder="nome1, nome2..." />
      </div>
      <button class="btn btn-primary" onclick="createMeetingFromForm()" data-i18n="meetings_generate_room">${t('meetings_generate_room')}</button>
    </div>
  `;
}

function renderMeetingCard(meeting) {
  const statusKey = 'meetings_status_' + meeting.status;
  const statusLabel = t(statusKey) || meeting.status;
  const date = new Date(meeting.created_at * 1000).toLocaleDateString();
  return `
    <div class="meetings-card meetings-card--${meeting.status}" data-meeting-id="${meeting.id}">
      <div class="meetings-card-header">
        <strong>${_esc(meeting.title)}</strong>
        <span class="meetings-card-status badge badge--${meeting.status}">${statusLabel}</span>
      </div>
      <div class="meetings-card-meta">
        <span>${_esc(meeting.project)}</span> · <span>${date}</span>
      </div>
      ${meeting.status === 'planned' ? `<button class="btn btn-sm" onclick="joinMeeting('${meeting.id}')">▶ ${t('meetings_generate_room')}</button>` : ''}
      ${meeting.status === 'finished' ? `<button class="btn btn-sm" onclick="openPostMeeting('${meeting.id}')">📋 ${t('meetings_post_title')}</button>` : ''}
    </div>
  `;
}

function _esc(str) {
  const el = document.createElement('span');
  el.textContent = str || '';
  return el.innerHTML;
}

async function createMeetingFromForm() {
  const title = document.getElementById('meetingTitle')?.value?.trim();
  const project = document.getElementById('meetingProject')?.value?.trim();
  const objective = document.getElementById('meetingObjective')?.value || 'alinhamento';
  const participantsRaw = document.getElementById('meetingParticipants')?.value || '';
  const participants = participantsRaw.split(',').map(s => s.trim()).filter(Boolean);

  if (!title || !project) {
    if (typeof showToast === 'function') showToast('Title and project required', 2500, 'warning');
    return;
  }

  try {
    const resp = await fetch('/api/meetings/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, project, objective, participants }),
    });
    const data = await resp.json();
    if (data.ok) {
      _activeMeeting = data.meeting;
      await startAndEmbed(data.meeting);
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Error', 2500, 'error');
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Network error', 2500, 'error');
  }
}

async function joinMeeting(meetingId) {
  try {
    const resp = await fetch(`/api/meetings/${meetingId}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const data = await resp.json();
    if (data.ok) {
      _activeMeeting = data.meeting;
      renderMeetingsPanel();
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Error starting meeting', 2500, 'error');
  }
}

async function startAndEmbed(meeting) {
  try {
    await fetch(`/api/meetings/${meeting.id}/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    _activeMeeting.status = 'active';
  } catch (e) { /* proceed anyway */ }
  renderMeetingsPanel();
}
```

- [ ] **Step 2: Commit partial (form + list + create)**

```bash
git add static/meetings.js
git commit -m "feat(meetings): add meetings.js — form, list, create flow"
```

---

## Task 8: Frontend — Jitsi Iframe Embed + Active Meeting View

**Files:**
- Modify: `static/meetings.js` (append)

- [ ] **Step 1: Add active meeting renderer with Jitsi iframe**

Append to `static/meetings.js`:

```javascript
function renderActiveMeeting(container) {
  const m = _activeMeeting;
  container.innerHTML = `
    <div class="meetings-active">
      <div class="meetings-active-header">
        <h3>${_esc(m.title)}</h3>
        <span class="badge badge--active">${t('meetings_status_active')}</span>
      </div>
      <div class="meetings-active-actions">
        <a href="${_esc(m.room_url)}" target="_blank" rel="noopener" class="btn btn-sm">${t('meetings_open_tab')}</a>
        <button class="btn btn-sm btn-danger" onclick="endCurrentMeeting()">⏹ ${t('meetings_end')}</button>
      </div>
      <div class="meetings-iframe-wrapper" id="meetingsIframeWrapper">
        <iframe
          id="meetingsJitsiFrame"
          src="${_esc(m.room_url)}"
          allow="camera; microphone; display-capture; autoplay; clipboard-write"
          allowfullscreen
          style="width:100%; height:100%; border:none; border-radius:8px;"
        ></iframe>
      </div>
    </div>
  `;
}

async function endCurrentMeeting() {
  if (!_activeMeeting) return;
  try {
    const resp = await fetch(`/api/meetings/${_activeMeeting.id}/finish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
    const data = await resp.json();
    if (data.ok) {
      _activeMeeting = data.meeting;
      renderMeetingsPanel();
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Error ending meeting', 2500, 'error');
  }
}
```

- [ ] **Step 2: Commit**

```bash
git add static/meetings.js
git commit -m "feat(meetings): add Jitsi iframe embed and active meeting view"
```

---

## Task 9: Frontend — Post-Meeting Flow

**Files:**
- Modify: `static/meetings.js` (append)

- [ ] **Step 1: Add post-meeting renderer**

Append to `static/meetings.js`:

```javascript
function renderPostMeeting(container) {
  const m = _activeMeeting;
  container.innerHTML = `
    <div class="meetings-post">
      <div class="meetings-post-header">
        <h3>${_esc(m.title)} — ${t('meetings_post_title')}</h3>
        <span class="badge badge--finished">${t('meetings_status_finished')}</span>
      </div>
      <div class="meetings-post-info">
        <p><strong>${t('meetings_project')}:</strong> ${_esc(m.project)}</p>
        <p><strong>${t('meetings_objective')}:</strong> ${t('meetings_obj_' + m.objective)}</p>
        ${m.participants.length ? `<p><strong>${t('meetings_participants')}:</strong> ${m.participants.map(_esc).join(', ')}</p>` : ''}
      </div>
      <div class="meetings-post-actions">
        <button class="btn btn-primary" onclick="generateMeetingSummary()">
          📝 ${t('meetings_post_summary')}
        </button>
        <button class="btn btn-sm" onclick="saveMeetingToObsidian()" disabled title="Phase 2">
          📓 ${t('meetings_post_obsidian')}
        </button>
        <button class="btn btn-sm" onclick="createMeetingJiraTask()" disabled title="Phase 2">
          🎫 ${t('meetings_post_jira')}
        </button>
      </div>
      <div id="meetingsSummaryOutput" class="meetings-summary-output"></div>
      <div class="meetings-post-footer">
        <button class="btn btn-sm" onclick="closeMeetingView()">← ${t('tab_meetings')}</button>
      </div>
    </div>
  `;
}

function openPostMeeting(meetingId) {
  const meeting = _meetingsData.find(m => m.id === meetingId);
  if (meeting) {
    _activeMeeting = meeting;
    renderMeetingsPanel();
  }
}

function generateMeetingSummary() {
  if (!_activeMeeting) return;
  const prompt = `Reunião "${_activeMeeting.title}" (projeto: ${_activeMeeting.project}, objetivo: ${_activeMeeting.objective}) acaba de terminar. ` +
    `Participantes: ${_activeMeeting.participants.join(', ') || 'não informados'}. ` +
    `Gere um resumo estruturado com: 1) Resumo objetivo, 2) Decisões tomadas, 3) Pendências e responsáveis, 4) Tarefas candidatas para Jira, 5) Próximos passos.`;

  // Switch to chat panel and inject prompt
  if (typeof switchPanel === 'function') switchPanel('chat');
  setTimeout(() => {
    const input = document.getElementById('msg');
    if (input) {
      input.value = prompt;
      input.dispatchEvent(new Event('input', { bubbles: true }));
      if (typeof showToast === 'function') showToast(t('meetings_post_summary'), 2000, 'info');
    }
  }, 300);
}

function saveMeetingToObsidian() {
  // Phase 2: will call Neo to persist structured note
  if (typeof showToast === 'function') showToast('Phase 2 — not yet implemented', 2500, 'info');
}

function createMeetingJiraTask() {
  // Phase 2: will call /api/jira/create with meeting context
  if (typeof showToast === 'function') showToast('Phase 2 — not yet implemented', 2500, 'info');
}

function closeMeetingView() {
  _activeMeeting = null;
  loadMeetingsPanel();
}

function showMeetingForm() {
  _activeMeeting = null;
  renderMeetingsPanel();
  setTimeout(() => {
    document.getElementById('meetingTitle')?.focus();
  }, 100);
}
```

- [ ] **Step 2: Commit**

```bash
git add static/meetings.js
git commit -m "feat(meetings): add post-meeting flow with summary prompt generation"
```

---

## Task 10: Frontend — CSS Styles

**Files:**
- Modify: `static/style.css` (append at end)

- [ ] **Step 1: Add meetings panel styles**

Append to `static/style.css`:

```css
/* ── Meetings Panel ─────────────────────────────────────────────── */
.meetings-panel { padding: 1.5rem; max-width: 900px; margin: 0 auto; }
.meetings-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; gap: 1rem; flex-wrap: wrap; }
.meetings-header h2 { margin: 0; font-size: 1.4rem; }
.meetings-subtitle { margin: 0.25rem 0 0; opacity: 0.7; font-size: 0.85rem; }
.meetings-new-btn { white-space: nowrap; }

.meetings-form { background: var(--bg-secondary, #1a1a2e); border-radius: 12px; padding: 1.25rem; margin-bottom: 1.5rem; display: flex; flex-direction: column; gap: 0.75rem; }
.meetings-form-row { display: flex; flex-direction: column; gap: 0.25rem; }
.meetings-form-row label { font-size: 0.8rem; font-weight: 500; opacity: 0.8; }

.meetings-list { display: flex; flex-direction: column; gap: 0.75rem; }
.meetings-card { background: var(--bg-secondary, #1a1a2e); border-radius: 10px; padding: 1rem; border-left: 3px solid var(--accent, #6c63ff); }
.meetings-card--active { border-left-color: #4caf50; }
.meetings-card--finished { border-left-color: #ff9800; }
.meetings-card--processed { border-left-color: #2196f3; }
.meetings-card-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 0.5rem; }
.meetings-card-meta { font-size: 0.8rem; opacity: 0.7; margin-bottom: 0.5rem; }

.meetings-empty { text-align: center; opacity: 0.6; padding: 2rem; }

.meetings-active { display: flex; flex-direction: column; height: 100%; }
.meetings-active-header { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; }
.meetings-active-header h3 { margin: 0; }
.meetings-active-actions { display: flex; gap: 0.5rem; margin-bottom: 1rem; flex-wrap: wrap; }
.meetings-iframe-wrapper { flex: 1; min-height: 400px; border-radius: 8px; overflow: hidden; background: #000; }

.meetings-post { padding: 0.5rem 0; }
.meetings-post-header { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 1rem; }
.meetings-post-header h3 { margin: 0; }
.meetings-post-info { margin-bottom: 1.25rem; font-size: 0.9rem; }
.meetings-post-info p { margin: 0.25rem 0; }
.meetings-post-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1.5rem; }
.meetings-summary-output { min-height: 2rem; }
.meetings-post-footer { margin-top: 1.5rem; padding-top: 1rem; border-top: 1px solid var(--border, #333); }

.badge--planned { background: var(--accent, #6c63ff); color: #fff; }
.badge--active { background: #4caf50; color: #fff; }
.badge--finished { background: #ff9800; color: #fff; }
.badge--processed { background: #2196f3; color: #fff; }

/* Responsive */
@media (max-width: 768px) {
  .meetings-panel { padding: 1rem; }
  .meetings-header { flex-direction: column; align-items: flex-start; }
  .meetings-iframe-wrapper { min-height: 280px; }
}

/* Main view toggle */
main.main.showing-meetings #mainMeetings { display: block; }
main.main.showing-meetings .main-view:not(#mainMeetings) { display: none; }
```

- [ ] **Step 2: Commit**

```bash
git add static/style.css
git commit -m "feat(meetings): add CSS for meetings panel, cards, iframe, post-meeting"
```

---

## Task 11: Integration Test — Panel Navigation

**Files:**
- Modify: `tests/test_neo_meetings.py` (append)

- [ ] **Step 1: Add frontend integration test**

Append to `tests/test_neo_meetings.py`:

```python
class TestMeetingsPanelRegistration:
    """Verify the meetings panel is properly wired in the frontend."""

    def test_panels_js_has_meetings(self):
        panels_js = (Path(__file__).parent.parent / "static" / "panels.js").read_text()
        assert "meetings: 'tab_meetings'" in panels_js
        assert "'meetings'" in panels_js  # in NEO_SHELL_PANELS
        assert "showing-meetings" in panels_js

    def test_index_html_has_meetings_elements(self):
        index_html = (Path(__file__).parent.parent / "static" / "index.html").read_text()
        assert 'data-panel="meetings"' in index_html
        assert 'id="mainMeetings"' in index_html
        assert 'data-dashboard-action="new_meeting"' in index_html

    def test_i18n_has_meetings_keys(self):
        i18n_js = (Path(__file__).parent.parent / "static" / "i18n.js").read_text()
        assert "tab_meetings" in i18n_js
        assert "meetings_title" in i18n_js
        assert "action_new_meeting" in i18n_js

    def test_dashboard_handles_new_meeting(self):
        dashboard_js = (Path(__file__).parent.parent / "static" / "dashboard.js").read_text()
        assert "new_meeting" in dashboard_js

    def test_style_has_meetings_classes(self):
        style_css = (Path(__file__).parent.parent / "static" / "style.css").read_text()
        assert ".meetings-panel" in style_css
        assert ".meetings-iframe-wrapper" in style_css
        assert "showing-meetings" in style_css
```

- [ ] **Step 2: Run all tests**

Run: `cd /home/jrmelo/Projetos/neo-webui && python -m pytest tests/test_neo_meetings.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_neo_meetings.py
git commit -m "test(meetings): add integration tests for panel registration and API"
```

---

## Task 12: Final Validation

- [ ] **Step 1: Run full test suite to check for regressions**

Run: `cd /home/jrmelo/Projetos/neo-webui && python -m pytest tests/ -x --timeout=60 -q`
Expected: No failures in existing tests

- [ ] **Step 2: Start dev server and verify visually**

Run: `cd /home/jrmelo/Projetos/neo-webui && python server.py`

Verify:
1. Sidebar shows "Reuniões" button with video icon
2. Clicking it activates meetings panel with form
3. Dashboard quick action "Nova Reunião" navigates to meetings panel
4. Creating a meeting generates a Jitsi URL and shows iframe
5. "Open in new tab" link works
6. "End Meeting" transitions to post-meeting view
7. "Generate Summary" switches to chat with structured prompt
8. Mobile bottom nav shows meetings button
9. No visual regressions in dashboard/chat/projects

- [ ] **Step 3: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix(meetings): address visual/integration issues from manual testing"
```

---

## Summary

| Task | Description | Estimated |
|------|-------------|-----------|
| 1 | Backend persistence (`api/meetings.py`) | 30 min |
| 2 | API routes | 20 min |
| 3 | Panel registration (`panels.js`) | 10 min |
| 4 | i18n keys | 15 min |
| 5 | HTML structure | 20 min |
| 6 | Dashboard quick action | 5 min |
| 7 | meetings.js — form + list + create | 30 min |
| 8 | Jitsi iframe embed | 15 min |
| 9 | Post-meeting flow | 20 min |
| 10 | CSS styles | 15 min |
| 11 | Integration tests | 15 min |
| 12 | Final validation | 15 min |
| **Total** | | **~3.5h** |
