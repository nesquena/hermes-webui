# App-Tabs Extension: Component Specification

> **Status:** Draft · **Target:** Hermes WebUI v0.5.0+  
> **File:** `static/panels.js` (primary), plus a dedicated module `static/app-tabs.js`  
> **Vanilla JS · No frameworks · No build step**

---

## Table of Contents

1. [Overview](#1-overview)
2. [Data Model & Storage](#2-data-model--storage)
3. [Module Structure & Integration Points](#3-module-structure--integration-points)
4. [App Manager Component](#4-app-manager-component)
5. [Icon Picker Component](#5-icon-picker-component)
6. [Config Dialog Component](#6-config-dialog-component)
7. [Update Icon Display Helper](#7-update-icon-display-helper)
8. [Keyboard Handling](#8-keyboard-handling)
9. [Edge Cases](#9-edge-cases)
10. [State Management & Lifecycle](#10-state-management--lifecycle)
11. [Backend API Contract](#11-backend-api-contract)
12. [DOM Structure Reference](#12-dom-structure-reference)

---

## 1. Overview

The **App Tabs** extension lets users manage self-hosted web applications that appear as iframe tabs in a dedicated panel. Three core UI components work together:

| Component | Entry Point | Purpose |
|-----------|-------------|---------|
| **App Manager** | `openManager()` | Sortable list of registered apps with move up/down, open, edit, delete |
| **Icon Picker** | `openIconPicker(current, callback)` | Modal grid of Lucide icons with search and selection |
| **Config Dialog** | `openConfig(appId, addNew)` | Add/edit form with label, URL, and icon fields |

All three follow existing Hermes WebUI overlay conventions (inline styles, `var(--css-prop)` tokens, `esc()` sanitization, `li()` icon rendering).

---

## 2. Data Model & Storage

### 2.1 App Object

```js
{
  id: 'a1b2c3d4',            // crypto.randomUUID() (or Date.now().toString(36) fallback)
  label: 'My App',           // max 24 chars, sanitized
  url: 'https://example.com', // validated http(s) URL
  icon: 'globe',             // key in LI_PATHS (63 valid values)
  order: 0                    // integer index for sort position
}
```

### 2.2 Storage

Apps are persisted in `localStorage` under key `hermes-app-tabs` as a JSON array of app objects, sorted by `order`.

```js
const STORAGE_KEY = 'hermes-app-tabs';

function loadApps() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY)) || [];
  } catch {
    return [];
  }
}

function saveApps(apps) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(apps));
}
```

### 2.3 Module-Level State

```js
let _apps = [];              // in-memory cache of all apps (sorted by order)
let _selectedAppId = null;   // id of the app whose iframe is shown in the rail
let _managerOpen = false;    // guard: manager overlay is displayed
let _pickerOpen = false;     // guard: icon picker is displayed
let _configOpen = false;     // guard: config dialog is displayed
```

---

## 3. Module Structure & Integration Points

### 3.1 File Placement

The components live in a new dedicated module `static/app-tabs.js`. Functions that need to be called from `panels.js` or `boot.js` are exported via global `window` assignments (matching the existing `registerHermesTtsEngine` pattern).

```
static/
  app-tabs.js       <-- NEW: all app-tabs component logic
  icons.js          <-- existing: LI_PATHS + li() (consumed by app-tabs)
  panels.js         <-- existing: calls openManager() from the Control Center
  ui.js             <-- existing: esc(), li(), showToast() (consumed by app-tabs)
```

### 3.2 Global Registration

```js
// In app-tabs.js — registration at module bottom
window.openAppManager = openManager;
window.openAppConfig = openConfig;
window._appTabState = { apps: _apps, selectedId: _selectedAppId };
```

### 3.3 Integration Point in Panels.js

In the Control Center or settings panel, a "Manage Apps" button calls:

```js
if (typeof window.openAppManager === 'function') {
  window.openAppManager();
}
```

### 3.4 Rail Integration

The main UI (in `panels.js` or `ui.js`) calls a `syncAppRail()` function after any app mutation to rebuild the tab bar. This function:

1. Clears the rail container
2. Iterates `_apps` in order
3. Creates one tab button per app with its icon + label
4. Highlights the selected tab
5. Adds a "Manage" button at the end

### 3.5 Dependencies

| Identifier | Source | Purpose |
|------------|--------|---------|
| `$('id')` | `ui.js` | getElementById shorthand |
| `esc(s)` | `ui.js:280` | HTML entity escaping |
| `li(name, size)` | `icons.js:88` | Lucide icon SVG string |
| `LI_PATHS` | `icons.js:7` | Icon path registry |
| `showToast(msg, ms, type)` | `ui.js:8109` | Toast notification |
| `t(key)` | `i18n.js` | Internationalization lookup |

---

## 4. App Manager Component

### 4.1 `openManager()`

**Behavior:** Opens a modal overlay listing all registered apps in order with move/delete/edit controls.

**Pseudocode:**

```
function openManager():
    if _managerOpen → return (guard)
    _managerOpen = true
    
    overlay = createElement('div')
    overlay.style = fullscreen backdrop (z-index: 9999)
    overlay.id = 'appManagerOverlay'
    
    panel = createElement('div')
    panel.style = centered card (width: min(540px, 95vw), max-height: 80vh)
    
    panel.innerHTML = `
      ┌─ Header ───────────────────────────────────────┐
      │ [←] Manage Apps                        [×]     │
      ├─────────────────────────────────────────────────┤
      │   app list container (#appManagerList)          │
      │                                                 │
      │   (or empty state if no apps)                   │
      │                                                 │
      ├─────────────────────────────────────────────────┤
      │ [+] Add App                         (footer)    │
      └─────────────────────────────────────────────────┘
    `
    
    overlay.onclick = if target === overlay → close
    document.addEventListener('keydown', escHandler)
    
    renderManagerView()
    document.body.appendChild(overlay)
```

**Close behavior:**
- Click on backdrop → `closeManager()`
- Escape key → `closeManager()`
- ✕ button → `closeManager()`
- `[←]` back button → `closeManager()` (if navigated into detail)

**`closeManager()`:**
```
function closeManager():
    remove overlay from DOM
    remove keydown listener
    _managerOpen = false
```

### 4.2 `renderManagerView()`

**Signature:** `function renderManagerView(queryFilter='')`

Renders the app list into `#appManagerList`. Each app row:

```html
<div class="app-manager-row" data-app-id="${app.id}">
  <span class="app-order">${app.order + 1}.</span>
  <span class="app-icon">${li(app.icon, 18)}</span>
  <span class="app-label">${esc(app.label)}</span>
  <span class="app-url">${esc(app.url)}</span>
  <span class="app-actions">
    <button onclick="openAppInTab('${app.id}')" title="Open">${li('external-link', 16)}</button>
    <button onclick="openConfig('${app.id}')" title="Edit">${li('pencil', 16)}</button>
    <button onclick="moveApp('${app.id}', -1)" title="Move up" ${app.order === 0 ? 'disabled' : ''}>${li('chevron-up', 16)}</button>
    <button onclick="moveApp('${app.id}', 1)" title="Move down" ${app.order === apps.length-1 ? 'disabled' : ''}>${li('chevron-down', 16)}</button>
    <button onclick="deleteApp('${app.id}')" title="Delete">${li('trash-2', 16)}</button>
  </span>
</div>
```

**Empty state** (when `filteredApps.length === 0`):

```html
<div class="app-manager-empty">
  ${li('folder', 32)}
  <p>No apps configured yet.</p>
  <p>Add your first self-hosted web app to get started.</p>
  <button onclick="openConfig(null, true)">${li('plus', 16)} Add App</button>
</div>
```

**Styling (inline):**

| Element | Style |
|---------|-------|
| Row | `display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border);font-size:13px` |
| Order | `color:var(--muted);min-width:24px;font-size:11px` |
| Label | `flex:0 0 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:140px;font-weight:600` |
| URL | `flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted);font-size:11px` |
| Actions | `display:flex;gap:4px;flex-shrink:0` |
| Action btn | `background:none;border:1px solid transparent;cursor:pointer;padding:4px;border-radius:4px;color:var(--muted);display:inline-flex` |
| Action btn:hover | `background:var(--hover,rgba(255,255,255,0.07));color:var(--text)` |
| Action btn:disabled | `opacity:0.3;cursor:default` |

### 4.3 `moveApp(appId, direction)`

**Signature:** `function moveApp(appId, direction)` where `direction` is `-1` (up) or `1` (down).

```
function moveApp(appId, direction):
    apps = loadApps()
    idx = apps.findIndex(a => a.id === appId)
    if idx === -1 → return
    target = idx + direction
    if target < 0 OR target >= apps.length → return
    
    // Swap order values
    [apps[idx].order, apps[target].order] = [apps[target].order, apps[idx].order]
    
    // Re-sort by order
    apps.sort((a, b) => a.order - b.order)
    
    // Re-index order to be sequential
    apps.forEach((a, i) => a.order = i)
    
    saveApps(apps)
    _apps = apps
    renderManagerView()         // re-render current view
    syncAppRail()               // update main rail
```

### 4.4 `deleteApp(appId)`

```
function deleteApp(appId):
    apps = loadApps()
    idx = apps.findIndex(a => a.id === appId)
    if idx === -1 → return
    
    apps.splice(idx, 1)
    
    // Re-index order
    apps.forEach((a, i) => a.order = i)
    
    saveApps(apps)
    _apps = apps
    
    // If deleted app was the active tab
    if _selectedAppId === appId:
        _selectedAppId = null
    
    renderManagerView()
    syncAppRail()
```

### 4.5 `openAppInTab(appId)`

```
function openAppInTab(appId):
    _selectedAppId = appId
    closeManager()       // close the manager overlay
    // The rail highlights the selected tab and shows its iframe content
    syncAppRail()
```

---

## 5. Icon Picker Component

### 5.1 `openIconPicker(currentIcon, callback)`

**Signature:** `function openIconPicker(currentIcon, onSelect)`

- `currentIcon` (string): the currently selected icon name (or empty string for none)
- `onSelect` (function): receives the selected icon name (`selectedName`). Called exactly once when the user picks an icon; not called on cancel/close.

**Behavior:**

```
function openIconPicker(currentIcon, onSelect):
    if _pickerOpen → return
    _pickerOpen = true
    
    overlay = createElement('div')
    overlay.style = fullscreen backdrop (z-index: 9999)
    overlay.id = 'iconPickerOverlay'
    
    panel = createElement('div')
    panel.style = centered card (width: min(460px, 95vw), max-height: 80vh)
    
    panel.innerHTML = `
      ┌─ Header ───────────────────────────────────┐
      │ Pick an Icon                       [×]      │
      ├──────────────────────────────────────────────┤
      │ [search input]  (filter icons by name)       │
      ├──────────────────────────────────────────────┤
      │  ┌───┐ ┌───┐ ┌───┐ ┌───┐ ┌───┐              │
      │  │svg│ │svg│ │svg│ │svg│ │svg│              │
      │  │nm │ │nm │ │nm │ │nm │ │nm │              │
      │  └───┘ └───┘ └───┘ └───┘ └───┘              │
      │  ┌───┐ ┌───┐ ┌───┐ ┌───┐                    │
      │  │svg│ │svg│ │svg│ │svg│                    │
      │  │nm │ │nm │ │nm │ │nm │                    │
      │  └───┘ └───┘ └───┘ └───┘                    │
      │  (scrollable grid)                           │
      └──────────────────────────────────────────────┘
    `
    
    overlay.onclick = if target === overlay → close
    document.addEventListener('keydown', escHandler)
    
    renderPickerGrid(currentIcon)
    
    // Wire search input
    searchInput.oninput = () => renderPickerGrid(currentIcon, searchInput.value)
    
    document.body.appendChild(overlay)
```

### 5.2 `renderPickerGrid(currentIcon, filterText)`

**Signature:** `function renderPickerGrid(currentIcon, filterText = '')`

Iterates `Object.keys(LI_PATHS)` (63 icons), filtering by `filterText` (case-insensitive `includes` match on the icon name).

Grid cell markup:

```html
<button class="icon-picker-cell ${name === currentIcon ? 'selected' : ''}"
        data-icon-name="${name}"
        onclick="window._pickIcon('${name}')">
  ${li(name, 28)}
  <span class="icon-picker-label">${esc(name)}</span>
</button>
```

**Grid layout (inline):**

```css
/* Grid container */
display: grid;
grid-template-columns: repeat(auto-fill, minmax(80px, 1fr));
gap: 6px;
padding: 8px;

/* Cell button */
display: flex;
flex-direction: column;
align-items: center;
gap: 4px;
padding: 8px 4px;
border-radius: 8px;
border: 1px solid transparent;
background: none;
cursor: pointer;
color: var(--text);
font-size: 10px;
transition: background 0.15s, border-color 0.15s;

/* Cell hover */
background: var(--hover, rgba(255,255,255,0.07));
border-color: var(--border);

/* Cell selected */
border-color: var(--accent);
background: rgba(var(--accent-rgb, 100, 200, 255), 0.12);

/* Label */
overflow: hidden;
text-overflow: ellipsis;
white-space: nowrap;
max-width: 100%;
```

### 5.3 `closeIconPicker()`

```
function closeIconPicker():
    remove overlay from DOM
    remove keydown listener
    _pickerOpen = false
```

### 5.4 `_pickIcon(name)` (internal callback)

```
function _pickIcon(name):
    onSelect(name)     // call the stored callback
    closeIconPicker()
```

The `onSelect` callback is stored in a module-level `_pickerCallback` variable:

```js
let _pickerCallback = null;
```

---

## 6. Config Dialog Component

### 6.1 `openConfig(appId, addNew)`

**Signature:** `function openConfig(appId, addNew = false)`

Two modes:

| Mode | `appId` | `addNew` | Title | Behavior |
|------|---------|----------|-------|----------|
| **Edit existing** | string (app id) | `false` | "Edit App" | Pre-fills fields from existing app |
| **Add new** | `null` | `true` | "Add App" | Empty form, no delete button |

**Layout:**

```
┌─ Header ───────────────────────────────────────┐
│ Edit App / Add App                     [×]      │
├─────────────────────────────────────────────────┤
│                                                  │
│  Label:   [________________]  (max 24 chars)    │
│                                                  │
│  URL:     [________________]  (must be http(s)) │
│                                                  │
│  Icon:    [icon preview]  [Choose] [Remove]     │
│                                                  │
├─────────────────────────────────────────────────┤
│  [Delete App]                 [Cancel] [Save]    │
└─────────────────────────────────────────────────┘
```

**Field details:**

| Field | Type | Validation | Error Message |
|-------|------|------------|---------------|
| Label | `input type="text"` | `value.trim().length > 0 && value.trim().length <= 24` | "Label is required (max 24 characters)" |
| URL | `input type="url"` | Must match `/^https?:\/\/.+/` | "Please enter a valid HTTP or HTTPS URL" |
| Icon | Preview + buttons | Icon name must be a key in `LI_PATHS` or empty → defaults to `'globe'` | n/a (graceful fallback) |

**Button text (i18n keys):** Save → `t('save')`, Cancel → `t('cancel')`, Choose → `t('app_tabs_choose_icon')`, Remove → `t('app_tabs_remove_icon')`, Delete App → `t('delete')`.

### 6.2 Save Logic

```
function _saveConfig(formData, appId, addNew):
    // Validate
    label = formData.label.trim()
    if label.length === 0 OR label.length > 24:
        showToast('Label is required (max 24 characters)', 3000, 'error')
        return
    
    url = formData.url.trim()
    if !/^https?:\/\/.+/.test(url):
        showToast('Please enter a valid HTTP or HTTPS URL', 3000, 'error')
        return
    
    icon = formData.icon || 'globe'
    if icon !== '' && !LI_PATHS[icon]:
        icon = 'globe'    // fallback
    
    apps = loadApps()
    
    if addNew:
        newApp = {
            id: crypto.randomUUID(),
            label,
            url,
            icon,
            order: apps.length
        }
        apps.push(newApp)
    else:
        idx = apps.findIndex(a => a.id === appId)
        if idx === -1 → return
        apps[idx].label = label
        apps[idx].url = url
        apps[idx].icon = icon
    
    saveApps(apps)
    _apps = apps
    
    closeConfig()
    syncAppRail()
```

### 6.3 Delete Button Logic

Visible only in **edit mode** (`addNew === false`). On click:

```
function _deleteFromConfig(appId):
    // No confirmation dialog — immediate delete
    apps = loadApps()
    apps = apps.filter(a => a.id !== appId)
    apps.forEach((a, i) => a.order = i)
    saveApps(apps)
    _apps = apps
    
    if _selectedAppId === appId:
        _selectedAppId = null
    
    closeConfig()
    
    // If manager overlay is also open, re-render it
    if _managerOpen:
        renderManagerView()
    
    syncAppRail()
```

### 6.4 Config Dialog Close

```
function closeConfig():
    remove overlay from DOM
    remove keydown listener
    _configOpen = false
```

**Confirm on unsaved changes?**  
For v1, no dirty-state detection — closing discards form input silently (matches the existing wiki browser and kanban modal patterns).

---

## 7. Update Icon Display Helper

### 7.1 `updateIconDisplay(iconName)`

**Signature:** `function updateIconDisplay(iconName)`

Called when the user selects an icon from the picker or clears the icon selection. Updates the icon preview element inside the config dialog.

```html
<!-- Icon preview container in config dialog -->
<div id="appConfigIconPreview">
  ${iconName ? li(iconName, 32) : esc(t('app_tabs_no_icon'))}
  <span>${iconName ? esc(iconName) : ''}</span>
</div>
```

```
function updateIconDisplay(iconName):
    preview = $('appConfigIconPreview')
    if !preview → return
    
    if iconName:
        preview.innerHTML = li(iconName, 32) 
                          + '<span style="font-size:11px;color:var(--muted);margin-left:6px">'
                          + esc(iconName) + '</span>'
    else:
        preview.innerHTML = '<span style="font-size:11px;color:var(--muted)">'
                          + esc(t('app_tabs_no_icon') || 'No icon')
                          + '</span>'
```

Called from:
- `openConfig()` to display the existing icon
- The icon picker callback after user selects
- The "Remove" button handler in the config dialog

---

## 8. Keyboard Handling

### 8.1 Escape Key

Every overlay (manager, picker, config) registers a `keydown` listener for Escape. Listener is removed when the overlay closes.

```js
function _onEscape(e) {
  if (e.key === 'Escape') {
    // Close whichever overlay is on top (stack order)
    if (_pickerOpen) closeIconPicker();
    else if (_configOpen) closeConfig();
    else if (_managerOpen) closeManager();
  }
}
```

Because only one overlay can be open at a time (guards prevent stacking), a single cascade works. If the Icon Picker is opened *from* the Config Dialog, the picker receives Escape first — on close, the config dialog behind it is still open (unaffected).

### 8.2 Enter on Save

In the Config Dialog, pressing Enter while focused on the Label or URL input triggers the Save action.

```js
configInputs.forEach(input => {
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); _saveConfig(...); }
  });
});
```

---

## 9. Edge Cases

### 9.1 Editing While Overlay Is Open

**Rule:** Only one overlay can be open at a time. Each entry function checks its guard variable (`_managerOpen`, `_pickerOpen`, `_configOpen`) and returns early if another overlay is already displayed.

Exception: The Icon Picker can be opened from within the Config Dialog. The Config Dialog remains open underneath it. The stack is:

```
Icon Picker overlay  (z-index: 10000)
Config Dialog overlay (z-index: 9999)
```

When the picker closes (via selection or Escape), the config dialog underneath is still visible and unchanged.

### 9.2 Deleting the Active App

When an app is deleted (from either the Manager list or the Config dialog), if `_selectedAppId === deletedApp.id`, set `_selectedAppId = null` before saving. The app rail re-syncs, showing no iframe (or showing the app manager as the default view).

```js
if (_selectedAppId === appId) {
  _selectedAppId = null;
  // The rail will show the manager or an empty bookmark state
}
```

### 9.3 Adding the First App at Empty State

When `_apps.length === 0`:

1. The Manager shows the empty state with an "Add App" button
2. No rail tabs are rendered (or a minimal "+" button is shown)
3. `openConfig(null, true)` creates the first app at `order: 0`
4. After save, `syncAppRail()` renders one tab and selects it

### 9.4 Re-ordering at Boundaries

- `moveApp()` with `direction = -1` at `order === 0` → no-op (button disabled)
- `moveApp()` with `direction = 1` at the last position → no-op (button disabled)
- Disabled buttons get `opacity: 0.3; cursor: default` (no `pointer-events: none` so tooltips still work)

### 9.5 Icon Name Mismatch

If an app's stored icon name is no longer in `LI_PATHS` (from a future removal or manual edit):

- `li(iconName)` returns `''` (silent fallback — already handled by `li()`)
- `updateIconDisplay()` shows the name but the SVG is empty
- The config dialog "Choose" flow always shows valid names from `Object.keys(LI_PATHS)`

### 9.6 localStorage Corruption

`loadApps()` wraps `JSON.parse` in try/catch, returning `[]` on failure. No migration logic needed for v1.

### 9.7 Multiple Rapid Clicks on Manager Buttons

Each row action button calls its handler directly by app id. No debounce is needed — state is read from `loadApps()` at the start of each mutation and written back atomically.

---

## 10. State Management & Lifecycle

### 10.1 Init (`initAppTabs()`)

Called from `boot.js` during startup:

```
function initAppTabs():
    _apps = loadApps()
    syncAppRail()
    // Wire the "+" button in the rail if present
```

### 10.2 `syncAppRail()`

Rebuilds the tab rail after any mutation:

```
function syncAppRail():
    rail = $('appRail')
    if !rail → return
    
    // Clear existing tabs
    rail.innerHTML = ''
    
    // Render one tab per app
    _apps.forEach(app => {
        tab = createElement('button')
        tab.className = 'app-tab' + (app.id === _selectedAppId ? ' active' : '')
        tab.dataset.appId = app.id
        tab.innerHTML = li(app.icon, 14) + esc(app.label)
        tab.title = app.url
        tab.onclick = () => openAppInTab(app.id)
        rail.appendChild(tab)
    })
    
    // Add Manage button
    manageBtn = createElement('button')
    manageBtn.className = 'app-tab-manage'
    manageBtn.innerHTML = li('settings', 14)
    manageBtn.title = 'Manage Apps'
    manageBtn.onclick = openManager
    rail.appendChild(manageBtn)
```

### 10.3 Lifecycle Diagram

```
boot.js → initAppTabs()
              │
              ├── loadApps() → _apps
              └── syncAppRail()
                      │
          ┌───────────┼───────────┐
          │           │           │
      [Manager]   [Config]   [Tab Click]
          │           │           │
      moveApp()   saveApp()   selectApp()
      deleteApp()  deleteApp()     │
          │           │        syncAppRail()
      syncAppRail()  │              │
          │      syncAppRail()  loadAppIframe()
          └───────────┘
```

---

## 11. Backend API Contract

For v1, **no backend APIs are needed**. All state is managed in `localStorage`. The app-tabs extension is entirely frontend-only.

If future versions need cross-device sync or server-side persistence, the API would be:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/app-tabs` | GET | List all registered apps |
| `/api/app-tabs` | POST | Create or update an app |
| `/api/app-tabs/:id` | DELETE | Remove an app |
| `/api/app-tabs/reorder` | POST | Batch-update order |

---

## 12. DOM Structure Reference

### 12.1 Manager Overlay IDs

| ID | Element | Purpose |
|----|---------|---------|
| `appManagerOverlay` | div | Backdrop |
| `appManagerPanel` | div | Modal card |
| `appManagerTitle` | div | "Manage Apps" header |
| `appManagerClose` | button | Close ✕ button |
| `appManagerList` | div | Scrollable app list container |
| `appManagerAddBtn` | button | "Add App" footer button |
| `appManagerEmpty` | div | Empty-state container (created dynamically) |

### 12.2 Icon Picker IDs

| ID | Element | Purpose |
|----|---------|---------|
| `iconPickerOverlay` | div | Backdrop |
| `iconPickerPanel` | div | Modal card |
| `iconPickerTitle` | div | "Pick an Icon" header |
| `iconPickerClose` | button | Close ✕ button |
| `iconPickerSearch` | input | Filter input |
| `iconPickerGrid` | div | Scrollable grid of icon cells |

### 12.3 Config Dialog IDs

| ID | Element | Purpose |
|----|---------|---------|
| `appConfigOverlay` | div | Backdrop |
| `appConfigPanel` | div | Modal card |
| `appConfigTitle` | div | "Edit App" / "Add App" |
| `appConfigClose` | button | Close ✕ button |
| `appConfigLabel` | input | App label text input |
| `appConfigUrl` | input | App URL input |
| `appConfigIconPreview` | div | Inline preview of selected icon |
| `appConfigChooseIcon` | button | Opens icon picker |
| `appConfigRemoveIcon` | button | Clears icon selection |
| `appConfigDeleteBtn` | button | Delete app (hidden in add mode) |
| `appConfigCancelBtn` | button | Cancel / close |
| `appConfigSaveBtn` | button | Save changes |

---

## Appendix: Reference Implementation Notes

### A.1 Overlay CSS Template (consistent with `_openWikiBrowser`)

```js
const OVERLAY_CSS = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';
const PANEL_CSS = 'background:var(--bg);border:1px solid var(--border);border-radius:8px;display:flex;flex-direction:column;overflow:hidden;';
const HEADER_CSS = 'display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border);';
const TITLE_CSS = 'font-size:14px;font-weight:700;';
const CLOSE_BTN_CSS = 'background:none;border:none;cursor:pointer;font-size:18px;color:var(--muted);padding:2px;line-height:1;';
```

### A.2 Escape-Key Close (shared utility)

```js
let _activeEscHandler = null;

function _registerEscClose(closeFn) {
  if (_activeEscHandler) document.removeEventListener('keydown', _activeEscHandler);
  _activeEscHandler = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); closeFn(); }
  };
  document.addEventListener('keydown', _activeEscHandler);
}

function _unregisterEscClose() {
  if (_activeEscHandler) {
    document.removeEventListener('keydown', _activeEscHandler);
    _activeEscHandler = null;
  }
}
```

### A.3 HTML Input Validation Pattern

```js
function _validateUrl(url) {
  return /^https?:\/\/.+/.test(url);
}

function _validateLabel(label) {
  const trimmed = (label || '').trim();
  return trimmed.length > 0 && trimmed.length <= 24;
}
```

---

> **End of spec.** This document covers App Manager, Icon Picker, Config Dialog, `updateIconDisplay()`, keyboard handling, six edge cases, state management lifecycle, and all DOM ID contracts for the Hermes WebUI `app-tabs` extension.
