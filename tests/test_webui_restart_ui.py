from pathlib import Path
import json
import shutil
import subprocess


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


def _run_restart_waiter(health_results):
    node = shutil.which("node")
    assert node, "node is required for restart recovery behavior coverage"
    boot = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function(boot, "_waitForServerRestart")
    driver = f"""
const healthResults = {json.dumps(health_results)};
let reloads = 0, timeoutMessage = null;
let index = 0;
global.window = {{ location: {{ reload: () => {{ reloads += 1; }} }} }};
global.api = async () => {{
  const result = healthResults[index++];
  if (result === "error") throw new Error("offline");
  return result;
}};
global.setTimeout = (resolve) => resolve();
global._showServerRestarting = (message) => {{ timeoutMessage = message; }};
global.t = () => "timed out";
{body}
(async () => {{
  await _waitForServerRestart("old-generation");
  console.log(JSON.stringify({{reloads, timeoutMessage}}));
}})().catch((error) => {{ console.error(error); process.exit(1); }});
"""
    result = subprocess.run([node, "-e", driver], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_restart_client_calls_managed_endpoint_then_recovers_by_bounded_health_polling():
    boot = (REPO / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function(boot, "restartServer")

    assert "showConfirmDialog" in body
    assert "api('/api/restart', { method: 'POST' })" in body
    assert "result.status !== 'restart_scheduled'" in body
    assert "_showServerRestarting" in body
    assert "api('/health'" in boot
    assert "server_started_at" in boot
    assert "window.location.reload()" in boot
    assert "attempt <" in boot, "Health recovery must have a bounded retry budget."


def test_restart_waiter_reloads_only_when_health_reports_a_new_server_generation():
    assert _run_restart_waiter([
        {"status": "ok", "server_started_at": "old-generation"},
        {"status": "ok", "server_started_at": "new-generation"},
    ])["reloads"] == 1

    stale = _run_restart_waiter([{"status": "ok", "server_started_at": "old-generation"}] * 30)
    assert stale == {"reloads": 0, "timeoutMessage": "timed out"}


def test_restart_waiter_handles_an_outage_before_the_new_server_generation():
    result = _run_restart_waiter([
        "error",
        {"status": "ok", "server_started_at": "new-generation"},
    ])
    assert result == {"reloads": 1, "timeoutMessage": None}


def test_restart_i18n_uses_english_keys_and_existing_locale_fallback():
    i18n = (REPO / "static" / "i18n.js").read_text(encoding="utf-8")
    for key in (
        "settings_desc_restart",
        "settings_btn_restart",
        "settings_restart_confirm_title",
        "settings_restart_confirm_message",
        "settings_restart_confirm_btn",
        "settings_restart_pending_message",
        "settings_restart_timeout_message",
        "settings_restart_unsupported_message",
    ):
        assert f"{key}:" in i18n

    locale_count = i18n.count("settings_label_shutdown:")
    assert i18n.count("settings_btn_restart:") == locale_count
    assert locale_count > 1, "Test assumes multiple locale bundles are present."
