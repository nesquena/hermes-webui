from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_settings_system_panel_exposes_restart_server_control():
    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    boot_js = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    i18n_js = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    assert 'id="shutdownServerBlock"' in index_html
    assert 'id="btnShutdownServer"' in index_html
    assert 'onclick="shutdownServer()"' in index_html
    assert "settings_label_shutdown" in i18n_js
    assert "settings_desc_shutdown_before_cmd" in i18n_js
    assert 'data-i18n="settings_desc_shutdown_before_cmd"' in index_html

    assert 'id="btnRestartServer"' in index_html
    assert 'onclick="restartServer()"' in index_html
    assert "async function restartServer()" in boot_js
    assert "'/api/restart'" in boot_js
    assert "settings_btn_restart" in i18n_js
    assert "settings_restart_confirm_title" in i18n_js
    assert "settings_restart_force_confirm_title" in i18n_js
    assert "requires_confirmation" in boot_js
    assert "JSON.stringify(force ? { force: true } : {})" in boot_js
    assert "textContent = restartingMsg" in boot_js
    assert "function waitForServerRestartAndReload()" in boot_js
    assert "const minDelayMs = 3000" in boot_js
    assert "const maxDelayMs = 12000" in boot_js
    assert "restart_check" in boot_js
    assert "res && res.ok" in boot_js
