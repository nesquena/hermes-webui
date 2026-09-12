"""
Test coverage for Issue #6936: Ensure OpenRouter presets (@preset/<name>) and provider/model slash entries
are properly stripped of openrouter/ prefix by _modelStateForSelect and _ensureModelOptionInDropdown.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")


_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function isIdentifierChar(ch) {
  return /[A-Za-z0-9_$]/.test(ch || '');
}

function previousSignificantToken(source, index) {
  let i = index - 1;
  while (i >= 0 && /\s/.test(source[i])) i -= 1;
  if (i < 0) return '';
  if (!isIdentifierChar(source[i])) return source[i];
  const end = i + 1;
  while (i >= 0 && isIdentifierChar(source[i])) i -= 1;
  return source.slice(i + 1, end);
}

function canStartRegexLiteral(source, index) {
  const token = previousSignificantToken(source, index);
  if (!token) return true;
  if ('({[=,:;!&|?+-*~^<>'.includes(token)) return true;
  return [
    'return', 'throw', 'case', 'delete', 'typeof', 'void', 'new', 'in', 'of', 'yield', 'await'
  ].includes(token);
}

function skipQuotedLiteral(source, index, quote) {
  for (let i = index + 1; i < source.length; i += 1) {
    if (source[i] === '\\') { i += 1; continue; }
    if (source[i] === quote) return i;
  }
  return source.length;
}

function skipRegexLiteral(source, index) {
  let inCharClass = false;
  for (let i = index + 1; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === '\\') { i += 1; continue; }
    if (ch === '[') { inCharClass = true; continue; }
    if (ch === ']' && inCharClass) { inCharClass = false; continue; }
    if (ch === '/' && !inCharClass) {
      i += 1;
      while (i < source.length && isIdentifierChar(source[i])) i += 1;
      return i - 1;
    }
  }
  return source.length;
}

function extractFunction(source, fnName) {
  const marker = `function ${fnName}(`;
  const idx = source.indexOf(marker);
  if (idx < 0) throw new Error(`Function not found: ${fnName}`);
  let depth = 0;
  let bodyStart = -1;
  for (let i = idx; i < source.length; i += 1) {
    const ch = source[i];
    if (ch === "'" || ch === '"' || ch === '`') {
      i = skipQuotedLiteral(source, i, ch);
      continue;
    }
    if (ch === '/' && canStartRegexLiteral(source, i)) {
      i = skipRegexLiteral(source, i);
      continue;
    }
    if (ch === '{') {
      if (depth === 0) bodyStart = i;
      depth += 1;
    } else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(idx, i + 1);
    }
  }
  throw new Error(`Unterminated function: ${fnName}`);
}

global.window = global;
global._refreshOpenModelDropdown = function() {};
global.document = {
  createElement: (tag) => ({ tagName: tag.toUpperCase(), dataset: {}, parentElement: null })
};

eval(extractFunction(uiSrc, '_providerFromModelValue'));
eval(extractFunction(uiSrc, '_getOptionProviderId'));
eval(extractFunction(uiSrc, '_modelPickerOptionIdentity'));
eval(extractFunction(uiSrc, '_deduplicateModelPickerOptions'));
eval(extractFunction(uiSrc, '_providerSkipsModelMismatchWarning'));
eval(extractFunction(uiSrc, '_providerDefersMissingModelFallback'));
eval(extractFunction(uiSrc, '_findModelInDropdown'));
eval(extractFunction(uiSrc, '_applyModelToDropdown'));
eval(extractFunction(uiSrc, '_modelStateForSelect'));
eval(extractFunction(uiSrc, '_ensureModelOptionInDropdown'));

// Setup test DOM structures for OpenRouter presets and catalog models
const openrouterPresetOpt = {
  value: 'openrouter/@preset/deepseek-v4-flash',
  textContent: 'DeepSeek V4 Flash (Preset)',
  dataset: {},
  parentElement: { tagName: 'OPTGROUP', dataset: { provider: 'openrouter' } },
};
const openrouterCatalogOpt = {
  value: 'openrouter/anthropic/claude-3-5-sonnet',
  textContent: 'Claude 3.5 Sonnet',
  dataset: {},
  parentElement: { tagName: 'OPTGROUP', dataset: { provider: 'openrouter' } },
};

const options = [openrouterPresetOpt, openrouterCatalogOpt];
let selectedIndex = 0;
options.forEach((o, i) => {
  Object.defineProperty(o, 'selected', {
    get() { return selectedIndex === i; },
    set(v) { if (v) selectedIndex = i; }
  });
});

const select = {
  id: 'modelSelect',
  options,
  querySelectorAll() { return []; },
  appendChild(option) {
    option.parentElement = null;
    options.push(option);
  },
  get selectedOptions() { return selectedIndex >= 0 ? [options[selectedIndex]] : []; },
  get value() { return selectedIndex >= 0 ? options[selectedIndex].value : ''; },
  set value(val) { selectedIndex = options.findIndex(o => o.value === val); }
};

// Test 1: Selecting OpenRouter Preset
selectedIndex = 0;
const presetState = _modelStateForSelect(select, select.value);

// Test 2: Selecting OpenRouter Catalog Model
selectedIndex = 1;
const catalogState = _modelStateForSelect(select, select.value);

// Test 3: ensureModelOptionInDropdown with @preset/custom-preset for openrouter
const appliedPreset = _ensureModelOptionInDropdown('@preset/my-custom-preset', select, 'openrouter');
const appliedState = _modelStateForSelect(select, select.value);

process.stdout.write(JSON.stringify({
  presetState,
  catalogState,
  appliedPreset,
  appliedState,
}));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_openrouter_preset_and_catalog_model_id_stripping():
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    assert payload["presetState"] == {
        "model": "@preset/deepseek-v4-flash",
        "model_provider": "openrouter",
    }
    assert payload["catalogState"] == {
        "model": "anthropic/claude-3-5-sonnet",
        "model_provider": "openrouter",
    }
    assert payload["appliedState"]["model"] == "@preset/my-custom-preset"
    assert payload["appliedState"]["model_provider"] == "openrouter"
