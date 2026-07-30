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

This ships the RESOLVER fix (stops all NEW contamination at the write site). A
separate follow-up (tracked issue) will repair sessions ALREADY poisoned before
this shipped, done at the backend chat-start boundary where session + profile
identity is unambiguous — NOT via the shared frontend #modelSelect DOM, which a
three-round gate proved is a cross-session race surface that re-manufactures the
very contamination it tries to fix.

This suite exercises the JS in Node with a mock <select> so it fails without the
fix and passes with it.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
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

// Minimal mock <select> option: dataset carries the provider (as the real
// optgroup/option markup does via data-provider).
function opt(value, provider) {
  return { value: value, dataset: provider ? { provider: provider } : {}, parentElement: null };
}

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

// --- Scenario 4: the resolver picks the matching option's provider even when a
//     DIFFERENT option is currently selected (the general form of the fix).
{
  const selectedOther = opt('gpt-4o', 'openai');
  const wanted = opt('claude-opus-4.6', 'anthropic');
  const sel = { options: [selectedOther, wanted], selectedOptions: [selectedOther] };
  results.match_by_value = _modelStateForSelect(sel, 'claude-opus-4.6');
}

// --- Scenario 5: a 'default' provider attribution yields null (not 'default').
{
  const o = opt('some-model', 'default');
  const sel = { options: [o], selectedOptions: [o] };
  results.default_provider = _modelStateForSelect(sel, 'some-model');
}

process.stdout.write(JSON.stringify(results));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_model_provider_resolution_behavior():
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS)],
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

    # Scenario 4 — resolver matches by value, not by which option is selected.
    assert r["match_by_value"]["model"] == "claude-opus-4.6"
    assert r["match_by_value"]["model_provider"] == "anthropic"

    # Scenario 5 — a 'default' provider attribution collapses to null.
    assert r["default_provider"]["model_provider"] is None
