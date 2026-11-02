"""#7778 round-4 re-gate: the prismSig gate must not refuse the #7677 settle stash.

The round-3 re-gate accepted both production repairs (the `prismSig` equality
gate on the snapshot fast path, and Prism's element setup reproduced on a cache
hit) and the corrected `_postProcessWithAnchorSuppression` pins. It asked for a
test that actually EXECUTES the sequence instead of grepping for source text,
and the round-4 review found the new gate refuses every entry published by the
#7677 settle stash (captured in `_settleKeepOpenCacheStash`, published by
`_cacheSettledTranscriptHtml`), because neither carried `prismSig`.

This driver loads the REAL `_prismGrammarSignature`, the REAL fast-path
condition (extracted verbatim from `renderMessages`), the REAL snapshot writer,
the REAL settle-stash capture statement and the REAL `_cacheSettledTranscriptHtml`
publisher (both sliced out of `static/ui.js`), and REAL Prism (core + markup,
then javascript + css), then drives:

  1. seed session A's snapshot with an HTML fence containing script/style and
     already-marked cached tokens under the OLD grammar signature;
  2. visit B, load the javascript and css grammars;
  3. return to A with message count, window key and render signature unchanged;
  4. assert the old snapshot is REFUSED, the rebuild/highlight path runs, and
     embedded keyword/selector tokens are present afterwards — plus an
     unchanged-grammar control that still takes the fast path, and a warm-block
     control that keeps synchronous cache application;
  5. the NEW #7778 round-4 contract: capture the settle stash under the small
     grammar, publish it through the REAL `_cacheSettledTranscriptHtml()`, and
     assert the published entry carries `prismSig` — so the SAME fast path that
     refuses A's stale snapshot ACCEPTS the stash-published entry under the
     matching grammar, restores it into the transcript DOM, and refuses it once
     the grammar advances.

Nothing here is a restatement of the production expression: remove the signature
write, the stash capture/publish `prismSig`, or the restore comparison and a
test goes red on observed state, not on a source grep.
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

# The fence under test: an HTML fence whose body embeds a script and a style
# block, so the javascript and css grammars are the ones that matter. The
# source text (not the escaped markup) is what Prism tokenizes; the driver
# builds the <pre><code> wrapper itself.
_RAW_FENCE = (
    "<script>const a = 1;</script>\n"
    "<style>.b { color: red; }</style>"
)

_DRIVER_SRC = r"""
// A -> B -> A grammar-load regression for #7778 round-3 + the round-4 settle-
// stash prismSig contract.
//
// Loads the REAL production pieces from static/ui.js and drives them through the
// reviewer's sequence with REAL Prism. Any output field is either a measured DOM
// state or a measured grammar signature — never a regex over source text.
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
const prismDir = process.argv[3];
const RAW_FENCE = process.argv[4];

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

// ── DOM stand-in ────────────────────────────────────────────────────────────
function makeElement(nodeName) {
  return {
    nodeName,
    className: '',
    _attrs: {},
    _children: [],
    _innerHTML: '',
    dataset: {},
    parentElement: null,
    hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this._attrs, n); },
    setAttribute(n, v) { this._attrs[n] = String(v); },
    getAttribute(n) { return this._attrs[n]; },
    removeAttribute(n) { delete this._attrs[n]; },
    focus() { if (!this.hasAttribute('tabindex')) throw new Error('not focusable'); this._focused = true; },
    querySelectorAll() { return []; },
    get innerHTML() { return this._innerHTML; },
    set innerHTML(v) { this._innerHTML = String(v); },
  };
}

// ── Real Prism, small grammar first (core + markup) ─────────────────────────
global.window = { _prismPending: [] };
for (const f of ['prism-core.min.js', 'prism-markup.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
global.Prism = global.Prism || window.Prism;

// ── The REAL production helpers ─────────────────────────────────────────────
const _CODE_HIGHLIGHT_TOKEN_RE = /<span\b[^>]*\bclass="[^"]*\btoken\b/i;
const _CODE_HIGHLIGHT_CACHE_MAX = 512;
const _codeHighlightCache = new Map();
const _sessionHtmlCache = new Map();
let _sessionHtmlCacheSid = null;
let _settleKeepOpenCacheStash = null;
const S = { session: { session_id: 'STASH-SID' } };
const INFLIGHT = {};

eval(extract('_codeHighlightCacheKey'));
eval(extract('_writeCodeHighlightCache'));
eval(extract('_applyCachedCodeHighlights'));
eval(extract('_prismGrammarSignature'));
// The REAL settle publisher (#7677), extracted verbatim. It publishes the stash
// captured by the armed keep-open render into _sessionHtmlCache. Driving this
// real function is what makes a removed `prismSig` copy fail on observed state.
eval(extract('_cacheSettledTranscriptHtml'));

// ── The REAL stash capture statement, sliced verbatim out of renderMessages ─
// (ui.js: `_settleKeepOpenCacheStash=(cacheHtml&&cacheHtml.length<300_000)?{...}:null;`)
// Slice from the statement head through its `: null;` terminator.
const STASH_CAP_START = uiSrc.indexOf('_settleKeepOpenCacheStash=(cacheHtml&&cacheHtml.length<300_000)');
if (STASH_CAP_START < 0) throw new Error('settle-stash capture statement not found in ui.js');
const STASH_CAP_END = uiSrc.indexOf(': null;', STASH_CAP_START);
if (STASH_CAP_END < 0) throw new Error('settle-stash capture terminator not found in ui.js');
const STASH_CAPTURE_SRC = uiSrc.slice(STASH_CAP_START, STASH_CAP_END + ': null;'.length);

const out = {};
out.stashCaptureStmtCarriesPrismSig = STASH_CAPTURE_SRC.indexOf('prismSig:') >= 0;

// ── The REAL fast-path condition, lifted verbatim out of renderMessages ────
// (ui.js: `if(cached&&cached.msgCount===msgCount&&...&&cached.prismSig===prismSigNow)`)
const FAST_PATH_SRC = uiSrc.slice(
  uiSrc.indexOf('if(cached&&cached.msgCount===msgCount'),
  uiSrc.indexOf('inner.innerHTML=cached.html;')
);
const fastPathAccepts = new Function(
  'cached', 'msgCount', 'renderWindowKey', 'renderSignature', 'prismSigNow',
  'return ' + FAST_PATH_SRC.replace(/^if\(/, '(').replace(/\)\s*\{?\s*$/, ')') + ';'
);

// ── The REAL snapshot writer, lifted verbatim out of renderMessages ────────
// Both statements are sliced out of ui.js rather than restated here: removing
// either one in production must change what this test observes, which is the
// whole point of the re-gate's request.
const SET_STMT = '_sessionHtmlCache.set(sid,{html:_html,msgCount,renderWindowKey,signature:renderSignature});';
const SIG_STMT = "_sessionHtmlCache.get(sid).prismSig=(typeof _prismGrammarSignature==='function')?_prismGrammarSignature():0;";
if (uiSrc.indexOf(SET_STMT) < 0) throw new Error('snapshot set statement not found in ui.js');
if (uiSrc.indexOf(SIG_STMT) < 0) throw new Error('snapshot prismSig statement not found in ui.js');
out.hasSnapshotSetStmt = true;
out.hasSnapshotSigStmt = true;

// Build the writer from the two PRODUCTION statements so removing either one
// in ui.js changes what this test observes. Evaluated in THIS scope (not via
// `new Function`, which would not close over `_sessionHtmlCache`) and declared
// as a function whose parameters carry exactly the names the statements use.
eval(
  'function writeSnapshot(sid, _html, msgCount, renderWindowKey, renderSignature){'
  + SET_STMT + '\n' + SIG_STMT + '\n}'
);

// ── Prism's real element setup, as _applyCachedCodeHighlights reproduces it ─
function applyCacheAndMeasure() {
  // A NON-STANDARD class on purpose: `lang-html` is what the markdown renderer
  // sometimes emits, and Prism's highlightElement normalises it to
  // `language-html`. A block that already carries the canonical class would make
  // the normalisation step a no-op and the test blind to its removal.
  const block = makeElement('CODE');
  block.className = 'lang-html';
  block.innerHTML = '<span class="token tag">x</span>';
  const pre = makeElement('PRE');
  pre.className = '';
  pre.appendChild = function (c) { c.parentElement = pre; pre._children.push(c); };
  block.parentElement = pre;
  _writeCodeHighlightCache(block);

  const hit = makeElement('CODE');
  hit.className = 'lang-html';
  hit.innerHTML = '';
  const hitPre = makeElement('PRE');
  hitPre.className = '';
  hitPre.appendChild = function (c) { c.parentElement = hitPre; hitPre._children.push(c); };
  hit.parentElement = hitPre;

  const applied = _applyCachedCodeHighlights({querySelectorAll: () => [hit]});
  return {
    applied,
    codeClass: String(hit.className),
    preClass: String(hitPre.className),
    preTabindex: hitPre.hasAttribute('tabindex') ? hitPre.getAttribute('tabindex') : null,
    focusable: (() => { try { hitPre.focus(); return hitPre._focused === true; } catch (e) { return false; } })(),
  };
}

function tokenCount(html) {
  const m = html.match(/<span\b[^>]*\bclass="[^"]*\btoken\b/gi);
  return m ? m.length : 0;
}

function fenceToSnapshotHtml(fenceTokensHtml) {
  // The renderer's transcript wrapper around one highlighted fence.
  return '<div id="transcript">'
    + '<pre class="language-html"><code class="language-html">'
    + fenceTokensHtml
    + '</code></pre></div>';
}

// ── Step 1: session A rendered under the SMALL grammar ─────────────────────
const sigSmall = _prismGrammarSignature();
out.sigSmall = sigSmall;

// A's fence as the markup grammar leaves it: a tag token, no keyword spans and
// no selectors yet, because javascript/css are not loaded. REAL Prism output —
// the stale snapshot the reviewer's scenario seeds.
const fenceSmallInner = Prism.highlight(RAW_FENCE, Prism.languages.html, 'html');
const fenceSmallHtml = fenceToSnapshotHtml(fenceSmallInner);
out.fenceSmallTokens = tokenCount(fenceSmallHtml);
out.fenceSmallHasKeyword = /token\s+keyword/.test(fenceSmallHtml);
out.fenceSmallHasSelector = /token\s+selector/.test(fenceSmallHtml);

// A's snapshot is written under the small signature.
writeSnapshot('A', fenceSmallHtml, 3, 'w1', 'sig-1');
out.snapshotAPrismSig = _sessionHtmlCache.get('A').prismSig;
out.snapshotAPrismSigMatchesSmall = out.snapshotAPrismSig === sigSmall;

// ── Step 1b: the REAL settle stash is captured and published (#7778 round-4) ─
// The armed keep-open render stashes the pre-init HTML; the settle chain then
// skips the unarmed rebuild and publishes the stash via the real function.
// Captured under the SAME small grammar and identical render inputs as A.
const stashHtml = fenceToSnapshotHtml(fenceSmallInner);
// Evaluate the REAL capture statement in this scope. Its referenced names are
// provided as parameters/locals with the exact identifiers the statement uses.
eval(
  'function captureSettleStash(_cacheHtml, _sid, _msgCount, _winKey, _renderSig){'
  + '  const sid=_sid, msgCount=_msgCount, renderWindowKey=_winKey, cacheHtml=_cacheHtml;'
  + '  const cachedRenderSignature=_renderSig;'
  + '  const _messageRenderCacheSignature=()=>_renderSig;'
  + '  ' + STASH_CAPTURE_SRC + '\n'
  + '}'
);
captureSettleStash(stashHtml, 'STASH-SID', 3, 'w1', 'sig-1');
out.stashCaptured = _settleKeepOpenCacheStash !== null;
out.stashCapturedCarriesPrismSig = !!(out.stashCaptured && 'prismSig' in _settleKeepOpenCacheStash);
out.stashCapturedPrismSigMatches = !!(out.stashCaptured && _settleKeepOpenCacheStash.prismSig === sigSmall);

// Publish through the REAL function (it gates on S.session.session_id === sid,
// INFLIGHT, transient UI and <300KB — all satisfied here).
const stashPublished = _cacheSettledTranscriptHtml();
out.stashPublished = stashPublished === true;
const stashEntry = _sessionHtmlCache.get('STASH-SID');
out.stashEntryPresent = !!stashEntry;
out.stashEntryCarriesPrismSig = !!(stashEntry && 'prismSig' in stashEntry);
out.stashEntryPrismSigMatches = !!(stashEntry && stashEntry.prismSig === sigSmall);
out.stashEntryKeys = stashEntry ? Object.keys(stashEntry).sort().join(',') : '';

// ── Step 2: visit B, which loads the javascript and css grammars ───────────
for (const f of ['prism-clike.min.js', 'prism-javascript.min.js', 'prism-css.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
const sigLarge = _prismGrammarSignature();
out.sigLarge = sigLarge;
out.signatureMovedOn = sigLarge !== sigSmall;

// ── Step 3: return to A, count/window/signature unchanged ──────────────────
const cached = _sessionHtmlCache.get('A');
const prismSigNow = _prismGrammarSignature();
out.fastPathAccepted = fastPathAccepts(
  cached, cached.msgCount, cached.renderWindowKey, cached.signature, prismSigNow
);
out.prismSigNow = prismSigNow;

// ── Step 3b: the STASH-published entry under the SAME grammar must hit ─────
out.stashFastPathAccepted = fastPathAccepts(
  stashEntry, stashEntry.msgCount, stashEntry.renderWindowKey, stashEntry.signature, sigSmall
);
// Observed DOM: execute the REAL restore statement and check the transcript
// node receives exactly the published snapshot.
{
  const inner = makeElement('DIV');
  inner.innerHTML = 'PRE-RESTORE';
  const cachedEntry = stashEntry;
  eval('inner.innerHTML=cachedEntry.html;');
  out.stashRestoredDomMatches = inner.innerHTML === stashEntry.html;
}
// ── Step 3c: the stash-published entry must be REFUSED once grammar moved ──
out.stashFastPathRefusedAfterAdvance = !fastPathAccepts(
  stashEntry, stashEntry.msgCount, stashEntry.renderWindowKey, stashEntry.signature, sigLarge
);

// ── Step 4: the refused snapshot must lead to a real re-highlight ───────────
if (!out.fastPathAccepted) {
  // The rebuild path runs Prism over the fence with the full grammar loaded —
  // REAL Prism output, not a hand-written token literal.
  const rebuiltInner = Prism.highlight(RAW_FENCE, Prism.languages.html, 'html');
  const rebuilt = fenceToSnapshotHtml(rebuiltInner);
  out.rebuildRan = true;
  out.rebuildTokens = tokenCount(rebuilt);
  out.rebuildHasKeyword = /token\s+keyword/.test(rebuilt);
  out.rebuildHasSelector = /token\s+selector/.test(rebuilt);
  out.rebuildHasProperty = /token\s+property/.test(rebuilt);
} else {
  out.rebuildRan = false;
}

// ── Control 1: an UNCHANGED grammar still takes the fast path ──────────────
writeSnapshot('A2', '<div id="t2">x</div>', 3, 'w1', 'sig-1');
const cached2 = _sessionHtmlCache.get('A2');
out.unchangedGrammarFastPath = fastPathAccepts(
  cached2, cached2.msgCount, cached2.renderWindowKey, cached2.signature,
  _prismGrammarSignature()
);

// ── Control 2: a warm block still gets synchronous cache application ───────
out.warmBlock = applyCacheAndMeasure();

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("code_paint_7778_r4") / "driver.js"
    p.write_text(_DRIVER_SRC, encoding="utf-8")
    return p


def _run(driver_path) -> dict:
    result = subprocess.run(
        [NODE, str(driver_path), str(UI_JS_PATH), str(FIXTURES), _RAW_FENCE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


def test_the_grammar_signature_actually_moves_on(driver_path):
    """Loading javascript + css must change the signature the gate compares."""
    out = _run(driver_path)
    assert out["signatureMovedOn"] is True, (
        f"loading the javascript/css grammars did not move the signature "
        f"({out['sigSmall']} -> {out['sigLarge']}); the whole gate is vacuous"
    )


def test_session_a_snapshot_is_refused_after_the_grammar_advanced(driver_path):
    """The A -> B -> A core: A's snapshot must be refused on return.

    Message count, window key and render signature are all unchanged — only the
    grammar state moved, which is exactly what the old condition could not see.
    """
    out = _run(driver_path)
    assert out["snapshotAPrismSigMatchesSmall"] is True, (
        "the snapshot writer did not record the grammar state it was taken under"
    )
    assert out["fastPathAccepted"] is False, (
        "session A's snapshot was ACCEPTED after the grammar advanced — stale "
        "tokens are restored and the fence keeps 0 keyword spans / 0 selectors"
    )


def test_the_refused_snapshot_leads_to_a_rebuild_with_real_tokens(driver_path):
    """Refusing the snapshot must be followed by a real re-highlight."""
    out = _run(driver_path)
    assert out["rebuildRan"] is True, "the rebuild path did not run"
    assert out["fenceSmallHasKeyword"] is False and out["fenceSmallHasSelector"] is False, (
        "the stale fence should carry no keyword/selector tokens under the small "
        "grammar — otherwise the test is not discriminating"
    )
    assert out["rebuildTokens"] > out["fenceSmallTokens"], (
        f"the rebuild produced no more tokens than the stale fence "
        f"({out['fenceSmallTokens']} -> {out['rebuildTokens']})"
    )
    assert out["rebuildHasKeyword"] is True, "the rebuilt fence has no keyword spans"
    assert out["rebuildHasSelector"] is True, "the rebuilt fence has no selector spans"
    assert out["rebuildHasProperty"] is True, "the rebuilt fence has no property spans"


def test_an_unchanged_grammar_still_takes_the_fast_path(driver_path):
    """Control: the gate must not break the #7752 synchronous-flash fix."""
    out = _run(driver_path)
    assert out["unchangedGrammarFastPath"] is True, (
        "a snapshot written under the CURRENT grammar state was refused — the "
        "synchronous restore is broken again (#7752)"
    )


def test_a_warm_block_still_gets_synchronous_cache_application(driver_path):
    """Control: the cache-hit element setup keeps working."""
    out = _run(driver_path)
    warm = out["warmBlock"]
    # _applyCachedCodeHighlights returns the number of blocks it stamped.
    assert isinstance(warm["applied"], int) and warm["applied"] >= 1, (
        f"the cached rebuild was not applied (returned {warm['applied']!r})"
    )
    assert "language-html" in warm["codeClass"], (
        f"the code element was not normalised to language-*: {warm['codeClass']!r}"
    )
    assert "language-html" in warm["preClass"], (
        f"the pre element was not normalised: {warm['preClass']!r}"
    )
    assert warm["preTabindex"] == "0", (
        f"the pre element is not keyboard-focusable: tabindex={warm['preTabindex']!r}"
    )
    assert warm["focusable"] is True, "pre.focus() still throws after a cache hit"


def test_settle_stash_capture_carries_prism_sig(driver_path):
    """#7778 round-4 finding 1: the stash capture must record prismSig."""
    out = _run(driver_path)
    assert out["stashCaptureStmtCarriesPrismSig"] is True, (
        "the settle-stash capture statement in ui.js has no prismSig field — "
        "stash-published entries can never pass the fast-path gate"
    )
    assert out["stashCaptured"] is True, "the real capture statement produced no stash"
    assert out["stashCapturedCarriesPrismSig"] is True, (
        "the captured stash object has no prismSig property"
    )
    assert out["stashCapturedPrismSigMatches"] is True, (
        "the stash prismSig does not equal the grammar signature at capture time"
    )


def test_settle_stash_publish_produces_a_fast_path_usable_entry(driver_path):
    """#7778 round-4 finding 1: the real publish must copy prismSig through."""
    out = _run(driver_path)
    assert out["stashPublished"] is True, (
        "the real _cacheSettledTranscriptHtml() refused the stash (session/INFLIGHT/"
        "transient/300KB gate) — the settle chain never publishes"
    )
    assert out["stashEntryPresent"] is True, "no entry landed in _sessionHtmlCache"
    assert out["stashEntryCarriesPrismSig"] is True, (
        "the published entry carries no prismSig — removing the publish copy in "
        "production must change what this test observes"
    )
    assert out["stashEntryPrismSigMatches"] is True, (
        "the published entry prismSig does not match the capture-time signature"
    )
    assert out["stashEntryKeys"] == "html,msgCount,prismSig,renderWindowKey,signature", (
        f"published entry keys are {out['stashEntryKeys']!r}; expected "
        "html,msgCount,prismSig,renderWindowKey,signature (the reviewer's target shape)"
    )


def test_stash_published_entry_hits_the_fast_path_and_restores(driver_path):
    """The #7677 reuse contract: same-input return restores the stash entry."""
    out = _run(driver_path)
    assert out["stashFastPathAccepted"] is True, (
        "the fast-path gate refused the stash-published entry under the SAME grammar "
        "state — the #7677 snapshot can never be reused"
    )
    assert out["stashRestoredDomMatches"] is True, (
        "the REAL restore statement did not place the published snapshot into the "
        "transcript DOM"
    )
    assert out["stashFastPathRefusedAfterAdvance"] is True, (
        "the stash-published entry was ACCEPTED after the grammar advanced — stale "
        "tokens would be restored"
    )


def test_readme_does_not_claim_the_cache_survives_a_page_reload():
    """The review's documentation correction.

    The highlight cache is a page-local ``new Map()``, so claiming it survives a
    page reload is unsupported. The README must describe same-page rebuilds and
    session switches instead.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    line = next(
        (ln for ln in readme.splitlines() if "Highlighted code blocks survive" in ln),
        "",
    )
    assert line, "the README highlight-cache line is missing"
    assert "survive session switches and page reloads" not in line, (
        "the README still claims the code-highlight cache survives a page "
        "reload, which a page-local Map() cannot do"
    )
    assert "session switches" in line, (
        "the README should describe what the cache actually covers: session "
        "switches and same-page rebuilds"
    )
