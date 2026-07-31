"""Regression guards for #6080 — mobile composer model picker lands off-screen.

Root cause: #composerModelDropdown is a child of .composer-footer, which sets
`container-type:inline-size` (and, under the Geist Contrast skin, a
`backdrop-filter`). Both establish a fixed containing block, so a phone
`position:fixed` dropdown resolves against the FOOTER (pinned to the bottom of
the screen) instead of the visual viewport, dropping the menu below the fold —
the "tapping the model picker does nothing / opens off-screen" symptom.

Fix (PR #6105 rework, credit @webtecnica): on the phone path the dropdown is
reparented to <body> and switched to position:fixed via a `--floating` class,
exactly the working #profileDropdown idiom, so `fixed` is viewport-relative on
ALL skins. Desktop (>640px) is unchanged: the menu stays an absolutely
positioned .composer-footer child.

The node-executed test below is the load-bearing one: it runs the real
`_positionModelDropdown` / `closeModelDropdown` against a stub DOM and asserts
DOM parentage, i.e. that the dropdown is NOT a descendant of the footer while
open on a phone (so no container-type ancestor can trap its fixed positioning),
and that it is restored into the footer on close and on desktop.
"""
import json
import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, marker: str) -> str:
    start = src.index(marker)
    depth = 0
    opened = False
    for idx, ch in enumerate(src[start:], start):
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"Could not extract function body for {marker}")


# ── Static structure guards ──────────────────────────────────────────────────

def test_css_has_floating_fixed_rule():
    """The reparented phone menu needs a body-level position:fixed rule."""
    m = re.search(r"\.model-dropdown\.model-dropdown--floating\{([^}]*)\}", CSS)
    assert m, ".model-dropdown--floating rule missing (phone reparent CSS)"
    body = m.group(1)
    assert "position:fixed" in body, (
        "floating model dropdown must be position:fixed so it anchors to the "
        "viewport once reparented to <body> (#6080)"
    )


def test_no_geist_backdrop_override_added_for_model_dropdown():
    """Because the menu leaves the footer, the Geist backdrop-filter workaround
    must NOT be introduced (the reparent is the fix, not a per-skin override)."""
    assert "container-type:normal" not in CSS.replace(" ", ""), (
        "do not neutralise container-type on the footer — reparent instead"
    )


def test_position_fn_has_phone_reparent_branch():
    body = _function_body(UI_JS, "function _positionModelDropdown(")
    assert "matchMedia('(max-width:640px)')" in body, "phone breakpoint check missing"
    assert "document.body.appendChild(dd)" in body, (
        "phone path must reparent the dropdown to <body> to escape the "
        ".composer-footer containing block (#6080)"
    )
    assert "model-dropdown--floating" in body, "phone path must apply the floating class"
    # PR #6105 visual-viewport math must be preserved.
    assert "window.visualViewport" in body
    assert "titlebar" in body and "app-titlebar" in body, "titlebar clamp preserved"
    assert "openAbove" in body, "above/below flip preserved"
    assert "menuWidth" in body and "margin*2" in body, "width=viewport-16px preserved"


def test_close_restores_home_parent():
    body = _function_body(UI_JS, "function closeModelDropdown(")
    assert "_restoreModelDropdownHome" in body, (
        "closeModelDropdown must return the reparented menu to the footer"
    )


def test_raf_coalesced_visualviewport_listeners_present():
    assert "_repositionOpenModelDropdown" in UI_JS
    assert "requestAnimationFrame" in _function_body(UI_JS, "function _repositionOpenModelDropdown(")
    assert "window.visualViewport.addEventListener('resize',_repositionOpenModelDropdown)" in UI_JS
    assert "window.visualViewport.addEventListener('scroll',_repositionOpenModelDropdown)" in UI_JS


# ── Load-bearing DOM-parentage guard (executed in node) ──────────────────────

def test_dropdown_escapes_footer_containing_block_when_open_on_phone():
    """Prove via real execution that the open phone dropdown is reparented to
    <body> and is NOT a descendant of .composer-footer (the container-type
    ancestor), and that it is restored into the footer on close / on desktop."""
    block_start = UI_JS.index("let _modelDropdownHome=null;")
    block_end = UI_JS.index("function _readModelOverflowData(")
    position_block = UI_JS[block_start:block_end]
    close_block = _function_body(UI_JS, "function closeModelDropdown(")
    snippets = [position_block, close_block]

    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const snippets = {json.dumps(snippets)};

        class ClassList {{
          constructor(el) {{ this.el = el; this.set = new Set(); }}
          add(n) {{ this.set.add(n); this._sync(); }}
          remove(n) {{ this.set.delete(n); this._sync(); }}
          contains(n) {{ return this.set.has(n); }}
          toggle(n, f) {{
            if (f === undefined) f = !this.set.has(n);
            if (f) this.add(n); else this.remove(n);
            return f;
          }}
          _sync() {{ this.el.className = [...this.set].join(' '); }}
        }}
        class Node {{
          constructor(tag, id) {{
            this.tagName = (tag || 'div').toUpperCase();
            this.id = id || '';
            this.className = '';
            this.classList = new ClassList(this);
            this.style = {{}};
            this.children = [];
            this.parentNode = null;
            this.dataset = {{}};
            this._rect = {{left:0, top:0, right:0, bottom:0, width:0, height:0}};
            this.scrollHeight = 0; this.offsetHeight = 0;
            this.offsetWidth = 0; this.clientWidth = 0;
            this.offsetParent = null;
          }}
          _detach(child) {{
            const i = this.children.indexOf(child);
            if (i >= 0) this.children.splice(i, 1);
            child.parentNode = null;
          }}
          appendChild(child) {{
            if (child.parentNode) child.parentNode._detach(child);
            child.parentNode = this; this.children.push(child); return child;
          }}
          insertBefore(child, ref) {{
            if (child.parentNode) child.parentNode._detach(child);
            child.parentNode = this;
            const i = ref ? this.children.indexOf(ref) : -1;
            if (i >= 0) this.children.splice(i, 0, child);
            else this.children.push(child);
            return child;
          }}
          get nextSibling() {{
            if (!this.parentNode) return null;
            const i = this.parentNode.children.indexOf(this);
            return this.parentNode.children[i + 1] || null;
          }}
          getBoundingClientRect() {{ return this._rect; }}
        }}

        const body = new Node('body', 'body');
        const footer = new Node('div');            // .composer-footer (container-type ancestor)
        footer.className = 'composer-footer';
        footer.clientWidth = 390;
        footer._rect = {{left:0, top:560, right:390, bottom:600, width:390, height:40}};
        const titlebar = new Node('div');
        titlebar.className = 'app-titlebar';
        titlebar._rect = {{left:0, top:0, right:390, bottom:44, width:390, height:44}};

        const dd = new Node('div', 'composerModelDropdown');
        dd.scrollHeight = 300; dd.offsetHeight = 300; dd.offsetWidth = 280;
        const chip = new Node('button', 'composerModelChip');
        chip.offsetParent = footer;   // visible on the desktop composer
        chip._rect = {{left:100, top:560, right:160, bottom:600, width:60, height:40}};
        const mobileAction = new Node('button', 'composerMobileModelAction');
        mobileAction.offsetParent = footer;
        mobileAction._rect = {{left:20, top:560, right:120, bottom:600, width:100, height:40}};
        const panel = new Node('div', 'composerMobileConfigPanel');

        // dd starts life inside the footer, followed by a sibling — proves the
        // insertBefore restore path returns it to the exact original slot.
        const trailingSibling = new Node('div', 'uploadBarWrap');
        footer.appendChild(dd);
        footer.appendChild(trailingSibling);

        const elements = new Map([
          ['composerModelDropdown', dd],
          ['composerModelChip', chip],
          ['composerMobileModelAction', mobileAction],
          ['composerMobileConfigPanel', panel],
        ]);

        let PHONE = true;
        globalThis.document = {{
          body,
          getElementById: (id) => elements.get(id) || null,
          querySelector: (sel) => {{
            if (sel === '.composer-footer') return footer;
            if (sel === '.app-titlebar') return titlebar;
            return null;
          }},
        }};
        globalThis.window = {{
          matchMedia: (q) => ({{ matches: q.indexOf('max-width:640px') !== -1 ? PHONE : false }}),
          innerWidth: 390, innerHeight: 800,
          visualViewport: {{ width: 390, height: 600, offsetTop: 0, offsetLeft: 0 }},
        }};
        globalThis.$ = (id) => elements.get(id) || null;

        eval(snippets.join(String.fromCharCode(10)) + String.fromCharCode(10) + `;globalThis.__t = {{
          position: _positionModelDropdown,
          close: closeModelDropdown,
          restore: _restoreModelDropdownHome,
        }};`);

        function ancestorIsFooter(el) {{
          let p = el.parentNode;
          while (p) {{ if (p === footer) return true; p = p.parentNode; }}
          return false;
        }}

        // ── Phone: open ──────────────────────────────────────────────────────
        PHONE = true;
        dd.classList.add('open');
        __t.position();
        assert.strictEqual(dd.parentNode, body,
          'open phone dropdown must be reparented to <body>');
        assert.notStrictEqual(dd.parentNode, footer,
          'open phone dropdown must NOT remain a footer child');
        assert.strictEqual(ancestorIsFooter(dd), false,
          'open phone dropdown must have NO .composer-footer ancestor — this is ' +
          'what escapes the container-type/backdrop-filter fixed-containing-block trap (#6080)');
        assert.strictEqual(dd.classList.contains('model-dropdown--floating'), true,
          'phone dropdown must carry the floating (position:fixed) class');
        assert.ok(dd.style.top && dd.style.top.endsWith('px'), 'phone path sets a px top');
        assert.ok(dd.style.width && dd.style.width.endsWith('px'), 'phone path sets a px width');

        // ── Phone: close restores it to the exact original footer slot ───────
        __t.close();
        assert.strictEqual(dd.parentNode, footer,
          'closed dropdown must be restored into .composer-footer');
        assert.strictEqual(dd.nextSibling, trailingSibling,
          'restore must reinsert the dropdown before its original next sibling');
        assert.strictEqual(dd.classList.contains('model-dropdown--floating'), false,
          'close must drop the floating class');
        assert.strictEqual(dd.style.top, '', 'close must clear the fixed inline top');
        assert.strictEqual(dd.style.width, '', 'close must clear the inline width');

        // ── Desktop: never reparents, stays an absolutely positioned footer child ──
        PHONE = false;
        dd.classList.add('open');
        __t.position();
        assert.strictEqual(dd.parentNode, footer,
          'desktop dropdown must stay inside .composer-footer (behaviour identical to master)');
        assert.strictEqual(dd.classList.contains('model-dropdown--floating'), false,
          'desktop dropdown must not use the floating class');
        assert.ok(dd.style.top === '' || dd.style.top === undefined || dd.style.top === null,
          'desktop path must not write a fixed top');

        console.log('OK');
        """
    )
    proc = subprocess.run(
        ["node", "-e", script],
        cwd=REPO_ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, (
        f"node DOM-parentage assertions failed:\n{proc.stdout}\n{proc.stderr}"
    )
    assert "OK" in proc.stdout
