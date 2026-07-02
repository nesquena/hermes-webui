"""Regression tests for the Settings → Extensions diagnostics and toggles."""
from pathlib import Path
import re


ROOT = Path(__file__).parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
DOCS_EXTENSIONS = (ROOT / "docs" / "EXTENSIONS.md").read_text(encoding="utf-8")
ROUTES_PY = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


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


def _locale_string(block: str, key: str) -> str:
    match = re.search(rf"\b{re.escape(key)}:\s*([\"'])(.*?)\1", block, re.DOTALL)
    assert match, f"{key} not found in locale block"
    return match.group(2)


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
    assert "Settings and extension-owned storage are browser-local and not for secrets" in pane
    assert "Only load trusted local extension directories" in pane
    assert "takes effect after reload" in pane
    assert "copyExtensionsDiagnostics()" in pane
    assert "saveSettings(" not in pane
    assert "api('/api/settings'" not in pane
    assert "type=\"checkbox\"" not in pane
    assert "marketplace" not in pane.lower()
    assert "static/extension_settings.js" in INDEX_HTML
    assert INDEX_HTML.index("static/extension_settings.js") < INDEX_HTML.index("static/panels.js")


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


def test_extensions_do_not_add_generic_backend_settings_write_route():
    assert "/api/extensions/settings" not in ROUTES_PY
    assert "/api/extensions/storage" not in ROUTES_PY
    assert "set_extension_settings" not in ROUTES_PY
    assert "write_extension_settings" not in ROUTES_PY


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
    assert "sidecar&&sidecar.proxy" in sidecar_block
    assert "proxy.available===true" in sidecar_block
    assert "proxy.consented===true" in sidecar_block
    assert "proxy.consent_required===true" in sidecar_block
    assert "proxy.origin_changed===true" in sidecar_block
    assert "Proxy path" in sidecar_block
    assert "data-extension-sidecar-proxy-id" in sidecar_block
    assert "data-extension-sidecar-proxy-approved" in sidecar_block
    assert 'data-sidecar-runtime-index="${index}"' in sidecar_block
    assert "fetch(healthUrl,{credentials:'omit',cache:'no-store'" in monitor_block
    assert "function _monitorExtensionSidecars(sidecars,seq)" in monitor_block
    assert "const seq=_extensionsSidecarMonitorSeq" not in monitor_block
    assert "_monitorExtensionSidecars(sidecars,seq)" in render_block
    assert "function _renderExtensionsPanel(data,seq)" in render_block
    assert "_bindExtensionSidecarProxyButtons(target)" in render_block
    assert "const seq=++_extensionsSidecarMonitorSeq" in load_block
    assert "opts&&opts.preserveExisting&&target.innerHTML.trim()" in load_block
    # A failed refresh must NOT be preserved as "existing content": the Loading/
    # error placeholders are excluded so a fetch error always renders the error
    # instead of leaving the panel stuck on "Loading extension diagnostics…".
    assert "!target.querySelector('.extensions-loading,.extensions-error')" in load_block
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


def test_extensions_panel_sidecar_proxy_consent_uses_dedicated_endpoint():
    bind_block = _between("function _bindExtensionSidecarProxyButtons", "async function handleExtensionToggle")
    consent_block = _between("async function handleExtensionSidecarProxyConsent", "function _readExtensionSettingsForm")

    assert "handleExtensionSidecarProxyConsent" in bind_block
    assert "data-extension-sidecar-proxy-id" in bind_block
    assert "api('/api/extensions/sidecar-proxy-consent',{method:'POST',body:JSON.stringify({id,approved})})" in consent_block
    assert "Extension sidecar proxy approved." in consent_block
    assert "Extension sidecar proxy consent revoked." in consent_block
    assert "Failed to update extension sidecar proxy consent" in consent_block
    assert "api('/api/extensions/toggle'" not in consent_block


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


def test_extensions_installed_settings_route_through_shared_accessor():
    installed_block = _between("function _extensionInstalledList", "function _extensionSidecarHealthBadge")
    settings_block = _between("function _configureExtensionSettingsFromStatus", "function _extensionInstalledList")
    bind_block = _between("function _bindExtensionSettingsButtons", "async function loadExtensionsPanel")
    gallery_block = _between("function _renderExtensionsGallery", "function _bindExtensionGalleryButtons")

    assert "entry&&entry.storage_owned" in settings_block
    assert "window.HermesExtensionSettings.settingsForExtension(id)" in settings_block
    assert "settingsApi&&settingsApi.schema" in settings_block
    assert "settingsApi||!settingsApi.trusted" in settings_block
    assert "data-extension-settings-save" in settings_block
    assert "data-extension-settings-reset" in settings_block
    assert "data-extension-storage-clear" in settings_block
    assert "Browser-local extension settings" in settings_block
    assert "Do not store secrets here" in settings_block
    assert "Reload WebUI after enabling or installing this extension to edit browser-local settings." in settings_block
    assert "_extensionSettingsControls(entry)" in installed_block
    assert "window.HermesExtensionSettings.settingsForExtension(id).reset()" in bind_block
    assert "window.HermesExtensionSettings.storageForExtension(id).clear()" in bind_block
    assert "api('/api/extensions/status')" not in bind_block
    assert "api('/api/settings'" not in bind_block
    assert "localStorage" not in bind_block
    assert "_extensionInstalledList(statusData&&statusData.extensions" in gallery_block
    assert "_bindExtensionSettingsButtons(installedEl)" in gallery_block


def test_extensions_gallery_renders_post_install_guidance():
    url_block = _between("function _extensionSafeHttpUrl", "function _extensionPostInstallNote")
    gallery_block = _between("function _extensionPostInstallNote", "async function loadExtensionsGallery")
    render_block = _between("function _renderExtensionsGallery", "function _bindExtensionGalleryButtons")
    install_block = _between("async function handleExtensionInstall", "async function handleExtensionUninstall")

    assert "/^https?:\\/\\//i.test(raw)" in url_block
    assert "url.username||url.password" in url_block
    assert "entry.post_install" in gallery_block
    assert "post&&post.docs_url" in gallery_block
    assert "sidecar_start_required" in gallery_block
    assert "native_host_start_required" in gallery_block
    assert "requires_local_app" in gallery_block
    assert "local_app_label" in gallery_block
    assert "t('ext_gallery_local_component_required')" in gallery_block
    assert "t('ext_gallery_local_app_label')" in gallery_block
    assert "t('ext_gallery_required_suffix',localAppLabel)" in gallery_block
    assert "t('ext_gallery_sidecar_required')" in gallery_block
    assert "t('ext_gallery_native_host_required')" in gallery_block
    assert "t('ext_gallery_open_setup_guide')" in gallery_block
    assert "t(isInstalled?'ext_gallery_next_step':'ext_gallery_after_install')" in gallery_block
    assert "target=\"_blank\"" in gallery_block
    assert "rel=\"noopener noreferrer\"" in gallery_block
    assert "extension-gallery-next-step" in gallery_block
    assert "esc(summary)" in gallery_block
    assert "esc(docsUrl)" in gallery_block
    assert "esc(item)" in gallery_block
    assert "_extensionPostInstallNote(entry,isInstalled)" in render_block
    assert "t('ext_gallery_install_restart_required')" in install_block
    assert "t('ext_gallery_install_followup')" in install_block
    assert "t('ext_gallery_install_ok')" in install_block
    assert "webui_restart_required" in install_block


def test_extensions_gallery_links_sources_and_humanizes_permissions():
    helper_block = _between("function _extensionRegistrySourceUrl", "function _extensionPostInstallNote")
    render_block = _between("function _renderExtensionsGallery", "function _bindExtensionGalleryButtons")

    assert "entry.homepage" in helper_block
    assert "entry.repository_url" in helper_block
    assert "entry.entry_path||entry.runtime_manifest_path" in helper_block
    assert "hermes-webui/hermes-webui-extensions/tree/main" in helper_block
    assert "encodeURIComponent" in helper_block
    assert "extension-gallery-source-link" in helper_block
    assert "target=\"_blank\"" in helper_block
    assert "rel=\"noopener noreferrer\"" in helper_block
    assert "t('ext_gallery_permissions_empty')" in helper_block
    assert "webui_api" in helper_block
    assert "sidecar_commands" in helper_block
    assert "dom.mutates_core_views" in helper_block
    assert "storage.shared_webui_keys" in helper_block
    assert "loopback_sidecar" in helper_block
    assert "native_host" in helper_block
    assert "network_external" in helper_block
    assert "extension-gallery-permission-row" in helper_block
    assert "_extensionSourceLink(entry)" in render_block
    assert "_extensionPermissionSummary(perms)" in render_block
    assert "JSON.stringify(perms" not in render_block
    assert "<pre>" not in render_block


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
    assert ".extension-settings-box" in STYLE_CSS
    assert ".extension-settings-actions" in STYLE_CSS
    assert ".extension-sidecar-list" in STYLE_CSS
    assert ".extension-sidecar-runtime" in STYLE_CSS
    assert ".extension-sidecar-status-badge" in STYLE_CSS
    assert ".extension-gallery-source-link" in STYLE_CSS
    assert ".extension-gallery-next-step" in STYLE_CSS
    assert ".extension-gallery-next-link" in STYLE_CSS
    assert ".extension-gallery-permission-list" in STYLE_CSS
    assert ".extension-gallery-permission-row" in STYLE_CSS


def test_extensions_i18n_keys_exist_for_all_locales():
    blocks = _locale_blocks()

    assert len(blocks) == _locale_count()
    required_keys = [
        "settings_tab_extensions",
        "ext_gallery_next_step",
        "ext_gallery_after_install",
        "ext_gallery_permissions_empty",
        "ext_gallery_local_component_required",
        "ext_gallery_local_app_label",
        "ext_gallery_required_suffix",
        "ext_gallery_sidecar_required",
        "ext_gallery_native_host_required",
        "ext_gallery_open_setup_guide",
        "ext_gallery_install_restart_required",
        "ext_gallery_install_followup",
    ]
    missing = {
        name: [key for key in required_keys if key not in block]
        for name, block in blocks.items()
    }
    missing = {name: keys for name, keys in missing.items() if keys}
    assert not missing, f"Locale(s) missing extension i18n key(s): {missing}"


def test_extensions_post_install_i18n_is_localized_outside_english():
    blocks = _locale_blocks()
    english = blocks["en"]
    post_install_keys = [
        "ext_gallery_next_step",
        "ext_gallery_after_install",
        "ext_gallery_local_component_required",
        "ext_gallery_local_app_label",
        "ext_gallery_required_suffix",
        "ext_gallery_sidecar_required",
        "ext_gallery_native_host_required",
        "ext_gallery_open_setup_guide",
        "ext_gallery_install_restart_required",
        "ext_gallery_install_followup",
    ]
    english_values = {key: _locale_string(english, key) for key in post_install_keys}
    untranslated = {
        name: [
            key
            for key in post_install_keys
            if _locale_string(block, key) == english_values[key]
        ]
        for name, block in blocks.items()
        if name != "en"
    }
    untranslated = {name: keys for name, keys in untranslated.items() if keys}

    assert not untranslated, f"Locale(s) keep English post-install guidance: {untranslated}"
    for name, block in blocks.items():
        assert "{0}" in _locale_string(block, "ext_gallery_required_suffix"), (
            f"{name} must preserve the local-app label placeholder"
        )


def test_extensions_i18n_does_not_include_replacement_characters():
    assert "\ufffd" not in I18N_JS


def test_extensions_docs_mentions_settings_panel_without_install_or_proxy_claims():
    diagnostics_section = DOCS_EXTENSIONS[DOCS_EXTENSIONS.index("## Diagnostics"):]

    assert "Settings → Extensions" in diagnostics_section
    assert "`POST /api/extensions/toggle`" in diagnostics_section
    assert "WebUI-managed override" in diagnostics_section
    assert "does not edit extension" in diagnostics_section
    assert "manifests" in diagnostics_section
    assert "fetch new extension assets" in diagnostics_section
    assert "uninstall files" in diagnostics_section
    assert "GET /api/extensions/status" in diagnostics_section
    assert "`POST /api/extensions/sidecar-proxy-consent`" in diagnostics_section
    assert "sanitized loopback sidecars" in diagnostics_section
    assert "credentials: 'omit'" in diagnostics_section
    assert "fixed per-extension sidecar path" in diagnostics_section
    assert "WebUI strips `Cookie`, `Authorization`, and CSRF headers" in diagnostics_section
    assert "does not create arbitrary extension-owned backend routes" in diagnostics_section
    assert "optional top-level `runtime` object" in diagnostics_section
    assert "allowlisted scalar fields" in diagnostics_section
    assert "browser-local controls" in diagnostics_section
    assert "`window.HermesExtensionSettings`" in DOCS_EXTENSIONS
    assert "does not store extension settings or expose a generic settings write route" in DOCS_EXTENSIONS
    assert "Settings persist only non-default overrides" in DOCS_EXTENSIONS
    assert "do **not**" in diagnostics_section
    assert "return `HERMES_WEBUI_EXTENSION_DIR`" in diagnostics_section
    assert "override state-file path" in diagnostics_section
