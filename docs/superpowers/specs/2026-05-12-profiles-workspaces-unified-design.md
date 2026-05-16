# Profiles & Workspaces — Unified Management Section

**Date:** 2026-05-12  
**Status:** Draft  
**Constraint:** Dashboard/chat visual remains untouched.

---

## 1. Problem Statement

Currently:
- **Profiles** tab shows cards in sidebar but main panel is an empty "Selecione um perfil" state. No clear CTA to create profiles. Only the default profile exists on most installs.
- **Workspaces** tab exists separately in sidebar, renders a list of paths with drag-reorder. Functional but disconnected from profiles.
- Two separate tabs for related concepts creates confusion. Users don't understand the relationship between profiles and workspaces.
- No onboarding — new users don't know what profiles are or why they'd create one.

## 2. Goal

Unify Profiles & Workspaces into a single section that:
1. Makes profile creation accessible (templates + wizard)
2. Shows workspaces as sub-items of each profile
3. Enables community adoption — users who want Neo WebUI but not the "Neo" agent can create their own profiles
4. Maintains existing terminology (Profiles & Workspaces)

## 3. Scope

**In scope:**
- Unified sidebar tab replacing separate Profiles and Workspaces tabs
- Main panel with profile grid (cards) + workspace management per profile
- Creation wizard with SOUL.md templates
- Profile CRUD (create, read, switch, delete)
- Workspace CRUD within profile context (add, rename, remove, reorder)
- Gateway status display per profile

**Out of scope:**
- Dashboard/chat page visual changes
- EP-AG (pixel-art agent monitoring) — separate feature
- Inline editing of config.yaml/.env (future iteration)
- Multi-user/auth (existing per-client cookie isolation stays)

## 4. Architecture

### 4.1 Navigation Change

**Before:** Two sidebar tabs — "Spaces" (workspaces) + "Profiles"  
**After:** Single tab "Profiles" with folder icon. Workspaces tab removed from sidebar/rail.

The composer workspace dropdown stays as-is (quick switch without leaving chat).

### 4.2 Layout — Main Panel

```
┌─────────────────────────────────────────────────┐
│ Profiles                          [+ New Profile]│
├─────────────────────────────────────────────────┤
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │  Avatar  │  │  Avatar  │  │  Avatar  │      │
│  │  "Neo"   │  │ "Coder"  │  │    "+"   │      │
│  │ Active ● │  │ 3 skills │  │  Create  │      │
│  │ model... │  │ model... │  │          │      │
│  └──────────┘  └──────────┘  └──────────┘      │
│                                                  │
├─────────────────────────────────────────────────┤
│ ▼ Workspaces (Neo)                    [+ Add]   │
│   ┌─ projeto-x        /home/user/proj-x    ●   │
│   ├─ docs             /home/user/docs           │
│   └─ research         /home/user/research       │
├─────────────────────────────────────────────────┤
│ Profile Details                                  │
│   Model: anthropic/claude-sonnet...              │
│   Provider: auto                                 │
│   Gateway: ● Running                             │
│   Skills: 5                                      │
│   [Switch to] [Delete]                           │
└─────────────────────────────────────────────────┘
```

### 4.3 Component Breakdown

| Component | Location | Responsibility |
|-----------|----------|----------------|
| Profile Grid | Main panel top | Card grid with avatars, status badges |
| Profile Detail | Main panel bottom | Expanded info when card selected |
| Workspace List | Inside profile detail | Workspaces belonging to selected profile |
| Creation Wizard | Main panel (replaces grid temporarily) | Step-by-step profile creation |
| Composer Dropdown | Composer area | Quick workspace switch (unchanged) |

### 4.4 Sidebar Panel

The sidebar panel becomes a compact list (like current profiles panel) for quick navigation. Clicking a profile in sidebar selects it in main panel.

```
┌─────────────────────┐
│ Profiles        [+] │
├─────────────────────┤
│ ● Neo (active)      │
│   anthropic · 5sk   │
├─────────────────────┤
│ ○ Coder             │
│   openai · 3sk      │
├─────────────────────┤
│ ○ Writer            │
│   auto · 0sk        │
└─────────────────────┘
```

## 5. Profile Creation Wizard

### 5.1 Flow

```
Step 1: Choose template or blank
  → [Blank] [Coder] [Researcher] [Writer] [Custom SOUL]

Step 2: Name + Provider
  → Name: _________ (lowercase, a-z0-9_-)
  → Provider: [Auto] [Anthropic] [OpenAI] [Custom]
  → Clone API keys from default: [✓]

Step 3: Confirm
  → Summary card showing what will be created
  → [Create] [Back]
```

### 5.2 Templates

Each template pre-fills a SOUL.md with personality/instructions:

| Template | SOUL.md Focus | Default Skills |
|----------|---------------|----------------|
| Blank | Empty | None |
| Coder | Code generation, debugging, architecture | None |
| Researcher | Information gathering, analysis, citations | None |
| Writer | Content creation, editing, tone adaptation | None |

Templates are stored as static strings in the frontend. On creation, the backend writes the SOUL.md to the profile directory.

### 5.3 Backend Changes

New endpoint needed:

```
POST /api/profile/create
Body: { name, clone, template }
```

Current `create_profile_api()` already handles `name` and `clone`. Add optional `template` field:
- If template provided, after profile creation write the template SOUL.md to `~/.hermes/profiles/<name>/SOUL.md`

New endpoint for workspace-profile association:

```
GET /api/profile/<name>/workspaces
```

Returns workspaces configured for that profile (reads from profile's config.yaml or a workspace list file).

## 6. Profile Cards

### 6.1 Visual Design

Inspired by hermes-desktop but adapted to Neo WebUI's dark theme:

- **Avatar:** Circle with first letter uppercase (colored background). Default profile shows Neo icon.
- **Name:** Bold, below avatar
- **Status:** Green dot + "Active" badge for current profile
- **Meta line:** Model name (short) · Provider · Skill count
- **Gateway indicator:** Small colored dot (green=running, gray=off)
- **Actions on hover/focus:** Chat button, Delete button (not for default)

### 6.2 Card States

| State | Visual |
|-------|--------|
| Active | Blue border, "Active" badge, green dot |
| Inactive | Default border, no badge |
| No config | Muted text "Not configured" |
| Gateway running | Green dot next to name |
| Gateway off | Gray dot |

## 7. Workspace Management (Per-Profile)

### 7.1 Current Behavior (Preserved)

- Drag-reorder via grip handle
- Add workspace (path input with validation)
- Rename workspace
- Remove workspace
- Active workspace badge

### 7.2 Changes

- Workspaces section appears INSIDE the selected profile's detail area
- When switching profiles, workspace list updates to show that profile's workspaces
- "Add workspace" button scoped to selected profile
- Composer dropdown continues showing workspaces for the ACTIVE profile only

### 7.3 Data Model

Currently workspaces are global (stored in `~/.hermes/workspaces.json` or similar). Two options:

**Option A (recommended):** Keep workspaces global but display filtered by active profile. Each workspace can be "associated" with a profile via a mapping file.

**Option B:** Move workspace storage per-profile (`~/.hermes/profiles/<name>/workspaces.json`). Breaking change for existing installs.

Recommend Option A — non-breaking, simpler migration.

## 8. Navigation & Routing

### 8.1 Tab Consolidation

| Before | After |
|--------|-------|
| Sidebar: Workspaces tab | Removed |
| Sidebar: Profiles tab | Stays (becomes unified entry point) |
| Rail: Workspaces button | Removed |
| Rail: Profiles button | Stays |
| Mobile nav: Workspaces | Removed |
| Mobile nav: Profiles | Stays |
| Dashboard menu: "Pessoal" (profiles) | Stays |
| Composer: workspace dropdown | Stays (unchanged) |

### 8.2 switchPanel() Changes

- `switchPanel('workspaces')` → redirect to `switchPanel('profiles')` (backwards compat)
- `switchPanel('profiles')` → loads unified panel
- Remove `panelWorkspaces` from sidebar, keep `panelProfiles`

## 9. Empty States

### 9.1 No Profiles (only default)

```
┌─────────────────────────────────────────────┐
│         [Neo Avatar]                         │
│                                              │
│   You're using the default profile.          │
│   Create specialized profiles with their     │
│   own personality, skills, and providers.    │
│                                              │
│   [+ Create Profile]                         │
│                                              │
│   Templates: Coder · Researcher · Writer     │
└─────────────────────────────────────────────┘
```

### 9.2 Profile Selected, No Workspaces

```
No workspaces configured for this profile.
[+ Add Workspace]
```

## 10. Mobile Considerations

- Profile grid becomes single-column cards (full width)
- Workspace list below selected profile (collapsible)
- Creation wizard uses full-screen steps
- Bottom nav loses Workspaces icon, keeps Profiles

## 11. Implementation Phases

### Phase 1: Unify Navigation
- Remove Workspaces tab from sidebar/rail/mobile nav
- Redirect `switchPanel('workspaces')` to profiles
- Add workspace section inside profile detail panel

### Phase 2: Profile Grid + Cards
- Replace current sidebar-only profile list with main panel grid
- Implement avatar, badges, status indicators
- Empty state with CTA

### Phase 3: Creation Wizard
- Template selection step
- Name + provider step
- Backend: add `template` field to create endpoint
- Write SOUL.md on creation

### Phase 4: Workspace-Profile Association
- Mapping file for workspace↔profile association
- Filter workspace display by selected profile
- Keep composer dropdown showing active profile's workspaces

## 12. Files Affected

| File | Changes |
|------|---------|
| `static/index.html` | Remove workspaces panel HTML, update profiles panel structure, remove workspaces from nav |
| `static/panels.js` | Rewrite `loadProfilesPanel()`, merge workspace rendering into profile detail, add wizard logic, redirect workspaces panel calls |
| `static/style.css` | New styles for profile grid, cards, wizard steps, avatar |
| `api/profiles.py` | Add `template` field handling, SOUL.md writing |
| `api/routes.py` | Add workspace-profile association endpoint |
| `static/i18n/*.json` | New translation keys for wizard, templates, empty states |

## 13. Success Criteria

1. User can create a new profile in ≤3 clicks using a template
2. Workspaces appear contextually within the selected profile
3. Existing functionality (switch, delete, gateway status) preserved
4. No visual changes to dashboard/chat page
5. Mobile-responsive layout works on 375px+ screens
6. Community user (non-Neo) can set up their own profile without documentation
