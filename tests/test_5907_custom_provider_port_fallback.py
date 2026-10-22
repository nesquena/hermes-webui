"""Regression coverage for PR #5907 — the metadata-less fallback must mirror
the shared server grammar.

The 2026-07-12 gate review of PR #5907 asked for two things on the client
fallback path (``static/ui.js::_providerFromModelValue``):

1. Replace the ``lastIndexOf(':')`` / first-segment heuristic with the
   right-to-left parse used by ``api.config._parse_provider_qualified_model_id``
   so ``localhost:<port>`` custom slugs and colon-bearing model names survive
   when a dropdown option carries NO ``data-provider`` metadata (pre-catalog
   load, hand-typed custom model).
2. Add no-catalog tests with and without a colon-bearing model.

The server half of that review (``_resolve_compatible_session_model_state``
re-splitting an exact ``requested_provider`` prefix) was fixed separately
(#5979), so this PR only carries the client fallback + its coverage.

These tests run the REAL ``static/ui.js`` functions in node — no option
metadata, no slug catalog — and compare every case against the live Python
parser, so the two layers cannot drift apart again. The expected results are
NOT hard-coded: each parse case is asserted against
``api.config._parse_provider_qualified_model_id`` itself, which is the single
grammar for ``@provider:model`` values (#1776, #1948, #2047, #6722, #7182).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.config import _parse_provider_qualified_model_id

ROOT = Path(__file__).resolve().parents[1]
UI_JS = ROOT / "static" / "ui.js"
NODE = shutil.which("node")


# ---------------------------------------------------------------------------
# Source-level guards (fast, no node) — lock the single-grammar shape in place.
# ---------------------------------------------------------------------------

def _function_body(name: str) -> str:
    src = UI_JS.read_text(encoding="utf-8")
    start = src.index(f"function {name}(")
    return src[start : src.index("\nfunction ", start + 1)]


def test_provider_fallback_is_right_to_left():
    """The fallback must split at the LAST colon (right-to-left), not the first."""
    body = _function_body("_providerFromModelValue")
    assert "lastIndexOf(':')" in body
    # The old heuristic split the custom slug at its FIRST colon.
    assert "afterCustom.indexOf(':')" not in body


def test_provider_fallback_reuses_the_hostport_grammar():
    """Peel-back must be gated by the ported host:port check (numeric port on
    localhost/dotted host), mirroring _custom_slug_rest_looks_like_host_port."""
    body = _function_body("_providerFromModelValue")
    assert "/^\\d+$/.test(portSegment)" in body
    assert "port>=1&&port<=65535" in body
    assert "host.toLowerCase()==='localhost'" in body


def test_option_provider_fallback_delegates_to_the_shared_grammar():
    """_getOptionProviderId must not carry its own colon heuristic anymore."""
    body = _function_body("_getOptionProviderId")
    assert "return _providerFromModelValue(value);" in body
    assert "afterCustom" not in body


# ---------------------------------------------------------------------------
# Behavioral tests in node — the REAL functions from ui.js, no metadata.
# ---------------------------------------------------------------------------

_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[1], 'utf8');

function extractFunction(source, name) {
  const marker = 'function ' + name + '(';
  const start = source.indexOf(marker);
  if (start < 0) throw new Error('not found: ' + name);
  const brace = source.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < source.length; i++) {
    if (source[i] === '{') depth += 1;
    else if (source[i] === '}') { depth -= 1; if (depth === 0) return source.slice(start, i + 1); }
  }
  throw new Error('unterminated: ' + name);
}

for (const name of ['_providerFromModelValue',
                    '_getOptionProviderId', '_modelStateForSelect']) {
  eval(extractFunction(uiSrc, name));
}

function makeOption(value, provider) {
  const o = { value: value, dataset: {} };
  if (provider) o.dataset.provider = provider;
  return o;
}

const out = { parse: {}, select: {} };

// --- 1. Pure fallback parses (no option, no slug catalog) -------------------
const parseCases = {
  simple_slug_plain: '@custom:litellm-proxy:gpt-x',
  simple_slug_colon_model: '@custom:litellm-proxy:qwen3.6:27b-vision',
  simple_slug_tagged_model: '@custom:backup:model-a:free',
  hostport_localhost_plain_model: '@custom:localhost:11434:llama3.2',
  hostport_localhost_colon_model: '@custom:localhost:11434:qwen3.6:27b-vision',
  hostport_dotted_host: '@custom:proxy.internal:8443:Qwen3-235B',
  hostport_ip: '@custom:10.8.71.41:8080:qwen3.6:27b-vision',
  hostport_no_model: '@custom:localhost:11434',
  single_segment_custom: '@custom:backup',
  peel_three_segments: '@custom:litellm-proxy:a:b:c',
  bare_host_port_multi_colon_model: '@custom:vps:8080:qwen:free',
  port_boundary_65535: '@custom:localhost:65535:m',
  port_out_of_range_65536: '@custom:localhost:65536:m',
  port_zero: '@custom:localhost:0:m',
  empty_slug: '@custom:',
  noncustom_tagged: '@openrouter:vendor/model:free',
  noncustom_ollama_tag: '@ollama:qwen3.8:27b-mtp-q8_0',
  noncustom_plain: '@openai:gpt-4o',
};
for (const [label, value] of Object.entries(parseCases)) {
  out.parse[label] = { value: value, provider: _providerFromModelValue(value) };
}

// The metadata-less option path must produce the SAME provider as the value
// parse (single grammar invariant through _getOptionProviderId).
const optionCases = {
  option_fallback_colon_model: ['@custom:litellm-proxy:qwen3.6:27b-vision', null],
  option_fallback_hostport: ['@custom:localhost:11434:qwen3.6:27b-vision', null],
  option_metadata_wins: ['@custom:localhost:11434:qwen3.6:27b-vision', 'custom:localhost:11434'],
};
for (const [label, [value, provider]] of Object.entries(optionCases)) {
  out.parse[label] = { value: value, provider: _getOptionProviderId(makeOption(value, provider)) };
}

// Empty / non-qualified inputs stay empty.
out.parse.bare_model = { value: 'plain-model', provider: _providerFromModelValue('plain-model') };
out.parse.no_colon = { value: '@openai', provider: _providerFromModelValue('@openai') };
out.parse.empty = { value: '', provider: _providerFromModelValue('') };

// --- 2. _modelStateForSelect: data-provider path and hand-typed path --------
// Catalog option WITH metadata: provider comes from data-provider, and the
// duplicated custom prefix is stripped from the model.
{
  const value = '@custom:localhost:11434:qwen3.6:27b-vision';
  const o = makeOption(value, 'custom:localhost:11434');
  const sel = { options: [o] };
  out.select.dataprovider_hostport = _modelStateForSelect(sel, value);
}
// Hand-typed value with NO matching option: the fallback parse must keep the
// host:port provider and the colon-bearing model intact.
{
  const value = '@custom:localhost:11434:qwen3.6:27b-vision';
  const sel = { options: [makeOption('@openai:gpt-4o', 'openai')] };
  out.select.hand_typed_hostport = _modelStateForSelect(sel, value);
}
// Hand-typed simple-slug custom provider with a colon-bearing model.
{
  const value = '@custom:litellm-proxy:qwen3.6:27b-vision';
  const sel = { options: [makeOption('@openai:gpt-4o', 'openai')] };
  out.select.hand_typed_simple_slug = _modelStateForSelect(sel, value);
}

process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_fallback_parses_agree_with_the_shared_server_parser():
    """Every metadata-less parse must equal _parse_provider_qualified_model_id."""
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    out = json.loads(proc.stdout)

    for label, case in out["parse"].items():
        expected = _parse_provider_qualified_model_id(case["value"])
        expected_provider = expected[1] if expected else ""
        assert case["provider"] == expected_provider, (
            f"{label}: JS fallback parsed {case['value']!r} to provider "
            f"{case['provider']!r} but the shared server parser says "
            f"{expected_provider!r} — the layers have drifted apart"
        )

    # The review's named scenarios, asserted explicitly (against the shared
    # parser, so the expectations track the server grammar automatically):
    parser = _parse_provider_qualified_model_id
    # Colon-bearing model on a simple custom slug: model must stay whole.
    assert parser(out["parse"]["simple_slug_colon_model"]["value"]) == (
        "qwen3.6:27b-vision", "custom:litellm-proxy",
    )
    # host:port custom slug WITHOUT a colon-bearing model: port must survive.
    assert parser(out["parse"]["hostport_localhost_plain_model"]["value"]) == (
        "llama3.2", "custom:localhost:11434",
    )
    assert parser(out["parse"]["hostport_localhost_colon_model"]["value"]) == (
        "qwen3.6:27b-vision", "custom:localhost:11434",
    )
    assert parser(out["parse"]["hostport_ip"]["value"]) == (
        "qwen3.6:27b-vision", "custom:10.8.71.41:8080",
    )
    # Non-custom providers keep tagged model names intact.
    assert parser(out["parse"]["noncustom_ollama_tag"]["value"]) == (
        "qwen3.8:27b-mtp-q8_0", "ollama",
    )
    assert parser(out["parse"]["noncustom_tagged"]["value"]) == (
        "vendor/model:free", "openrouter",
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_model_state_survives_colon_models_end_to_end():
    """_modelStateForSelect must route colon-bearing custom models correctly
    both with option metadata (authoritative) and hand-typed (fallback)."""
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    out = json.loads(proc.stdout)

    # data-provider path: metadata wins, custom prefix is stripped from model.
    dp = out["select"]["dataprovider_hostport"]
    assert dp["model_provider"] == "custom:localhost:11434"
    assert dp["model"] == "qwen3.6:27b-vision"

    # Hand-typed host:port value, no matching option: fallback keeps the port.
    ht = out["select"]["hand_typed_hostport"]
    assert ht["model_provider"] == "custom:localhost:11434"
    assert ht["model"] == "qwen3.6:27b-vision"

    # Hand-typed simple slug with colon-bearing model.
    hs = out["select"]["hand_typed_simple_slug"]
    assert hs["model_provider"] == "custom:litellm-proxy"
    assert hs["model"] == "qwen3.6:27b-vision"

    # And the provider the browser would send must be exactly what the shared
    # server parser derives from the same value — end-to-end agreement.
    value = "@custom:localhost:11434:qwen3.6:27b-vision"
    assert _parse_provider_qualified_model_id(value)[1] == ht["model_provider"]
