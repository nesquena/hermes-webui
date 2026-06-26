"""Regression tests for the Settings → Extensions diagnostics and toggles."""
from pathlib import Path
import re


ROOT = Path(__file__).parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
DOCS_EXTENSIONS = (ROOT / "docs" / "EXTENSIONS.md").read_text(encoding="utf-8")


def _function_block(name: str, *, extra: int = 2200) -> str:
    start = PANELS_JS.find(f"function {name}")
    assert start >= 0, f"{name} not found"
    return PANELS_JS[start:start + extra]


def _between(start_marker: str, end_marker: str) -> str:
    start = PANELS_JS.find(start_marker)
    assert start >= 0, f"{start_marker} not found"
    end = PANELS_JS.find(end_marker, start)
    assert end >= 0, f"{end_marker} not found after {start_marker}"
    return PANELS_JS[start:end]


def _locale_count() -> int:
    return len(re.findall(r"^  (?:[A-Za-z_][A-Za-z0-9_]*|'[^']+'):\s*\{", I18N_JS, re.MULTILINE))


def _locale_blocks() -> dict[str, str]:
    matches = list(re.finditer(r"^  ((?:[A-Za-z_][A-Za-z0-9_]*|'[^']+')):\s*\{", I18N_JS, re.MULTILINE))
    blocks = {}
    for idx, match in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(I18N_JS)
        blocks[match.group(1)] = I18N_JS[match.start():end]
    return blocks


def _contains_post_method(block: str) -> bool:
    """Return True when a JS block contains a method: 'POST' style mutation."""
    return bool(re.search(r"\bmethod\s*:\s*([\"'`])POST\1", block))


def test_settings_sidebar_has_extensions_section_and_pane():
    assert 'data-settings-section="extensions"' in INDEX_HTML
    assert "switchSettingsSection('extensions',{fromSidebarItem:true})" in INDEX_HTML
    assert 'id="settingsPaneExtensions"' in INDEX_HTML
    assert 'id="extensionsDiagnostics"' in INDEX_HTML
    assert 'id="extensionsCopyDiagnosticsBtn"' in INDEX_HTML
    assert 'data-i18n="settings_tab_extensions"' in INDEX_HTML


def test_extensions_panel_warns_about_trust_model_and_stays_install_free():
    pane_start = INDEX_HTML.index('id="settingsPaneExtensions"')
    pane_end = INDEX_HTML.index('id="settingsPaneSystem"', pane_start)
    pane = INDEX_HTML[pane_start:pane_end]

    assert "Extensions run in the WebUI browser origin" in pane
    assert "Only load trusted local extension directories" in pane
    assert "takes effect after reload" in pane
    assert "copyExtensionsDiagnostics()" in pane
    assert "saveSettings(" not in pane
    assert "api('/api/settings'" not in pane
    assert "type=\"checkbox\"" not in pane
    assert "marketplace" not in pane.lower()


def test_switch_settings_section_supports_extensions_lazy_load():
    switch_block = _function_block("switchSettingsSection", extra=2300)

    assert "name==='extensions'" in switch_block
    assert "plugins:'Plugins',extensions:'Extensions'" in switch_block
    assert "'plugins','extensions','system'" in switch_block.replace("\n", "")
    assert "if(section==='extensions') loadExtensionsPanel();" in switch_block


def test_settings_search_knows_extensions_pane():
    build_block = _function_block("_buildSettingsIndex", extra=2600)
    filter_block = _function_block("filterSettings", extra=1700)
    resolve_block = _function_block("_resolveSettingsField", extra=1400)

    assert "loadExtensionsPanel()" in build_block
    assert "settingsPaneExtensions: 'extensions'" in build_block
    assert "extensions: t('settings_tab_extensions') || 'Extensions'" in filter_block
    assert "extensions: 'settingsPaneExtensions'" in resolve_block


def test_extensions_panel_fetches_status_endpoint_without_mutating_settings():
    load_block = _function_block("loadExtensionsPanel", extra=900)

    assert "api('/api/extensions/status')" in load_block
    assert "api('/api/settings'" not in load_block
    assert not _contains_post_method(load_block)
    assert "extensions-error" in load_block


def test_extensions_diagnostics_tab_refreshes_runtime_status():
    tab_block = _function_block("switchExtensionsTab", extra=900)

    assert "if(tab==='diagnostics') loadExtensionsPanel({preserveExisting:true});" in tab_block
    assert "if(tab==='gallery'&&!_extensionsGalleryLoaded) loadExtensionsGallery();" in tab_block


def test_extensions_panel_renders_sanitized_status_payload():
    render_block = _between("function _renderExtensionsPanel", "async function loadExtensionsPanel")
    warning_block = _function_block("_extensionWarningList", extra=900)
    asset_block = _function_block("_extensionAssetList", extra=500)

    assert "extension_dir_configured" in render_block
    assert "extension_dir_valid" in render_block
    assert "manifest.status" in render_block
    assert "manifest.entry_count" in render_block
    assert "manifest.script_count" in render_block
    assert "manifest.stylesheet_count" in render_block
    assert "manifest.sidecar_count" in render_block
    assert "script_urls" in render_block
    assert "stylesheet_urls" in render_block
    assert "data&&data.sidecars" in render_block
    assert "data&&data.extensions" in render_block
    assert "counts,'manifest_extensions'" in render_block
    assert "counts,'user_disabled'" in render_block
    assert "_extensionInstalledList(extensions,!!(data&&data.extension_dir_configured))" in render_block
    assert "_extensionSidecarCard(sidecars)" in render_block
    assert "data&&data.warnings" in render_block
    assert "esc(url)" in asset_block
    assert "esc(manifest.status||'unknown')" in render_block
    assert "const rawCode=(item&&item.code)||'unknown_warning'" in warning_block
    assert "const code=esc(rawCode)" in warning_block
    assert "esc((item&&item.source)||'unknown')" in warning_block
    assert "extension_state_unknown_ids" in warning_block
    assert "Some saved disabled-extension overrides no longer match the current manifest" in warning_block
    assert "Rejected" not in render_block  # rejected values must never be rendered directly


def test_extensions_panel_renders_loopback_sidecar_monitor_safely():
    runtime_block = _between("function _extensionRuntimeStatusValue", "function _extensionSidecarCard")
    sidecar_block = _between("function _extensionSidecarCard", "function _setExtensionSidecarHealth")
    runtime_setter_block = _between("function _setExtensionSidecarRuntime", "async function _checkExtensionSidecarHealth")
    monitor_block = _between("async function _checkExtensionSidecarHealth", "function _renderExtensionsPanel")
    render_block = _between("function _renderExtensionsPanel", "async function loadExtensionsPanel")
    load_block = _between("async function loadExtensionsPanel", "async function copyExtensionsDiagnostics")
    load_catch_block = load_block[load_block.index("}catch(e){"):]

    assert "Loopback sidecars" in sidecar_block
    assert "No loopback sidecars declared." in sidecar_block
    assert "esc(title)" in sidecar_block
    assert "esc(meta)" in sidecar_block
    assert "esc(origin)" in sidecar_block
    assert "esc(healthPath)" in sidecar_block
    assert "esc(healthUrl)" in sidecar_block
    assert 'data-sidecar-runtime-index="${index}"' in sidecar_block
    assert "fetch(healthUrl,{credentials:'omit',cache:'no-store'" in monitor_block
    assert "function _monitorExtensionSidecars(sidecars,seq)" in monitor_block
    assert "const seq=_extensionsSidecarMonitorSeq" not in monitor_block
    assert "_monitorExtensionSidecars(sidecars,seq)" in render_block
    assert "function _renderExtensionsPanel(data,seq)" in render_block
    assert "const seq=++_extensionsSidecarMonitorSeq" in load_block
    assert "preserveExisting=!!(opts&&opts.preserveExisting&&target.innerHTML.trim())" in load_block
    assert "if(!preserveExisting) target.innerHTML" in load_block
    assert "loadExtensionsPanel({preserveExisting:true})" in load_block
    assert "if(seq!==_extensionsSidecarMonitorSeq) return;" in load_block
    assert "if(seq!==_extensionsSidecarMonitorSeq) return;" in load_catch_block
    assert "if(preserveExisting&&target.innerHTML.trim()) return;" in load_catch_block
    assert "_renderExtensionsPanel(data,seq)" in load_block
    assert "res.ok" in monitor_block
    assert "res.text" not in monitor_block
    assert "body=await res.json()" in monitor_block
    assert "_setExtensionSidecarRuntime(index,body&&typeof body==='object'?body.runtime:null)" in monitor_block
    assert "String(value??'').trim()" in runtime_block
    assert r"/^\d+(?:\.\d+)?$/.test(text)" in runtime_block
    assert "seconds>now+300" in runtime_block
    assert "runtime.sidecar" in runtime_block
    assert "runtime.native_host" in runtime_block
    assert "runtime.bridge" in runtime_block
    assert "runtime.last_seen_at" in runtime_block
    assert "runtime.webui_origin" in runtime_block
    assert "el.innerHTML=details" in runtime_setter_block
    assert "api('/api/settings'" not in monitor_block
    assert "api('/extensions/" not in monitor_block
    assert "sidecar/*" not in monitor_block
    assert not _contains_post_method(monitor_block)


def test_extensions_panel_toggle_uses_dedicated_endpoint_without_settings_or_install():
    installed_block = _between("function _extensionInstalledList", "function _extensionSidecarHealthBadge")
    toggle_block = _between("async function handleExtensionToggle", "async function loadExtensionsPanel")

    assert "data-extension-toggle-id" in installed_block
    assert "data-extension-next-enabled" in installed_block
    assert "No extension directory is configured." in installed_block
    assert "No manifest extensions are installed in the configured bundle." in installed_block
    assert "extensionDirConfigured" in installed_block
    assert "Manifest-disabled entries cannot be enabled from WebUI." in installed_block
    assert "api('/api/extensions/toggle',{method:'POST',body:JSON.stringify({id,enabled})})" in toggle_block
    assert "Reload WebUI to apply changes" in toggle_block
    combined = installed_block + toggle_block
    assert "api('/api/settings'" not in combined
    assert ">Install<" not in combined
    assert "Install extension" not in combined
    assert "Uninstall" not in combined
    assert "marketplace" not in combined.lower()


def test_copy_extensions_diagnostics_copies_current_sanitized_payload():
    copy_block = _between("function copyExtensionsDiagnostics", "// ── Plugins panel")

    assert "JSON.stringify(_extensionsStatusData,null,2)" in copy_block
    assert "navigator.clipboard.writeText(text)" in copy_block
    assert "api('/api/settings'" not in copy_block
    assert "api('/api/extensions'" not in copy_block
    assert not _contains_post_method(copy_block)


def test_extensions_styles_are_scoped_to_extensions_panel():
    assert ".extensions-diagnostics" in STYLE_CSS
    assert ".extensions-trust-note" in STYLE_CSS
    assert ".extension-summary-grid" in STYLE_CSS
    assert ".extension-warning-list" in STYLE_CSS
    assert ".extension-url-list" in STYLE_CSS
    assert ".extension-installed-list" in STYLE_CSS
    assert ".extension-toggle-btn" in STYLE_CSS
    assert ".extension-sidecar-list" in STYLE_CSS
    assert ".extension-sidecar-runtime" in STYLE_CSS
    assert ".extension-sidecar-status-badge" in STYLE_CSS


def test_extensions_tab_i18n_key_exists_for_all_locales():
    blocks = _locale_blocks()

    assert len(blocks) == _locale_count()
    missing = [name for name, block in blocks.items() if "settings_tab_extensions" not in block]
    assert not missing, f"Locale(s) missing settings_tab_extensions: {missing}"


def test_extensions_docs_mentions_settings_panel_without_install_or_proxy_claims():
    diagnostics_section = DOCS_EXTENSIONS[DOCS_EXTENSIONS.index("## Diagnostics"):]

    assert "Settings → Extensions" in diagnostics_section
    assert "`POST /api/extensions/toggle`" in diagnostics_section
    assert "WebUI-managed override" in diagnostics_section
    assert "does not edit extension" in diagnostics_section
    assert "manifests" in diagnostics_section
    assert "fetch new extension assets" in diagnostics_section
    assert "uninstall files" in diagnostics_section
    assert "proxy sidecars" in diagnostics_section
    assert "GET /api/extensions/status" in diagnostics_section
    assert "sanitized loopback sidecars" in diagnostics_section
    assert "credentials: 'omit'" in diagnostics_section
    assert "does **not** proxy sidecar requests" in diagnostics_section
    assert "optional top-level `runtime` object" in diagnostics_section
    assert "allowlisted scalar fields" in diagnostics_section
    assert "do **not**" in diagnostics_section
    assert "return `HERMES_WEBUI_EXTENSION_DIR`" in diagnostics_section
    assert "override state-file path" in diagnostics_section
