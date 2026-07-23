import json
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import re
import pytest

REPO = pathlib.Path(__file__).parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")
UI_PATH = REPO / "static" / "ui.js"
PANELS_PATH = REPO / "static" / "panels.js"
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_DASHBOARD_LINK_DRIVER = textwrap.dedent(
    """\
    const fs = require('fs');

    function extractFn(src, name) {
      const markers = [`async function ${name}(`, `function ${name}(`];
      let start = -1;
      for (const marker of markers) {
        start = src.indexOf(marker);
        if (start >= 0) break;
      }
      if (start < 0) throw new Error(`${name}() not found`);
      let i = src.indexOf('{', start);
      let depth = 0;
      let inString = null;
      let escaped = false;
      let inLineComment = false;
      let inBlockComment = false;
      for (; i < src.length; i++) {
        const ch = src[i];
        const nxt = src[i + 1] || '';
        if (inLineComment) {
          if (ch === '\\n') inLineComment = false;
          continue;
        }
        if (inBlockComment) {
          if (ch === '*' && nxt === '/') inBlockComment = false;
          continue;
        }
        if (inString) {
          if (escaped) {
            escaped = false;
          } else if (ch === '\\\\') {
            escaped = true;
          } else if (ch === inString) {
            inString = null;
          }
          continue;
        }
        if (ch === '/' && nxt === '/') { inLineComment = true; continue; }
        if (ch === '/' && nxt === '*') { inBlockComment = true; continue; }
        if (ch === '\\'' || ch === '\"' || ch === '`') { inString = ch; continue; }
        if (ch === '{') depth += 1;
        if (ch === '}') {
          depth -= 1;
          if (depth === 0) return src.slice(start, i + 1);
        }
      }
      throw new Error(`could not extract ${name}`);
    }

    function makeEl() {
      return {
        children: [],
        style: {},
        classList: {
          _set: new Set(),
          add(c){this._set.add(c);},
          remove(c){this._set.delete(c);},
          toggle(c, on){const want = on === undefined ? !this._set.has(c) : Boolean(on); if (want) this._set.add(c); else this._set.delete(c);},
          contains(c){return this._set.has(c);},
        },
        dataset: {},
        _attrs: {},
        textContent: '',
        datasetUrl: '',
        setAttribute(name, value) {
          this._attrs[name] = String(value);
          if (name === 'data-dashboard-url') this.datasetUrl = String(value);
        },
        getAttribute(name) { return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null; },
        hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this._attrs, name); },
        appendChild(child) { this.children.push(child); return child; },
      };
    }

    function makeButton(id) {
      const btn = makeEl();
      btn.id = id;
      btn._classes = btn.classList._set;
      if (id === 'dashboardRailBtn' || id === 'dashboardMobileBtn') {
        btn.setAttribute('data-dashboard-link', '');
        btn.setAttribute('aria-label', 'Dashboard');
      }
      return btn;
    }

    const action = process.argv[2] || 'load';
    const mode = process.argv[3] || 'auto';
    const url = process.argv[4] || '';
    const uiSrc = fs.readFileSync(process.argv[5], 'utf8');
    const panelsSrc = fs.readFileSync(process.argv[6], 'utf8');

    const modeEl = makeButton('settingsDashboardMode');
    const urlEl = makeButton('settingsDashboardUrl');
    const statusEl = makeButton('settingsDashboardStatus');
    modeEl.value = mode;
    urlEl.value = url;
    const railBtn = makeButton('dashboardRailBtn');
    const mobileBtn = makeButton('dashboardMobileBtn');
    const buttons = [railBtn, mobileBtn];
    const result = { calls: [], renderCalls: 0, statusCalls: 0, buttonStates: [] };
    let delayedConfigResolve = null;
    const delayedConfigValue = { enabled: 'never', url: 'http://stale.local:1234' };
    let delayedConfigUsed = false;

    global._dashboardLastNonNeverMode = 'auto';
    global._dashboardStatusCache = null;
    global._dashboardStatusFetchedAt = 0;
    global._dashboardSettingsLoadSeq = 0;
    global._dashboardSettingsWriteSeq = 0;
    global.window = { location: { hostname: '127.0.0.1' } };
    global.document = {
      createElement: () => makeEl(),
      querySelectorAll: (sel) => {
        if (sel === '[data-dashboard-link]') return buttons;
        return [];
      },
      querySelector: () => null,
      addEventListener: () => {},
    };
    global.$ = (id) => {
      if (id === 'settingsDashboardMode') return modeEl;
      if (id === 'settingsDashboardUrl') return urlEl;
      if (id === 'settingsDashboardStatus') return statusEl;
      return null;
    };

    global.t = (key) => {
      if (key === 'tab_dashboard') return 'Dashboard';
      if (key === 'dashboard_loopback_warning') return 'Loopback';
      return key;
    };
    global._dashboardIsBrowserLoopback = () => false;

    global.api = (url, opts = {}) => {
      result.calls.push({ url: String(url), method: (opts.method || 'GET').toUpperCase(), body: opts.body || '', timeoutToast: !!(opts.timeoutToast) });
      if (String(url) === '/api/dashboard/config') {
        if (
          (opts.method || 'GET').toUpperCase() === 'GET' &&
          (action === 'stale-load' || action === 'failed-save-stale-load') &&
          !delayedConfigUsed
        ) {
          delayedConfigUsed = true;
          return new Promise((resolve) => {
            delayedConfigResolve = () => resolve(delayedConfigValue);
          });
        }
        if ((opts.method || 'GET').toUpperCase() === 'GET' && action === 'failed-save-stale-load') {
          return Promise.resolve(delayedConfigValue);
        }
        const payload = opts.body ? JSON.parse(opts.body) : {};
        if ((opts.method || 'GET').toUpperCase() === 'POST' && action === 'failed-save-stale-load') {
          return Promise.reject(new Error('save failed'));
        }
        const enabled = payload.enabled || modeEl.value || 'auto';
        const configuredUrl = payload.url || urlEl.value || '';
        return Promise.resolve({ enabled, url: configuredUrl });
      }
      if (String(url) === '/api/dashboard/status') {
        result.statusCalls += 1;
        return Promise.resolve({
          running: modeEl.value !== 'never',
          browser_url: modeEl.value === 'never' ? '' : 'http://127.0.0.1:1234',
        });
      }
      return Promise.resolve({ running: false });
    };
    global._renderTabVisibilityChips = () => {
      result.renderCalls += 1;
      result.lastRenderMode = modeEl.value;
    };

    for (const name of ['_normalizeDashboardEnabledMode','_setDashboardModeForChip','_getDashboardChipRestoreMode']) {
      eval(extractFn(uiSrc, name));
    }
    for (const name of ['_dashboardBrowserUrl', '_applyDashboardStatus', 'refreshDashboardStatus', 'loadDashboardSettings', 'saveDashboardSettings']) {
      let src = extractFn(uiSrc, name);
      if(name === 'saveDashboardSettings'){
        src = src.replace(
          "if(typeof _renderTabVisibilityChips==='function') _renderTabVisibilityChips();",
          "if(typeof globalThis._renderTabVisibilityChips==='function') globalThis._renderTabVisibilityChips();"
        );
      }
      eval(src);
    }
    for (const name of ['_dashboardPanelMode', '_toggleDashboardVisibilityChip']) {
      eval(extractFn(panelsSrc, name));
    }

    function recordButtons() {
      result.buttonStates = buttons.map((btn) => ({
        id: btn.id,
        classes: Array.from(btn.classList._set || []),
        display: btn.style.display || '',
        dashboardUrl: btn._attrs['data-dashboard-url'] || '',
        tooltip: btn._attrs['data-tooltip'] || '',
      }));
    }

    (async () => {
      if (action === 'load') {
        await loadDashboardSettings();
        recordButtons();
        console.log(JSON.stringify({
          mode: modeEl.value,
          url: urlEl.value,
          renderCalls: result.renderCalls,
          lastRenderMode: result.lastRenderMode || '',
          calls: result.calls,
          buttonStates: result.buttonStates,
        }));
        return;
      }

      if (action === 'failed-save-stale-load') {
        const loadPromise = loadDashboardSettings();
        modeEl.value = 'always';
        urlEl.value = 'http://fresh.local:4321';
        await saveDashboardSettings();
        if (!delayedConfigResolve) throw new Error('delayed config read was not started');
        delayedConfigResolve();
        await loadPromise;
        await Promise.resolve();
        await new Promise((resolve) => setTimeout(resolve, 0));
        recordButtons();
        console.log(JSON.stringify({
          mode: modeEl.value,
          url: urlEl.value,
          renderCalls: result.renderCalls,
          lastRenderMode: result.lastRenderMode || '',
          calls: result.calls,
          statusCalls: result.statusCalls,
          buttonStates: result.buttonStates,
        }));
        return;
      }

      if (action === 'stale-load') {
        const loadPromise = loadDashboardSettings();
        modeEl.value = 'always';
        urlEl.value = 'http://fresh.local:4321';
        await saveDashboardSettings();
        if (!delayedConfigResolve) throw new Error('delayed config read was not started');
        delayedConfigResolve();
        await loadPromise;
        await Promise.resolve();
        await new Promise((resolve) => setTimeout(resolve, 0));
        recordButtons();
        console.log(JSON.stringify({
          mode: modeEl.value,
          url: urlEl.value,
          renderCalls: result.renderCalls,
          lastRenderMode: result.lastRenderMode || '',
          calls: result.calls,
          statusCalls: result.statusCalls,
          buttonStates: result.buttonStates,
        }));
        return;
      }

      if (action === 'chip-toggle') {
        _toggleDashboardVisibilityChip();
      } else {
        await saveDashboardSettings();
      }
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
      recordButtons();
      console.log(JSON.stringify({
        mode: modeEl.value,
        url: urlEl.value,
        renderCalls: result.renderCalls,
        lastRenderMode: result.lastRenderMode || '',
        calls: result.calls,
        statusCalls: result.statusCalls,
        buttonStates: result.buttonStates,
      }));
    })().catch((err) => { console.error(err); process.exit(1); });
    """
)


def _run_dashboard_link_driver(action: str, mode: str = 'auto', url: str = '') -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(_DASHBOARD_LINK_DRIVER)
        driver = f.name
    try:
        result = subprocess.run(
            [
                NODE,
                driver,
                action,
                mode,
                url,
                str(UI_PATH),
                str(PANELS_PATH),
            ],
            cwd=REPO,
            text=True,
            capture_output=True,
            timeout=2.0,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"node harness failed: {result.stderr or result.stdout}")
        return json.loads(result.stdout.strip())
    finally:
        try:
            pathlib.Path(driver).unlink()
        except OSError:
            pass


def test_dashboard_nav_buttons_are_hidden_by_default_and_subpath_safe():
    assert 'id="dashboardRailBtn"' in INDEX_HTML
    assert 'id="dashboardMobileBtn"' in INDEX_HTML
    assert 'data-dashboard-link' in INDEX_HTML
    assert 'data-i18n-title="tab_dashboard"' in INDEX_HTML
    assert 'display:none' in INDEX_HTML
    assert "Dashboard" in INDEX_HTML
    assert "href=\"/" not in INDEX_HTML


def test_dashboard_rail_item_sits_between_insights_and_settings_spacer():
    rail = re.search(r'<nav class="rail".*?</nav>', INDEX_HTML, re.DOTALL).group(0)
    assert rail.index('data-panel="insights"') < rail.index('id="dashboardRailBtn"') < rail.index('rail-spacer')


def test_dashboard_frontend_fetches_status_with_sixty_second_cache():
    assert "DASHBOARD_STATUS_TTL_MS=60000" in UI_JS
    assert "function refreshDashboardStatus" in UI_JS
    assert "api('/api/dashboard/status',{timeoutToast:false})" in UI_JS
    assert "setInterval(refreshDashboardStatus,DASHBOARD_STATUS_TTL_MS)" in UI_JS
    assert 'fetch("/api/dashboard/status"' not in UI_JS
    assert "fetch('/api/dashboard/status'" not in UI_JS


def test_dashboard_probe_initializes_after_shared_api_helper_is_loaded():
    assert "function _initDashboardLinkProbe" in UI_JS
    assert "document.addEventListener('DOMContentLoaded',_initDashboardLinkProbe,{once:true})" in UI_JS
    assert "else _initDashboardLinkProbe();" not in UI_JS


def test_dashboard_frontend_opens_external_tab_safely_and_derives_browser_host_url():
    assert "function openHermesDashboard" in UI_JS
    assert "window.open" in UI_JS
    assert "noopener,noreferrer" in UI_JS
    assert "window.location.hostname" in UI_JS
    assert "_dashboardBrowserUrl" in UI_JS
    assert 'id="dashboardRailBtn"' in INDEX_HTML
    assert re.search(r'id="dashboardRailBtn"[^>]*onclick="openHermesDashboard\(event\)"', INDEX_HTML)


def test_dashboard_loopback_warning_and_external_badge_are_present():
    assert "dashboard_loopback_warning" in UI_JS
    assert "dashboard-external-badge" in INDEX_HTML
    assert ".dashboard-external-badge" in STYLE_CSS
    assert "dashboard-link-visible" in STYLE_CSS


def test_dashboard_settings_controls_live_in_system_panel():
    assert 'id="settingsDashboardMode"' in INDEX_HTML
    assert 'id="settingsDashboardUrl"' in INDEX_HTML
    assert "function saveDashboardSettings" in UI_JS
    assert "api('/api/dashboard/config'" in UI_JS


def test_dashboard_frontend_uses_browser_url_without_requiring_probe_port():
    match = re.search(r"function _dashboardBrowserUrl\(status\).*?\n}\nfunction _syncNavActionMirrors", UI_JS, re.DOTALL)
    assert match is not None
    helper = match.group(0)
    assert "status.browser_url||status.url" in helper
    assert "!status.port" in helper
    assert helper.index("status.browser_url||status.url") < helper.index("!status.port")


def test_mobile_dashboard_link_uses_shared_visible_action_class():
    match = re.search(
        r"@media\(max-width:640px\)\{([\s\S]*?)\n\s*}\s*\n\s*@media \(hover:none\)",
        STYLE_CSS,
        re.DOTALL,
    )
    assert match is not None
    mobile_css = match.group(1)
    assert re.search(r"\.dashboard-link-visible,\s*\.nav-action-visible\{display:flex!important;\}", STYLE_CSS)
    assert ".sidebar-nav .dashboard-link:not(.nav-action-visible)" in mobile_css
    assert ".sidebar-nav [data-nav-action-mirror]:not(.nav-action-visible){display:none!important;}" in mobile_css
    assert ".sidebar-nav .dashboard-link.dashboard-link-visible{display:none!important;}" not in mobile_css
    assert ".sidebar-nav .dashboard-link:not(.dashboard-link-visible){display:none!important;}" not in mobile_css


@requires_node
def test_extension_rail_actions_are_mirrored_to_mobile_nav():
    driver = textwrap.dedent(
        """\
        const fs = require('fs');
        const src = fs.readFileSync(process.argv[2], 'utf8');

        function extractFn(name) {
          const start = src.indexOf(`function ${name}(`);
          if (start < 0) throw new Error(`${name}() not found`);
          let i = src.indexOf('{', start);
          let depth = 0;
          for (; i < src.length; i++) {
            if (src[i] === '{') depth += 1;
            if (src[i] === '}') {
              depth -= 1;
              if (depth === 0) return src.slice(start, i + 1);
            }
          }
          throw new Error(`could not extract ${name}`);
        }

        class El {
          constructor(tag) {
            this.tagName = tag;
            this.children = [];
            this._attrs = {};
            this.dataset = {};
            this.innerHTML = '';
            this.id = '';
            this.onclick = null;
            this.parentNode = null;
            this._handlers = {};
            this.classList = {
              _set: new Set(),
              add: (...classes) => classes.forEach(c => this.classList._set.add(c)),
              remove: (...classes) => classes.forEach(c => this.classList._set.delete(c)),
              contains: c => this.classList._set.has(c),
              toggle: (cls, force) => {
                if (force === undefined) {
                  if (this.classList._set.has(cls)) {
                    this.classList._set.delete(cls);
                    return false;
                  }
                  this.classList._set.add(cls);
                  return true;
                }
                if (force) this.classList._set.add(cls);
                else this.classList._set.delete(cls);
                return !!force;
              },
            };
          }
          appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
          insertBefore(child, anchor) {
            const idx = anchor ? this.children.indexOf(anchor) : -1;
            child.parentNode = this;
            if (idx >= 0) this.children.splice(idx, 0, child);
            else this.children.push(child);
            return child;
          }
          remove() {
            if (!this.parentNode) return;
            const idx = this.parentNode.children.indexOf(this);
            if (idx >= 0) this.parentNode.children.splice(idx, 1);
            this.parentNode = null;
          }
          setAttribute(name, value) {
            this._attrs[name] = String(value);
            if (name.startsWith('data-')) this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(value);
          }
          removeAttribute(name) {
            delete this._attrs[name];
            if (name.startsWith('data-')) delete this.dataset[name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())];
          }
          get attributes() { return Object.entries(this._attrs).map(([name, value]) => ({ name, value })); }
          getAttribute(name) { return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null; }
          hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this._attrs, name); }
          addEventListener(name, fn) { this._handlers[name] = fn; }
          click() { if (this._handlers.click) this._handlers.click({ preventDefault() {} }); }
          cloneNode() {
            const clone = new El(this.tagName);
            clone.id = this.id;
            clone.innerHTML = this.innerHTML;
            clone.onclick = this.onclick;
            this.classList._set.forEach(c => clone.classList.add(c));
            Object.entries(this._attrs).forEach(([k, v]) => clone.setAttribute(k, v));
            return clone;
          }
          querySelectorAll(sel) {
            if (sel === '.nav-tab:not([data-panel]):not([data-dashboard-link])') {
              return this.children.filter(el => el.classList.contains('nav-tab') && !el.hasAttribute('data-panel') && !el.hasAttribute('data-dashboard-link'));
            }
            if (sel === '[data-nav-action-mirror]') {
              return this.children.filter(el => el.hasAttribute('data-nav-action-mirror'));
            }
            return [];
          }
          querySelector(sel) {
            if (sel.startsWith('[data-nav-action-mirror="')) {
              const id = sel.slice(25, -2);
              return this.children.find(el => el.getAttribute('data-nav-action-mirror') === id) || null;
            }
            if (sel === '.dashboard-link,[data-dashboard-link]') return this.children.find(el => el.classList.contains('dashboard-link') || el.hasAttribute('data-dashboard-link')) || null;
            if (sel === '[data-panel="logs"]') return this.children.find(el => el.getAttribute('data-panel') === 'logs') || null;
            return null;
          }
        }

        const rail = new El('nav');
        const sidebar = new El('div');
        const source = new El('button');
        const dashboard = new El('button');
        let clicked = 0;
        let sidebarClosed = 0;
        source.id = 'hwxThemeCreatorRailBtn';
        source.classList.add('rail-btn', 'nav-tab', 'has-tooltip');
        source.setAttribute('data-tooltip', 'Theme Creator');
        source.setAttribute('onclick', 'clicked += 100;');
        source.onclick = () => { clicked += 100; };
        source.innerHTML = '<svg></svg>';
        source.click = () => { clicked += 1; };
        dashboard.classList.add('dashboard-link');
        sidebar.appendChild(dashboard);
        let observerCallback = null;
        let observedRail = null;
        let observedOptions = null;

        global.document = {
          readyState: 'complete',
          querySelector(sel) {
            if (sel === '.rail') return rail;
            if (sel === '.sidebar-nav') return sidebar;
            return null;
          },
        };
        global.closeMobileSidebar = () => { sidebarClosed += 1; };
        global.window = {
          getComputedStyle(el) {
            return {
              display: el.hidden || (el.style && el.style.display === 'none') ? 'none' : '',
              visibility: (el.style && el.style.visibility) || '',
            };
          },
        };
        global.MutationObserver = window.MutationObserver = class {
          constructor(callback) { observerCallback = callback; }
          observe(target, options) { observedRail = target; observedOptions = options; }
        };

        const helpers = Function(extractFn('_stripInlineEventHandlers') + '\\n' + extractFn('_syncNavActionMirrors') + '\\n' + extractFn('_initNavActionMirrors') + '; return { _initNavActionMirrors };')();
        helpers._initNavActionMirrors();
        rail.appendChild(source);
        observerCallback();
        const mirror = sidebar.children.find(el => el.getAttribute('data-nav-action-mirror') === 'hwxThemeCreatorRailBtn');
        source.hidden = true;
        observerCallback();
        const mirrorHiddenWhenSourceHidden = !mirror.classList.contains('nav-action-visible');
        source.hidden = false;
        observerCallback();
        mirror.click();
        const mirrorBeforeDashboard = sidebar.children[0] === mirror;
        source.remove();
        observerCallback();
        console.log(JSON.stringify({
          observerArmed: observedRail === rail,
          observerAttributes: !!(observedOptions && observedOptions.attributes),
          observerSubtree: !!(observedOptions && observedOptions.subtree),
          sourceVisible: source.classList.contains('nav-action-visible'),
          mirrorVisible: mirror.classList.contains('nav-action-visible'),
          mirrorHiddenWhenSourceHidden,
          mirrorRailClass: mirror.classList.contains('rail-btn'),
          mirrorLabel: mirror.getAttribute('data-label'),
          mirrorOnclickAttribute: mirror.getAttribute('onclick'),
          mirrorOnclickProperty: mirror.onclick === null,
          mirrorBeforeDashboard,
          mirrorRemoved: !sidebar.children.some(el => el.getAttribute('data-nav-action-mirror') === 'hwxThemeCreatorRailBtn'),
          clicked,
          sidebarClosed,
        }));
        """
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(driver)
        path = handle.name
    try:
        result = subprocess.run([NODE, path, str(UI_PATH)], text=True, capture_output=True, timeout=15, check=True)
        out = json.loads(result.stdout)
    finally:
        pathlib.Path(path).unlink(missing_ok=True)
    assert out == {
        "observerArmed": True,
        "observerAttributes": True,
        "observerSubtree": True,
        "sourceVisible": False,
        "mirrorVisible": True,
        "mirrorHiddenWhenSourceHidden": True,
        "mirrorRailClass": False,
        "mirrorLabel": "Theme Creator",
        "mirrorOnclickAttribute": None,
        "mirrorOnclickProperty": True,
        "mirrorBeforeDashboard": True,
        "mirrorRemoved": True,
        "clicked": 1,
        "sidebarClosed": 1,
    }


def test_desktop_dashboard_link_button_stays_in_desktop_nav():
    rail_nav = re.search(r'<nav class="rail".*?</nav>', INDEX_HTML, re.DOTALL).group(0)
    assert 'id="dashboardRailBtn"' in rail_nav
    mobile_nav_match = re.search(
        r'<div class="sidebar-nav">([\s\S]*?)</div>\s*(?=<!--)',
        INDEX_HTML,
    )
    assert mobile_nav_match is not None
    mobile_nav = mobile_nav_match.group(0)
    assert 'id="dashboardMobileBtn"' in mobile_nav

@requires_node
def test_dashboard_dropdown_save_resyncs_chip_state():
    out = _run_dashboard_link_driver("save", mode="never", url="http://example.local:1234")
    assert out["mode"] == "never"
    assert out["url"] == "http://example.local:1234"
    assert out["lastRenderMode"] == "never"
    assert out["renderCalls"] == 1
    assert any(call["url"] == "/api/dashboard/config" and call["method"] == "POST" for call in out["calls"])
    assert any(call["url"] == "/api/dashboard/status" for call in out["calls"])
    assert out["statusCalls"] >= 1
    assert all(
        "dashboard-link-visible" not in state["classes"]
        and "nav-action-visible" not in state["classes"]
        and state["display"] == "none"
        for state in out["buttonStates"]
    )


@requires_node
def test_dashboard_chip_save_keeps_buttons_refreshed():
    out = _run_dashboard_link_driver("chip-toggle", mode="never", url="http://example.local:1234")
    assert out["mode"] == "auto"
    assert out["url"] == "http://example.local:1234"
    assert out["renderCalls"] == 1
    assert out["lastRenderMode"] == "auto"
    assert any(call["url"] == "/api/dashboard/config" and call["method"] == "POST" for call in out["calls"])
    assert any(call["url"] == "/api/dashboard/status" for call in out["calls"])
    assert out["statusCalls"] >= 1
    assert all(
        "dashboard-link-visible" in state["classes"]
        and "nav-action-visible" in state["classes"]
        and state["display"] != "none"
        for state in out["buttonStates"]
    )


@requires_node
def test_stale_dashboard_load_does_not_overwrite_newer_save():
    out = _run_dashboard_link_driver("stale-load", mode="never", url="http://stale.local:1234")
    assert out["mode"] == "always"
    assert out["url"] == "http://fresh.local:4321"
    assert out["renderCalls"] == 1
    assert out["lastRenderMode"] == "always"
    assert [
        (call["url"], call["method"])
        for call in out["calls"]
    ] == [
        ("/api/dashboard/config", "GET"),
        ("/api/dashboard/config", "POST"),
        ("/api/dashboard/status", "GET"),
    ]
    assert out["statusCalls"] == 1
    assert all(
        "dashboard-link-visible" in state["classes"]
        and "nav-action-visible" in state["classes"]
        and state["display"] != "none"
        for state in out["buttonStates"]
    )


@requires_node
def test_failed_dashboard_save_reloads_backend_after_stale_load_is_dropped():
    out = _run_dashboard_link_driver("failed-save-stale-load", mode="never", url="http://stale.local:1234")
    assert out["mode"] == "never"
    assert out["url"] == "http://stale.local:1234"
    assert out["renderCalls"] == 1
    assert out["lastRenderMode"] == "never"
    assert [
        (call["url"], call["method"])
        for call in out["calls"]
    ] == [
        ("/api/dashboard/config", "GET"),
        ("/api/dashboard/config", "POST"),
        ("/api/dashboard/config", "GET"),
    ]
    assert out["statusCalls"] == 0
