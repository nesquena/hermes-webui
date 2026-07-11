"""Regression coverage for PR #5907: custom provider port-preservation in fallback path.

When a custom provider's base_url contains a port (e.g. ``custom:proxy.internal:8443``)
and the model name itself contains colons (e.g. ``Qwen3-235B`` or ``qwen3.6:27b-vision``),
the value string passed to the frontend looks like::

    @custom:proxy.internal:8443:Qwen3-235B

The primary resolution path (``_modelStateForSelect``) reads the option's
``data-provider`` attribute, which carries the authoritative provider ID from the
backend — that path was fixed in an earlier commit and works correctly.

The FALLBACK path (``_providerFromModelValue``) is used when:
  - The select element is not available (programmatic callers without a live DOM).
  - The option markup lacks a ``data-provider`` attribute (older markup).
  - The slug cache (``window._customProviderSlugs``) has not loaded yet (boot/restore).

Previously, the fallback used ``lastIndexOf(':')`` which corrupted provider IDs
for any custom provider whose slug contained a colon. The heuristic fix improved
this for simple slugs but still DROPPED THE PORT for host:port providers —
returning ``custom:proxy.internal`` instead of ``custom:proxy.internal:8443``.

This test covers BOTH paths:
  1. ``data-provider`` path → provider must be ``custom:proxy.internal:8443``
  2. Fallback path (no data-provider, no slug cache) → same correct result

It also covers the slug-cache path with longest-first matching to prevent
shorter slugs from shadowing longer ones (e.g. ``custom:litellm`` vs
``custom:litellm-proxy``).
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
# Source-level guards (fast, no node) — lock the fix shape in place.
# ---------------------------------------------------------------------------

def test_fallback_preserves_port_in_host_slug():
    """The fallback must include the numeric port segment when the slug looks
    like host:port, not just return the host without the port."""
    src = _read(UI_JS)
    start = src.index("function _providerFromModelValue(")
    body = src[start : src.index("function _providerSkipsModelMismatchWarning", start)]
    # Must check for numeric port and include it.
    assert "/^\\d+$/.test(portSegment)" in body
    assert "potentialSlug+':'+portSegment" in body


def test_slug_sort_by_length_descending():
    """The slug cache matching must sort by length descending to prevent
    shorter slugs from matching before longer ones."""
    src = _read(UI_JS)
    start = src.index("function _providerFromModelValue(")
    body = src[start : src.index("function _providerSkipsModelMismatchWarning", start)]
    assert "sort((a,b)=>b.length-a.length)" in body


# ---------------------------------------------------------------------------
# Behavioral tests in Node — exercise the real functions from ui.js.
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

// Pull in the real functions from ui.js.
eval(extractFunction(uiSrc, '_getOptionProviderId'));
eval(extractFunction(uiSrc, '_providerFromModelValue'));
eval(extractFunction(uiSrc, '_modelStateForSelect'));

// Minimal mock option: dataset carries the provider (as real markup does).
function opt(value, provider) {
  return { value: value, dataset: provider ? { provider: provider } : {}, parentElement: null };
}

const results = {};

// --- Scenario 1: data-provider path for host:port custom provider + colon model.
//     The option has data-provider="custom:proxy.internal:8443" and the model
//     value contains colons. The provider must come from data-provider, not
//     from string-splitting.
{
  const o = opt('@custom:proxy.internal:8443:Qwen3-235B', 'custom:proxy.internal:8443');
  const sel = { options: [o], selectedOptions: [o] };
  results.dataprovider_hostport = _modelStateForSelect(sel, '@custom:proxy.internal:8443:Qwen3-235B');
}

// --- Scenario 2: FALLBACK path for host:port custom provider + colon model.
//     No data-provider attribute, no slug cache (window._customProviderSlugs
//     is not set). The heuristic must preserve the port.
{
  const o = opt('@custom:proxy.internal:8443:Qwen3-235B', null);
  const sel = { options: [o], selectedOptions: [o] };
  // Ensure no slug cache is available.
  if (typeof globalThis !== 'undefined') delete globalThis._customProviderSlugs;
  if (typeof window !== 'undefined') delete window._customProviderSlugs;
  results.fallback_hostport = _modelStateForSelect(sel, '@custom:proxy.internal:8443:Qwen3-235B');
}

// --- Scenario 3: FALLBACK path for IP:port custom provider + colon-in-model.
//     e.g. @custom:10.8.71.41:8080:qwen3.6:27b-vision
{
  const o = opt('@custom:10.8.71.41:8080:qwen3.6:27b-vision', null);
  const sel = { options: [o], selectedOptions: [o] };
  results.fallback_ipport = _modelStateForSelect(sel, '@custom:10.8.71.41:8080:qwen3.6:27b-vision');
}

// --- Scenario 4: slug-cache path with longest-first matching.
//     Both custom:litellm and custom:litellm-proxy are registered.
//     The value @custom:litellm-proxy:qwen3.6:27b-vision must match the
//     LONGER slug, not the shorter one.
{
  // Simulate window._customProviderSlugs being populated.
  globalThis._customProviderSlugs = ['custom:litellm', 'custom:litellm-proxy'];
  const provider = _providerFromModelValue('@custom:litellm-proxy:qwen3.6:27b-vision');
  results.slug_cache_longest_match = provider;
  delete globalThis._customProviderSlugs;
}

// --- Scenario 5: FALLBACK for simple slug + colon-in-model (no cache).
//     e.g. @custom:litellm-proxy:qwen3.6:27b-vision → custom:litellm-proxy
{
  const provider = _providerFromModelValue('@custom:litellm-proxy:qwen3.6:27b-vision');
  results.fallback_simple_slug = provider;
}

// --- Scenario 6: non-custom provider still works.
{
  const provider = _providerFromModelValue('@openrouter:vendor/model-name');
  results.non_custom = provider;
}

// --- Scenario 7: FALLBACK for host:port with NO model after the port.
//     e.g. @custom:proxy.internal:8443 → custom:proxy.internal (no port to peel)
{
  const provider = _providerFromModelValue('@custom:proxy.internal:8443');
  results.fallback_hostport_no_model = provider;
}

// --- Scenario 8: data-provider path for IP:port + colon-in-model.
{
  const o = opt('@custom:10.8.71.41:8080:qwen3.6:27b-vision', 'custom:10.8.71.41:8080');
  const sel = { options: [o], selectedOptions: [o] };
  results.dataprovider_ipport = _modelStateForSelect(sel, '@custom:10.8.71.41:8080:qwen3.6:27b-vision');
}

process.stdout.write(JSON.stringify(results));
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_custom_provider_port_preservation():
    """Both data-provider and fallback paths must preserve the port in
    host:port custom provider IDs when the model name contains colons."""
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"node driver failed: {proc.stderr}"
    r = json.loads(proc.stdout)

    # Scenario 1 — data-provider path: host:port + colon model.
    assert r["dataprovider_hostport"]["model_provider"] == "custom:proxy.internal:8443"
    assert r["dataprovider_hostport"]["model"] == "@custom:proxy.internal:8443:Qwen3-235B"

    # Scenario 2 — fallback path: host:port + colon model, no cache.
    assert r["fallback_hostport"]["model_provider"] == "custom:proxy.internal:8443", (
        f"Fallback dropped the port! Got: {r['fallback_hostport']['model_provider']}"
    )

    # Scenario 3 — fallback path: IP:port + colon-in-model, no cache.
    assert r["fallback_ipport"]["model_provider"] == "custom:10.8.71.41:8080", (
        f"Fallback dropped the port! Got: {r['fallback_ipport']['model_provider']}"
    )

    # Scenario 4 — slug cache longest-first matching.
    assert r["slug_cache_longest_match"] == "custom:litellm-proxy", (
        f"Shorter slug matched first! Got: {r['slug_cache_longest_match']}"
    )

    # Scenario 5 — fallback simple slug + colon model.
    assert r["fallback_simple_slug"] == "custom:litellm-proxy"

    # Scenario 6 — non-custom provider.
    assert r["non_custom"] == "openrouter"

    # Scenario 7 — fallback host:port with no model (no extra colon).
    assert r["fallback_hostport_no_model"] == "custom:proxy.internal"

    # Scenario 8 — data-provider path: IP:port + colon-in-model.
    assert r["dataprovider_ipport"]["model_provider"] == "custom:10.8.71.41:8080"
