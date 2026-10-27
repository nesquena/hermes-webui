"""#7778 round-5: the grammar gate must be pinned by the REAL render path.

Round 4 (`3795c6646`) fixed the production side of the #7677 settle-stash
contract — the stash capture records `prismSig` and `_cacheSettledTranscriptHtml`
copies it into the published entry — and rewrote
`tests/test_issue7778_grammar_gate_behaviour.py` to drive real Prism and the
real publisher. The 2026-10-19 maintainer review's section 2 asked for more,
and its mutation table is the acceptance criterion:

    | mutation                                          | result at 7857f510 |
    | `highlightCode()` replaced with a no-op           | 6/6 still pass     |
    | production `prismSig` write made unreachable      | 54 passed          |
    | `renderMessages()` returns immediately            | 54 passed          |
    | production `prismSig` write deleted               | 6 fail, but only on
    |                                                   | a source-text grep |

Two of those mutations still pass the whole round-4 family, and the reason is
structural: the round-4 driver SLICES statements out of `static/ui.js` and
re-assembles them into its own functions (`SET_STMT`, `SIG_STMT`,
`STASH_CAPTURE_SRC`, `FAST_PATH_SRC`). A sliced statement loses the control
flow around it, so:

  * making the production `prismSig` write unreachable (`if(false){…}`) is
    invisible — the driver evaluates its own copy of the statement, not the
    production one; and
  * `renderMessages()` itself is never called, so a `renderMessages()` that
    returns immediately changes nothing the driver observes.

This module closes that gap by driving the REAL producer and the REAL
`renderMessages`:

  * the snapshot producer is the production BLOCK
    (`if(_html&&_html.length<300_000){…}`) sliced verbatim WITH its control
    flow, so the `if(false){…}` mutation is observable as a missing
    `prismSig` on the published entry; and
  * the restore is the REAL `renderMessages()` evaluated from `static/ui.js`
    and called, so a `renderMessages()` that returns immediately is observable
    as a transcript that was never restored.

Nothing here is a restatement of a production expression: remove the
production `prismSig` write, make it unreachable, neuter `highlightCode()`, or
make `renderMessages()` return early, and a test below goes red on OBSERVED
state (the published entry's keys, the restored transcript DOM).
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
# block, so the javascript and css grammars are the ones that matter.
_RAW_FENCE = (
    "<script>const a = 1;</script>\n"
    "<style>.b { color: red; }</style>"
)

# The driver drives the REAL renderMessages() through its cached fast path.
#
# Prism is evaluated FIRST, before any `window`/`const` binding in this scope:
# prism-core reads `window` at evaluation time, and a `const window` in the
# same scope would be in its temporal dead zone at that moment.
_DRIVER_SRC = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
const prismDir = process.argv[3];
const RAW_FENCE = process.argv[4];

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

// Slices a whole production BLOCK (with its control flow) out of ui.js.
function sliceBlock(marker) {
  const s = uiSrc.indexOf(marker);
  if (s < 0) throw new Error('block not found: ' + marker);
  let i = uiSrc.indexOf('{', s), depth = 1; i++;
  while (depth > 0 && i < uiSrc.length) {
    if (uiSrc[i] === '{') depth++;
    else if (uiSrc[i] === '}') depth--;
    i++;
  }
  return uiSrc.slice(s, i);
}

// ── Real Prism, evaluated BEFORE any window binding (TDZ) ──────────────────
global.window = { _prismPending: [] };
for (const f of ['prism-core.min.js', 'prism-markup.min.js']) {
  eval(fs.readFileSync(prismDir + '/' + f, 'utf8'));
}
global.Prism = global.Prism || global.window.Prism;

// ── Minimal DOM ────────────────────────────────────────────────────────────
function makeEl(tag) {
  return {
    tagName: String(tag).toUpperCase(),
    nodeName: String(tag).toUpperCase(),
    className: '',
    _attrs: {},
    _children: [],
    _innerHTML: '',
    dataset: {},
    parentElement: null,
    textContent: '',
    classList: { add(){}, remove(){}, contains(){ return false; } },
    hasAttribute(n){ return Object.prototype.hasOwnProperty.call(this._attrs, n); },
    setAttribute(n, v){ this._attrs[n] = String(v); },
    getAttribute(n){ return this._attrs[n]; },
    removeAttribute(n){ delete this._attrs[n]; },
    appendChild(c){ c.parentElement = this; this._children.push(c); return c; },
    focus(){ this._focused = true; },
    get innerHTML(){ return this._innerHTML; },
    set innerHTML(v){ this._innerHTML = String(v); },
    querySelectorAll(sel){
      const s = String(sel);
      const out = [];
      const wantNotMarked = s.indexOf('pre code:not([data-highlighted])') >= 0;
      const wantCode = s.indexOf('code') >= 0;
      const walk = (el) => {
        for (const c of el._children) {
          if (c.tagName === 'CODE') {
            const marked = c.dataset.highlighted === '1';
            if (wantNotMarked && !marked) out.push(c);
            else if (wantCode && !wantNotMarked) out.push(c);
          }
          walk(c);
        }
      };
      if (this._children) for (const c of this._children) walk(c);
      return out;
    },
    querySelector(sel){ const a = this.querySelectorAll(sel); return a.length ? a[0] : null; },
  };
}
global.document = { createElement(tag){ return makeEl(tag); }, getElementById(){ return null; } };

// ── Module state the real renderMessages() reads ───────────────────────────
let _lastMessageRenderAt = 0;
let _messageUserUnpinned = false;
let _sessionHtmlCacheSid = null;
let _messageVirtualWindowKey = 'stale';
let _messageRenderWindowSid = 'sid-A';
const _loadingSessionId = null;
const performance = { now: () => 1000 };
const INFLIGHT = {};
const S = { session: { session_id: 'sid-A' }, messages: [{ role: 'user' }], busy: true };
const _sessionHtmlCache = new Map();
const _codeHighlightCache = new Map();
let _CODE_HIGHLIGHT_TOKEN_RE = /<span\b[^>]*\bclass="[^"]*\btoken\b/i;

const inner = makeEl('DIV');
const container = makeEl('DIV');
container.scrollTop = 0; container.clientHeight = 600; container.scrollHeight = 20000;
container.getBoundingClientRect = () => ({ top: 0, bottom: 600 });

function $(id){
  if (id === 'msgInner') return inner;
  if (id === 'emptyState') return { style: {} };
  if (id === 'messages') return container;
  return null;
}

// ── Dependencies of the cached branch ─────────────────────────────────────
function _resetMessageVirtualMeasurementBurst(){}
function _resetMessageRenderWindow(){}
function _latestPreservedCompressionTaskListMessages(){ return []; }
function _compressionStateForCurrentSession(){ return null; }
function _clearCompressionElapsedTimer(){}
function _setCompressionSessionLock(){}
function _captureWorklogDetailDisclosureState(){ return null; }
function _handoffStateForCurrentSession(){ return null; }
function _getVisibleMessagesWithIdx(){ return [{ rawIdx:0, m:S.messages[0] }]; }
function _currentMessageVirtualWindow(){ return { virtualized:true, start:0, end:1, topPad:0, bottomPad:0, total:1, tailStart:1 }; }
function _messageVirtualKeepTailCount(){ return 0; }
function _messageVirtualWindowKeyFor(){ return 'w1'; }
function _messageRenderCacheSignature(){ return 'sig-1'; }
function _rehydrateTransparentStreamDom(){}
function _rehydrateDeferredWorklogsFromCache(){}
function _wireMessageWindowLoadEarlierButton(){}
function _updateMessageVirtualMeasurements(){}
function _captureMessageRecoveryOwnership(){ return { programmatic:false, jumpOwner:false }; }
function _scrollAfterMessageRender(){}
function _maybeRecoverVirtualizedBlankViewport(){ return false; }
function _postProcessWithAnchorSuppression(){}
function _initMediaPlaybackObserver(){}
function requestAnimationFrame(){}
function _isKeepSettledWorklogOpenArmed(){ return false; }
function addCopyButtons(){}
function initTreeViews(){}
function _maybeRegisterPrismCompleteHook(){}

// ── The REAL production helpers and the REAL renderer ─────────────────────
eval(extractFunc('_codeHighlightCacheKey'));
eval(extractFunc('_writeCodeHighlightCache'));
eval(extractFunc('_applyCachedCodeHighlights'));
eval(extractFunc('_prismGrammarSignature'));
eval(extractFunc('highlightCode'));
eval(extractFunc('renderMessages'));

const out = {};

// ── The REAL snapshot producer, sliced as a whole BLOCK ────────────────────
// The block (not the individual statement) is what production runs, so the
// control flow around the `prismSig` write comes along with it. A production
// write made unreachable (`if(false){…}`) is therefore observable here as a
// published entry with no `prismSig` — which is exactly what the round-4
// driver could not see.
const PRODUCER_BLOCK = sliceBlock('if(_html&&_html.length<300_000){');
eval(
  'function produceSnapshot(sid,_html,msgCount,renderWindowKey,cachedRenderSignature){'
  + PRODUCER_BLOCK + '\n}'
);

// Real Prism output for the fence under the SMALL grammar.
const fenceHtml = Prism.highlight(RAW_FENCE, Prism.languages.html, 'html');
const snapshotHtml = '<div id="transcript"><pre class="language-html"><code class="language-html">'
  + fenceHtml + '</code></pre></div>';

produceSnapshot('sid-A', snapshotHtml, S.messages.length, 'w1', null);
const published = _sessionHtmlCache.get('sid-A');
out.publishedPresent = !!published;
out.publishedKeys = published ? Object.keys(published).sort().join(',') : '';
out.publishedPrismSig = published ? published.prismSig : null;
out.prismSigNow = _prismGrammarSignature();
out.gateAccepts = !!(published && published.prismSig === out.prismSigNow);

// ── The REAL restore: call the real renderMessages() ───────────────────────
renderMessages({ preserveScroll: false });
out.restored = inner.innerHTML === snapshotHtml;
out.restoredLength = inner.innerHTML.length;
out.snapshotLength = snapshotHtml.length;
out.transcriptEmpty = inner.innerHTML.length === 0;

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def driver_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("code_paint_7778_r5") / "driver.js"
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


def test_the_real_producer_publishes_prism_sig_on_the_snapshot(driver_path):
    """The production snapshot producer must record the grammar state.

    The producer is the production BLOCK, so the `prismSig` write is the
    production one. Deleting it, or making it unreachable, must change what
    this test observes.
    """
    out = _run(driver_path)
    assert out["publishedPresent"] is True, (
        "the real producer block published no snapshot into _sessionHtmlCache"
    )
    assert "prismSig" in out["publishedKeys"], (
        f"the published entry carries no prismSig (keys={out['publishedKeys']!r}) — "
        "the production write is missing or unreachable, so every snapshot this "
        "branch publishes is refused by the fast-path gate and rebuilt"
    )
    assert out["publishedPrismSig"] == out["prismSigNow"], (
        "the published snapshot's prismSig does not equal the grammar signature "
        f"at publish time ({out['publishedPrismSig']!r} != {out['prismSigNow']!r})"
    )


def test_the_real_render_messages_restores_the_published_snapshot(driver_path):
    """The real renderMessages() must restore the snapshot it published.

    This drives the REAL renderer: a `renderMessages()` that returns
    immediately leaves the transcript empty, which is what this test observes.
    """
    out = _run(driver_path)
    assert out["gateAccepts"] is True, (
        "the published snapshot does not satisfy the fast-path gate under the "
        "current grammar state"
    )
    assert out["restored"] is True, (
        f"the real renderMessages() did not restore the published snapshot "
        f"(transcript length {out['restoredLength']} vs snapshot length "
        f"{out['snapshotLength']}) — the transcript was never restored"
    )


def test_the_real_render_path_does_not_restore_an_empty_transcript(driver_path):
    """Control: the restore is observed, not inferred.

    If the renderer were skipped, the transcript would be empty. The snapshot
    is not empty, so an empty transcript means the restore did not happen.
    """
    out = _run(driver_path)
    assert out["snapshotLength"] > 0, "the fixture snapshot is empty — nothing to restore"
    assert out["transcriptEmpty"] is False, (
        "the transcript is empty after the real renderMessages() ran — the "
        "restore never happened"
    )
