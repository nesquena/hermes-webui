# Neo Mobile Composer (Phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Simplify the mobile composer from 8-11 competing chips to a ChatGPT-style layout: `[+ menu] [textarea] [mic] [send]` as primary line, with a single collapsible chip that opens a bottom-sheet for model/profile/workspace/reasoning settings.

**Architecture:** CSS changes scoped to `@media(max-width:640px)`. New bottom-sheet overlay uses the same pattern as `dashboard-mobile-overlay` (fixed position + `.mobile-open` toggle). JS additions in `static/boot.js`. No new dependencies.

**Tech Stack:** HTML/CSS/JS vanilla, pytest (static file assertions)

---

### Task 1: Add textarea font-size 16px for mobile (iOS zoom fix)

**Files:**
- Modify: `static/style.css` (inside `@media(max-width:640px)` block, around line 1197)

- [ ] **Step 1: Add font-size rule for textarea in mobile breakpoint**

Inside the `@media(max-width:640px)` block, after `.composer-box textarea{min-height:40px;}` (line 1197), add:

```css
    .composer-box textarea{min-height:40px;font-size:16px;}
```

(Merge into existing rule — replace the line.)

- [ ] **Step 2: Commit**

```bash
git add static/style.css
git commit -m "fix(mobile): textarea font-size 16px to prevent iOS Safari zoom on focus"
```

---

### Task 2: Increase send-btn to 44x44 on mobile

**Files:**
- Modify: `static/style.css` (line 1220 in `@media(max-width:640px)`)

- [ ] **Step 1: Update send-btn size**

Replace:
```css
    .send-btn{width:32px;height:32px;}
```

With:
```css
    .send-btn{width:44px;height:44px;border-radius:50%;}
```

- [ ] **Step 2: Commit**

```bash
git add static/style.css
git commit -m "fix(mobile): send button 44x44 touch target (was 32x32)"
```

---

### Task 3: Hide non-essential elements on mobile, reorganize composer layout

**Files:**
- Modify: `static/style.css` (inside `@media(max-width:640px)` block)

- [ ] **Step 1: Hide drop-hint, ctx-indicator, bg-badge, composer-status, and all individual chips on mobile**

After the existing `.composer-divider{display:none;}` rule (line 1218), add:

```css
    .drop-hint{display:none!important;}
    .composer-status{display:none!important;}
    .ctx-indicator-wrap{display:none!important;}
    .bg-badge{display:none!important;}
    .composer-profile-wrap{display:none!important;}
    .composer-ws-wrap{display:none!important;}
    .composer-model-wrap{display:none!important;}
    .composer-reasoning-wrap{display:none!important;}
    .yolo-pill{display:none!important;}
```

- [ ] **Step 2: Reorganize composer-footer as single row with primary actions**

Replace the existing composer-footer mobile rules:
```css
    .composer-footer{padding:6px 8px 8px!important;gap:8px;}
```

With:
```css
    .composer-footer{padding:6px 8px 8px!important;gap:6px;flex-wrap:nowrap;}
    .composer-left{gap:4px;flex:0 0 auto;}
    .composer-right{gap:6px;flex:0 0 auto;margin-left:auto;}
```

- [ ] **Step 3: Commit**

```bash
git add static/style.css
git commit -m "feat(mobile): hide non-essential composer elements, streamline layout"
```

---

### Task 4: Add "+" menu button (replaces attach as primary action)

**Files:**
- Modify: `static/index.html` (composer-left area, around line 911-915)
- Modify: `static/style.css` (add mobile plus-menu styles)
- Modify: `static/boot.js` (add toggleComposerPlusMenu function)

- [ ] **Step 1: Add plus-menu button in HTML before the attach button**

In `static/index.html`, before the `<button class="icon-btn" id="btnAttach"...>` (line 913), insert:

```html
            <button class="icon-btn composer-plus-btn" id="btnComposerPlus" type="button" title="Mais ações" aria-label="Mais ações" onclick="toggleComposerPlusMenu()">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><path d="M5 12h14"/></svg>
            </button>
            <div class="composer-plus-menu" id="composerPlusMenu">
              <button class="composer-plus-item" type="button" onclick="document.getElementById('fileInput').click();toggleComposerPlusMenu(false)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                <span>Anexar arquivo</span>
              </button>
              <button class="composer-plus-item" type="button" onclick="toggleWorkspacePanel();toggleComposerPlusMenu(false)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                <span>Workspace files</span>
              </button>
              <button class="composer-plus-item" type="button" onclick="cmdYolo();toggleComposerPlusMenu(false)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>
                <span>YOLO mode</span>
              </button>
              <button class="composer-plus-item" type="button" onclick="openComposerTerminal();toggleComposerPlusMenu(false)">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
                <span>Terminal</span>
              </button>
            </div>
```

- [ ] **Step 2: Add CSS for plus-menu and plus-btn**

In `static/style.css`, add after the `.model-dropdown` rules (around line 1278), these new rules:

```css
/* ── Composer plus-menu (mobile) ── */
.composer-plus-btn{display:none;}
.composer-plus-menu{display:none;position:absolute;bottom:calc(100% + 8px);left:8px;min-width:200px;background:var(--surface);border:1px solid var(--border2);border-radius:12px;box-shadow:0 -4px 24px rgba(0,0,0,.4);z-index:200;overflow:hidden;padding:6px 0;}
.composer-plus-menu.open{display:block;}
.composer-plus-item{display:flex;align-items:center;gap:10px;width:100%;padding:12px 16px;background:none;border:none;color:var(--text);font-size:14px;cursor:pointer;transition:background-color .12s;}
.composer-plus-item:hover{background:var(--hover-bg);}
.composer-plus-item svg{color:var(--muted);flex-shrink:0;}
```

And inside the `@media(max-width:640px)` block, add:

```css
    .composer-plus-btn{display:flex!important;min-width:44px;min-height:44px;align-items:center;justify-content:center;}
    #btnAttach{display:none!important;}
```

- [ ] **Step 3: Add toggleComposerPlusMenu in boot.js**

In `static/boot.js`, after the `handleNewChat` function, add:

```javascript
function toggleComposerPlusMenu(force){
  const menu=$('composerPlusMenu');
  if(!menu)return;
  const open=typeof force==='boolean'?force:!menu.classList.contains('open');
  menu.classList.toggle('open',open);
  if(open){
    const close=e=>{if(!menu.contains(e.target)&&e.target.id!=='btnComposerPlus'){menu.classList.remove('open');document.removeEventListener('pointerdown',close);}};
    document.addEventListener('pointerdown',close);
  }
}
```

- [ ] **Step 4: Commit**

```bash
git add static/index.html static/style.css static/boot.js
git commit -m "feat(mobile): add composer plus-menu replacing individual attach/workspace/yolo/terminal buttons"
```

---

### Task 5: Add model-context chip with bottom-sheet

**Files:**
- Modify: `static/index.html` (add bottom-sheet markup after composer-box)
- Modify: `static/style.css` (bottom-sheet styles)
- Modify: `static/boot.js` (toggle logic)

- [ ] **Step 1: Add bottom-sheet HTML after the composer-box closing div (after line ~1024)**

Find the line `</div>` that closes `.composer-box` (the one with `upload-bar-wrap` just before it). After the `</div>` that closes `.composer-wrap`, add:

Actually, insert inside the `.composer-footer` div, in `.composer-left`, after the plus-menu div. Add a model-context chip:

In `static/index.html`, after the `</div>` closing `composerPlusMenu`, add:

```html
            <button class="composer-context-chip" id="composerContextChip" type="button" onclick="toggleComposerBottomSheet()" aria-label="Modelo e contexto">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg>
              <span class="composer-context-label" id="composerContextLabel"></span>
              <svg class="composer-context-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
```

- [ ] **Step 2: Add bottom-sheet overlay HTML before closing `</body>`**

Before `</body>` in `static/index.html`, add:

```html
<div class="composer-bottomsheet-overlay" id="composerBottomSheetOverlay" onclick="toggleComposerBottomSheet(false)"></div>
<div class="composer-bottomsheet" id="composerBottomSheet">
  <div class="composer-bottomsheet-handle" aria-hidden="true"></div>
  <div class="composer-bottomsheet-title">Modelo e contexto</div>
  <div class="composer-bottomsheet-section">
    <label class="composer-bottomsheet-label">Modelo</label>
    <div class="composer-bottomsheet-model" id="bottomSheetModelWrap"></div>
  </div>
  <div class="composer-bottomsheet-section">
    <label class="composer-bottomsheet-label">Profile</label>
    <div class="composer-bottomsheet-profile" id="bottomSheetProfileWrap"></div>
  </div>
  <div class="composer-bottomsheet-section">
    <label class="composer-bottomsheet-label">Workspace</label>
    <div class="composer-bottomsheet-workspace" id="bottomSheetWorkspaceWrap"></div>
  </div>
  <div class="composer-bottomsheet-section" id="bottomSheetReasoningSection" style="display:none">
    <label class="composer-bottomsheet-label">Raciocínio</label>
    <div class="composer-bottomsheet-reasoning">
      <div class="reasoning-segmented" id="bottomSheetReasoning">
        <button class="reasoning-seg" data-effort="none" type="button">Rápido</button>
        <button class="reasoning-seg active" data-effort="medium" type="button">Normal</button>
        <button class="reasoning-seg" data-effort="high" type="button">Profundo</button>
      </div>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Add bottom-sheet CSS**

In `static/style.css`, after the composer-plus-menu rules, add:

```css
/* ── Composer bottom-sheet (mobile) ── */
.composer-bottomsheet-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:399;-webkit-tap-highlight-color:transparent;}
.composer-bottomsheet-overlay.visible{display:block;}
.composer-bottomsheet{display:none;position:fixed;bottom:0;left:0;right:0;background:var(--surface);border-top-left-radius:16px;border-top-right-radius:16px;z-index:400;padding:12px 20px max(20px,env(safe-area-inset-bottom));max-height:70vh;overflow-y:auto;transform:translateY(100%);transition:transform .25s ease;}
.composer-bottomsheet.open{display:block;transform:translateY(0);}
.composer-bottomsheet-handle{width:36px;height:4px;border-radius:2px;background:var(--border);margin:0 auto 14px;}
.composer-bottomsheet-title{font-size:16px;font-weight:600;color:var(--text);margin-bottom:16px;}
.composer-bottomsheet-section{margin-bottom:16px;}
.composer-bottomsheet-label{font-size:12px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.04em;margin-bottom:8px;display:block;}
.composer-context-chip{display:none;align-items:center;gap:6px;padding:6px 10px;border-radius:8px;background:var(--hover-bg);border:1px solid var(--border);color:var(--text);font-size:12px;cursor:pointer;min-height:36px;transition:background-color .12s;}
.composer-context-chip:hover{background:var(--border);}
.composer-context-chevron{color:var(--muted);}
.reasoning-segmented{display:flex;gap:0;border-radius:8px;overflow:hidden;border:1px solid var(--border);}
.reasoning-seg{flex:1;padding:10px 12px;background:none;border:none;color:var(--muted);font-size:13px;font-weight:500;cursor:pointer;transition:all .12s;}
.reasoning-seg.active{background:var(--accent-bg);color:var(--accent-text);}
.reasoning-seg:hover:not(.active){background:var(--hover-bg);}
```

And inside `@media(max-width:640px)`, add:

```css
    .composer-context-chip{display:flex;}
```

- [ ] **Step 4: Add toggleComposerBottomSheet in boot.js**

In `static/boot.js`, after `toggleComposerPlusMenu`, add:

```javascript
function toggleComposerBottomSheet(force){
  const sheet=$('composerBottomSheet');
  const overlay=$('composerBottomSheetOverlay');
  if(!sheet)return;
  const open=typeof force==='boolean'?force:!sheet.classList.contains('open');
  sheet.classList.toggle('open',open);
  if(overlay)overlay.classList.toggle('visible',open);
  if(open)syncBottomSheetContent();
}
function syncBottomSheetContent(){
  const modelLabel=$('composerModelLabel');
  const ctxLabel=$('composerContextLabel');
  if(modelLabel&&ctxLabel)ctxLabel.textContent=modelLabel.textContent||'Modelo';
  const modelWrap=$('bottomSheetModelWrap');
  if(modelWrap){
    const sel=$('modelSelect');
    if(sel)modelWrap.textContent=sel.options[sel.selectedIndex]?.text||'';
  }
  const profileWrap=$('bottomSheetProfileWrap');
  if(profileWrap){
    const lbl=$('profileChipLabel');
    profileWrap.textContent=lbl?lbl.textContent:'default';
  }
  const wsWrap=$('bottomSheetWorkspaceWrap');
  if(wsWrap){
    const lbl=$('composerWorkspaceLabel');
    wsWrap.textContent=lbl?lbl.textContent:'—';
  }
  const reasonSec=$('bottomSheetReasoningSection');
  const reasonWrap=$('composerReasoningWrap');
  if(reasonSec)reasonSec.style.display=(reasonWrap&&reasonWrap.style.display!=='none')?'':'none';
}
```

- [ ] **Step 5: Add reasoning segmented control click handler in boot.js**

After `syncBottomSheetContent`, add:

```javascript
document.addEventListener('click',function(e){
  const seg=e.target.closest('.reasoning-seg');
  if(!seg)return;
  const container=seg.parentElement;
  container.querySelectorAll('.reasoning-seg').forEach(s=>s.classList.remove('active'));
  seg.classList.add('active');
  const effort=seg.dataset.effort;
  if(typeof setReasoningEffort==='function')setReasoningEffort(effort);
});
```

- [ ] **Step 6: Commit**

```bash
git add static/index.html static/style.css static/boot.js
git commit -m "feat(mobile): add model-context chip and bottom-sheet for composer settings"
```

---

### Task 6: Write tests — test_neo_mobile_composer.py

**Files:**
- Create: `tests/test_neo_mobile_composer.py`

- [ ] **Step 1: Write the test file**

```python
"""Phase 2 — Neo mobile composer contract tests.

Verify the mobile composer meets the spec:
- Textarea font-size 16px in mobile breakpoint (iOS zoom prevention)
- Send button 44x44 in mobile breakpoint
- Plus-menu button present with menu items
- Individual chips hidden on mobile (profile/workspace/model/reasoning)
- Context chip present for bottom-sheet trigger
- Bottom-sheet overlay and panel present in HTML
- Bottom-sheet has reasoning segmented control (3 states)
- drop-hint hidden on mobile
- ctx-indicator hidden on mobile
- bg-badge hidden on mobile
"""

import pathlib
import re

REPO = pathlib.Path(__file__).parent.parent
HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
BOOT_JS = (REPO / "static" / "boot.js").read_text(encoding="utf-8")


def _mobile_640_block():
    start = CSS.find("@media(max-width:640px)")
    return CSS[start:start + 4000]


def test_textarea_font_size_16px_mobile():
    block = _mobile_640_block()
    assert "font-size:16px" in block, \
        "textarea must have font-size:16px in mobile to prevent iOS zoom"


def test_send_btn_44x44_mobile():
    block = _mobile_640_block()
    match = re.search(r'\.send-btn\{[^}]*width:44px[^}]*height:44px', block)
    assert match, "send-btn must be 44x44 in mobile breakpoint"


def test_plus_menu_button_present():
    assert 'id="btnComposerPlus"' in HTML, \
        "Plus-menu button must be present in composer"


def test_plus_menu_items():
    assert 'id="composerPlusMenu"' in HTML, \
        "Plus-menu container must exist"
    menu_start = HTML.find('id="composerPlusMenu"')
    menu_block = HTML[menu_start:menu_start + 2000]
    assert "Anexar arquivo" in menu_block
    assert "Workspace files" in menu_block
    assert "YOLO mode" in menu_block
    assert "Terminal" in menu_block


def test_individual_chips_hidden_mobile():
    block = _mobile_640_block()
    assert ".composer-profile-wrap{display:none" in block, \
        "Profile chip must be hidden on mobile"
    assert ".composer-model-wrap{display:none" in block, \
        "Model chip must be hidden on mobile"


def test_context_chip_present():
    assert 'id="composerContextChip"' in HTML, \
        "Context chip must be present for bottom-sheet trigger"


def test_context_chip_visible_mobile():
    block = _mobile_640_block()
    assert ".composer-context-chip{display:flex" in block, \
        "Context chip must be display:flex on mobile"


def test_bottomsheet_overlay_present():
    assert 'id="composerBottomSheetOverlay"' in HTML, \
        "Bottom-sheet overlay must exist"


def test_bottomsheet_panel_present():
    assert 'id="composerBottomSheet"' in HTML, \
        "Bottom-sheet panel must exist"


def test_bottomsheet_reasoning_segmented():
    assert 'id="bottomSheetReasoning"' in HTML
    sheet_start = HTML.find('id="bottomSheetReasoning"')
    sheet_block = HTML[sheet_start:sheet_start + 500]
    assert 'data-effort="none"' in sheet_block
    assert 'data-effort="medium"' in sheet_block
    assert 'data-effort="high"' in sheet_block


def test_drop_hint_hidden_mobile():
    block = _mobile_640_block()
    assert ".drop-hint{display:none" in block, \
        "drop-hint must be hidden on mobile"


def test_ctx_indicator_hidden_mobile():
    block = _mobile_640_block()
    assert ".ctx-indicator-wrap{display:none" in block, \
        "ctx-indicator must be hidden on mobile"


def test_bg_badge_hidden_mobile():
    block = _mobile_640_block()
    assert ".bg-badge{display:none" in block, \
        "bg-badge must be hidden on mobile"


def test_toggle_composer_plus_menu_js():
    assert "function toggleComposerPlusMenu(" in BOOT_JS, \
        "toggleComposerPlusMenu must be defined in boot.js"


def test_toggle_composer_bottom_sheet_js():
    assert "function toggleComposerBottomSheet(" in BOOT_JS, \
        "toggleComposerBottomSheet must be defined in boot.js"
```

- [ ] **Step 2: Run tests**

```bash
.venv/bin/pytest tests/test_neo_mobile_composer.py -v
```

Expected: All tests PASS.

- [ ] **Step 3: Run related tests for regression**

```bash
.venv/bin/pytest tests/test_mobile_layout.py tests/test_neo_mobile_composer.py -v
```

- [ ] **Step 4: Commit**

```bash
git add tests/test_neo_mobile_composer.py
git commit -m "test(mobile): add contract tests for Phase 2 mobile composer"
```

---

### Task 7: Final verification

- [ ] **Step 1: Run full related test suite**

```bash
.venv/bin/pytest tests/test_mobile_layout.py tests/test_neo_dashboard_mobile_rail.py tests/test_neo_mobile_composer.py -q
```

- [ ] **Step 2: Push branch**

```bash
git push -u origin feat/neo-mobile-composer
```
