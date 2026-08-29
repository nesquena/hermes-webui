"""Regression coverage for provider-aware duplicate model selection (#6131)."""

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
const preferredProvider = process.argv[2] || '';

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
    'return',
    'throw',
    'case',
    'delete',
    'typeof',
    'void',
    'new',
    'in',
    'of',
    'yield',
    'await',
  ].includes(token);
}

function skipQuotedLiteral(source, index, quote) {
  for (let i = index + 1; i < source.length; i += 1) {
    if (source[i] === '\\') {
      i += 1;
      continue;
    }
    if (source[i] === quote) return i;
  }
  throw new Error('unterminated string literal');
}

function skipTemplateLiteral(source, index) {
  for (let i = index + 1; i < source.length; i += 1) {
    if (source[i] === '\\') {
      i += 1;
      continue;
    }
    if (source[i] === '`') return i;
  }
  throw new Error('unterminated template literal');
}

function skipLineComment(source, index) {
  const end = source.indexOf('\n', index + 2);
  return end < 0 ? source.length - 1 : end;
}

function skipBlockComment(source, index) {
  const end = source.indexOf('*/', index + 2);
  if (end < 0) throw new Error('unterminated block comment');
  return end + 1;
}

function skipRegexLiteral(source, index) {
  let inClass = false;
  for (let i = index + 1; i < source.length; i += 1) {
    if (source[i] === '\\') {
      i += 1;
      continue;
    }
    if (source[i] === '[') inClass = true;
    else if (source[i] === ']') inClass = false;
    else if (source[i] === '/' && !inClass) {
      while (/[A-Za-z]/.test(source[i + 1] || '')) i += 1;
      return i;
    }
  }
  throw new Error('unterminated regex literal');
}

function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = source.indexOf('{', source.indexOf(')', start));
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    const ch = source[i];
    const next = source[i + 1];
    if (ch === '"' || ch === "'") i = skipQuotedLiteral(source, i, ch);
    else if (ch === '`') i = skipTemplateLiteral(source, i);
    else if (ch === '/' && next === '/') i = skipLineComment(source, i);
    else if (ch === '/' && next === '*') i = skipBlockComment(source, i);
    else if (ch === '/' && canStartRegexLiteral(source, i)) i = skipRegexLiteral(source, i);
    else if (ch === '{') depth += 1;
    else if (ch === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error('unterminated: ' + name);
}

function assertExtractorSkipsLexicalBraces() {
  const trickySource = [
    'function trickyExtractorTarget() {',
    "  const stringValue = '}';",
    '  const templateValue = `raw } ${1 + 1}`;',
    '  const regexValue = /}/;',
    '  // }',
    '  /* { */',
    '  return /}/.test(stringValue) ? templateValue : regexValue;',
    '}',
    'function afterTarget() { return 2; }',
  ].join('\n');
  const extracted = extractFunction(trickySource, 'trickyExtractorTarget');
  if (!extracted.includes('return /}/.test') || extracted.includes('afterTarget')) {
    throw new Error('extractFunction did not ignore lexical braces');
  }
}

assertExtractorSkipsLexicalBraces();

eval([
  '_getOptionProviderId',
  '_providerFromModelValue',
  '_modelStateForSelect',
  '_captureModelDropdownSelection',
  '_findModelInDropdown',
  '_applyModelToDropdown',
].map(name => extractFunction(uiSrc, name)).join('\n'));

globalThis._refreshOpenModelDropdown = () => {};
globalThis.syncSettingsModelChip = () => {};

const options = [
  {
    value: 'z-ai/glm-5.2',
    textContent: 'GLM-5.2 via Z.AI',
    dataset: {},
    parentElement: {tagName: 'OPTGROUP', dataset: {provider: 'zai'}},
  },
  {
    value: 'z-ai/glm-5.2',
    textContent: 'GLM-5.2 via NVIDIA',
    dataset: {},
    parentElement: {tagName: 'OPTGROUP', dataset: {provider: 'nvidia'}},
  },
];

let selectedIndex = 0;
for (const option of options) {
  Object.defineProperty(option, 'selected', {
    get() { return options[selectedIndex] === option; },
    set(value) { if (value) selectedIndex = options.indexOf(option); },
  });
}

const select = {
  id: 'settingsModel',
  options,
  get selectedIndex() { return selectedIndex; },
  set selectedIndex(value) { selectedIndex = Number(value); },
  get selectedOptions() {
    return selectedIndex >= 0 ? [options[selectedIndex]] : [];
  },
  get value() {
    return selectedIndex >= 0 ? options[selectedIndex].value : '';
  },
  set value(value) {
    selectedIndex = options.findIndex(option => option.value === value);
  },
};

const before = _modelStateForSelect(select, select.value);
const applied = _applyModelToDropdown('z-ai/glm-5.2', select, preferredProvider);
const after = _modelStateForSelect(select, select.value);
const captured = _captureModelDropdownSelection(select);

process.stdout.write(JSON.stringify({
  before,
  applied,
  after,
  captured,
  selectedIndex,
  selectedProvider: _getOptionProviderId(select.selectedOptions[0]),
  optionProviders: options.map(option => _getOptionProviderId(option)),
}));
"""


def _run_driver(preferred_provider: str) -> dict:
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS), preferred_provider],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_model_dropdown_preserves_preferred_provider_for_duplicate_value():
    result = _run_driver("nvidia")

    assert result["before"]["model_provider"] == "zai"
    assert result["applied"] == "z-ai/glm-5.2"
    assert result["selectedIndex"] == 1
    assert result["selectedProvider"] == "nvidia"
    assert result["after"] == {
        "model": "z-ai/glm-5.2",
        "model_provider": "nvidia",
    }
    assert result["captured"] == result["after"]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_model_dropdown_does_not_fabricate_missing_provider_option():
    result = _run_driver("anthropic")

    assert result["applied"] == "z-ai/glm-5.2"
    assert result["selectedIndex"] == 0
    assert result["selectedProvider"] == "zai"
    assert result["after"]["model_provider"] == "zai"
    assert result["captured"] == result["after"]
    assert result["optionProviders"] == ["zai", "nvidia"]
