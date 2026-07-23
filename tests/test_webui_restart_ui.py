from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _extract_function(src: str, name: str) -> str:
    anchor = f"async function {name}("
    start = src.find(anchor)
    assert start != -1, f"{name}() must exist"
    body_start = src.find("{", start)
    assert body_start != -1, f"{name}() must have a body"
    depth = 1
    index = body_start + 1
    while depth and index < len(src):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
        index += 1
    assert depth == 0, f"{name}() body must balance braces"
    return src[start:index]


def test_control_center_places_restart_before_existing_destructive_stop_control():
    html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    block_start = html.find('<div class="settings-field" id="shutdownServerBlock"')
    assert block_start != -1
    restart_at = html.find('id="btnRestartServer"', block_start)
    stop_at = html.find('id="btnShutdownServer"', block_start)

    assert restart_at != -1, "Control Center must expose a restart action."
    assert stop_at != -1, "Existing stop action must remain available."
    assert restart_at < stop_at, "Neutral restart should precede the destructive stop action."
    assert 'onclick="restartServer()"' in html[block_start:stop_at]


def test_restart_client_calls_managed_endpoint_then_recovers_by_bounded_health_polling():
    boot = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function(boot, "restartServer")

    assert "showConfirmDialog" in body
    assert "api('/api/restart', { method: 'POST' })" in body
    assert "result.status !== 'restarting'" in body
    assert "_showServerRestarting" in body
    assert "api('/health'" in boot
    assert "let observedUnavailable = false;" in boot
    assert "if (observedUnavailable)" in boot
    assert "observedUnavailable = true;" in boot
    assert "window.location.reload()" in boot
    assert "attempt <" in boot, "Health recovery must have a bounded retry budget."


def test_restart_i18n_uses_english_keys_and_existing_locale_fallback():
    i18n = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in (
        "settings_btn_restart",
        "settings_restart_confirm_title",
        "settings_restart_confirm_message",
        "settings_restart_confirm_btn",
        "settings_restart_pending_message",
        "settings_restart_timeout_message",
    ):
        assert f"{key}:" in i18n

    locale_count = i18n.count("settings_label_shutdown:")
    assert i18n.count("settings_btn_restart:") == locale_count
    assert locale_count > 1, "Test assumes multiple locale bundles are present."
