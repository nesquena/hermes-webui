"""Regression coverage for #5567 (frontend vector): model_provider contamination.

Two distinct bugs produce the identical "Provider 'ollama'…no API key" symptom:

  * #5577 (shipped) — backend HERMES_HOME clobber → init reads a FOREIGN
    profile's config.yaml. Fixed at the agent reader (context-local home override).
  * THIS one — the frontend resolver `_modelStateForSelect` read
    `sel.selectedOptions[0]` (the DOM's currently-selected option) instead of the
    option whose value matches the requested model. During a profile/tab switch
    the dropdown transiently still has the PREVIOUS profile's default selected
    (e.g. an ollama model), so a send in that window stamps `ollama` onto a model
    it doesn't own. That wrong provider is persisted into the session JSON
    (`model_provider`) and, because `_modelProviderForSend` reads the stored value
    FIRST, re-sent on every subsequent turn — a sticky brick.

This suite exercises the JS in Node with a mock <select> so it fails without the
fix and passes with it, plus covers the sticky-session repair helper.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
SESSIONS_JS = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Source-level guards (fast, no node) — lock the shape of the fix in place.
# ---------------------------------------------------------------------------

def test_model_state_no_longer_blindly_reads_selected_option():
    """The bug was `const opt=sel&&sel.selectedOptions&&sel.selectedOptions[0];`
    used unconditionally as the provider source. That exact unconditional read
    must be gone; the resolver must match the option by value."""
    src = _read(UI_JS)
    start = src.index("function _modelStateForSelect(sel, modelId)")
    body = src[start : src.index("function _captureModelDropdownSelection", start)]
    # It must resolve by matching option value.
    assert "Array.from(sel.options).find(o=>String(o.value||'')===value)" in body
    # It may still prefer the selected option, but ONLY when it matches the value.
    assert "selected&&String(selected.value||'')===value" in body
    # The unconditional "trust selectedOptions[0]" read must not survive.
    assert "const opt=sel&&sel.selectedOptions&&sel.selectedOptions[0];" not in body


def test_repair_helper_exists_and_is_wired_into_session_load():
    ui = _read(UI_JS)
    assert "function _repairContaminatedSessionModelProvider(session)" in ui
    sess = _read(SESSIONS_JS)
    # Wired at the load chokepoint, right after S.session is assigned.
    assert "_repairContaminatedSessionModelProvider(S.session)" in sess


# ---------------------------------------------------------------------------
# Behavioral test in Node — the real repro the maintainer asked for.
# ---------------------------------------------------------------------------

_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') { depth -= 1; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error('unterminated: ' + name);
}

// Dependencies pulled in verbatim from ui.js so we test the real code.
eval(extractFunction(uiSrc, '_getOptionProviderId'));
eval(extractFunction(uiSrc, '_providerFromModelValue'));
eval(extractFunction(uiSrc, '_modelStateForSelect'));
eval(extractFunction(uiSrc, '_repairContaminatedSessionModelProvider'));

// Minimal mock <select> option: dataset carries the provider (as the real
// optgroup/option markup does via data-provider).
function opt(value, provider) {
  return { value: value, dataset: provider ? { provider: provider } : {}, parentElement: null };
}

const args = JSON.parse(process.argv[2]);
const results = {};

// --- Scenario 1: the tab-switch race. Dropdown still has the PREVIOUS profile's
//     ollama default selected, but we're resolving a kilo/* model. Provider must
//     NOT come out as ollama.
{
  const ollamaOpt = opt('qwen3.6:27b-mlx', 'ollama');
  const kiloOpt = opt('kilo/minimax/minimax-m3', 'kilocode');
  const sel = { options: [ollamaOpt, kiloOpt], selectedOptions: [ollamaOpt] };
  results.race = _modelStateForSelect(sel, 'kilo/minimax/minimax-m3');
}

// --- Scenario 2: same-value/different-provider collision. The user explicitly
//     selected the option (selectedOptions[0].value === requested value); that
//     exact pick must be preserved.
{
  const a = opt('shared-model', 'provider-a');
  const b = opt('shared-model', 'provider-b');
  const sel = { options: [a, b], selectedOptions: [b] };
  results.collision = _modelStateForSelect(sel, 'shared-model');
}

// --- Scenario 3: model not present in the (rebuilt) dropdown at all → null
//     provider, never the stale selected one.
{
  const stale = opt('qwen3.6:27b-mlx', 'ollama');
  const sel = { options: [stale], selectedOptions: [stale] };
  results.missing = _modelStateForSelect(sel, 'kilo/minimax/minimax-m3');
}

// --- Scenario 4: sticky-session repair. A session poisoned with ollama but whose
//     model string is kilo/* must have its stored provider dropped on load.
{
  const s = { model: 'kilo/minimax/minimax-m3', model_provider: 'ollama' };
  const changed = _repairContaminatedSessionModelProvider(s);
  results.repair = { changed: changed, provider_after: s.model_provider };
}

// --- Scenario 5: repair must NOT touch a legitimate custom-provider session that
//     routes a slash-prefixed vendor id.
{
  const s = { model: 'opencode_go/deepseek-v4', model_provider: 'custom:llm-proxy' };
  const changed = _repairContaminatedSessionModelProvider(s);
  results.repair_custom = { changed: changed, provider_after: s.model_provider };
}

// --- Scenario 6: repair must NOT fire when prefix agrees (alias-normalized).
{
  const s = { model: 'claude/sonnet', model_provider: 'anthropic' };
  const changed = _repairContaminatedSessionModelProvider(s);
  results.repair_alias = { changed: changed, provider_after: s.model_provider };
}

// --- Scenario 7: bare model name (no inferable prefix) → never repaired.
{
  const s = { model: 'gpt-4o', model_provider: 'ollama' };
  const changed = _repairContaminatedSessionModelProvider(s);
  results.repair_bare = { changed: changed, provider_after: s.model_provider };
}

// --- Scenario 8: a HEALTHY Nous session must never be repaired. Nous is an
//     aggregator (hermes-agent _AGGREGATOR_PROVIDERS) whose sessions persist the
//     RESOLVED pair: resolve_model_provider('@nous:anthropic/claude-opus-4.6')
//     unpacks to model 'anthropic/claude-opus-4.6' + provider 'nous' before the
//     session write. Treating the vendor prefix as a contradiction would null a
//     correct provider and re-route the session (e.g. to openrouter).
{
  const s = { model: 'anthropic/claude-opus-4.6', model_provider: 'nous' };
  const changed = _repairContaminatedSessionModelProvider(s);
  results.repair_nous = { changed: changed, provider_after: s.model_provider };
}

// --- Scenario 9: kilocode (also an aggregator) owns kilo/vendor/model ids; the
//     'kilo' prefix is not the literal string 'kilocode' but is NOT a
//     contradiction — the stored provider must survive the load repair.
{
  const s = { model: 'kilo/minimax/minimax-m3', model_provider: 'kilocode' };
  const changed = _repairContaminatedSessionModelProvider(s);
  results.repair_kilocode = { changed: changed, provider_after: s.model_provider };
}

process.stdout.write(JSON.stringify(results));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_model_provider_resolution_behavior():
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS), json.dumps({})],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    r = json.loads(proc.stdout)

    # Scenario 1 — the core bug: a tab-switch-race resolution must not attribute
    # the previous profile's ollama provider to a kilo/* model.
    assert r["race"]["model"] == "kilo/minimax/minimax-m3"
    assert r["race"]["model_provider"] != "ollama"
    assert r["race"]["model_provider"] == "kilocode"

    # Scenario 2 — explicit same-value pick is preserved.
    assert r["collision"]["model_provider"] == "provider-b"

    # Scenario 3 — missing model resolves to a null provider (backend re-infers),
    # never the stale selected ollama.
    assert r["missing"]["model_provider"] is None

    # Scenario 4 — poisoned session is repaired.
    assert r["repair"]["changed"] is True
    assert r["repair"]["provider_after"] is None

    # Scenario 5 — legitimate custom-provider cross-routing is untouched.
    assert r["repair_custom"]["changed"] is False
    assert r["repair_custom"]["provider_after"] == "custom:llm-proxy"

    # Scenario 6 — alias-agreeing prefix is untouched.
    assert r["repair_alias"]["changed"] is False
    assert r["repair_alias"]["provider_after"] == "anthropic"

    # Scenario 7 — bare model name is never repaired.
    assert r["repair_bare"]["changed"] is False
    assert r["repair_bare"]["provider_after"] == "ollama"

    # Scenario 8 — a healthy Nous aggregator session (persisted resolved shape:
    # vendor-prefixed model + provider 'nous') is NEVER repaired; nulling it
    # would let the backend re-route the session to a different provider.
    assert r["repair_nous"]["changed"] is False
    assert r["repair_nous"]["provider_after"] == "nous"

    # Scenario 9 — kilocode aggregator session keeps its stored provider even
    # though the 'kilo' prefix is not the literal provider id.
    assert r["repair_kilocode"]["changed"] is False
    assert r["repair_kilocode"]["provider_after"] == "kilocode"
