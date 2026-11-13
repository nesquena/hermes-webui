"""#6506 release-stage follow-ups (gate 2026-10-06): quota chip identity and the profile-switch override.

1. A same-profile, same-provider refetch (switching between two conversations on one provider, tab return) keeps the
   numbers visible while the request is pending instead of blanking the chip (senior review SHOULD-FIX 2).
2. A provider change still clears the chip immediately (the PR's own contract).
3. The active profile is part of the quota identity: switching profile with the same session/provider issues a new
   request and clears the old account's numbers at once (senior review SHOULD-FIX 3).
4. A successful profile switch drops the retained empty-composer model override, so New Chat / Send cannot post the
   old profile's model (Codex CORE: base posts DeepSeek, the PR head posted Codex until this fix).
The quota checks run the real static/ui.js quota region in a Node harness (same pattern as
tests/test_provider_quota_session_identity.py).
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
PANELS_JS = ROOT / "static" / "panels.js"
NODE = shutil.which("node")


def _quota_region() -> str:
    source = UI_JS.read_text(encoding="utf-8")
    start = source.index("// ── Ambient provider quota indicator")
    return source[start:source.index("// Dynamic model labels", start)]


def _run(body: str) -> dict:
    if NODE is None:
        pytest.skip("node not on PATH")
    script = f"""
const source = {json.dumps(_quota_region())};
const requests = [];
const pending = [];
function makeNode() {{
  return {{hidden: true, title: '', textContent: '', style: {{display: ''}},
          removeAttribute(name) {{ if (name === 'title') this.title = ''; }}}};
}}
const nodes = {{providerQuotaChip: makeNode(), providerQuotaChipLabel: makeNode(),
               composerMobileQuotaAction: makeNode(), composerMobileQuotaLabel: makeNode()}};
function $(id) {{ return nodes[id] || null; }}
const window = {{_showQuotaChip: true, addEventListener: () => {{}}}};
const document = {{visibilityState: 'visible'}};
const S = {{session: null, _bootReady: true, activeProfile: 'alpha'}};
function _readEmptyComposerModelOverride() {{ return null; }}
function api(url) {{ requests.push(url); return new Promise((resolve, reject) => pending.push({{resolve, reject}})); }}
eval(source);
function available(pct) {{
  return {{status: 'available', provider: 'openai-codex', display_name: 'OpenAI Codex', message: 'loaded',
          account_limits: {{windows: [{{remaining_percent: pct}}]}}}};
}}
const tick = () => new Promise(r => setTimeout(r, 0));
function chip() {{ return {{hidden: nodes.providerQuotaChip.hidden, label: nodes.providerQuotaChipLabel.textContent}}; }}
async function main() {{ {body} }}
main().then(r => process.stdout.write(JSON.stringify(r))).catch(e => {{ console.error(e && e.stack || String(e)); process.exit(1); }});
"""
    result = subprocess.run([NODE, "-e", script], check=False, text=True, capture_output=True, cwd=ROOT)
    if result.returncode:
        raise RuntimeError(f"Node quota harness failed:\n{result.stderr}")
    return json.loads(result.stdout)


SEED = """
S.session = {session_id: 's1', model_provider: 'openai-codex'};
_syncProviderQuotaForActiveContext();
pending.shift().resolve(available(42)); await tick();
const seeded = chip();
"""


def test_same_account_refetch_keeps_numbers_visible_while_pending():
    out = _run(SEED + """
S.session = {session_id: 's2', model_provider: 'openai-codex'};
_syncProviderQuotaForActiveContext();
const during = chip();
pending.shift().resolve(available(40)); await tick();
return {seeded, during, after: chip(), requests: requests.length};
""")
    assert out["seeded"] == {"hidden": False, "label": "42%"}
    assert out["requests"] == 2, "a conversation switch still refreshes"
    assert out["during"] == {"hidden": False, "label": "42%"}, "no blank while the same account refetches"
    assert out["after"] == {"hidden": False, "label": "40%"}


def test_provider_change_clears_immediately():
    out = _run(SEED + """
S.session = {session_id: 's3', model_provider: 'deepseek'};
_syncProviderQuotaForActiveContext();
return {seeded, during: chip(), last: requests[requests.length - 1]};
""")
    assert out["seeded"]["label"] == "42%"
    assert out["during"] == {"hidden": True, "label": ""}
    assert out["last"].endswith("?provider=deepseek")


def test_profile_change_refreshes_and_clears_the_old_account():
    out = _run(SEED + """
const before = requests.length;
S.activeProfile = 'beta';
_syncProviderQuotaForActiveContext();
return {seeded, during: chip(), newRequests: requests.length - before};
""")
    assert out["seeded"]["label"] == "42%"
    assert out["newRequests"] == 1, "same session and provider under another profile is a new quota identity"
    assert out["during"] == {"hidden": True, "label": ""}


def test_unspecified_provider_refetch_always_clears():
    # No session and no composer override: the request carries no provider and the server answers for its current
    # default, which may have changed (Codex delta review, 2026-10-06).
    out = _run("""
S.session = null;
_syncProviderQuotaForActiveContext();
pending.shift().resolve(available(42)); await tick();
const seeded = chip();
void refreshProviderQuotaIndicator(null);
return {seeded, during: chip(), last: requests[requests.length - 1]};
""")
    assert out["seeded"] == {"hidden": False, "label": "42%"}
    assert out["during"] == {"hidden": True, "label": ""}
    assert out["last"] == "/api/provider/quota"


def test_successful_profile_switch_drops_the_empty_composer_override():
    panels = PANELS_JS.read_text(encoding="utf-8")
    start = panels.index("const data = await api('/api/profile/switch'")
    guard = panels.index("if (_switchGen !== _profileSwitchGeneration) return false;", start)
    clear = panels.index("_clearEmptyComposerModelOverride()", guard)
    assign = panels.index("S.activeProfile = data.active || name;", guard)
    assert guard < clear < assign
