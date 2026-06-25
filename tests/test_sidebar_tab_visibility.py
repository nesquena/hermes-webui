"""Regression tests for sidebar tab visibility feature.

Covers backend validation round-trip, frontend static contracts,
i18n coverage, and the key integration points that have broken before.
"""
import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = (ROOT / "api" / "config.py").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_PATH = ROOT / "static" / "panels.js"
UI_PATH = ROOT / "static" / "ui.js"
BOOT_JS = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")

_PANELS_DASHBOARD_DRIVER = textwrap.dedent(
    """\
    const fs = require('fs');
    const path = require('path');

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
          add(c) { this._set.add(c); },
          remove(c) { this._set.delete(c); },
          toggle(c, on) {
            const want = on === undefined ? !this._set.has(c) : Boolean(on);
            if (want) this._set.add(c); else this._set.delete(c);
          },
          contains(c) { return this._set.has(c); },
        },
        dataset: {},
        _attrs: {},
        textContent: '',
        setAttribute(name, value) {
          this._attrs[name] = String(value);
          if (name === 'data-tab-panel') this.dataset.tabPanel = String(value);
        },
        getAttribute(name) {
          return Object.prototype.hasOwnProperty.call(this._attrs, name)
            ? this._attrs[name]
            : null;
        },
        hasAttribute(name) {
          return Object.prototype.hasOwnProperty.call(this._attrs, name);
        },
        appendChild(child) {
          this.children.push(child);
          return child;
        },
        querySelector: () => null,
        querySelectorAll: () => [],
      };
    }

    const action = process.argv[2] || 'render';
    const mode = process.argv[3] || 'auto';
    const priorMode = process.argv[4] || '';
    const forceNoRestore = process.argv[5] === '1';
    const failSave = process.argv[6] === '1';
    const panelsSrc = fs.readFileSync(process.argv[7], 'utf8');
    const uiSrc = fs.readFileSync(process.argv[8], 'utf8');

    const container = makeEl();
    const modeEl = makeEl();
    modeEl.id = 'settingsDashboardMode';
    modeEl.value = mode;
    const urlEl = makeEl();
    urlEl.id = 'settingsDashboardUrl';
    urlEl.value = '';

    const registry = Object.create(null);
    registry.tabVisibilityChips = container;
    registry.settingsDashboardMode = modeEl;
    registry.settingsDashboardUrl = urlEl;

    let apiCalls = [];
    let renderCalls = 0;
    let hiddenCalls = 0;
    let tabOrderCalls = 0;

    global._dashboardLastNonNeverMode = 'auto';
    global.window = {};
    global.localStorage = {
      _store: Object.create(null),
      getItem(k) {
        return Object.prototype.hasOwnProperty.call(this._store, k) ? this._store[k] : null;
      },
      setItem(k, v) {
        this._store[k] = String(v);
      },
    };
    global.document = {
      createElement: () => makeEl(),
      querySelector: () => null,
      querySelectorAll: () => [],
      addEventListener: () => {},
    };
    global.$ = (id) => registry[id] || null;
    global._orderedSidebarPanels = () => [];
    global._getHiddenTabs = () => [];
    global._wireTabChipDrag = () => {};
    global._tabVisibilityDragSuppressUntil = 0;
    global._ALWAYS_VISIBLE_TABS = new Set(['chat', 'settings']);
    global.t = (key) => key === 'tab_dashboard' ? 'Hermes Dashboard' : String(key);

    let failNextSave = failSave;
    global.api = (url, opts = {}) => {
      apiCalls.push({ url: String(url), method: (opts.method || 'GET').toUpperCase(), body: opts.body || '' });
      if (String(url) === '/api/dashboard/config' && failNextSave) {
        failNextSave = false;
        return Promise.reject(new Error('save failed'));
      }
      const payload = opts.body ? JSON.parse(opts.body) : {};
      return Promise.resolve({ enabled: payload.enabled || 'auto', url: payload.url || '' });
    };
    global.saveDashboardSettings = async function (opts = {}) {
      const payload = { enabled: modeEl.value || 'auto', url: (urlEl.value || '').trim() };
      try {
        const saved = await api('/api/dashboard/config', { method: 'POST', body: JSON.stringify(payload) });
        const normalized = _normalizeDashboardEnabledMode(saved && saved.enabled);
        modeEl.value = normalized;
        _setDashboardModeForChip(normalized);
        return saved;
      } catch (err) {
        if (opts.raiseOnError) throw err;
      }
    };
    global._setHiddenTabs = () => { hiddenCalls += 1; };
    global._setTabOrder = () => { tabOrderCalls += 1; };
    global._renderTabVisibilityChips = () => { renderCalls += 1; };

    for (const name of ['_normalizeDashboardEnabledMode', '_setDashboardModeForChip', '_getDashboardChipRestoreMode']) {
      eval(extractFn(uiSrc, name));
    }
    for (const name of [
      '_dashboardPanelMode',
      '_isDashboardChipOn',
      '_renderDashboardVisibilityChip',
      '_renderTabVisibilityChips',
      '_toggleDashboardVisibilityChip'
    ]) {
      eval(extractFn(panelsSrc, name));
    }
    const realRenderTabVisibilityChips = _renderTabVisibilityChips;
    _renderTabVisibilityChips = function () {
      renderCalls += 1;
      return realRenderTabVisibilityChips();
    };

    (async () => {
      if (forceNoRestore) global._dashboardLastNonNeverMode = null;
      if (priorMode) _setDashboardModeForChip(priorMode);

      if (action === 'render') {
        _renderTabVisibilityChips();
        const chip = container.children.find((node) => node.getAttribute('data-tab-panel') === '__hermes_dashboard__');
        console.log(JSON.stringify({
          chipCount: container.children.length,
          hasChip: !!chip,
          chipText: chip && chip.textContent,
          chipAriaChecked: chip && chip.getAttribute('aria-checked'),
          chipIsOff: chip && chip.classList.contains('chip-off'),
        }));
        return;
      }

      _toggleDashboardVisibilityChip();
      await Promise.resolve();
      await new Promise((resolve) => setTimeout(resolve, 0));
      const firstMode = modeEl.value;

      if (action === 'toggle-fail') {
        console.log(JSON.stringify({
          firstMode,
          dashboardLastNonNeverMode: global._dashboardLastNonNeverMode,
          apiCalls,
          hiddenCalls,
          tabOrderCalls,
          renderCalls,
        }));
        return;
      }

      _toggleDashboardVisibilityChip();
      await new Promise((resolve) => setTimeout(resolve, 0));
      const secondMode = modeEl.value;

      console.log(JSON.stringify({
        firstMode,
        secondMode,
        dashboardLastNonNeverMode: global._dashboardLastNonNeverMode,
        apiCalls,
        hiddenCalls,
        tabOrderCalls,
        renderCalls,
      }));
    })().catch((err) => { console.error(err); process.exit(1); });
    """
)


def _run_panels_driver(
    action: str,
    mode: str = 'auto',
    prior_mode: str = '',
    force_no_restore: bool = False,
    fail_save: bool = False,
) -> dict:
    """Run the dashboard-visibility chip helpers with a DOM shim."""
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(_PANELS_DASHBOARD_DRIVER)
        driver = f.name
    try:
        result = subprocess.run(
            [
                NODE,
                driver,
                action,
                mode,
                prior_mode,
                "1" if force_no_restore else "0",
                "1" if fail_save else "0",
                str(PANELS_PATH),
                str(UI_PATH),
            ],
            cwd=ROOT,
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
            Path(driver).unlink()
        except OSError:
            pass

def test_backend_round_trip_and_validation(monkeypatch, tmp_path):
    """hidden_tabs defaults to [], saves/reloads, rejects non-list, filters empty strings."""
    import api.config as config
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    loaded = config.load_settings()
    assert loaded["hidden_tabs"] == [], "default must be empty list"
    assert loaded["tab_order"] == [], "tab order default must be empty list"

    saved = config.save_settings({"hidden_tabs": ["kanban", "insights"]})
    assert saved["hidden_tabs"] == ["kanban", "insights"]
    assert config.load_settings()["hidden_tabs"] == ["kanban", "insights"]

    saved = config.save_settings({"tab_order": ["logs", "tasks", "kanban"]})
    assert saved["tab_order"] == ["logs", "tasks", "kanban"]
    assert config.load_settings()["tab_order"] == ["logs", "tasks", "kanban"]

    bad_order = config.save_settings({"tab_order": "logs,tasks"})
    assert bad_order["tab_order"] == ["logs", "tasks", "kanban"]

    # Non-list is rejected, default preserved
    bad = config.save_settings({"hidden_tabs": "not-a-list"})
    assert bad["hidden_tabs"] == ["kanban", "insights"]

    # Empty strings filtered, empty list clears
    saved = config.save_settings({"hidden_tabs": ["kanban", "", "  ", "logs"]})
    assert saved["hidden_tabs"] == ["kanban", "logs"]
    cleared = config.save_settings({"hidden_tabs": []})
    assert cleared["hidden_tabs"] == []

    # Must NOT be in bool keys (would corrupt the list)
    assert "hidden_tabs" not in config._SETTINGS_BOOL_KEYS
    assert "hidden_tabs" in config._SETTINGS_ALLOWED_KEYS


def test_frontend_static_contracts():
    """All required HTML, JS, CSS, and boot elements exist with correct wiring."""
    # HTML: container in Appearance pane
    assert 'id="tabVisibilityChips"' in INDEX_HTML
    assert 'data-i18n="settings_label_tab_visibility"' in INDEX_HTML
    assert 'data-i18n="settings_desc_tab_visibility"' in INDEX_HTML
    appearance_start = INDEX_HTML.find('id="settingsPaneAppearance"')
    prefs_start = INDEX_HTML.find('id="settingsPanePreferences"', appearance_start + 1)
    chips_pos = INDEX_HTML.find('id="tabVisibilityChips"')
    assert appearance_start < chips_pos < prefs_start, \
        "tabVisibilityChips must be inside Appearance pane"

    # JS: constants, functions, and wiring
    assert "_ALWAYS_VISIBLE_TABS" in PANELS_JS
    assert "'chat'" in PANELS_JS.split("_ALWAYS_VISIBLE_TABS")[1][:80]
    assert "'settings'" in PANELS_JS.split("_ALWAYS_VISIBLE_TABS")[1][:80]
    assert "_HIDDEN_TABS_LS_KEY" in PANELS_JS
    assert "hermes-webui-hidden-tabs" in PANELS_JS
    assert "_TAB_ORDER_LS_KEY" in PANELS_JS
    assert "hermes-webui-tab-order" in PANELS_JS
    for fn in ("_getHiddenTabs", "_setHiddenTabs", "_getTabOrder", "_setTabOrder",
               "_applyTabOrder", "_applyTabVisibility", "_renderTabVisibilityChips",
               "_toggleTabVisibilityChip", "_moveTabOrderPanel", "_wireTabChipDrag"):
        assert f"function {fn}(" in PANELS_JS, f"panels.js must define {fn}()"

    # Toggle must autosave and respect always-visible tabs
    toggle_block = PANELS_JS[PANELS_JS.find("function _toggleTabVisibilityChip"):]
    toggle_body = toggle_block[:toggle_block.find("\nfunction ", 1) or 2000]
    assert "_scheduleAppearanceAutosave" in toggle_body
    assert "_ALWAYS_VISIBLE_TABS" in toggle_body

    # Appearance payload must include hidden_tabs and tab_order
    payload_block = PANELS_JS[PANELS_JS.find("function _appearancePayloadFromUi"):]
    payload_body = payload_block[:payload_block.find("\nfunction ", 1) or 2000]
    assert "hidden_tabs" in payload_body
    assert "_getHiddenTabs" in payload_body
    assert "tab_order" in payload_body
    assert "_getTabOrder" in payload_body

    # CSS: hidden class and chip styles
    assert ".nav-tab-hidden" in STYLE_CSS
    assert "display:none" in STYLE_CSS.split(".nav-tab-hidden")[1][:80].replace(" ", "")
    assert ".tab-visibility-chip" in STYLE_CSS

    # No flash-prevention script in <head> (DOM elements don't exist at that point)
    head_end = INDEX_HTML.find("</head>")
    assert "hermes-webui-hidden-tabs" not in INDEX_HTML[:head_end]


def test_boot_restores_visibility_from_localstorage():
    """boot.js must call _applyTabVisibility at boot time so hidden tabs take effect."""
    assert "_restoreTabVisibility" in BOOT_JS
    block = BOOT_JS[BOOT_JS.find("_restoreTabVisibility"):][:1500]
    assert "_applyTabVisibility" in block, \
        "boot.js must call _applyTabVisibility so tabs are hidden before first paint"


def test_i18n_coverage():
    """Label and description keys must exist in all locales with matching counts."""
    label_count = I18N_JS.count("settings_label_tab_visibility")
    desc_count = I18N_JS.count("settings_desc_tab_visibility")
    assert label_count >= 12, f"Expected ≥12 locales, found {label_count}"
    assert desc_count >= 12, f"Expected ≥12 locales, found {desc_count}"
    assert label_count == desc_count, \
        f"Label ({label_count}) and desc ({desc_count}) counts must match"


def test_backend_rejects_chat_and_settings_in_hidden_tabs(monkeypatch, tmp_path):
    """Server-side belt-and-suspenders: a malicious POST that tries to hide
    `chat` or `settings` (the always-visible nav tabs) must be filtered out
    server-side, not just client-side. The client already applies the same
    filter at apply time, but the server should not let a tampered payload
    persist the forbidden values."""
    import api.config as config
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    saved = config.save_settings({"hidden_tabs": ["chat", "kanban", "settings", "logs"]})
    assert saved["hidden_tabs"] == ["kanban", "logs"], \
        "chat and settings must be stripped server-side"

    # Even an all-forbidden payload reduces to empty (not rejected — empty is fine)
    saved = config.save_settings({"hidden_tabs": ["chat", "settings"]})
    assert saved["hidden_tabs"] == []


def test_profile_switch_reconciles_hidden_tabs():
    """When a user switches profiles, the new profile's hidden_tabs value
    must be applied — the per-profile settings.json is the source of truth,
    not the previous profile's localStorage value. Stage-394 added a
    /api/settings refetch in _refreshProfileSwitchBackground; verify it stays
    wired (the API call + the _applyTabVisibility call)."""
    bg_start = PANELS_JS.find("function _refreshProfileSwitchBackground")
    assert bg_start >= 0, "_refreshProfileSwitchBackground not found"
    bg_end = PANELS_JS.find("\nfunction ", bg_start + 1)
    if bg_end < 0:
        bg_end = bg_start + 4000
    bg_body = PANELS_JS[bg_start:bg_end]
    assert "/api/settings" in bg_body, \
        "profile-switch background refresh must re-fetch settings for the new profile"
    assert "_applyTabVisibility" in bg_body, \
        "profile-switch background refresh must re-apply tab visibility"
    assert "hidden_tabs" in bg_body, \
        "profile-switch background refresh must read hidden_tabs from server response"


def test_chip_a11y_uses_switch_role_with_aria_checked():
    """Chips should use role=switch + aria-checked instead of plain
    aria-pressed. The pressed/not-pressed wording is confusing for a toggle
    that visually represents an on/off switch; role=switch + aria-checked
    matches user mental model."""
    render_block = PANELS_JS[PANELS_JS.find("function _renderTabVisibilityChips"):]
    body = render_block[:render_block.find("\nfunction ", 1) or 3000]
    assert "role" in body and "'switch'" in body, \
        "chip should declare role='switch' for clearer screen-reader narration"
    assert "aria-checked" in body, "chip should use aria-checked to match role=switch"
    # Group container also has role=group + aria-labelledby
    assert 'role="group"' in INDEX_HTML, "chip container needs role=group"
    assert 'aria-labelledby="tabVisibilityLabel"' in INDEX_HTML, \
        "chip container needs aria-labelledby pointing at the label"
    # Focus-visible and drag styles exist
    assert ".tab-visibility-chip:focus-visible" in STYLE_CSS, \
        "chip needs a :focus-visible style for keyboard nav"
    assert ".tab-visibility-chip.dragging" in STYLE_CSS, \
        "chip needs dragging style for mouse reorder feedback"
    assert ".tab-visibility-chip.drag-over" in STYLE_CSS, \
        "chip needs drag-over style for mouse reorder target feedback"


@requires_node
def test_dashboard_chip_renders_in_tab_visibility_grid():
    """Behavioral verification for render state and placement in the chip grid."""
    for mode in ("auto", "always", "never"):
        out = _run_panels_driver("render", mode=mode)
        assert out["hasChip"] is True, out
        if mode == "never":
            assert out["chipIsOff"] is True, out
            assert out["chipAriaChecked"] == "false", out
        else:
            assert out["chipIsOff"] is False, out
            assert out["chipAriaChecked"] == "true", out
        assert out["chipText"] == "Hermes Dashboard", out
        assert out["chipCount"] >= 1, out


@requires_node
def test_dashboard_chip_off_sets_never_and_chip_on_restores_prior_mode():
    """Chip toggling must save 'never' and restore non-never mode on the next toggle."""
    out = _run_panels_driver("toggle", mode="always", prior_mode="always")
    assert out["firstMode"] == "never", out
    assert out["secondMode"] == "always", out
    assert len([call for call in out["apiCalls"] if call["url"] == "/api/dashboard/config"]) == 2, out
    assert out["hiddenCalls"] == 0, out
    assert out["tabOrderCalls"] == 0, out
    assert out["dashboardLastNonNeverMode"] == "always", out


@requires_node
def test_dashboard_chip_toggle_does_not_mutate_hidden_tabs_or_tab_order():
    """Dashboard chip toggles stay on the dashboard config path, never the sidebar-tab payload."""
    out = _run_panels_driver("toggle", mode="auto", prior_mode="auto")
    assert out["firstMode"] == "never", out
    assert len([call for call in out["apiCalls"] if call["url"] == "/api/dashboard/config"]) == 2, out
    assert out["hiddenCalls"] == 0, out
    assert out["tabOrderCalls"] == 0, out


@requires_node
def test_dashboard_chip_on_defaults_to_auto_without_prior_mode():
    """Missing restore state should fall back to 'auto' before returning chip-on."""
    out = _run_panels_driver("toggle", mode="never", force_no_restore=True)
    assert out["firstMode"] == "auto", out
    assert out["secondMode"] == "never", out
    assert out["hiddenCalls"] == 0, out
    assert out["tabOrderCalls"] == 0, out


@requires_node
def test_dashboard_chip_failed_save_restores_previous_mode():
    """A failed chip save must roll the dropdown and chip state back to the prior mode."""
    out = _run_panels_driver("toggle-fail", mode="auto", prior_mode="auto", fail_save=True)
    assert out["firstMode"] == "auto", out
    assert len([call for call in out["apiCalls"] if call["url"] == "/api/dashboard/config"]) == 1, out
    assert out["hiddenCalls"] == 0, out
    assert out["tabOrderCalls"] == 0, out
    assert out["renderCalls"] == 1, out
    assert out["dashboardLastNonNeverMode"] == "auto", out


def test_tab_order_excludes_always_visible_tabs(monkeypatch, tmp_path):
    """Server-side tab_order validation mirrors hidden_tabs: chat and settings
    remain fixed, so a tampered payload must not persist them in custom order."""
    import api.config as config
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    saved = config.save_settings({"tab_order": ["chat", "logs", "settings", "tasks", "logs"]})
    assert saved["tab_order"] == ["logs", "tasks"], \
        "chat/settings must be stripped and duplicate panel ids collapsed server-side"


def test_profile_switch_reconciles_tab_order():
    """Profile switching must also restore per-profile custom tab ordering."""
    bg_start = PANELS_JS.find("function _refreshProfileSwitchBackground")
    assert bg_start >= 0, "_refreshProfileSwitchBackground not found"
    bg_end = PANELS_JS.find("\nfunction ", bg_start + 1)
    if bg_end < 0:
        bg_end = bg_start + 4000
    bg_body = PANELS_JS[bg_start:bg_end]
    assert "tab_order" in bg_body, \
        "profile-switch background refresh must read tab_order from server response"
    assert "_setTabOrder" in bg_body, \
        "profile-switch background refresh must store tab_order for the new profile"
    assert "_applyTabOrder" in bg_body, \
        "profile-switch background refresh must apply tab ordering"
