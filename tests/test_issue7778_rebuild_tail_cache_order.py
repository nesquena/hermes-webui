"""#7778 round-6: the rebuild tail must apply the per-block cache BEFORE the
synchronous highlight, and that ORDER must be pinned by observed behaviour.

The 2026-10-19 maintainer review's section 3 (SHOULD-FIX) said:

    `_applyCachedCodeHighlights(inner)` selects `pre code:not([data-highlighted])`.
    The main rebuild tail runs sync `highlightCode(inner)` first, which stamps
    `data-highlighted` on every block, and `_applyCachedCodeHighlights` is the
    only caller. [...] Either apply it before the sync highlight and show a
    measured benefit, or remove it from this PR.

The production side was repaired (the cache pass now runs before the sync
highlight). The round-5 test file pins that the two calls are both PRESENT in
the rebuild tail and that the cache pass precedes the deferred rAF post-process
— but both of those are structural pins. They stay green if the two calls are
SWAPPED, which is exactly the bug the maintainer reported: with the sync
highlight first, every block is stamped `data-highlighted='1'` and the cache
pass's `:not([data-highlighted])` selector matches nothing, so the per-block
cache never applies.

This module closes that hole with a behavioural oracle:

  * the DRIVER executes the production rebuild-tail region verbatim — the
    contiguous production source between `let cacheHtml='';` and the
    `const recoveryOwnership=` capture that follows it. The region is sliced
    with its statements in production order, so the order under test IS the
    shipped order; the driver never re-assembles the calls into its own
    sequence;
  * the driver runs it against a DOM stand-in holding ONE code block that
    already has a CACHED, tokenised entry whose HTML carries a unique marker,
    so "the cache was applied" and "Prism re-highlighted the block" are
    distinguishable by the block's final HTML;
  * `Prism.highlightElement` is instrumented, so "a second Prism pass ran on
    this block" is directly observed.

Observable states:

  * shipped order (cache first): the block keeps the CACHED html (marker
    present) and Prism.highlightElement was NOT called on it — the cache hit
    avoided a second Prism pass;
  * swapped order (the maintainer's bug): the sync highlight runs first, Prism
    re-highlights the block, the cache pass then finds nothing to restore and
    the marker is gone;
  * cache pass removed: same as the swapped order.

The region must contain both calls; if a future refactor moves either out of
the region, the precondition assertion fails loudly rather than silently
passing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
FIXTURES = Path(__file__).parent / "fixtures" / "prism"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not UI_JS_PATH.exists() or not FIXTURES.exists(),
    reason="node, static/ui.js or the Prism fixtures are unavailable",
)

# The fence under test. Its Prism output contains token spans, so the cache
# writer accepts it.
_RAW_FENCE = "<p>hello</p>"

# The unique marker appended to the CACHED html. It proves the block's final
# HTML came from the cache rather than from a fresh Prism pass.
_CACHE_MARKER = "<!--7778-CACHE-APPLIED-->"

_DRIVER_SRC = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
const prismDir = process.argv[3];
const RAW_FENCE = process.argv[4];
const CACHE_MARKER = process.argv[5];

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = uiSrc.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = uiSrc.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < uiSrc.length) {
    if (uiSrc[i] === '{') depth++;
    else if (uiSrc[i] === '}') depth--;
    i++;
  }
  return uiSrc.slice(start, i);
}

// ── Real Prism, evaluated BEFORE any window binding (TDZ) ──────────────────
global.window = { _prismPending: [] };
for (const f of ['prism-core.min.js', 'prism-markup.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
global.Prism = global.Prism || global.window.Prism;

// ── Instrument Prism.highlightElement so a second Prism pass is observable ─
const prismCalls = [];
const realHighlightElement = Prism.highlightElement;
Prism.highlightElement = function (element, async, callback) {
  prismCalls.push(element);
  return realHighlightElement.call(this, element, async, callback);
};

// ── DOM stand-in ────────────────────────────────────────────────────────────
function makePre() {
  return {
    nodeName: 'PRE',
    tagName: 'PRE',
    className: '',
    _attrs: {},
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n); },
    setAttribute(n, v) { this._attrs[n] = String(v); },
    getAttribute(n) { return this._attrs[n]; },
  };
}

function makeCodeBlock(cls, text, inner) {
  const pre = makePre();
  const code = {
    nodeName: 'CODE',
    tagName: 'CODE',
    className: cls,
    textContent: text,
    _innerHTML: inner,
    dataset: {},
    parentElement: pre,
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = String(v); },
  };
  return code;
}

// The container mirrors production's selector semantics: the cache pass and
// the sync highlight both select `pre code:not([data-highlighted])`.
function makeContainer(block) {
  return {
    querySelectorAll(sel) {
      const s = String(sel);
      const wantNotMarked = s.indexOf('pre code:not([data-highlighted])') >= 0;
      if (wantNotMarked) return block.dataset.highlighted === '1' ? [] : [block];
      return [];
    },
  };
}

// ── Module state the production helpers read ───────────────────────────────
const _CODE_HIGHLIGHT_TOKEN_RE = /<span\b[^>]*\bclass="[^"]*\btoken\b/i;
const _CODE_HIGHLIGHT_CACHE_MAX = 512;
const _codeHighlightCache = new Map();

function _maybeRegisterPrismCompleteHook(){}
function addCopyButtons(){}
function initTreeViews(){}
function _captureMessageRecoveryOwnership(){ return { programmatic:false, jumpOwner:false }; }
function _scrollAfterMessageRender(){}

// ── The REAL production helpers ────────────────────────────────────────────
eval(extractFunc('_codeHighlightCacheKey'));
eval(extractFunc('_writeCodeHighlightCache'));
eval(extractFunc('_applyCachedCodeHighlights'));
eval(extractFunc('_prismGrammarSignature'));
eval(extractFunc('highlightCode'));

// ── The production rebuild-tail region, sliced VERBATIM ────────────────────
// Everything between `let cacheHtml='';` and the `const recoveryOwnership=`
// capture that follows it. The statements stay in production order, so the
// order this module pins is the shipped order.
const REGION_START = "let cacheHtml='';";
const REGION_END = 'const recoveryOwnership=';
const iStart = uiSrc.indexOf(REGION_START);
if (iStart < 0) throw new Error('rebuild-tail region start not found: ' + REGION_START);
const iEnd = uiSrc.indexOf(REGION_END, iStart);
if (iEnd < 0) throw new Error('rebuild-tail region end not found: ' + REGION_END);
const REGION = uiSrc.slice(iStart, iEnd);

// Precondition: the region must contain BOTH calls, otherwise this module
// would be pinning something other than the order under test.
const regionHasCachePass = REGION.indexOf('_applyCachedCodeHighlights(inner)') >= 0;
const regionHasSyncHighlight = REGION.indexOf('highlightCode(inner)') >= 0;

// The region is executed as-is. The identifiers it reads are supplied here.
const runRegion = new Function(
  'inner', 'sid', 'INFLIGHT', 'hasTransientTranscriptUi',
  'highlightCode', 'addCopyButtons', 'initTreeViews',
  '_applyCachedCodeHighlights', '_captureMessageRecoveryOwnership',
  '_scrollAfterMessageRender', 'preserveScroll', 'scrollSnapshot',
  REGION
);

// ── Seed the cache with a CACHED, tokenised entry for this block ───────────
const seed = makeCodeBlock('language-html', RAW_FENCE, '');
seed.innerHTML = Prism.highlight(RAW_FENCE, Prism.languages.html, 'html') + CACHE_MARKER;
_writeCodeHighlightCache(seed);
const cacheKey = _codeHighlightCacheKey(seed);
const seeded = _codeHighlightCache.get(cacheKey);

// ── The rebuilt block: same source, NOT yet highlighted ────────────────────
const block = makeCodeBlock('language-html', RAW_FENCE, RAW_FENCE);
const container = makeContainer(block);

runRegion(
  container, 'sid-A', {}, false,
  highlightCode, addCopyButtons, initTreeViews,
  _applyCachedCodeHighlights, _captureMessageRecoveryOwnership,
  _scrollAfterMessageRender, false, null
);

const out = {};
out.regionHasCachePass = regionHasCachePass;
out.regionHasSyncHighlight = regionHasSyncHighlight;
out.cacheSeeded = !!seeded;
out.finalHtml = block.innerHTML;
out.cacheMarkerPresent = block.innerHTML.indexOf(CACHE_MARKER) >= 0;
out.blockMarked = block.dataset.highlighted === '1';
out.prismCallsOnBlock = prismCalls.filter((c) => c === block).length;
out.prismCallsTotal = prismCalls.length;

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("code_paint_7778_r6") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return p


def _run(driver_path) -> dict:
    result = subprocess.run(
        [NODE, str(driver_path), str(UI_JS_PATH), str(FIXTURES), _RAW_FENCE, _CACHE_MARKER],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_the_rebuild_tail_region_contains_both_calls(driver_path):
    """Precondition: the sliced region must hold both calls under test."""
    out = _run(driver_path)
    assert out["regionHasCachePass"] is True, (
        "the rebuild-tail region no longer contains the "
        "_applyCachedCodeHighlights(inner) call — this module would be "
        "pinning something other than the order under test"
    )
    assert out["regionHasSyncHighlight"] is True, (
        "the rebuild-tail region no longer contains the highlightCode(inner) "
        "call — this module would be pinning something other than the order "
        "under test"
    )


def test_the_cached_block_is_restored_without_a_second_prism_pass(driver_path):
    """The cache pass must run BEFORE the sync highlight, so the cache hit
    survives and no second Prism pass is spent on the block.

    If the sync highlight ran first it would stamp `data-highlighted='1'` on
    the block, the cache pass's `:not([data-highlighted])` selector would then
    match nothing, and the per-block cache would never apply — the maintainer's
    2026-10-19 section-3 finding.
    """
    out = _run(driver_path)
    assert out["cacheSeeded"] is True, (
        "the fixture's cache entry was not written — nothing to observe"
    )
    assert out["cacheMarkerPresent"] is True, (
        "the rebuilt block does NOT carry the cached HTML — the per-block cache "
        "was not applied, so the sync highlight ran first and stamped "
        "data-highlighted='1' before the cache pass could restore the block"
    )
    assert out["prismCallsOnBlock"] == 0, (
        f"Prism.highlightElement ran on the cached block {out['prismCallsOnBlock']} "
        "time(s) — the cache hit did not avoid a second Prism pass, which means "
        "the sync highlight ran before the cache pass"
    )
    assert out["blockMarked"] is True, (
        "the rebuilt block is not stamped data-highlighted='1' after the "
        "rebuild tail ran"
    )
