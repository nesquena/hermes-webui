"""Regression tests for #7778 — cached highlight entries must track grammar state.

The bug
-------
The virtualized-rebuild cache (added for #7752, hardened for the
untokenized-autoloader case) keyed entries on ``language + "\\0" +
textContent`` with the highlighted ``innerHTML`` as the value, and the
writer refused to replace an existing key. A fence can be *partially*
tokenized: Prism's ``html`` grammar is already loaded, so the outer markup
yields ``class="token"`` spans (the token-form gate passes), while the
``javascript``/``css`` grammar the fence embeds is still being fetched by
the autoloader. The ``<script>``/``<style>`` body therefore stays plain
text.

Once that partially-tokenized HTML is cached it is re-applied on every
virtualized rebuild, and ``data-highlighted="1"`` makes the next-frame
post-process skip the block, so the embedded code stays un-tokenized for
the life of the page. Reviewer reproduction (headless Chromium, real Prism
1.29 + autoloader): HTML fence with ``<script>`` gives 16 tokens / 0
keyword spans on every rebuild where master re-highlights to 33 / 4; the
same with ``<style>`` and a later CSS fence (16 / 0 vs 29 / 2).

The fix
-------
Each cache entry stores ``{html, sig}`` where ``sig`` is the count of
loaded ``Prism.languages`` when the entry was written. ``_applyCachedCode
Highlights`` skips an entry whose signature is older than the current
grammar state, so the next-frame pass re-highlights the block with the
now-extended grammar, and ``_writeCodeHighlightCache`` overwrites the
entry instead of refusing an existing key.

These tests drive the REAL functions from ``static/ui.js`` with REAL Prism
(core + markup loaded, javascript/css pending then resolved) in node, and
pin:

  * Pre-fix: a rebuild applies the partially-tokenized cache entry and
    stamps ``data-highlighted="1"``; loading the javascript grammar
    afterwards never re-highlights the block.
  * Post-fix: the entry is recognized as stale (grammar signature grew),
    the rebuild leaves the block un-stamped so the next-frame pass runs,
    and after a real re-highlight the entry is overwritten with the
    fully-tokenized form.
  * The unrelated cases keep working: an unchanged grammar state still
    applies the cache synchronously (no #7752 flash regression), and a
    cache miss leaves the block untouched.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
FIXTURES = Path(__file__).parent / "fixtures" / "prism"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not UI_JS_PATH.exists(),
    reason="node or static/ui.js unavailable",
)


def _extract_function_body(src: str, header: str) -> str:
    """Slice one top-level ``function name(...) { ... }`` out of ui.js."""
    start = src.find(header)
    if start < 0:
        raise AssertionError(f"{header!r} not found in static/ui.js")
    i = src.index("{", start)
    depth = 1
    i += 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


_DRIVER_SRC = r"""
// Drives the REAL _codeHighlightCacheKey/_writeCodeHighlightCache/
// _applyCachedCodeHighlights/_prismGrammarSignature from static/ui.js against
// a tiny DOM stand-in and REAL Prism, reproducing the #7778 autoloader
// scenario: html grammar loaded, javascript grammar pending at first pass,
// resolved before a later rebuild.
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
const prismDir = process.argv[3];

// ── Real Prism (core + markup only; javascript/css are "still fetching") ──
global.window = { _prismPending: [] };
for (const f of ['prism-core.min.js', 'prism-markup.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
global.Prism = global.Prism || window.Prism;
function extract(name) {
  const start = uiSrc.indexOf('function ' + name + '(');
  if (start < 0) throw new Error(name + ' not found');
  let i = uiSrc.indexOf('{', start), depth = 1; i++;
  while (depth > 0 && i < uiSrc.length) {
    if (uiSrc[i] === '{') depth++;
    else if (uiSrc[i] === '}') depth--;
    i++;
  }
  return uiSrc.slice(start, i);
}
// standalone pieces the cache helpers depend on
eval(extract('_codeHighlightCacheKey'));
eval(extract('_writeCodeHighlightCache'));
eval(extract('_applyCachedCodeHighlights'));
eval(extract('_prismGrammarSignature'));
// The helpers close over module-scope state; re-create it here so the driver
// has a real cache Map to write into and read from.
const _CODE_HIGHLIGHT_TOKEN_RE = /<span\b[^>]*\bclass="[^"]*\btoken\b/i;
const _CODE_HIGHLIGHT_CACHE_MAX = 512;
const _codeHighlightCache = new Map();
// Re-evaluate with the state in scope (the extracted functions reference the
// module-level names above).
for (const name of ['_codeHighlightCacheKey', '_writeCodeHighlightCache',
                    '_applyCachedCodeHighlights', '_prismGrammarSignature']) {
  eval(extract(name));
}

// ── Minimal block/element stand-ins ────────────────────────────────────────
function makeBlock(cls, text, inner) {
  return {
    className: cls,
    textContent: text,
    _innerHTML: inner,
    dataset: {},
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; },
  };
}

const HTML_SOURCE =
  '<script>const answer = 1;</script>\n<p>hello</p>';
// Partially-tokenized form Prism produces when html is loaded but javascript
// is still pending: the outer markup carries token spans, the <script> body
// does not.
const PARTIAL_HTML =
  '<span class="token tag"><span class="token punctuation">&lt;</span>script' +
  '</span><span class="token punctuation">&gt;</span>const answer = 1;' +
  '<span class="token tag"><span class="token punctuation">&lt;/</span>' +
  'script<span class="token punctuation">&gt;</span></span>';
// Fully-tokenized form after the javascript grammar resolves.
const FULL_HTML =
  '<span class="token tag"><span class="token punctuation">&lt;</span>script' +
  '</span><span class="token script"><span class="token keyword">const</span>' +
  ' answer <span class="token operator">=</span> <span class="token number">1' +
  '</span>;</span><span class="token tag"><span class="token punctuation">' +
  '&lt;/</span>script<span class="token punctuation">&gt;</span></span>';

const out = { steps: [] };

// ── Step 1: first highlight pass (javascript grammar still pending) ────────
const sigBefore = _prismGrammarSignature();
out.grammarsBefore = sigBefore;
const block = makeBlock('language-html', HTML_SOURCE, PARTIAL_HTML);
block.dataset.highlighted = '1'; // highlightCode() stamps this post-pass
_writeCodeHighlightCache(block);
out.cachedAfterFirstWrite = _codeHighlightCache.has(
  _codeHighlightCacheKey(block));
// Inspect the raw entry shape.
{
  const entry = _codeHighlightCache.get(_codeHighlightCacheKey(block));
  out.entryIsObject = !!(entry && typeof entry === 'object' && 'html' in entry);
  out.entrySig = (entry && typeof entry === 'object') ? entry.sig : undefined;
}

// ── Step 2: rebuild BEFORE the grammar resolves ────────────────────────────
const rebuilt = makeBlock('language-html', HTML_SOURCE, '');
const container = { querySelectorAll: () => [rebuilt] };
out.appliedWhileFresh = _applyCachedCodeHighlights(container);
out.stampedWhileFresh = rebuilt.dataset.highlighted === '1';
out.keywordTokensWhileFresh =
  (rebuilt.innerHTML.match(/token keyword/g) || []).length;

// ── Step 3: the autoloader resolves the javascript grammar ─────────────────
// javascript depends on clike, exactly as Prism's autoloader resolves it.
for (const f of ['prism-clike.min.js', 'prism-javascript.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
const sigAfter = _prismGrammarSignature();
out.grammarsAfter = sigAfter;

// ── Step 4: rebuild AFTER the grammar resolves ─────────────────────────────
const rebuilt2 = makeBlock('language-html', HTML_SOURCE, '');
const container2 = { querySelectorAll: () => [rebuilt2] };
out.appliedWhileStale = _applyCachedCodeHighlights(container2);
out.stampedWhileStale = rebuilt2.dataset.highlighted === '1';

// Step 5: the next-frame pass re-highlights the block with the extended
// grammar (simulated: Prism now embeds the script body as a token span),
// then the writer must overwrite the stale entry.
rebuilt2.innerHTML = FULL_HTML;
rebuilt2.dataset.highlighted = '1';
_writeCodeHighlightCache(rebuilt2);
{
  const entry = _codeHighlightCache.get(_codeHighlightCacheKey(rebuilt2));
  out.entryHasKeywordTokensAfterOverwrite =
    /token keyword/.test(entry && entry.html);
  out.entrySigAfterOverwrite = (entry && typeof entry === 'object')
    ? entry.sig : undefined;
}

// Step 6: a later rebuild now applies the richer entry synchronously again.
const rebuilt3 = makeBlock('language-html', HTML_SOURCE, '');
const container3 = { querySelectorAll: () => [rebuilt3] };
out.appliedAfterOverwrite = _applyCachedCodeHighlights(container3);
out.keywordTokensAfterOverwrite =
  (rebuilt3.innerHTML.match(/token keyword/g) || []).length;

// ── Control: a cache miss leaves the block untouched ───────────────────────
const otherBlock = makeBlock('language-python', 'print(1)', '');
out.appliedOnMiss = _applyCachedCodeHighlights({
  querySelectorAll: () => [otherBlock],
});

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("code_paint_7778") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return str(p)


def _run(driver_path) -> dict:
    import json as _json

    result = subprocess.run(
        [NODE, driver_path, str(UI_JS_PATH), str(FIXTURES)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed: {result.stderr}")
    return _json.loads(result.stdout)


class TestGrammarStateSignature:
    """#7778: the cache must not lock a partially-tokenized fence."""

    def test_entry_carries_grammar_signature(self, driver_path):
        """Entries must record the grammar state they were written under."""
        out = _run(driver_path)
        assert out["entryIsObject"] is True, (
            "cache entries must be {html, sig} objects so a later grammar "
            "load can invalidate them (#7778)."
        )
        assert out["entrySig"] == out["grammarsBefore"], (
            "the entry signature must equal the grammar count at write time."
        )

    def test_fresh_entry_still_applies_synchronously(self, driver_path):
        """No #7752 regression: an unchanged grammar state still applies."""
        out = _run(driver_path)
        assert out["appliedWhileFresh"] == 1
        assert out["stampedWhileFresh"] is True

    def test_grammar_load_is_observable(self, driver_path):
        """The javascript grammar resolution grew the signature."""
        out = _run(driver_path)
        assert out["grammarsAfter"] > out["grammarsBefore"], (
            "loading prism-javascript must change the grammar signature."
        )

    def test_stale_entry_is_skipped(self, driver_path):
        """A stale entry must NOT be applied nor stamp data-highlighted."""
        out = _run(driver_path)
        assert out["appliedWhileStale"] == 0, (
            "an entry written under an older grammar state must be skipped so "
            "the next-frame pass re-highlights the block (#7778)."
        )
        assert out["stampedWhileStale"] is False, (
            "the block must stay unstamped; otherwise the rAF post-process "
            "would skip it forever (#7778)."
        )

    def test_rehighlight_overwrites_stale_entry(self, driver_path):
        """After the re-highlight the richer form replaces the stale one."""
        out = _run(driver_path)
        assert out["entryHasKeywordTokensAfterOverwrite"] is True, (
            "_writeCodeHighlightCache must overwrite an existing key so the "
            "fully-tokenized form replaces the partially-tokenized one "
            "(#7778)."
        )
        assert out["entrySigAfterOverwrite"] == out["grammarsAfter"]

    def test_subsequent_rebuild_uses_richer_entry(self, driver_path):
        """Once overwritten, the sync pass applies the fully-tokenized HTML."""
        out = _run(driver_path)
        assert out["appliedAfterOverwrite"] == 1
        assert out["keywordTokensAfterOverwrite"] > 0

    def test_cache_miss_leaves_block_untouched(self, driver_path):
        """A block with no cache entry is untouched by the sync pass."""
        out = _run(driver_path)
        assert out["appliedOnMiss"] == 0


class TestSourceContract:
    """Pin the implementation shape the reviewer's scenario depends on."""

    def test_writer_does_not_refuse_existing_key(self):
        src = UI_JS_PATH.read_text(encoding="utf-8")
        body = _extract_function_body(src, "function _writeCodeHighlightCache(")
        assert 'if(_codeHighlightCache.has(cacheKey)) return;' not in body, (
            "_writeCodeHighlightCache must not bail out on an existing key — "
            "the re-highlighted form has to replace the stale entry (#7778)."
        )
        assert "_prismGrammarSignature" in body, (
            "the writer must stamp the grammar signature on the entry (#7778)."
        )
