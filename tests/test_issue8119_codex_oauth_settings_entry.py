"""Codex OAuth login reachable from Settings -> Providers, not only the first-run wizard (#8119)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function


ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
ONBOARDING_JS = (ROOT / "static" / "onboarding.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _codex_flow_block() -> str:
    start = ONBOARDING_JS.index("/* ── Codex OAuth device-code flow ── */")
    end = ONBOARDING_JS.index("/* ── Anthropic / Claude Code credential-link flow ── */")
    return ONBOARDING_JS[start:end]


_DRIVER = r"""
const fs = require('fs');
const scenario = JSON.parse(process.argv[2]);
const src = fs.readFileSync(process.argv[3], 'utf8');

class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.style = {};
    this.className = ''; this.textContent = ''; this.innerHTML = ''; this.disabled = false;
    this.listeners = {};
    this.classList = { toggle() {} };
  }
  appendChild(c) { this.children.push(c); return c; }
  addEventListener(ev, fn) { this.listeners[ev] = fn; }
}
function walk(el, out = []) { out.push(el); el.children.forEach((c) => walk(c, out)); return out; }

globalThis.document = { createElement: (tag) => new El(tag) };
globalThis.t = (key, ...args) => (args.length ? key + ':' + args.join(',') : key);
globalThis.esc = (s) => String(s);
globalThis.jsArg = (s) => JSON.stringify(s);
globalThis.showToast = () => {};
globalThis.S = { activeProfile: scenario.activeProfile || 'default' };
const wizardFlow = new El('div');
const wizardBtn = new El('button');
globalThis.$ = (id) => (id === 'codexOAuthFlow' ? wizardFlow : id === 'codexOAuthBtn' ? wizardBtn : null);

const calls = [];
let pollStatus = 'pending';
globalThis.api = async (url, opts) => {
  calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  if (url === '/api/onboarding/oauth/start') {
    return { flow_id: 'f1', user_code: 'ABCD-1234', verification_uri: 'https://auth.openai.com/codex/device', poll_interval_seconds: 1 };
  }
  if (url.startsWith('/api/onboarding/oauth/poll')) return { status: pollStatus };
  return {};
};
const timers = [];
globalThis.setTimeout = (fn) => { timers.push(fn); return timers.length; };
globalThis.clearTimeout = () => {};
let providersReloads = 0;
let wizardReloads = 0;
globalThis.loadProvidersPanel = async () => { providersReloads += 1; };
globalThis.loadOnboardingWizard = async () => { wizardReloads += 1; };

eval(src);

(async () => {
  const result = { cards: {} };
  for (const p of scenario.providers) {
    const card = _buildProviderCard(p);
    const login = walk(card).find((el) => el.dataset.codexOauthLogin === '1');
    result.cards[p.id] = login
      ? {
          login: true,
          label: walk(login).find((el) => el.tag === 'button').textContent,
          note: walk(login).find((el) => el.className === 'provider-card-hint').textContent,
        }
      : { login: false };
  }

  if (scenario.clickCodex) {
    const card = _buildProviderCard(scenario.providers.find((p) => p.id === 'openai-codex'));
    const login = walk(card).find((el) => el.dataset.codexOauthLogin === '1');
    const btn = walk(login).find((el) => el.tag === 'button');
    await btn.listeners.click();
    const flow = login.children[login.children.length - 1];
    result.flowShowsCode = flow.innerHTML.includes('ABCD-1234');
    result.wizardFlowTouched = wizardFlow.innerHTML !== '';
    pollStatus = 'success';
    await timers.shift()();
    result.flowShowsSuccess = flow.innerHTML.includes('oauth_codex_success');
  }

  if (scenario.wizard) {
    await startCodexOAuth();
    result.wizardShowsCode = wizardFlow.innerHTML.includes('ABCD-1234');
    pollStatus = 'success';
    await timers.shift()();
  }

  result.calls = calls;
  result.providersReloads = providersReloads;
  result.wizardReloads = wizardReloads;
  process.stdout.write(JSON.stringify(result));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run(tmp_path, scenario):
    if NODE is None:
        pytest.skip("node is required to execute the provider card harness")
    src = "\n".join([
        _codex_flow_block(),
        extract_function(PANELS_JS, "_buildCodexOAuthLogin", prefix="function"),
        extract_function(PANELS_JS, "_buildProviderCard", prefix="function"),
    ])
    src_path = tmp_path / "src.js"
    src_path.write_text(src, encoding="utf-8")
    driver_path = tmp_path / "driver.js"
    driver_path.write_text(_DRIVER, encoding="utf-8")
    out = subprocess.run(
        [NODE, str(driver_path), json.dumps(scenario), str(src_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


def _oauth_provider(pid, **extra):
    base = {"id": pid, "display_name": pid, "is_oauth": True, "has_key": False, "key_source": "none", "models": []}
    base.update(extra)
    return base


def test_codex_card_offers_login_without_credentials_and_reconnect_with_them(tmp_path):
    # The no-credential state renders through the auth_error branch of the card.
    result = _run(tmp_path, {"providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")]})
    assert result["cards"]["openai-codex"] == {
        "login": True,
        "label": "oauth_login_codex",
        "note": "providers_codex_profile_note:default",
    }

    result = _run(tmp_path, {
        "activeProfile": "work",
        "providers": [_oauth_provider("openai-codex", has_key=True, key_source="oauth")],
    })
    assert result["cards"]["openai-codex"] == {
        "login": True,
        "label": "providers_codex_reconnect",
        "note": "providers_codex_profile_note:work",
    }


def test_login_action_is_codex_only_and_skips_config_yaml_tokens(tmp_path):
    result = _run(tmp_path, {
        "providers": [
            _oauth_provider("copilot"),
            _oauth_provider("nous"),
            _oauth_provider("anthropic"),
            _oauth_provider("openai-codex", has_key=True, key_source="config_yaml"),
        ],
    })
    assert result["cards"] == {
        "copilot": {"login": False},
        "nous": {"login": False},
        "anthropic": {"login": False},
        "openai-codex": {"login": False},
    }


def test_settings_login_runs_device_flow_and_refreshes_providers_not_wizard(tmp_path):
    result = _run(tmp_path, {
        "clickCodex": True,
        "providers": [_oauth_provider("openai-codex", auth_error="No Codex credentials stored.")],
    })
    assert result["calls"][0] == {"url": "/api/onboarding/oauth/start", "body": {"provider": "openai-codex"}}
    assert result["calls"][1]["url"] == "/api/onboarding/oauth/poll?flow_id=f1"
    assert result["flowShowsCode"] is True
    assert result["wizardFlowTouched"] is False
    assert result["flowShowsSuccess"] is True
    assert result["providersReloads"] == 1
    assert result["wizardReloads"] == 0


def test_wizard_login_still_renders_in_wizard_and_reloads_it(tmp_path):
    result = _run(tmp_path, {"wizard": True, "providers": []})
    assert result["wizardShowsCode"] is True
    assert result["wizardReloads"] == 1
    assert result["providersReloads"] == 0
