"""
Keyboard accessibility guards for the profile dropdown menu.

User-visible failure: the composer/titlebar profile dropdown was a plain-div
menu with no keyboard support — opening it (click or Enter) left focus on the
trigger button, ArrowUp/ArrowDown did nothing, and there was no way to select a
profile without a mouse. This pins the listbox contract (roles, tabindex,
aria-selected), the arrow/Home/End/Enter/Escape key handler, focus-on-open,
focus-restore-on-close, and the two focus-lifecycle guarantees demanded by
review: the handler only acts while focus is inside the menu, and a background
refresh preserves an in-progress keyboard selection.

All guards are behavioral: they execute the real dropdown module from
static/panels.js in node under a minimal DOM shim and assert on observable
state (focused element, aria attributes, open/close, switch target), never on
source text.
"""
import json
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _dropdown_module_snippets():
    return [
        PANELS_JS[
            PANELS_JS.index("let _profilesCache = null;")
            : PANELS_JS.index("function _openProfileSwitchSessionBrowser(){")
        ],
    ]


def test_keyboard_can_open_navigate_select_escape_and_is_focus_scoped():
    snippets = _dropdown_module_snippets()
    script = textwrap.dedent(
        f"""
        const assert = require('assert');
        const snippets = {json.dumps(snippets)};

        class ClassList {{
          constructor() {{ this.values = new Set(); }}
          add(name) {{ this.values.add(name); }}
          remove(name) {{ this.values.delete(name); }}
          contains(name) {{ return this.values.has(name); }}
        }}
        class Element {{
          constructor(tag, id) {{
            this.tagName = tag;
            this.id = id || '';
            this.children = [];
            this.className = '';
            this.classList = new ClassList();
            this.dataset = {{}};
            this.style = {{}};
            this.onclick = null;
            this.textContent = '';
            this.isConnected = true;
            this._innerHTML = '';
            this._attrs = {{}};
            this._keydowns = [];
          }}
          set innerHTML(value) {{
            this._innerHTML = String(value || '');
            if (this._innerHTML === '') {{
              // Mirror the DOM: clearing innerHTML detaches the old children.
              this.children.forEach((c) => {{ c.isConnected = false; }});
              this.children = [];
            }}
          }}
          get innerHTML() {{ return this._innerHTML; }}
          appendChild(child) {{ this.children.push(child); return child; }}
          setAttribute(key, value) {{ this._attrs[key] = String(value); }}
          getAttribute(key) {{ return this._attrs[key] !== undefined ? this._attrs[key] : null; }}
          addEventListener(type, fn) {{ if (type === 'keydown') this._keydowns.push(fn); }}
          querySelector(selector) {{ return this._qs && this._qs[selector] ? this._qs[selector] : null; }}
          querySelectorAll(selector) {{
            if (selector === '.profile-opt') return this.children.filter((child) => String(child.className).split(/\\s+/).includes('profile-opt'));
            return [];
          }}
          contains(child) {{ return child === this || !!this.children.find((c) => typeof c.contains === 'function' && c.contains(child)); }}
          focus() {{ document.activeElement = this; }}
          click() {{ return typeof this.onclick === 'function' ? this.onclick() : undefined; }}
        }}
        const elements = new Map();
        for (const id of ['profileDropdown', 'profileChip', 'titlebarProfileBtn', 'titlebarProfileLabel', 'msg', 'panelProfiles', 'panelProfilesHead']) {{
          elements.set(id, new Element('div', id));
        }}
        // Mirror static/index.html: the triggers declare a menu popup.
        elements.get('profileChip').setAttribute('aria-haspopup', 'menu');
        elements.get('titlebarProfileBtn').setAttribute('aria-haspopup', 'menu');
        // Profiles panel with its heading, for the Manage-activation case.
        elements.get('panelProfiles').appendChild(elements.get('panelProfilesHead'));
        elements.get('panelProfiles')._qs = {{ '.panel-head': elements.get('panelProfilesHead') }};
        globalThis.document = {{
          hidden: false,
          activeElement: null,
          createElement: (tag) => new Element(tag),
          getElementById: (id) => elements.get(id) || null,
          addEventListener: (type, fn) => {{ if (type !== 'keydown') return; (document._keydowns || (document._keydowns = [])).push(fn); }},
          _keydowns: [],
        }};
        globalThis.window = {{ addEventListener: () => {{}} }};
        const store = new Map();
        globalThis.localStorage = {{
          getItem: (key) => store.has(key) ? store.get(key) : null,
          setItem: (key, value) => store.set(key, String(value)),
          removeItem: (key) => store.delete(key),
        }};
        globalThis.$ = (id) => elements.get(id) || null;
        globalThis.S = {{ activeProfile: 'default' }};
        globalThis.t = (key, n) => key === 'profile_skill_count' ? `${{n}} skills` : key;
        globalThis.esc = (value) => String(value == null ? '' : value)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
          .replace(/\"/g, '&quot;').replace(/'/g, '&#39;');
        globalThis.li = () => '';
        globalThis.closeWsDropdown = () => {{}};
        globalThis.closeModelDropdown = () => {{}};
        globalThis._positionProfileDropdown = () => {{}};
        globalThis.showToast = () => {{}};
        globalThis.mobileSwitchPanel = () => {{}};
        let switchedTo = null;
        globalThis.switchToProfile = async (name) => {{ switchedTo = name; S.activeProfile = name; }};
        const multiProfileResponse = {{
          active: 'default',
          single_profile_mode: false,
          profiles: [
            {{ name: 'default', visible: true, is_default: true }},
            {{ name: 'alpha', visible: true }},
            {{ name: 'beta', visible: true }},
          ],
        }};
        globalThis.api = () => Promise.resolve(multiProfileResponse);

        // Export a test API from INSIDE the eval: the snippet's `let` bindings
        // (e.g. _profilesCache) are scoped to the eval, so the runner must reach
        // them through a closure created in the same eval, not through globals.
        eval(snippets.join(String.fromCharCode(10)) + String.fromCharCode(10) + `;globalThis.__kbTest={{
          reset() {{
            _profilesCache = null;
            _profileDropdownFetchPromise = null;
            _profileDropdownCacheLoadedFromStorage = false;
            _profileDropdownOpenGeneration = 0;
            _profileDropdownFocusedName = null;
            switchedTo = null;
            S.activeProfile = 'default';
            document.activeElement = null;
            const dd = document.getElementById('profileDropdown');
            dd.children = [];
            dd.innerHTML = '';
            dd.classList.remove('open');
          }},
          seedCache(data) {{ _profilesCache = data; }},
          toggle(triggerId) {{ toggleProfileDropdown({{ currentTarget: document.getElementById(triggerId) }}); }},
          render(data) {{ renderProfileDropdown(data); }},
          isOpen() {{ return document.getElementById('profileDropdown').classList.contains('open'); }},
          options() {{ return document.getElementById('profileDropdown').querySelectorAll('.profile-opt'); }},
          ddRole() {{ return document.getElementById('profileDropdown').getAttribute('role'); }},
          active() {{ return document.activeElement; }},
          chipExpanded() {{ return document.getElementById('profileChip').getAttribute('aria-expanded'); }},
          focusOn(id) {{ document.activeElement = document.getElementById(id); }},
          dispatchKey(key) {{
            const ev = {{ key, preventDefault() {{ this._pd = true; }}, stopPropagation() {{}} }};
            document._keydowns.forEach((fn) => fn(ev));
            return ev;
          }},
          triggerKeydown(triggerId, key) {{
            document.getElementById(triggerId)._keydowns.forEach((fn) => fn({{ key, preventDefault() {{}} }}));
          }},
          switched() {{ return switchedTo; }},
        }};`);
        if (document._keydowns.length !== 1) throw new Error('keydown handler must be registered exactly once');

        async function runOpenNavigationEnter() {{
          __kbTest.reset();
          __kbTest.seedCache(multiProfileResponse);
          __kbTest.toggle('profileChip');
          assert.strictEqual(__kbTest.isOpen(), true, 'dropdown should open');
          assert.strictEqual(__kbTest.ddRole(), 'menu', 'popup must use the menu contract (rows + Manage command)');
          const items = __kbTest.options();
          assert.strictEqual(items.length, 4, 'three profiles + manage row should be rendered');
          // Menu contract, asserted on the live DOM: every row is a menuitem,
          // the active profile is marked with aria-current, and nothing uses
          // listbox option semantics.
          for (const o of items) {{
            assert.strictEqual(o.getAttribute('role'), 'menuitem');
            assert.strictEqual(o.getAttribute('tabindex'), '-1');
            assert.strictEqual(o.getAttribute('aria-selected'), null, 'no listbox option semantics on menu rows');
          }}
          assert.strictEqual(items[0].getAttribute('aria-current'), 'true', 'active profile marked aria-current');
          assert.strictEqual(items[1].getAttribute('aria-current'), null);
          assert.strictEqual(items[0].getAttribute('data-profile'), 'default');
          assert.strictEqual(document.getElementById('profileChip').getAttribute('aria-haspopup'), 'menu');
          assert.strictEqual(__kbTest.active(), items[0], 'focus should land on the active profile option on open');
          assert.strictEqual(__kbTest.chipExpanded(), 'true');

          __kbTest.dispatchKey('ArrowDown');
          assert.strictEqual(__kbTest.active(), items[1], 'ArrowDown moves to the next profile');
          __kbTest.dispatchKey('ArrowDown');
          assert.strictEqual(__kbTest.active(), items[2], 'ArrowDown moves again');
          __kbTest.dispatchKey('ArrowDown');
          assert.strictEqual(__kbTest.active(), items[3], 'ArrowDown wraps to manage option');
          __kbTest.dispatchKey('End');
          assert.strictEqual(__kbTest.active(), items[3], 'End should go to the last option');
          __kbTest.dispatchKey('Home');
          assert.strictEqual(__kbTest.active(), items[0], 'Home should go to the first option');
          __kbTest.dispatchKey('ArrowUp');
          assert.strictEqual(__kbTest.active(), items[3], 'ArrowUp wraps backwards');
          __kbTest.dispatchKey('ArrowUp');
          __kbTest.dispatchKey('ArrowUp');
          assert.strictEqual(__kbTest.active(), items[1], 'ArrowUp lands on alpha');

          // Enter on the 'alpha' profile selects it, closes the menu, and restores focus.
          __kbTest.dispatchKey('Enter');
          await new Promise((resolve) => setImmediate(resolve));
          assert.strictEqual(__kbTest.switched(), 'alpha', 'Enter on an option must switch profiles');
          assert.strictEqual(__kbTest.isOpen(), false, 'selecting must close the dropdown');
          assert.strictEqual(__kbTest.active(), document.getElementById('profileChip'), 'focus must return to the trigger after selection');
          assert.strictEqual(__kbTest.chipExpanded(), 'false');
        }}

        async function runEscapeClosesAndRestores() {{
          __kbTest.reset();
          __kbTest.seedCache(multiProfileResponse);
          __kbTest.toggle('profileChip');
          assert.strictEqual(__kbTest.isOpen(), true);
          assert.strictEqual(__kbTest.active(), __kbTest.options()[0]);
          __kbTest.dispatchKey('Escape');
          assert.strictEqual(__kbTest.isOpen(), false, 'Escape must close the dropdown');
          assert.strictEqual(__kbTest.active(), document.getElementById('profileChip'), 'Escape must restore focus to the trigger');
          assert.strictEqual(__kbTest.switched(), null, 'Escape must not switch profiles');
        }}

        async function runArrowDownOnTriggerOpens() {{
          __kbTest.reset();
          __kbTest.seedCache(multiProfileResponse);
          // Menu closed: ArrowDown on the composer chip should open the menu.
          assert.strictEqual(__kbTest.isOpen(), false);
          __kbTest.triggerKeydown('profileChip', 'ArrowDown');
          assert.strictEqual(__kbTest.isOpen(), true, 'ArrowDown on the trigger chip should open the menu');
          assert.strictEqual(__kbTest.active(), __kbTest.options()[0], 'opening via arrow should focus the first option');
        }}

        async function runHandlerInertWhenFocusLeftMenu() {{
          __kbTest.reset();
          __kbTest.seedCache(multiProfileResponse);
          __kbTest.toggle('profileChip');
          assert.strictEqual(__kbTest.isOpen(), true);
          // Simulate Tab leaving the listbox: focus moves to an unrelated
          // control (the composer textarea).
          __kbTest.focusOn('msg');
          const evDown = __kbTest.dispatchKey('ArrowDown');
          assert.ok(!evDown._pd, 'ArrowDown outside the menu must not be swallowed');
          assert.strictEqual(__kbTest.active(), document.getElementById('msg'), 'focus must not move while outside the menu');
          const evEnter = __kbTest.dispatchKey('Enter');
          assert.ok(!evEnter._pd, 'Enter outside the menu must not be swallowed');
          assert.strictEqual(__kbTest.switched(), null, 'Enter outside the menu must not switch profiles');
          const evEsc = __kbTest.dispatchKey('Escape');
          assert.ok(!evEsc._pd, 'Escape outside the menu must not be swallowed');
          assert.strictEqual(__kbTest.isOpen(), true, 'menu stays open (click-outside still dismisses it); keys pass through to the app');
          // And a background refresh while focus is elsewhere must not yank it
          // back into the menu.
          __kbTest.render({{
            active: 'default',
            single_profile_mode: false,
            profiles: [
              {{ name: 'default', visible: true, is_default: true, model: 'openai/gpt-5.4-mini' }},
              {{ name: 'alpha', visible: true, model: 'google/gemini-2.5-pro' }},
              {{ name: 'beta', visible: true, model: 'anthropic/claude-sonnet-4-6' }},
            ],
          }});
          assert.strictEqual(__kbTest.active(), document.getElementById('msg'), 'refresh must not steal focus from the composer');
        }}

        async function runRefreshPreservesInProgressSelection() {{
          __kbTest.reset();
          __kbTest.seedCache(multiProfileResponse);
          __kbTest.toggle('profileChip');
          __kbTest.dispatchKey('ArrowDown'); // alpha holds focus
          assert.strictEqual(__kbTest.active(), __kbTest.options()[1]);
          // Background /api/profiles refresh re-renders the menu (same names,
          // different model text) — the in-progress selection must survive.
          __kbTest.render({{
            active: 'default',
            single_profile_mode: false,
            profiles: [
              {{ name: 'default', visible: true, is_default: true, model: 'openai/gpt-5.4-mini' }},
              {{ name: 'alpha', visible: true, model: 'google/gemini-2.5-pro' }},
              {{ name: 'beta', visible: true, model: 'anthropic/claude-sonnet-4-6' }},
            ],
          }});
          assert.strictEqual(__kbTest.isOpen(), true, 'refresh keeps the menu open');
          const items = __kbTest.options();
          assert.strictEqual(__kbTest.active(), items[1], 'focus must stay on alpha after the refresh rebuild');
          assert.strictEqual(items[1].getAttribute('data-profile'), 'alpha');
        }}

        async function runManageRowActivation() {{
          __kbTest.reset();
          __kbTest.seedCache(multiProfileResponse);
          __kbTest.toggle('profileChip');
          let switchedPanel = null;
          globalThis.mobileSwitchPanel = (name) => {{ switchedPanel = name; }};
          const items = __kbTest.options();
          __kbTest.dispatchKey('End');
          assert.strictEqual(__kbTest.active(), items[items.length - 1], 'End lands on the Manage row');
          assert.strictEqual(items[items.length - 1].getAttribute('data-profile'), '__manage__');
          __kbTest.dispatchKey('Enter');
          await new Promise((resolve) => setImmediate(resolve));
          assert.strictEqual(switchedPanel, 'profiles', 'Enter on Manage must open the Profiles panel');
          assert.strictEqual(__kbTest.isOpen(), false, 'menu must close');
          assert.strictEqual(__kbTest.active(), document.getElementById('panelProfilesHead'),
            'focus must land on the Profiles panel heading, not the (possibly hidden on mobile) opener');
        }}

        (async () => {{
          await runOpenNavigationEnter();
          await runEscapeClosesAndRestores();
          await runArrowDownOnTriggerOpens();
          await runHandlerInertWhenFocusLeftMenu();
          await runRefreshPreservesInProgressSelection();
          await runManageRowActivation();
        }})().catch((err) => {{ console.error(err && err.stack || err); process.exit(1); }});
        """
    )
    subprocess.run(["node", "-e", script], cwd=REPO_ROOT, check=True, text=True, capture_output=True)