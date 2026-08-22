"""Behavioral regression coverage for session-scoped composer quota state.

The production quota helpers are executed in a small Node DOM harness.  These
checks deliberately exercise the request/DOM boundary instead of only asserting
source-string placement: a provider switch must immediately remove old quota
content, request the selected provider, and never let an older response restore
stale account information.
"""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _quota_region() -> str:
    source = UI_JS.read_text(encoding="utf-8")
    start = source.index("// ── Ambient provider quota indicator")
    end = source.index("// Dynamic model labels", start)
    return source[start:end]


def _run_quota_scenario(body: str) -> dict:
    assert NODE is not None
    source = json.dumps(_quota_region())
    script = f"""
const source = {source};
const requests = [];
const pending = [];
function makeNode() {{
  return {{
    hidden: false,
    title: '',
    textContent: '',
    style: {{display: ''}},
    removeAttribute(name) {{
      if (name === 'title') this.title = '';
    }},
  }};
}}
const nodes = {{
  providerQuotaChip: makeNode(),
  providerQuotaChipLabel: makeNode(),
  composerMobileQuotaAction: makeNode(),
  composerMobileQuotaLabel: makeNode(),
}};
function $(id) {{ return nodes[id] || null; }}
const window = {{_showQuotaChip: true, addEventListener: () => {{}}}};
const S = {{session: null}};
let _emptyComposerModelOverride = null;
function _readEmptyComposerModelOverride() {{
  return _emptyComposerModelOverride;
}}
function api(url) {{
  requests.push(url);
  return new Promise((resolve, reject) => pending.push({{resolve, reject}}));
}}
eval(source);
function snapshot() {{
  return {{
    desktop: {{
      hidden: nodes.providerQuotaChip.hidden,
      label: nodes.providerQuotaChipLabel.textContent,
      title: nodes.providerQuotaChip.title,
    }},
    mobile: {{
      hidden: nodes.composerMobileQuotaAction.hidden,
      display: nodes.composerMobileQuotaAction.style.display,
      label: nodes.composerMobileQuotaLabel.textContent,
      title: nodes.composerMobileQuotaAction.title,
    }},
  }};
}}
async function main() {{
  {body}
}}
main().then(result => process.stdout.write(JSON.stringify(result))).catch(error => {{
  console.error(error && error.stack || String(error));
  process.exit(1);
}});
"""
    result = subprocess.run(
        [NODE, "-e", script],
        check=False,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    if result.returncode:
        raise RuntimeError(f"Node quota harness failed:\n{result.stderr}")
    return json.loads(result.stdout)


def _seed_stale_codex() -> str:
    return """
nodes.providerQuotaChip.hidden = false;
nodes.providerQuotaChip.title = 'OpenAI Codex — 42% remaining';
nodes.providerQuotaChipLabel.textContent = '42%';
nodes.composerMobileQuotaAction.hidden = false;
nodes.composerMobileQuotaAction.style.display = '';
nodes.composerMobileQuotaAction.title = 'OpenAI Codex — 42% remaining';
nodes.composerMobileQuotaLabel.textContent = '42%';
"""


def test_refresh_scopes_request_to_explicit_session_provider_and_clears_codex_dom_first():
    report = _run_quota_scenario(
        _seed_stale_codex()
        + """
const request = refreshProviderQuotaIndicator('ollama-cloud');
return {requests, beforeResponse: snapshot(), pendingCount: pending.length};
"""
    )

    assert report["requests"] == ["/api/provider/quota?provider=ollama-cloud"]
    assert report["pendingCount"] == 1
    assert report["beforeResponse"]["desktop"] == {"hidden": True, "label": "", "title": ""}
    assert report["beforeResponse"]["mobile"] == {
        "hidden": True,
        "display": "",
        "label": "",
        "title": "",
    }


def test_unsupported_selected_provider_keeps_desktop_and_mobile_quota_controls_hidden():
    report = _run_quota_scenario(
        _seed_stale_codex()
        + """
const request = refreshProviderQuotaIndicator('ollama-cloud');
pending[0].resolve({status: 'unsupported', quota: null, account_limits: null});
await request;
return snapshot();
"""
    )

    assert report["desktop"] == {"hidden": True, "label": "", "title": ""}
    assert report["mobile"] == {"hidden": True, "display": "", "label": "", "title": ""}


def test_late_codex_quota_response_cannot_overwrite_newer_provider_state():
    report = _run_quota_scenario(
        """
const codex = refreshProviderQuotaIndicator('openai-codex');
const ollama = refreshProviderQuotaIndicator('ollama-cloud');
pending[1].resolve({status: 'unsupported', quota: null, account_limits: null});
await ollama;
pending[0].resolve({
  status: 'available',
  provider: 'openai-codex',
  display_name: 'OpenAI Codex',
  message: 'Account usage loaded',
  account_limits: {windows: [{remaining_percent: 50}]},
});
await codex;
return {requests, finalState: snapshot()};
"""
    )

    assert report["requests"] == [
        "/api/provider/quota?provider=openai-codex",
        "/api/provider/quota?provider=ollama-cloud",
    ]
    assert report["finalState"]["desktop"] == {"hidden": True, "label": "", "title": ""}
    assert report["finalState"]["mobile"] == {
        "hidden": True,
        "display": "",
        "label": "",
        "title": "",
    }


def test_disabled_setting_invalidates_any_inflight_quota_response():
    report = _run_quota_scenario(
        """
const request = refreshProviderQuotaIndicator('openai-codex');
window._showQuotaChip = false;
const disabled = refreshProviderQuotaIndicator('ollama-cloud');
pending[0].resolve({
  status: 'available',
  provider: 'openai-codex',
  display_name: 'OpenAI Codex',
  account_limits: {windows: [{remaining_percent: 50}]},
});
await Promise.all([request, disabled]);
return {requests, finalState: snapshot()};
"""
    )

    assert report["requests"] == ["/api/provider/quota?provider=openai-codex"]
    assert report["finalState"]["desktop"] == {"hidden": True, "label": "", "title": ""}
    assert report["finalState"]["mobile"] == {
        "hidden": True,
        "display": "",
        "label": "",
        "title": "",
    }


def test_current_quota_provider_falls_back_to_empty_composer_override():
    report = _run_quota_scenario(
        """
// Simulate an empty composer with a non-default provider selected.
_emptyComposerModelOverride = {model: 'ollama-cloud/llama-3.3', model_provider: 'ollama-cloud'};
const provider = _currentQuotaProvider();
const request = refreshProviderQuotaIndicator(provider);
return {requests, provider, pendingCount: pending.length};
"""
    )

    assert report["provider"] == "ollama-cloud"
    assert report["requests"] == ["/api/provider/quota?provider=ollama-cloud"]


def test_current_quota_provider_prefers_session_provider_over_empty_composer_override():
    report = _run_quota_scenario(
        """
// Active session takes priority over the empty-composer override.
S.session = {session_id: 's1', model_provider: 'anthropic'};
_emptyComposerModelOverride = {model: 'ollama-cloud/llama-3.3', model_provider: 'ollama-cloud'};
const provider = _currentQuotaProvider();
return {provider};
"""
    )

    assert report["provider"] == "anthropic"


def test_current_quota_provider_does_not_fall_back_past_an_active_session():
    report = _run_quota_scenario(
        """
S.session = {session_id: 's1', model_provider: null};
_emptyComposerModelOverride = {model: 'ollama-cloud/llama-3.3', model_provider: 'ollama-cloud'};
return {provider: _currentQuotaProvider()};
"""
    )

    assert report["provider"] is None


def test_current_quota_provider_returns_null_when_no_session_and_no_override():
    report = _run_quota_scenario(
        """
const provider = _currentQuotaProvider();
return {provider};
"""
    )

    assert report["provider"] is None


def test_active_context_sync_refreshes_only_when_provider_identity_changes():
    report = _run_quota_scenario(
        """
S._bootReady = false;
S.session = {session_id: 's1', model_provider: 'openai-codex'};
_syncProviderQuotaForActiveContext();
const beforeBoot = requests.length;

S._bootReady = true;
_syncProviderQuotaForActiveContext();
_syncProviderQuotaForActiveContext();
S.session = {session_id: 's2', model_provider: 'anthropic'};
_syncProviderQuotaForActiveContext();
S.session = null;
_emptyComposerModelOverride = {model: 'llama-3.3', model_provider: 'ollama-cloud'};
_syncProviderQuotaForActiveContext();
_emptyComposerModelOverride = null;
_syncProviderQuotaForActiveContext();

return {beforeBoot, requests, pendingCount: pending.length};
"""
    )

    assert report == {
        "beforeBoot": 0,
        "requests": [
            "/api/provider/quota?provider=openai-codex",
            "/api/provider/quota?provider=anthropic",
            "/api/provider/quota?provider=ollama-cloud",
            "/api/provider/quota",
        ],
        "pendingCount": 4,
    }


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    return source[start_at : source.index(end, start_at)]


def test_provider_quota_refresh_follows_each_authoritative_provider_transition():
    ui_js = UI_JS.read_text(encoding="utf-8")
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    messages = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

    topbar = _between(ui_js, "function syncTopbar(){", "function msgContent")
    empty_topbar = _between(topbar, "if(!S.session){", "const sessionTitle")
    assert "_syncProviderQuotaForActiveContext()" in empty_topbar
    assert topbar.rstrip().endswith("_syncProviderQuotaForActiveContext();\n}")

    onchange = _between(boot, "$('modelSelect').onchange=async()=>", "$('msg').addEventListener")
    active_assign = onchange.index("S.session.model_provider=modelState.model_provider||null;")
    active_sync = onchange.index("syncTopbar()")
    update = onchange.index("await api('/api/session/update'")
    assert active_assign < active_sync < update

    empty_branch = _between(onchange, "if(!S.session){", "if(typeof _rememberPendingSessionModel")
    assert "_syncProviderQuotaForActiveContext()" in empty_branch

    load = _between(sessions, "async function loadSession", "activeStreamId=S.session.active_stream_id")
    pending_model = load.index("_applyPendingSessionModelForSession(sid)")
    first_topbar = load.index("syncTopbar()")
    assert pending_model < first_topbar

    new_session = _between(sessions, "async function newSession", "function _clearStuckSessionOnBoot")
    server_session = new_session.index("S.session=data.session;S.messages=data.session.messages||[];")
    new_topbar = new_session.index("syncTopbar();renderMessages();")
    assert server_session < new_topbar

    # Direct refreshes are reserved for explicit force-refresh events. Normal
    # state transitions converge on the de-duplicated active-context helper.
    assert "refreshProviderQuotaIndicator(" not in boot
    assert "refreshProviderQuotaIndicator(" not in sessions
    assert "refreshProviderQuotaIndicator(" not in messages

    batch_clear = _between(sessions, "if(S.session&&ids.includes(S.session.session_id)){", "const remaining=await api")
    single_clear = _between(sessions, "if(S.session&&S.session.session_id===sid){", "const remaining=await api")
    terminal_clear = _between(messages, "if(e&&e.status===404){", "const conflictActiveStream")
    assert "_syncProviderQuotaForActiveContext()" in batch_clear
    assert "_syncProviderQuotaForActiveContext()" in single_clear
    assert "_syncProviderQuotaForActiveContext()" in terminal_clear

    boot_prefix = boot[: boot.index("const saved=urlSession||savedLocal;")]
    assert "refreshProviderQuotaIndicator();" not in boot_prefix


def test_reviewer_identified_direct_session_transitions_reach_sync_topbar():
    ui_js = UI_JS.read_text(encoding="utf-8")
    panels = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    messages = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")

    start_correction = _between(
        messages,
        "if(startData&&startData.effective_model && S.session){",
        "if(S.session&&typeof startData.pending_started_at==='number')",
    )
    assert start_correction.count("syncTopbar()") == 2
    assert "S.session.model_provider=startData.effective_model_provider||S.session.model_provider||null;" in start_correction
    assert "S.session.model_provider=startData.effective_model_provider;" in start_correction

    new_file = _between(ui_js, "async function promptNewFile", "async function promptNewFolder")
    new_folder = _between(ui_js, "async function promptNewFolder", "async function uploadPendingFiles")
    workspace_prompt = _between(panels, "async function promptWorkspacePath", "async function switchToWorkspace")
    workspace_switch = _between(panels, "async function switchToWorkspace", "// ── Profile panel + dropdown")
    for transition in (new_file, new_folder, workspace_prompt, workspace_switch):
        assert "S.session=r.session" in transition
        assert "syncTopbar()" in transition


def test_restored_empty_session_keeps_provider_through_boot_demotion():
    assert NODE is not None
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
    ui_js = UI_JS.read_text(encoding="utf-8")
    remember_override = _between(
        sessions,
        "function _rememberEmptyComposerModelOverride",
        "let _newSessionWorkspaceAnnouncementClearTimer",
    )
    clear_override = "if(typeof _clearEmptyComposerModelOverride==='function') _clearEmptyComposerModelOverride();"
    assert clear_override in sessions
    load_session = sessions[sessions.index("async function loadSession") :]
    load_transition = _between(
        load_session,
        "S.session=data.session;",
        "// Loading a real existing session abandons",
    )
    direct_load_refresh = "if(typeof refreshProviderQuotaIndicator==='function') void refreshProviderQuotaIndicator(S.session.model_provider||null);"
    assert direct_load_refresh not in sessions
    quota_provider = _between(ui_js, "function _currentQuotaProvider", "window.addEventListener('visibilitychange'")
    refresh_helper = _between(boot, "const _syncQuotaForBootContext=()=>", "const urlSession")
    demotion = _between(
        boot,
        "if(S.session && (S.session.message_count||0) === 0 && !_restoredInFlight && !_restoredHasDraft){",
        "// Restore the panel from localStorage when the session has a workspace.",
    )
    script = f"""
const source = {json.dumps(_quota_region())};
const requests = [];
const pending = [];
const window = {{_showQuotaChip: true, addEventListener: () => {{}}}};
const localStorage = {{getItem: () => null}};
const S = {{session: null, messages: []}};
const _emptyComposerModelOverrideHost = window;
function makeNode() {{
  return {{
    hidden: false,
    title: '',
    textContent: '',
    style: {{display: ''}},
    removeAttribute(name) {{ if(name === 'title') this.title = ''; }},
  }};
}}
const nodes = {{
  providerQuotaChip: makeNode(),
  providerQuotaChipLabel: makeNode(),
  composerMobileQuotaAction: makeNode(),
  composerMobileQuotaLabel: makeNode(),
  emptyState: makeNode(),
}};
function $(id) {{ return nodes[id] || null; }}
{remember_override}
{quota_provider}
function api(url) {{
  requests.push(url);
  return new Promise((resolve, reject) => pending.push({{resolve, reject}}));
}}
eval(source);
async function _maybeBindFreshDefaultWorkspaceSession() {{}}
function _isCompactWorkspaceViewport() {{ return false; }}
function syncTopbar() {{}}
function syncWorkspacePanelState() {{}}
async function renderSessionList() {{}}
async function _finalizeComposerPrefillOnBoot() {{}}
function startGatewaySSE() {{}}
{refresh_helper}
async function run(session) {{
  window._emptyComposerModelOverride = {{model: 'stale-model', model_provider: 'stale-provider'}};
  const data = {{session}};
  {load_transition}
  const _restoredInFlight = false;
  const _restoredHasDraft = false;
  const prefillIntent = null;
  let _workspacePanelMode = null;
  {demotion}
}}
function snapshot() {{
  return {{
    desktop: {{hidden: nodes.providerQuotaChip.hidden, label: nodes.providerQuotaChipLabel.textContent}},
    mobile: {{hidden: nodes.composerMobileQuotaAction.hidden, label: nodes.composerMobileQuotaLabel.textContent}},
  }};
}}
async function main() {{
  await run({{session_id: 'saved-empty', message_count: 0, model: 'llama-3.3', model_provider: 'ollama-cloud'}});
  const retainedRequests = requests.splice(0);
  const retainedPending = pending.splice(0);
  retainedPending[0].resolve({{
    status: 'available', display_name: 'Ollama Cloud',
    account_limits: {{windows: [{{remaining_percent: 25}}]}},
  }});
  await Promise.all(retainedPending);
  const retained = {{requests: retainedRequests, finalState: snapshot()}};
  nodes.providerQuotaChip.hidden = false;
  nodes.providerQuotaChipLabel.textContent = 'old';
  nodes.composerMobileQuotaAction.hidden = false;
  nodes.composerMobileQuotaLabel.textContent = 'old';
  await run({{session_id: 'saved-empty-default', message_count: 0, model: 'default-model', model_provider: null}});
  const fallback = {{requests: requests.splice(0)}};
  return {{retained, fallback}};
}}
main().then(report => process.stdout.write(JSON.stringify(report))).catch(error => {{
  console.error(error && error.stack || String(error));
  process.exit(1);
}});
"""
    result = subprocess.run(
        [NODE, "-e", script],
        check=False,
        text=True,
        capture_output=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["retained"]["requests"] == [
        "/api/provider/quota?provider=ollama-cloud",
    ]
    assert report["retained"]["finalState"] == {
        "desktop": {"hidden": False, "label": "25%"},
        "mobile": {"hidden": False, "label": "25%"},
    }
    assert report["fallback"]["requests"] == ["/api/provider/quota"]


def test_visibility_and_settings_refresh_use_current_quota_provider_helper():
    ui_js = UI_JS.read_text(encoding="utf-8")
    panels_js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    # visibilitychange in ui.js uses _currentQuotaProvider
    vis_region = _between(ui_js, "window.addEventListener('visibilitychange'", ");")
    assert "_currentQuotaProvider()" in vis_region

    # Settings toggle in panels.js uses _currentQuotaProvider
    toggle_region = _between(panels_js, "showQuotaChipCb.addEventListener('change'", "_schedulePreferencesAutosave();")
    assert "_currentQuotaProvider()" in toggle_region
