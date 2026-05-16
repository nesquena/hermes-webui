# Neo Mobile Header (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the mobile header from a 38px legacy titlebar into a proper 52px mobile-native header with safe-area support, Lucide-style icon, brand identity, and contextual actions.

**Architecture:** All changes scoped to `@media(max-width:640px)` in `static/style.css` plus minimal HTML edits in `static/index.html` (titlebar region lines 71-83) and one small JS addition in `static/boot.js`. No new files except the test. Desktop (≥641px) behavior unchanged.

**Tech Stack:** HTML/CSS/JS vanilla, pytest (static file assertions)

---

### Task 1: Create branch and add viewport-fit=cover + theme-color meta

**Files:**
- Modify: `static/index.html:5` (viewport meta)
- Modify: `static/index.html:15` (add theme-color after apple-touch-icon)

- [ ] **Step 1: Create feature branch from develop**

```bash
git checkout develop
git pull origin develop
git checkout -b feat/neo-mobile-header
```

- [ ] **Step 2: Update viewport meta to include viewport-fit=cover**

In `static/index.html` line 5, change:
```html
<meta name="viewport" content="width=device-width, initial-scale=1">
```
to:
```html
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
```

- [ ] **Step 3: Add theme-color meta tag after apple-touch-icon (line 15)**

Insert after line 15:
```html
<meta name="theme-color" content="#0a0a0f" media="(prefers-color-scheme: dark)">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
```

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat(mobile): add viewport-fit=cover and theme-color meta tags"
```

---

### Task 2: Refactor app-titlebar HTML — Lucide hamburger, brand, contextual actions

**Files:**
- Modify: `static/index.html:71-83` (app-titlebar block)

- [ ] **Step 1: Replace the entire app-titlebar block (lines 71-83)**

Replace:
```html
<header class="app-titlebar" role="banner">
  <button class="app-titlebar-hamburger" id="btnHamburger" onclick="toggleMobileSidebar()" type="button" title="Menu" aria-label="Menu">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
  </button>
  <div class="app-titlebar-inner">
    <span class="app-titlebar-icon" aria-hidden="true">
      <img src="static/brand/neo-mark.svg" width="16" height="16" alt="">
    </span>
    <span class="app-titlebar-title" id="appTitlebarTitle">Neo</span>
    <span class="app-titlebar-sub" id="appTitlebarSub" hidden></span>
  </div>
  <div class="app-titlebar-spacer" aria-hidden="true"></div>
</header>
```

With:
```html
<header class="app-titlebar" role="banner">
  <button class="app-titlebar-hamburger" id="btnHamburger" onclick="toggleMobileSidebar()" type="button" title="Menu" aria-label="Menu">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/></svg>
  </button>
  <div class="app-titlebar-inner">
    <img class="app-titlebar-icon" src="static/brand/neo-ico.png" width="24" height="24" alt="" aria-hidden="true">
    <span class="app-titlebar-title" id="appTitlebarTitle">Neo</span>
  </div>
  <div class="app-titlebar-actions">
    <button class="app-titlebar-action" id="btnTitlebarNewChat" type="button" title="Novo chat" aria-label="Novo chat" onclick="handleNewChat()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
    </button>
    <button class="app-titlebar-action" id="btnTitlebarMore" type="button" title="Mais opções" aria-label="Mais opções" onclick="toggleDashboardMobileRail()">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="15" y1="3" x2="15" y2="21"/></svg>
    </button>
  </div>
</header>
```

- [ ] **Step 2: Commit**

```bash
git add static/index.html
git commit -m "feat(mobile): refactor app-titlebar with Lucide icon, brand, and contextual actions"
```

---

### Task 3: Update CSS — header height, safe-area, brand, actions, remove app-region:drag in PWA

**Files:**
- Modify: `static/style.css:325-333` (app-titlebar base styles)
- Modify: `static/style.css:1160-1169` (640px breakpoint — hamburger/spacer rules)

- [ ] **Step 1: Replace app-titlebar base styles (lines 325-333)**

Replace:
```css
.app-titlebar{display:flex;align-items:center;justify-content:center;height:38px;flex-shrink:0;background:var(--sidebar);border-bottom:1px solid var(--border);padding:0 12px;padding-top:env(safe-area-inset-top,0);padding-left:max(12px,env(safe-area-inset-left,0));padding-right:max(12px,env(safe-area-inset-right,0));box-sizing:content-box;font-size:12px;color:var(--muted);user-select:none;-webkit-app-region:drag;position:relative;z-index:20;}
.app-titlebar-inner{display:flex;align-items:center;gap:8px;min-width:0;max-width:100%;justify-content:center;}
.app-titlebar-icon{display:inline-flex;align-items:center;color:var(--accent);}
.app-titlebar-title{font-size:12px;font-weight:600;color:var(--text);letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:60vw;}
.app-titlebar-sub{font-size:10px;color:var(--muted);background:var(--hover-bg);padding:2px 7px;border-radius:4px;font-family:'SF Mono',ui-monospace,monospace;white-space:nowrap;flex-shrink:0;}
.app-titlebar-sub[hidden]{display:none;}
.app-titlebar-hamburger,.app-titlebar-spacer{display:none;width:32px;height:32px;flex-shrink:0;}
.app-titlebar-hamburger{-webkit-app-region:no-drag;align-items:center;justify-content:center;background:none;border:none;color:var(--muted);border-radius:8px;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent;transition:background-color .15s,color .15s;}
.app-titlebar-hamburger:hover{background:var(--hover-bg);color:var(--text);}
```

With:
```css
.app-titlebar{display:flex;align-items:center;justify-content:center;height:38px;flex-shrink:0;background:var(--sidebar);border-bottom:1px solid var(--border);padding:0 12px;padding-top:env(safe-area-inset-top,0);padding-left:max(12px,env(safe-area-inset-left,0));padding-right:max(12px,env(safe-area-inset-right,0));box-sizing:content-box;font-size:12px;color:var(--muted);user-select:none;position:relative;z-index:20;}
@media (display-mode: window-controls-overlay){.app-titlebar{-webkit-app-region:drag;}}
.app-titlebar-inner{display:flex;align-items:center;gap:8px;min-width:0;max-width:100%;justify-content:center;}
.app-titlebar-icon{display:inline-flex;align-items:center;width:24px;height:24px;border-radius:6px;flex-shrink:0;}
.app-titlebar-title{font-size:12px;font-weight:600;color:var(--text);letter-spacing:-.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:60vw;}
.app-titlebar-actions{display:none;align-items:center;gap:4px;}
.app-titlebar-action{display:flex;align-items:center;justify-content:center;width:44px;height:44px;background:none;border:none;color:var(--muted);border-radius:10px;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent;transition:background-color .15s,color .15s;}
.app-titlebar-action:hover{background:var(--hover-bg);color:var(--text);}
.app-titlebar-hamburger,.app-titlebar-spacer{display:none;width:44px;height:44px;flex-shrink:0;}
.app-titlebar-hamburger{align-items:center;justify-content:center;background:none;border:none;color:var(--muted);border-radius:10px;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent;transition:background-color .15s,color .15s;}
.app-titlebar-hamburger:hover{background:var(--hover-bg);color:var(--text);}
```

- [ ] **Step 2: Update the @media(max-width:640px) block for titlebar (lines 1167-1169)**

Replace:
```css
    .app-titlebar{justify-content:space-between;}
    .app-titlebar-hamburger,.app-titlebar-spacer{display:flex;}
    .app-titlebar-inner{flex:1 1 auto;}
```

With:
```css
    .app-titlebar{justify-content:space-between;height:52px;font-size:14px;}
    .app-titlebar-hamburger{display:flex;}
    .app-titlebar-actions{display:flex;}
    .app-titlebar-inner{flex:1 1 auto;justify-content:flex-start;}
    .app-titlebar-title{font-size:16px;font-weight:700;letter-spacing:-.01em;}
    .app-titlebar-icon{width:24px;height:24px;}
```

- [ ] **Step 3: Commit**

```bash
git add static/style.css
git commit -m "feat(mobile): 52px header, safe-area, Lucide brand, contextual actions, remove app-region:drag from PWA"
```

---

### Task 4: Add handleNewChat helper in boot.js

**Files:**
- Modify: `static/boot.js` (after closeMobileSidebar, ~line 165)

- [ ] **Step 1: Add handleNewChat function after closeMobileSidebar (line 165)**

Insert after `closeMobileSidebar`:
```javascript
function handleNewChat(){
  if(typeof newChat==='function')newChat();
  else if(typeof switchPanel==='function')switchPanel('chat');
}
```

- [ ] **Step 2: Commit**

```bash
git add static/boot.js
git commit -m "feat(mobile): add handleNewChat helper for titlebar action button"
```

---

### Task 5: Remove app-titlebar-spacer from dashboard-shell-mode rule

**Files:**
- Modify: `static/style.css:2668` (remove .app-titlebar-spacer from the hide list)

- [ ] **Step 1: Update the dashboard-shell-mode hide rule (line 2665-2668)**

Replace:
```css
body.dashboard-shell-mode .rail,
body.dashboard-shell-mode .sidebar-nav,
body.dashboard-shell-mode #sidebarResize,
body.dashboard-shell-mode .app-titlebar-spacer{display:none!important;}
```

With:
```css
body.dashboard-shell-mode .rail,
body.dashboard-shell-mode .sidebar-nav,
body.dashboard-shell-mode #sidebarResize{display:none!important;}
```

- [ ] **Step 2: Commit**

```bash
git add static/style.css
git commit -m "refactor(mobile): remove app-titlebar-spacer from shell-mode hide list (element removed)"
```

---

### Task 6: Write test — test_neo_mobile_header.py

**Files:**
- Create: `tests/test_neo_mobile_header.py`

- [ ] **Step 1: Write the test file**

```python
"""Phase 1 — Neo mobile header contract tests.

Verify the mobile header meets the spec:
- Lucide-style hamburger icon (24x24, stroke-width 1.75)
- Brand: neo-ico.png 24x24 + title "Neo" (not neo-mark.svg 16x16)
- Contextual actions: new-chat and more buttons present
- Height 52px in mobile breakpoint
- viewport-fit=cover in viewport meta
- theme-color meta tags present
- No -webkit-app-region:drag outside window-controls-overlay
- app-titlebar-action targets 44x44
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")


def test_viewport_fit_cover():
    assert "viewport-fit=cover" in HTML, \
        "viewport meta must include viewport-fit=cover for safe-area support"


def test_theme_color_meta_present():
    assert 'name="theme-color"' in HTML, \
        "theme-color meta tag must be present for PWA status bar"


def test_hamburger_lucide_style():
    hamburger_match = re.search(
        r'id="btnHamburger"[^>]*>.*?<svg[^>]*width="24"[^>]*height="24"[^>]*stroke-width="1\.75"',
        HTML, re.DOTALL
    )
    assert hamburger_match, \
        "Hamburger must be 24x24 SVG with stroke-width 1.75 (Lucide style)"


def test_brand_neo_ico_24():
    assert 'src="static/brand/neo-ico.png"' in HTML, \
        "Brand icon must use neo-ico.png (not neo-mark.svg)"
    assert 'width="24" height="24"' in HTML[HTML.find("neo-ico.png")-50:HTML.find("neo-ico.png")+100], \
        "Brand icon must be 24x24"


def test_title_neo_in_titlebar():
    title_match = re.search(r'id="appTitlebarTitle"[^>]*>Neo<', HTML)
    assert title_match, "Titlebar must show 'Neo' text"


def test_contextual_action_new_chat():
    assert 'id="btnTitlebarNewChat"' in HTML, \
        "New chat action button must be in titlebar"


def test_contextual_action_more():
    assert 'id="btnTitlebarMore"' in HTML, \
        "More (drawer toggle) action button must be in titlebar"


def test_action_buttons_44x44():
    action_rule = re.search(r'\.app-titlebar-action\{[^}]*width:44px[^}]*height:44px', CSS)
    assert action_rule, \
        "app-titlebar-action must have 44x44 touch target"


def test_hamburger_44x44():
    hamburger_rule = re.search(r'\.app-titlebar-hamburger[^{]*\{[^}]*width:44px[^}]*height:44px', CSS)
    assert hamburger_rule, \
        "Hamburger button must have 44x44 touch target (was 32x32)"


def test_header_52px_mobile():
    mobile_640 = CSS[CSS.find("@media(max-width:640px)"):]
    assert "height:52px" in mobile_640[:2000], \
        "app-titlebar must be 52px height in mobile breakpoint"


def test_no_app_region_drag_base():
    titlebar_base = re.search(r'\.app-titlebar\{([^}]+)\}', CSS)
    assert titlebar_base, "app-titlebar base rule must exist"
    assert "-webkit-app-region:drag" not in titlebar_base.group(1), \
        "app-titlebar must NOT have -webkit-app-region:drag in base (only in window-controls-overlay)"


def test_app_region_drag_only_in_wco():
    assert "display-mode: window-controls-overlay" in CSS, \
        "-webkit-app-region:drag must be guarded by @media (display-mode: window-controls-overlay)"


def test_titlebar_sub_removed():
    assert 'app-titlebar-sub' not in HTML, \
        "app-titlebar-sub element should be removed from HTML (unused debug pill)"
```

- [ ] **Step 2: Run tests to verify they pass**

```bash
python -m pytest tests/test_neo_mobile_header.py -v
```

Expected: All 12 tests PASS.

- [ ] **Step 3: Run full test suite to check no regressions**

```bash
python -m pytest tests/test_mobile_layout.py tests/test_neo_dashboard_mobile_rail.py tests/test_neo_mobile_header.py -v
```

Expected: All tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/test_neo_mobile_header.py
git commit -m "test(mobile): add contract tests for Phase 1 mobile header"
```

---

### Task 7: Final verification and cleanup

- [ ] **Step 1: Run the full test suite**

```bash
python -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

Expected: No failures related to titlebar, mobile layout, or dashboard rail.

- [ ] **Step 2: Visual check — confirm desktop is unchanged**

Verify in CSS that the 52px height and action buttons only activate inside `@media(max-width:640px)`. Desktop keeps 38px centered titlebar with no action buttons visible.

- [ ] **Step 3: Push branch**

```bash
git push -u origin feat/neo-mobile-header
```
