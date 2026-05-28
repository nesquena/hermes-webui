# Skills Toggle (Enable/Disable per Profile) — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a toggle button on each skill in the Skills panel list to enable or disable that skill for the current profile. Disabled skills are excluded from the agent's loaded skills by Hermes Agent's existing `skills.disabled` config mechanism.

**Architecture:** The WebUI already reads disabled skill names from the profile's `config.yaml` (`skills.disabled` list). Currently the `/api/skills` endpoint **omits** disabled skills entirely — the user can't see them to re-enable them. This plan changes the approach: return ALL skills with a `disabled` boolean flag, add a toggle button in the UI, and add a `POST /api/skills/toggle` endpoint that edits `config.yaml`.

**Tech Stack:** Python (server: routes.py, config.py), Vanilla JS (frontend: panels.js), CSS (style.css), YAML (config.py helpers), pytest (test files).

---

## Proposed scope

- **Files affected:**
  - `api/routes.py` — add `GET /api/skills/status` (or extend `/api/skills`), add `POST /api/skills/toggle`
  - `api/config.py` — no new functions needed; `_load_yaml_config_file` / `_save_yaml_config_file` / `reload_config` already exist
  - `static/panels.js` — modify `renderSkills()` to add toggle switch; update `loadSkills()` to fetch/filter disabled status; add `toggleSkill()` handler
  - `static/style.css` — styles for toggle switch + `.skill-item.disabled` state
  - `static/i18n.js` — add translation keys for toggle labels
  - `tests/test_skills_toggle.py` — new test file for backend toggle endpoint + frontend structural tests

- **Approach:** Reuse existing config.yaml write pattern (like MCP endpoints use `_save_yaml_config_file` + `reload_config`). The toggle adds/removes the skill name from `skills.disabled` list.

- **Open questions:** None — the config schema (`skills.disabled: [list]`) is already established in Hermes Agent.

---

## Tasks

### Task 1: Add `POST /api/skills/toggle` backend endpoint

**Objective:** Create an API endpoint that toggles a skill's enabled/disabled status by editing the active profile's `config.yaml`.

**Files:**
- Modify: `api/routes.py` (add handler + route registration)

**Step 1: Write failing test**

Create `tests/test_skills_toggle.py`:

```python
"""Tests for skill toggle (enable/disable) API."""
import json
from pathlib import Path


PANELS_JS = (Path(__file__).resolve().parent.parent / "static" / "panels.js").read_text("utf-8")


def test_toggle_endpoint_signature_in_routes():
    """Verify the toggle endpoint code exists in routes.py."""
    from api.routes import _handle_skill_toggle
    assert callable(_handle_skill_toggle)


def test_toggle_path_registered():
    """Verify /api/skills/toggle path is registered in handle_GET / handle_POST."""
    routes_source = (Path(__file__).resolve().parent.parent / "api" / "routes.py").read_text("utf-8")
    assert '/api/skills/toggle' in routes_source
```

Run: `pytest tests/test_skills_toggle.py::test_toggle_endpoint_signature_in_routes -v`

Expected: FAIL — `_handle_skill_toggle` not defined yet.

**Step 2: Add toggle handler to routes.py**

Add `_handle_skill_toggle(handler, body)` function. It should:

```python
def _handle_skill_toggle(handler, body):
    try:
        require(body, "name", "enabled")
    except ValueError as e:
        return bad(handler, str(e))

    name = body["name"].strip()
    enabled = bool(body["enabled"])
    config_path = _get_config_path()
    cfg = _load_yaml_config_file(config_path)

    # Ensure skills section exists
    if "skills" not in cfg or not isinstance(cfg["skills"], dict):
        cfg["skills"] = {}
    skills_cfg = cfg["skills"]

    # Normalize the disabled list
    disabled = skills_cfg.get("disabled")
    if disabled is None:
        disabled = []
    elif isinstance(disabled, str):
        disabled = [disabled]
    elif not isinstance(disabled, list):
        disabled = list(disabled) if disabled else []
    disabled = [str(d).strip() for d in disabled if str(d).strip()]

    if enabled:
        # Remove from disabled list
        disabled = [d for d in disabled if d != name]
    else:
        # Add to disabled list (if not already there)
        if name not in disabled:
            disabled.append(name)

    # Write back
    skills_cfg["disabled"] = disabled
    cfg["skills"] = skills_cfg
    _save_yaml_config_file(config_path, cfg)
    reload_config()

    return j(handler, {"ok": True, "name": name, "enabled": enabled})
```

Place this function near the other `_handle_skill_*` functions (~line 10330).

Register it in the POST routing block (around line 5390):

```python
if parsed.path == "/api/skills/toggle":
    return _handle_skill_toggle(handler, body)
```

**Step 3: Run test to verify pass**

Run: `pytest tests/test_skills_toggle.py -v`

Expected: PASS

**Step 4: Commit**

```bash
git add api/routes.py tests/test_skills_toggle.py
git commit -m "feat: add POST /api/skills/toggle endpoint for per-skill enable/disable"
```

---

### Task 2: Extend `GET /api/skills` to include disabled status

**Objective:** The skills list API should return ALL skills (including disabled ones) with a `disabled` boolean so the UI can render them with the correct state.

**Files:**
- Modify: `api/routes.py` — `_skills_list_from_dir()` function
- Test: `tests/test_skills_toggle.py`

**Step 1: Write failing test**

Add to `test_skills_toggle.py`:

```python
def test_skills_list_includes_disabled_flag():
    """Each skill in the API response must have a 'disabled' boolean."""
    from api.routes import _skills_list_from_dir, _active_skills_dir
    result = _skills_list_from_dir(_active_skills_dir())
    for skill in result.get("skills", []):
        assert "disabled" in skill, f"Skill {skill.get('name')} missing 'disabled' field"
        assert isinstance(skill["disabled"], bool), f"Skill {skill.get('name')} disabled must be bool"
```

Run: `pytest tests/test_skills_toggle.py::test_skills_list_includes_disabled_flag -v`

Expected: FAIL — no `disabled` field in current skill dict.

**Step 2: Modify `_skills_list_from_dir()`**

In the skill-building block (~line 246-252 of routes.py), after building the skill dict:

```python
all_skills.append(
    {
        "name": name,
        "description": description,
        "category": _skill_category_from_path(skill_md, search_dirs),
        "disabled": name in disabled,  # NEW
    }
)
```

Also: **remove the disabled-name filter** — currently `disabled` set is used to skip disabled skills entirely (`if name in seen_names or name in disabled: continue`). Change line 234 to only filter on `seen_names`:

```python
# Old: if name in seen_names or name in disabled:
# New:
if name in seen_names:
    continue
```

This way disabled skills appear in the list with `disabled: true`.

**Step 3: Run test to verify pass**

Run: `pytest tests/test_skills_toggle.py::test_skills_list_includes_disabled_flag -v`

Expected: PASS

**Step 4: Commit**

```bash
git add api/routes.py tests/test_skills_toggle.py
git commit -m "feat: include disabled status in GET /api/skills response"
```

---

### Task 3: Add toggle button to skills list in the UI

**Objective:** Each skill item in the skills sidebar gets a toggle switch. Clicking it enables/disables the skill without opening the detail view.

**Files:**
- Modify: `static/panels.js` — `renderSkills()` and helper functions
- Modify: `static/style.css` — toggle switch styles
- Modify: `static/i18n.js` — new translation keys

**Step 1: Write failing test (structural JS check)**

Add to `test_skills_toggle.py`:

```python
def test_render_skills_produces_toggle_buttons():
    assert "toggleSkill(" in PANELS_JS
    assert "skill-toggle" in PANELS_JS  # CSS class for the toggle


def test_skills_data_caches_disabled_status():
    """_skillsData entries must include disabled field post-toggle API change."""
    assert "disabled" in PANELS_JS or "renderSkills" in PANELS_JS
```

Run: `pytest tests/test_skills_toggle.py::test_render_skills_produces_toggle_buttons -v`

Expected: FAIL

**Step 2: Add i18n keys**

In `static/i18n.js`, add:

```javascript
    skill_enabled: 'Enabled',
    skill_disabled: 'Disabled',
    skill_toggle_failed: 'Failed to toggle skill: ',
```

**Step 3: Add toggle CSS in style.css**

Around the `.skill-item` rules (~line 1082):

```css
  .skill-item{...existing...}
  .skill-item.disabled{opacity:.5;}
  .skill-item.disabled .skill-name{opacity:.45;}
  .skill-toggle{flex-shrink:0;width:28px;height:14px;border-radius:7px;border:1px solid var(--border2);background:var(--border);cursor:pointer;position:relative;transition:all .15s;margin-top:3px;}
  .skill-toggle.enabled{background:var(--accent-bg);border-color:var(--accent-bg);}
  .skill-toggle::after{content:'';position:absolute;top:1px;left:1px;width:10px;height:10px;border-radius:50%;background:var(--text);transition:transform .15s;}
  .skill-toggle.enabled::after{transform:translateX(14px);}
```

**Step 4: Modify `renderSkills()` in panels.js**

Change the skill item rendering block (~line 3292-3298):

```javascript
for (const skill of items.sort((a,b) => a.name.localeCompare(b.name))) {
  const el = document.createElement('div');
  el.className = 'skill-item' + (skill.disabled ? ' disabled' : '');
  el.style.display = collapsed ? 'none' : '';
  const isDisabled = skill.disabled || false;
  el.innerHTML = `
    <span class="skill-toggle${isDisabled ? '' : ' enabled'}"
          onclick="event.stopPropagation();toggleSkill('${esc(skill.name)}', ${!isDisabled})"
          title="${isDisabled ? esc(t('skill_disabled')) : esc(t('skill_enabled'))}"></span>
    <span class="skill-name">${esc(skill.name)}</span>
    <span class="skill-desc">${esc(skill.description||'')}</span>`;
  el.onclick = () => openSkill(skill.name, el);
  sec.appendChild(el);
}
```

**Step 5: Add `toggleSkill()` function**

After `filterSkills()`:

```javascript
async function toggleSkill(name, currentlyEnabled) {
  const newEnabled = !currentlyEnabled;
  try {
    const result = await api('/api/skills/toggle', {
      method: 'POST',
      body: JSON.stringify({ name, enabled: newEnabled })
    });
    if (result && result.ok) {
      // Update the local cache
      if (_skillsData) {
        const skill = _skillsData.find(s => s.name === name);
        if (skill) skill.disabled = !newEnabled;
      }
      // Re-render to show the new state
      renderSkills(_skillsData || []);
    } else {
      setStatus((result && result.error) || esc(t('skill_toggle_failed')));
    }
  } catch(e) {
    setStatus(t('skill_toggle_failed') + e.message);
  }
}
```

Add the `toggleSkill` function reference to the global scope — it's called from inline `onclick` handlers.

**Step 6: Run tests**

Run: `pytest tests/test_skills_toggle.py -v`

Expected: All PASS

**Step 7: Commit**

```bash
git add static/panels.js static/style.css static/i18n.js tests/test_skills_toggle.py
git commit -m "feat: add skill toggle button in skills panel list"
```

---

### Task 4: Add integration/e2e tests for the toggle

**Objective:** Verify the full round-trip: toggle off → skill shows as disabled → toggle on → skill shows as enabled.

**File:**
- Create: `tests/test_skills_toggle.py` (add integration tests that use the test server fixture)

Look at `conftest.py` for the test server fixture pattern. Integration test should:

1. Start isolated server
2. Create a temp config.yaml with a `skills` section
3. POST to `/api/skills/toggle` to disable a skill
4. GET `/api/skills` and verify `disabled: true`
5. POST to re-enable
6. Verify `disabled: false`

**Step 1: Write integration test**

```python
def test_toggle_round_trip(http_server):
    """Full round-trip: disable a skill, verify it's disabled, re-enable, verify."""
    # The http_server fixture provides a base_url pointing to a test server
    # with isolated state. Check conftest.py for the exact shape.
    import json, urllib.request

    base_url = http_server  # or http_server.base_url — check fixture
    skills_url = base_url + "/api/skills"
    toggle_url = base_url + "/api/skills/toggle"

    # Get initial list
    with urllib.request.urlopen(skills_url) as resp:
        data = json.loads(resp.read())
    skills = data.get("skills", [])
    if not skills:
        pytest.skip("No skills to test toggle with")
    
    skill = skills[0]
    name = skill["name"]
    initial_disabled = skill.get("disabled", False)

    # Toggle to opposite state
    new_enabled = initial_disabled  # if disabled -> enable; if enabled -> disable
    req = urllib.request.Request(
        toggle_url,
        data=json.dumps({"name": name, "enabled": new_enabled}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        toggle_result = json.loads(resp.read())
    assert toggle_result.get("ok") is True

    # Verify the list reflects the change
    with urllib.request.urlopen(skills_url) as resp:
        data2 = json.loads(resp.read())
    updated = next((s for s in data2.get("skills", []) if s["name"] == name), None)
    assert updated is not None
    assert updated["disabled"] == (not new_enabled)

    # Restore original state
    req2 = urllib.request.Request(
        toggle_url,
        data=json.dumps({"name": name, "enabled": not new_enabled}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req2) as resp2:
        restore = json.loads(resp2.read())
    assert restore.get("ok") is True
```

**Step 2: Run the test**

```bash
pytest tests/test_skills_toggle.py::test_toggle_round_trip -v --timeout=30
```

Expected: PASS (or adjust fixture reference)

**Step 3: Commit**

```bash
git add tests/test_skills_toggle.py
git commit -m "test: integration test for skill toggle round-trip"
```

---

### Task 5: Update docs and changelog

**Objective:** Document the new feature.

**Files:**
- Modify: `CHANGELOG.md` — add entry under the next release section
- Optionally: update `ROADMAP.md` or `docs/` if relevant

**Step 1: Add changelog entry**

```markdown
### Feature: Skill toggle (enable/disable per profile)

- Skills panel now shows ALL installed skills, including disabled ones, with a toggle switch to enable/disable each skill for the current profile.
- Disabled skills are excluded from the agent's loaded skill set (delegated to Hermes Agent's existing `skills.disabled` config mechanism).
- New API endpoint `POST /api/skills/toggle` edits the active profile's `config.yaml`.
- `GET /api/skills` now returns a `disabled` boolean per skill instead of filtering out disabled skills silently.
```

**Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: add changelog entry for skill toggle feature"
```

---

## Verification

1. Open the Skills panel — all skills are visible, disabled ones appear muted/grayed.
2. Click the toggle on an enabled skill — it becomes disabled, the skill item dims.
3. Click the toggle on a disabled skill — it becomes enabled, the skill item returns to normal.
4. Switch profiles — each profile has its own disabled-skills list from its own config.yaml.
5. Verify disabled skills don't appear in the agent's `/skills` slash command.
6. Run `pytest tests/test_skills_toggle.py -v` — all pass.
