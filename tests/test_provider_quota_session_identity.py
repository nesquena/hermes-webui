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


def test_current_quota_provider_returns_null_when_no_session_and_no_override():
    report = _run_quota_scenario(
        """
const provider = _currentQuotaProvider();
return {provider};
"""
    )

    assert report["provider"] is None


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    return source[start_at : source.index(end, start_at)]


def test_provider_quota_refresh_follows_each_authoritative_provider_transition():
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    sessions = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")

    onchange = _between(boot, "$('modelSelect').onchange=async()=>", "$('msg').addEventListener")
    active_assign = onchange.index("S.session.model_provider=modelState.model_provider||null;")
    active_refresh = onchange.index("refreshProviderQuotaIndicator(S.session.model_provider||null)")
    update = onchange.index("await api('/api/session/update'")
    assert active_assign < active_refresh < update

    empty_branch = _between(onchange, "if(!S.session){", "if(typeof _rememberPendingSessionModel")
    assert "refreshProviderQuotaIndicator(modelState.model_provider||null)" in empty_branch

    load = _between(sessions, "async function loadSession", "activeStreamId=S.session.active_stream_id")
    pending_model = load.index("_applyPendingSessionModelForSession(sid)")
    loaded_refresh = load.index("refreshProviderQuotaIndicator(S.session.model_provider||null)")
    first_topbar = load.index("syncTopbar()")
    assert pending_model < loaded_refresh < first_topbar

    new_session = _between(sessions, "async function newSession", "function _clearStuckSessionOnBoot")
    server_session = new_session.index("S.session=data.session;S.messages=data.session.messages||[];")
    new_refresh = new_session.index("refreshProviderQuotaIndicator(S.session.model_provider||null)")
    new_topbar = new_session.index("syncTopbar();renderMessages();")
    assert server_session < new_refresh < new_topbar

    boot_prefix = boot[: boot.index("const saved=urlSession||savedLocal;")]
    assert "refreshProviderQuotaIndicator();" not in boot_prefix


def test_empty_composer_boot_refresh_uses_retained_provider_or_default_fallback():
    assert NODE is not None
    boot = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    helper = _between(boot, "const _refreshQuotaForEmptyComposer=()=>", "const urlSession")
    script = f"""
function run(provider) {{
  const calls = [];
  const S = {{session: null}};
  const refreshProviderQuotaIndicator = value => calls.push(value);
  const _currentQuotaProvider = provider === undefined ? undefined : () => provider;
  const refresh = (function() {{
    {helper}
    return _refreshQuotaForEmptyComposer;
  }})();
  refresh();
  return calls;
}}
process.stdout.write(JSON.stringify({{retained: run('ollama-cloud'), fallback: run(undefined)}}));
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
    assert report["retained"] == ["ollama-cloud"]
    assert report["fallback"] == [None]


def test_visibility_and_settings_refresh_use_current_quota_provider_helper():
    ui_js = UI_JS.read_text(encoding="utf-8")
    panels_js = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    # visibilitychange in ui.js uses _currentQuotaProvider
    vis_region = _between(ui_js, "window.addEventListener('visibilitychange'", ");")
    assert "_currentQuotaProvider()" in vis_region

    # Settings toggle in panels.js uses _currentQuotaProvider
    toggle_region = _between(panels_js, "showQuotaChipCb.addEventListener('change'", "_schedulePreferencesAutosave();")
    assert "_currentQuotaProvider()" in toggle_region
