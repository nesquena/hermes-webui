# Escritório (Agents Panel) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the placeholder "Agentes" tab into a working "Escritório" panel that renders the pixel-agents Canvas2D visualization consuming real agent activity via SSE from the Neo WebUI backend.

**Architecture:** Pre-process pixel-agents sprites into a JSON bundle at build time (in the pixel-agents-standalone fork). Serve the Vite-built React app as static files from `static/agents-app/`. Add a Python SSE endpoint (`GET /api/agents/stream`) that reads active sessions from `state.db` and translates Hermes tool events into `ServerMessage` protocol. The frontend loads lazily when the "Escritório" tab is selected.

**Tech Stack:** Python stdlib HTTP (existing server.py), SQLite (state.db), SSE, React/Canvas2D bundle (pre-built), vanilla JS bridge in neo-webui.

---

### Task 1: Pre-process Sprites into JSON Bundle

**Files:**
- Create: `pixel-agents-standalone/scripts/export-sprites-json.ts`
- Output: `pixel-agents-standalone/dist/sprites-bundle.json`

- [ ] Create a Node script that reuses `server/assetLoader.ts` to load all sprites (characters, walls, floors, furniture) and writes them as a single JSON file.

```typescript
// scripts/export-sprites-json.ts
import { loadCharacterSprites, loadWallTiles, loadFloorTiles, loadFurnitureAssets, loadDefaultLayout } from "../server/assetLoader.js";
import { writeFileSync, mkdirSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const assetsRoot = join(__dirname, "..", "webview-ui", "public", "assets");

const bundle = {
  characters: loadCharacterSprites(assetsRoot),
  walls: loadWallTiles(assetsRoot),
  floors: loadFloorTiles(assetsRoot),
  furniture: loadFurnitureAssets(assetsRoot),
  layout: loadDefaultLayout(assetsRoot),
};

mkdirSync(join(__dirname, "..", "dist"), { recursive: true });
writeFileSync(join(__dirname, "..", "dist", "sprites-bundle.json"), JSON.stringify(bundle));
console.log("Sprites bundle written to dist/sprites-bundle.json");
```

- [ ] Run: `cd ~/Projetos/pixel-agents-standalone && npx tsx scripts/export-sprites-json.ts`
- [ ] Verify `dist/sprites-bundle.json` exists and contains character data.

---

### Task 2: Build Neo Frontend Bundle

**Files:**
- Modify: `pixel-agents-standalone/webview-ui/vite.config.neo.ts` (already created in PoC)
- Modify: `pixel-agents-standalone/webview-ui/src/neo/sse-client.ts` (make URL configurable from host page)
- Output: `pixel-agents-standalone/dist/public-neo/`

- [ ] Update `sse-client.ts` to read SSE URL from `window.__NEO_SSE_URL` (set by host page) with fallback:

```typescript
const SSE_URL = (window as any).__NEO_SSE_URL || import.meta.env.VITE_NEO_SSE_URL || "/api/agents/stream";
```

- [ ] Build the Neo frontend:

```bash
cd ~/Projetos/pixel-agents-standalone/webview-ui
npx vite build --config vite.config.neo.ts
```

- [ ] Verify `dist/public-neo/index.html` and JS assets exist.

---

### Task 3: Copy Bundle to neo-webui

**Files:**
- Create: `neo-webui/static/agents-app/` (directory with built assets)
- Create: `neo-webui/static/agents-app/sprites-bundle.json`

- [ ] Copy built frontend:

```bash
cp -r ~/Projetos/pixel-agents-standalone/dist/public-neo/* ~/Projetos/neo-webui/static/agents-app/
cp ~/Projetos/pixel-agents-standalone/dist/sprites-bundle.json ~/Projetos/neo-webui/static/agents-app/
```

- [ ] Verify `static/agents-app/index.html` exists.

---

### Task 4: Backend SSE Endpoint

**Files:**
- Create: `neo-webui/api/agents_activity.py`
- Modify: `neo-webui/api/routes.py` (add GET /api/agents/stream route)

- [ ] Create `api/agents_activity.py` with:
  - Function to read active sessions from `state.db` (reuse pattern from `api/agent_sessions.py`)
  - SSE generator that emits init sequence (sprites bundle + existing agents + layout) then streams agent activity
  - Translation of Hermes tool events to `ServerMessage` format

```python
"""
Neo Agents Activity — SSE endpoint for pixel-agents visualization.
Reads state.db for active sessions and emits ServerMessage events.
"""

import json
import time
import threading
from pathlib import Path

SPRITES_BUNDLE_PATH = Path(__file__).parent.parent / "static" / "agents-app" / "sprites-bundle.json"

_sprites_cache = None

def _load_sprites():
    global _sprites_cache
    if _sprites_cache is None and SPRITES_BUNDLE_PATH.exists():
        _sprites_cache = json.loads(SPRITES_BUNDLE_PATH.read_text())
    return _sprites_cache

def get_active_sessions(state_db_path):
    """Read active sessions from state.db."""
    import sqlite3
    if not Path(state_db_path).exists():
        return []
    conn = sqlite3.connect(state_db_path, timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT session_id, project, parent_session_id, created_at "
            "FROM sessions WHERE ended_at IS NULL ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        conn.close()

def generate_init_events(state_db_path):
    """Generate SSE init sequence."""
    sprites = _load_sprites()
    
    # Settings
    yield _sse_event({"type": "settingsLoaded", "soundEnabled": False})
    
    # Sprites
    if sprites:
        if sprites.get("characters"):
            yield _sse_event({"type": "characterSpritesLoaded", "characters": sprites["characters"]["characters"]})
        if sprites.get("walls"):
            yield _sse_event({"type": "wallTilesLoaded", "sprites": sprites["walls"]["sprites"]})
        if sprites.get("floors"):
            yield _sse_event({"type": "floorTilesLoaded", "sprites": sprites["floors"]["sprites"]})
        if sprites.get("furniture"):
            yield _sse_event({"type": "furnitureAssetsLoaded", "catalog": sprites["furniture"]["catalog"], "sprites": sprites["furniture"]["sprites"]})
    
    # Existing agents
    sessions = get_active_sessions(state_db_path)
    agent_ids = []
    folder_names = {}
    for i, s in enumerate(sessions, 1):
        if not s.get("parent_session_id"):
            agent_ids.append(i)
            folder_names[i] = s.get("project") or "Neo"
    
    yield _sse_event({"type": "existingAgents", "agents": agent_ids, "folderNames": folder_names, "agentMeta": {}})
    
    # Layout
    layout = sprites.get("layout") if sprites else None
    version = 1 if layout else 0
    yield _sse_event({"type": "layoutLoaded", "layout": layout, "version": version})

def _sse_event(data):
    return f"data: {json.dumps(data)}\n\n"

def sse_heartbeat():
    return ": heartbeat\n\n"
```

- [ ] Add route in `api/routes.py`:

In the `handle_get` function, add before the 404 fallback:

```python
if parsed.path == "/api/agents/stream":
    from api.agents_activity import generate_init_events, sse_heartbeat
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    state_db = str(Path.home() / ".hermes" / "state.db")
    try:
        for event in generate_init_events(state_db):
            handler.wfile.write(event.encode())
            handler.wfile.flush()
        # Keep-alive loop with heartbeat
        while True:
            time.sleep(30)
            handler.wfile.write(sse_heartbeat().encode())
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    return True
```

---

### Task 5: Frontend Integration — Mount/Restore in Dashboard Shell

**Files:**
- Modify: `neo-webui/static/dashboard.js` (add mountDashboardAgents/restoreDashboardAgents)
- Modify: `neo-webui/static/panels.js` (wire mount/restore for agents panel)
- Modify: `neo-webui/static/index.html` (replace placeholder with iframe container)

- [ ] In `static/index.html`, replace the agents placeholder content (lines 756-769):

```html
<div id="mainAgents" class="main-view">
  <div class="main-view-header">
    <div>
      <div class="main-view-title" data-i18n="tab_agents">Escritório</div>
      <div class="main-view-subtitle" data-i18n="neo_agents_subtitle">Mapa de subagentes ativos</div>
    </div>
  </div>
  <div class="main-view-body agents-app-container">
    <iframe id="agentsAppFrame" class="agents-app-frame" style="display:none;"></iframe>
    <div id="agentsEmptyState" class="neo-placeholder-panel">
      <div class="main-view-empty-title" data-i18n="neo_agents_empty">Nenhum agente trabalhando agora</div>
      <div class="main-view-empty-sub" data-i18n="neo_agents_empty_sub">Quando o Neo iniciar uma sessão, os agentes aparecerão aqui.</div>
    </div>
  </div>
</div>
```

- [ ] In `static/dashboard.js`, add mount/restore functions:

```javascript
function mountDashboardAgents() {
  const frame = document.getElementById('agentsAppFrame');
  if (!frame) return;
  if (!frame.src || frame.src === 'about:blank') {
    frame.src = '/static/agents-app/index.html';
  }
  frame.style.display = 'block';
  const empty = document.getElementById('agentsEmptyState');
  if (empty) empty.style.display = 'none';
}

function restoreDashboardAgents() {
  const frame = document.getElementById('agentsAppFrame');
  if (!frame) return;
  frame.style.display = 'none';
  const empty = document.getElementById('agentsEmptyState');
  if (empty) empty.style.display = '';
}
```

- [ ] In `static/panels.js`, add restore call and mount trigger:

After line 196 (`restoreDashboardTasks`), add:
```javascript
if (nextPanel !== 'agents' && typeof restoreDashboardAgents === 'function') restoreDashboardAgents();
```

After the tasks mount block (~line 203), add:
```javascript
if (nextPanel === 'agents') {
  if (typeof mountDashboardAgents === 'function') mountDashboardAgents();
}
```

---

### Task 6: Rename "Agentes" → "Escritório" in i18n

**Files:**
- Modify: `neo-webui/static/i18n.js`

- [ ] Update all locale blocks:

For `pt-BR` (and `pt`):
```javascript
tab_agents: 'Escritório',
neo_agents_subtitle: 'Visualização dos agentes em tempo real',
neo_agents_empty: 'Nenhum agente trabalhando agora',
neo_agents_empty_sub: 'Quando o Neo iniciar uma sessão, os agentes aparecerão aqui.',
```

For `en`:
```javascript
tab_agents: 'Office',
neo_agents_subtitle: 'Real-time agent visualization',
neo_agents_empty: 'No agents working right now',
neo_agents_empty_sub: 'When Neo starts a session, agents will appear here.',
```

For `es`:
```javascript
tab_agents: 'Oficina',
neo_agents_subtitle: 'Visualización de agentes en tiempo real',
neo_agents_empty: 'Ningún agente trabajando ahora',
neo_agents_empty_sub: 'Cuando Neo inicie una sesión, los agentes aparecerán aquí.',
```

---

### Task 7: CSS for Agents App Container

**Files:**
- Modify: `neo-webui/static/style.css`

- [ ] Add styles for the iframe container:

```css
/* ── Agents App (Escritório) ─────────────────────────────────── */
.agents-app-container {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 400px;
  overflow: hidden;
}
.agents-app-frame {
  width: 100%;
  height: 100%;
  min-height: 400px;
  border: none;
  border-radius: var(--radius-lg, 12px);
  background: var(--surface, #0d1117);
}
```

---

### Task 8: Verification

- [ ] Start neo-webui server: `python3 server.py`
- [ ] Navigate to Escritório tab in browser
- [ ] Verify iframe loads `static/agents-app/index.html`
- [ ] Verify SSE connection to `/api/agents/stream` succeeds
- [ ] Verify sprites render (characters visible on canvas)
- [ ] Verify empty state shows when no sessions active
- [ ] Verify switching away from tab hides iframe (restoreDashboardAgents)
- [ ] Run existing test suite: `pytest tests/ -q`

---

## Self-Review

- **Spec coverage:** HU-AG.2 (bundle), HU-AG.3 (partial — init sequence only, no live streaming yet), HU-AG.4 (SSE endpoint), HU-AG.5 (mount/restore), HU-AG.8 (empty state). Live tool streaming (full HU-AG.3) requires subscribing to streaming.py events — deferred to next iteration.
- **Placeholder scan:** All code blocks complete. No TBD/TODO.
- **Type consistency:** `ServerMessage` types match between SSE server output and frontend expectations. `mountDashboardAgents`/`restoreDashboardAgents` naming follows established pattern.
- **Scope note:** This plan delivers the visual shell with sprites + static agent list from state.db. Real-time tool activity streaming (watching JSONL files) is a follow-up task.
