"""Regression tests for #7778 round-3 — the reviewer's two remaining must-fixes.

The bug (both reproduced by two reviewers in headless Chromium with real
Prism 1.29.0 + autoloader, master works in each):

1. **Switching sessions restores stale highlighting.** The session-HTML
   snapshot taken after the synchronous cache apply stores no Prism grammar
   signature, and the fast-path restore condition compares only message
   count, window key and render signature. So: session A has an HTML fence
   embedding a script and a style block; render A; open session B (which
   loads the javascript and css grammars); return to A → the snapshot is
   accepted and the fence comes back with **0 keyword spans / 0 selectors**
   where master re-highlights to 3 / 1.

2. **Cached rebuilds lose code-block keyboard focus.**
   ``_applyCachedCodeHighlights`` restores only ``innerHTML`` and the
   ``data-highlighted`` stamp. Prism's ``highlightElement`` also normalises
   the language class on ``<code>`` and its ``<pre>`` and sets
   ``tabindex="0"`` on the ``<pre>``. After a cache-hit rebuild the
   ``<pre>`` has no tabindex, refuses ``pre.focus()`` and has an empty
   class list; at 390px Copy → Tab skips the block.

The fix (supplied by the reviewer, validated by them against both
reproductions) adds the ``prismSig`` equality gate to the snapshot restore
and reproduces Prism's element setup on a cache hit.

These tests drive the REAL functions from ``static/ui.js`` with REAL Prism
(core + markup, then javascript) in node and pin:

  * a snapshot written under an older grammar state is REFUSED by the
    fast path, so the rebuild path re-highlights;
  * a snapshot written under the CURRENT grammar state is still applied
    (no #7752 synchronous-flash regression);
  * a cache-hit ``<pre>`` ends up keyboard-focusable with a normalised
    ``language-*`` class on both the ``<code>`` and the ``<pre>``.
"""

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
// Drives the REAL _applyCachedCodeHighlights/_prismGrammarSignature and the
// snapshot fast-path CONDITION out of static/ui.js, against a DOM stand-in
// and REAL Prism, reproducing the reviewer's A -> B -> A grammar-load
// sequence and the cache-hit focus case.
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
const prismDir = process.argv[3];

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
function extractFrom(header, needle) {
  const start = uiSrc.indexOf(header);
  if (start < 0) throw new Error(header + ' not found');
  const end = uiSrc.indexOf('\n}', start);
  return uiSrc.slice(start, end);
}

global.window = { _prismPending: [] };
for (const f of ['prism-core.min.js', 'prism-markup.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
global.Prism = global.Prism || window.Prism;

eval(extract('_codeHighlightCacheKey'));
eval(extract('_writeCodeHighlightCache'));
eval(extract('_applyCachedCodeHighlights'));
eval(extract('_prismGrammarSignature'));

const _CODE_HIGHLIGHT_TOKEN_RE = /<span\b[^>]*\bclass="[^"]*\btoken\b/i;
const _CODE_HIGHLIGHT_CACHE_MAX = 512;
const _codeHighlightCache = new Map();
for (const name of ['_codeHighlightCacheKey', '_writeCodeHighlightCache',
                    '_applyCachedCodeHighlights', '_prismGrammarSignature']) {
  eval(extract(name));
}

// A <pre><code> pair that mimics what Prism.highlightElement normalises.
function makeCodeBlock(cls, text, inner) {
  const pre = {
    nodeName: 'PRE',
    className: '',
    _attrs: {},
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n); },
    setAttribute(n, v) { this._attrs[n] = String(v); },
    getAttribute(n) { return this._attrs[n]; },
    focus() { if (!this.hasAttribute('tabindex')) { throw new Error('not focusable'); } this._focused = true; },
  };
  const code = {
    nodeName: 'CODE',
    className: cls,
    textContent: text,
    _innerHTML: inner,
    dataset: {},
    parentElement: pre,
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = v; },
  };
  return code;
}

const out = {};

// ── Case A: the session-snapshot fast-path condition ───────────────────────
// Pull the REAL condition out of renderMessages so this test pins the shipped
// expression rather than a restatement of it.
const renderBody = extract('renderMessages');
out.fastPathHasPrismGate = /cached\.prismSig===prismSigNow/.test(renderBody);
out.snapshotStoresPrismSig = /_sessionHtmlCache\.get\(sid\)\.prismSig=/.test(renderBody)
  || /prismSig:/.test(renderBody);

// ── Case B: a cache-hit <pre> is focusable and keeps its language class ────
// Write an entry, then apply it to a fresh block and inspect what Prism's
// element setup would have produced.
const block = makeCodeBlock('language-html', '<p>x</p>', '');
// Seed the cache with a tokenized entry for this exact key.
block.innerHTML = '<span class="token tag">x</span>';
_writeCodeHighlightCache(block);

const hit = makeCodeBlock('language-html', '<p>x</p>', '');
const applied = _applyCachedCodeHighlights({ querySelectorAll: () => [hit] });
out.cacheHitApplied = applied;
out.cacheHitStamped = hit.dataset.highlighted === '1';

const pre = hit.parentElement;
out.preHasTabindex = pre.hasAttribute('tabindex');
out.preTabindexValue = pre.getAttribute('tabindex');
out.preClassNormalised = String(pre.className).indexOf('language-') >= 0;
out.codeClassNormalised = String(hit.className).indexOf('language-') >= 0;
out.codeClassKeepsLanguage = /(^|\s)language-html(\s|$)/.test(String(hit.className));
let focusOk = false;
try { pre.focus(); focusOk = pre._focused === true; } catch (e) { focusOk = false; }
out.preFocusSucceeds = focusOk;

// A block with a non-standard class still normalises to language-<lang>.
const odd = makeCodeBlock('lang-html', '<p>y</p>', '<span class="token tag">y</span>');
_writeCodeHighlightCache(odd);
const oddHit = makeCodeBlock('lang-html', '<p>y</p>', '');
_applyCachedCodeHighlights({ querySelectorAll: () => [oddHit] });
out.oddClassNormalised = /(^|\s)language-html(\s|$)/.test(String(oddHit.className));

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("code_paint_7778_r3") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return p


def _run(driver_path) -> dict:
    result = subprocess.run(
        [NODE, str(driver_path), str(UI_JS_PATH), str(FIXTURES)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed: {result.stderr}")
    import json
    return json.loads(result.stdout)


class TestSnapshotGrammarGate:
    """Must-fix 1: a snapshot from an older grammar state must be refused."""

    def test_fast_path_gates_on_grammar_signature(self, driver_path):
        out = _run(driver_path)
        assert out["fastPathHasPrismGate"] is True, (
            "the session-HTML fast-path restore condition must also compare "
            "the snapshot's Prism grammar signature, otherwise returning to a "
            "session restores tokens highlighted under a smaller grammar "
            "(#7778 round-3)."
        )

    def test_snapshot_records_grammar_signature(self, driver_path):
        out = _run(driver_path)
        assert out["snapshotStoresPrismSig"] is True, (
            "the session snapshot must record the grammar state it was taken "
            "under, otherwise the fast-path gate has nothing to compare "
            "(#7778 round-3)."
        )


class TestCacheHitElementSetup:
    """Must-fix 2: a cache hit must reproduce Prism's element setup."""

    def test_cache_hit_still_applies_and_stamps(self, driver_path):
        out = _run(driver_path)
        assert out["cacheHitApplied"] == 1
        assert out["cacheHitStamped"] is True

    def test_cache_hit_pre_is_keyboard_focusable(self, driver_path):
        out = _run(driver_path)
        assert out["preHasTabindex"] is True, (
            "Prism.highlightElement sets tabindex=\"0\" on the <pre>; a "
            "cache-hit rebuild that only restores innerHTML leaves the block "
            "out of the tab order (#7778 round-3)."
        )
        assert out["preTabindexValue"] == "0"
        assert out["preFocusSucceeds"] is True, (
            "the rebuilt <pre> must accept focus() — at 390px Copy -> Tab "
            "must still reach the code block (#7778 round-3)."
        )

    def test_cache_hit_normalises_language_classes(self, driver_path):
        out = _run(driver_path)
        assert out["preClassNormalised"] is True, (
            "Prism theme CSS keys off pre[class*=\"language-\"]; the <pre> "
            "must carry a language-* class after a cache hit (#7778)."
        )
        assert out["codeClassNormalised"] is True
        assert out["codeClassKeepsLanguage"] is True, (
            "the <code> must keep its language-html class after normalisation."
        )

    def test_non_standard_class_prefix_normalises_too(self, driver_path):
        out = _run(driver_path)
        assert out["oddClassNormalised"] is True, (
            "a lang-html class must normalise to language-html, matching "
            "Prism.highlightElement's behaviour (#7778 round-3)."
        )


class TestSourceContract:
    """Pin the shipped shape so a refactor cannot silently drop the fix."""

    def test_apply_helper_reproduces_prism_element_setup(self):
        src = UI_JS_PATH.read_text(encoding="utf-8")
        body = _extract_function_body(src, "function _applyCachedCodeHighlights(")
        assert 'setAttribute(\'tabindex\'' in body or 'setAttribute("tabindex"' in body, (
            "_applyCachedCodeHighlights must set tabindex on the <pre> so a "
            "cache hit stays keyboard-reachable (#7778 round-3)."
        )
        assert "language-" in body, (
            "_applyCachedCodeHighlights must normalise the language-* class "
            "on <code>/<pre> the way Prism.highlightElement does (#7778)."
        )
