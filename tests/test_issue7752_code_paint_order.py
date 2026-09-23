"""Regression tests for #7752 — code blocks rebuilt by virtualization must
be highlighted immediately if the same text was already highlighted before.

The bug
-------
With ``virtualize_transcript = true``, scrolling a previously-off-screen code
block into view rebuilds the transcript, which creates a fresh ``<pre><code>``
DOM node that no longer carries ``data-highlighted="1"``. The post-process
pass that calls ``Prism.highlightElement`` runs one frame AFTER the render
(``static/ui.js:18851`` → ``_postProcessWithAnchorSuppression``), so the
rebuilt block paints unhighlighted for one frame before snapping to its
tokenized form. The same source text renders identically either way — the
bug is purely a paint-order timing artifact, not a content artifact.

The fix
-------
``static/ui.js`` now maintains a small in-memory cache of already-highlighted
code blocks (``_codeHighlightCache``), keyed by
``language + "\\0" + textContent`` with the highlighted innerHTML as the
value. ``highlightCode()`` populates the cache after a successful Prism
pass. A new ``_applyCachedCodeHighlights(container)`` synchronously walks
``pre code:not([data-highlighted])`` in a freshly-rebuilt container and, for
each block whose source text is already in the cache, applies the cached
innerHTML and stamps ``data-highlighted="1"`` immediately — no rAF wait.

The renderMessages() rebuild path calls the sync cache pass BEFORE scheduling
the rAF post-process. Blocks with a cache hit paint highlighted on the very
first frame after the rebuild; blocks without a cache hit (genuinely new
code) keep the existing deferred-frame behavior, preserving the by-design
post-process deferral for first-appearance code blocks.

These tests pin the contract:
  * Pre-fix: ``_applyCachedCodeHighlights`` does not exist, the rebuild path
    does not call it, and a code block rebuilt with the same text paints
    unhighlighted on the first frame.
  * Post-fix: the helper exists, the rebuild path calls it, the cache is
    populated by ``highlightCode``, a block rebuilt with the same text gets
    ``data-highlighted="1"`` synchronously, and a brand-new code block
    (no cache entry) is left untouched by the sync pass.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
UI_JS = REPO / "static" / "ui.js"
NODE = shutil.which("node")


# ── Static-source assertions (cheap, fail-fast on regressions) ───────────────


def _read_ui_js() -> str:
    return UI_JS.read_text(encoding="utf-8")


def _extract_function_body(src: str, signature: str) -> str:
    """Return the source of a top-level function declaration via brace balance."""
    idx = src.find(signature)
    if idx == -1:
        raise AssertionError(f"signature {signature!r} not found in source")
    open_idx = src.find("{", idx)
    if open_idx == -1:
        raise AssertionError(f"could not find opening brace after {signature!r}")
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[idx : i + 1]
    raise AssertionError(f"unbalanced braces in {signature!r}")


class TestSyncHighlightCachePresent:
    """The cache + helper must exist in ``static/ui.js`` (#7752)."""

    def test_cache_storage_declared(self):
        src = _read_ui_js()
        assert "_codeHighlightCache" in src, (
            "_codeHighlightCache Map must be defined in static/ui.js — it is the "
            "store of already-highlighted <pre><code> innerHTML keyed by "
            "language+textContent (#7752)."
        )
        assert "new Map()" in src, (
            "_codeHighlightCache must be a Map instance (keyed by string, value "
            "is the highlighted innerHTML string)."
        )

    def test_cache_key_helper_defined(self):
        src = _read_ui_js()
        body = _extract_function_body(src, "function _codeHighlightCacheKey(")
        assert "language-" in body, (
            "_codeHighlightCacheKey must read the `language-xxx` class off the "
            "block to build a language-aware cache key — otherwise identical "
            "textContent in two different languages would collide and one "
            "would render with the wrong token set."
        )
        assert "textContent" in body, (
            "_codeHighlightCacheKey must use textContent so the same source text "
            "in different positions hits the same cache entry across rebuilds."
        )

    def test_apply_cached_helper_defined(self):
        src = _read_ui_js()
        body = _extract_function_body(src, "function _applyCachedCodeHighlights(")
        # The helper must skip blocks that already have the marker — otherwise
        # we'd overwrite the live cache with itself on every call.
        assert "data-highlighted" in body, (
            "_applyCachedCodeHighlights must use the "
            "`pre code:not([data-highlighted])` selector so it only touches "
            "rebuilds, not the existing live nodes."
        )
        assert "innerHTML" in body, (
            "_applyCachedCodeHighlights must set the block's innerHTML from the "
            "cache — that is the actual highlight payload, not just the marker."
        )
        assert "dataset.highlighted" in body, (
            "_applyCachedCodeHighlights must stamp data-highlighted='1' so the "
            "subsequent rAF post-process (which uses the same :not selector) "
            "skips the already-highlighted block."
        )

    def test_highlight_code_populates_cache(self):
        src = _read_ui_js()
        body = _extract_function_body(src, "function highlightCode(")
        assert "_codeHighlightCacheKey(" in body, (
            "highlightCode must call _codeHighlightCacheKey so each block it "
            "highlights is recorded for the next virtualized rebuild (#7752)."
        )
        assert "_codeHighlightCache.set(" in body, (
            "highlightCode must write into _codeHighlightCache — the only way "
            "to populate the cache for the sync rebuild pass."
        )

    def test_cache_has_size_cap(self):
        """A naive unbounded cache would grow for the lifetime of the page.
        Pin a cap so a long-lived instance with many distinct code blocks
        cannot blow up memory."""
        src = _read_ui_js()
        body = _extract_function_body(src, "function highlightCode(")
        assert "_CODE_HIGHLIGHT_CACHE_MAX" in body, (
            "highlightCode must bound _codeHighlightCache size via "
            "_CODE_HIGHLIGHT_CACHE_MAX to prevent unbounded growth across the "
            "lifetime of a page with many distinct code blocks."
        )
        m = re.search(r"_CODE_HIGHLIGHT_CACHE_MAX\s*=\s*(\d+)", src)
        assert m, "_CODE_HIGHLIGHT_CACHE_MAX must be a numeric constant"
        cap = int(m.group(1))
        assert cap > 0 and cap <= 4096, (
            f"_CODE_HIGHLIGHT_CACHE_MAX={cap} is outside the expected "
            f"reasonable range (1..4096) — pick a sane cap."
        )


class TestRebuildPathCallsSyncPass:
    """The virtualized rebuild path must invoke the sync cache pass before
    the deferred rAF post-process. Without this call, the helper exists but
    does nothing — the one-frame flash comes back."""

    REBUILD_RAF_TOKEN = "requestAnimationFrame(()=>_postProcessWithAnchorSuppression(inner))"

    def _get_rebuild_request_animation_frame_block(self) -> str:
        src = _read_ui_js()
        # There are two rAF sites that schedule _postProcessWithAnchorSuppression(inner):
        #   * the cache fast path (line ~17197) — brings back already-highlighted HTML
        #   * the full rebuild path (line ~18851) — the bug site
        # We want the LATER occurrence, which is the full rebuild path. Find all
        # occurrences and pick the last one (the rebuild site).
        positions = [
            i for i in range(len(src))
            if src.startswith(self.REBUILD_RAF_TOKEN, i)
        ]
        assert positions, (
            "Could not locate the rebuild-path rAF that schedules "
            "_postProcessWithAnchorSuppression(inner) — the path shape may "
            "have changed; update this test."
        )
        # Source order: the rebuild-path rAF is the SECOND occurrence (the
        # first is the cache fast path at line ~17197). Either there are
        # exactly two, or — if the cache fast path was removed in some future
        # refactor — exactly one. The rebuild site is the LAST occurrence
        # in either case.
        rebuild_idx = positions[-1]
        return src[:rebuild_idx]

    def test_sync_pass_call_present_in_rebuild_path(self):
        prefix = self._get_rebuild_request_animation_frame_block()
        assert "_applyCachedCodeHighlights(inner)" in prefix, (
            "The virtualized rebuild path in static/ui.js must call "
            "_applyCachedCodeHighlights(inner) BEFORE scheduling the rAF "
            "post-process — that is the #7752 fix site."
        )

    def test_sync_pass_runs_before_raf_post_process(self):
        """Source order: sync cache pass → requestAnimationFrame(post-process).
        Reversed order would mean the rAF fires first and the flash remains."""
        prefix = self._get_rebuild_request_animation_frame_block()
        sync_pos = prefix.rfind("_applyCachedCodeHighlights(inner)")
        assert sync_pos != -1, "sync pass call not found in rebuild path"
        # Confirm the rAF appears AFTER the sync pass in source order.
        assert sync_pos < len(prefix), (
            "source-order sanity check failed"
        )


# ── Behavioral test (node-eval ui.js, asserts the actual contract) ────────────


def _snapshot_via_node() -> dict:
    """Extract the relevant helpers from ui.js and exercise the
    virtualized-rebuild contract in a node vm sandbox with a minimal Prism
    stub. Returns a JSON dict of observed behaviors that the assertions
    below consume.
    """
    assert NODE, "node is required for #7752 behavioral test"
    src = _read_ui_js()

    # Pull just the helper bodies we need. The extraction is by brace-balance
    # so the test does not depend on the surrounding file loading cleanly —
    # this is the same isolation pattern as
    # tests/test_stable_assistant_turn_anchor_normalizer.py.
    def extract(signature: str) -> str:
        idx = src.find(signature)
        if idx == -1:
            raise AssertionError(f"signature {signature!r} not found")
        open_idx = src.find("{", idx)
        depth = 0
        for i in range(open_idx, len(src)):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    return src[idx : i + 1]
        raise AssertionError(f"unbalanced braces in {signature!r}")

    cache_key_fn = extract("function _codeHighlightCacheKey(")
    apply_fn = extract("function _applyCachedCodeHighlights(")
    highlight_fn = extract("function highlightCode(")

    # Extract the cache constant + map declarations. They sit on two lines
    # ABOVE `_codeHighlightCacheKey` in the source; capture both lines by
    # looking for the unique `_CODE_HIGHLIGHT_CACHE_MAX = ` token.
    cache_consts = []
    for marker in ("_CODE_HIGHLIGHT_CACHE_MAX = ", "const _codeHighlightCache = new Map();"):
        idx = src.find(marker)
        if idx == -1:
            raise AssertionError(
                f"cache setup token {marker!r} not found in static/ui.js — "
                f"the cache-instrumentation edit must be present for #7752."
            )
        # Capture the line end (assume single-line declarations; verify).
        end = src.index("\n", idx)
        cache_consts.append(src[idx:end])

    cache_setup = "\n".join(cache_consts)

    # Minimal Prism stub: highlightElement sets data-highlighted on the
    # element and wraps the contents in a single token span so the cache
    # payload differs from the unhighlighted innerHTML.
    prism_stub = """
    globalThis.Prism = {
      highlightElement(el) {
        // Mark + tokenize (one wrapping span is enough to prove the cache
        // payload differs from the unhighlighted form).
        el.innerHTML = '<span class="token">' + el.textContent + '</span>';
        el.dataset.highlighted = '1';
      }
    };
    """

    # Minimal DOM stub — the test exercises a tiny, well-defined subset of
    # the DOM (createElement('div'), innerHTML set to <pre><code> children
    # OR a tokenized <span> form after Prism, querySelectorAll /
    # querySelector, textContent/className/dataset access). A real jsdom is
    # heavier than the test needs; the stub keeps the harness hermetic and
    # dependency-free.
    dom_stub = r"""
    // Very small HTML parser that handles three shapes the test exercises:
    //   * <pre><code class="...">T</code></pre>         (raw, pre-render)
    //   * <pre><code class="..."><span ...>T</span></code></pre>  (after Prism)
    //   * <pre><code>T</code></pre>                     (no class)
    // Returns a freshly-rooted <pre> tree, or null if the shape is not
    // recognized (the caller treats that case as a literal-text innerHTML).
    function parsePreCode(html) {
      const m = String(html).match(/^<pre>([\s\S]*?)<\/pre>$/);
      if (!m) return null;
      const inner = m[1];
      const cm = inner.match(/^<code(?:\s+class="([^"]*)")?>([\s\S]*)<\/code>$/);
      if (!cm) return null;
      const code = makeEl('code');
      code._className = cm[1] || '';
      // The code body may be plain text OR a single <span ...>T</span>
      // wrapping (what Prism's highlightElement writes). Parse that
      // variant so textContent stays the underlying source — otherwise
      // a Prism-rendered block would have textContent = "<span>...</span>"
      // and the cache key would never match the unhighlighted form.
      const body = cm[2];
      const spanMatch = body.match(/^<span(?:\s+class="([^"]*)")?>([\s\S]*)<\/span>$/);
      if (spanMatch) {
        const span = makeEl('span');
        span._className = spanMatch[1] || '';
        span._textContent = spanMatch[2];
        span._children = [];
        code._children = [span];
        code._textContent = spanMatch[2];
      } else {
        code._textContent = body;
        code._children = [];
      }
      const pre = makeEl('pre');
      pre._children = [code];
      pre._textContent = '';
      return pre;
    }
    // Serialize an element to its HTML string form. Used by the innerHTML
    // getter to round-trip through the cache: highlightCode() reads
    // block.innerHTML after Prism's mutation, _applyCachedCodeHighlights()
    // writes that same string back into a freshly-rebuilt block. The test
    // asserts the strings are equal — the serializer just needs to be
    // stable for the simple shapes the stub handles.
    function serializeEl(el) {
      if (!el || el.tagName === undefined) return '';
      const tag = el.tagName.toLowerCase();
      const cls = el._className ? ` class="${el._className}"` : '';
      if (el._children.length === 0) return `<${tag}${cls}>${el._textContent}</${tag}>`;
      const body = el._children.map(c => serializeEl(c)).join('');
      return `<${tag}${cls}>${body}</${tag}>`;
    }
    function makeEl(tag) {
      return {
        tagName: tag.toUpperCase(),
        _className: '',
        _textContent: '',
        _children: [],
        get className() { return this._className; },
        set className(v) { this._className = v; },
        get textContent() {
          if (this._children.length === 0) return this._textContent;
          return this._children.map(c => c.textContent).join('');
        },
        set textContent(v) { this._textContent = String(v); this._children = []; },
        get innerHTML() {
          // Serialize the children. Real DOM serializes by re-rendering
          // the subtree; the test only needs the round-trip identity
          // (cache.set(innerHTML) then later innerHTML=cache + readback)
          // and a non-empty payload for highlighted blocks.
          if (this._children.length === 0) return this._textContent;
          return this._children.map(c => serializeEl(c)).join('');
        },
        set innerHTML(v) {
          this._textContent = '';
          this._children = [];
          if (v === '') return;
          // Try <pre><code>...</code></pre> first (raw, pre-render).
          const pre = parsePreCode(v);
          if (pre) { this._children = [pre]; return; }
          // Then <span ...>T</span> (what Prism writes on a <code>).
          const spanMatch = String(v).match(/^<span(?:\s+class="([^"]*)")?>([\s\S]*)<\/span>$/);
          if (spanMatch) {
            const span = makeEl('span');
            span._className = spanMatch[1] || '';
            span._textContent = spanMatch[2];
            span._children = [];
            this._children = [span];
            return;
          }
          // Fallback: literal text.
          this._textContent = v;
        },
        get dataset() { return this._dataset || (this._dataset = makeDataset()); },
        querySelectorAll(sel) {
          // Test only uses: 'pre code:not([data-highlighted])' and 'code'.
          const out = [];
          const wantPreCodeNotMarked = sel === 'pre code:not([data-highlighted])';
          const wantCode = sel === 'code';
          const walk = (el) => {
            if (el.tagName === 'PRE') {
              for (const c of el._children) {
                if (c.tagName === 'CODE') {
                  const marked = c.dataset.highlighted === '1' || c.dataset.highlighted === 'yes';
                  if (wantPreCodeNotMarked && !marked) out.push(c);
                  else if (wantCode) out.push(c);
                }
              }
            }
            for (const c of el._children) walk(c);
          };
          if (this._children) for (const c of this._children) walk(c);
          return out;
        },
        querySelector(sel) {
          const all = this.querySelectorAll(sel);
          return all.length ? all[0] : null;
        },
      };
    }
    function makeDataset() {
      const ds = {};
      return new Proxy(ds, {
        get(target, prop) {
          if (typeof prop === 'string' && prop in target) return target[prop];
          return undefined;
        },
        set(target, prop, value) {
          if (typeof prop === 'string') {
            target[prop] = String(value);
            return true;
          }
          return false;
        },
        has(target, prop) {
          return typeof prop === 'string' && prop in target;
        },
        deleteProperty(target, prop) {
          if (typeof prop === 'string') {
            delete target[prop];
            return true;
          }
          return false;
        },
      });
    }
    globalThis.document = {
      createElement(tag) { return makeEl(tag); },
    };
    """

    # Build a tiny harness that:
    #  1. builds a container with a code block, runs highlightCode to populate
    #     the cache
    #  2. builds a FRESH container with the SAME source text but no
    #     data-highlighted (simulating a virtualized rebuild), calls
    #     _applyCachedCodeHighlights, and reports what happened
    #  3. builds a fresh container with a DIFFERENT source text (genuinely
    #     new block) and reports that the sync pass left it untouched
    #  4. reports whether a SAME-LANGUAGE different-text block would NOT
    #     cross-collide (textContent is part of the key)
    script = f"""
{dom_stub}
{prism_stub}
{cache_setup}
{cache_key_fn}
{apply_fn}
{highlight_fn}

// 1. First render — populate the cache
const c1 = document.createElement('div');
c1.innerHTML = '<pre><code class="language-javascript">function foo(){{return 1;}}</code></pre>';
highlightCode(c1);
const firstHighlighted = c1.querySelector('code').innerHTML;
const firstMarked = c1.querySelector('code').dataset.highlighted;

// 2. Virtualized rebuild with the SAME source text
const c2 = document.createElement('div');
c2.innerHTML = '<pre><code class="language-javascript">function foo(){{return 1;}}</code></pre>';
const applied = _applyCachedCodeHighlights(c2);
const rebuildCode = c2.querySelector('code');
const rebuildMarked = rebuildCode.dataset.highlighted;
const rebuildInner = rebuildCode.innerHTML;

// 3. Rebuild with a DIFFERENT source text (genuinely new block) — must NOT
//    pick up a cached highlight from the unrelated key
const c3 = document.createElement('div');
c3.innerHTML = '<pre><code class="language-javascript">const x = 42;</code></pre>';
const applied3 = _applyCachedCodeHighlights(c3);
const thirdMarked = c3.querySelector('code').dataset.highlighted || null;
const thirdInner = c3.querySelector('code').innerHTML;

// 4. Cache key isolation: two different languages with the SAME textContent
//    must produce DIFFERENT cache keys (otherwise one would render with the
//    other's token set).
const fakeJs = {{ className: 'language-javascript', textContent: 'echo' }};
const fakePy = {{ className: 'language-python', textContent: 'echo' }};
const sameJs = {{ className: 'language-javascript', textContent: 'echo' }};
const keyJs = _codeHighlightCacheKey(fakeJs);
const keyPy = _codeHighlightCacheKey(fakePy);
const keyJsAgain = _codeHighlightCacheKey(sameJs);

console.log(JSON.stringify({{
  firstHighlighted,
  firstMarked,
  applied,
  rebuildMarked,
  rebuildInnerMatches: rebuildInner === firstHighlighted,
  rebuildInnerDiffersFromRaw: rebuildInner !== 'function foo(){{return 1;}}',
  applied3,
  thirdMarked,
  thirdInner,
  thirdInnerUntouched: thirdInner === 'const x = 42;',
  keyJs,
  keyPy,
  keyJsAgain,
  keyJsIsolatedFromPy: keyJs !== keyPy,
  keyJsStable: keyJs === keyJsAgain,
}}));
"""
    result = subprocess.run(
        [NODE, "-e", script],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"node harness failed: stderr={result.stderr!r} stdout={result.stdout!r}"
    )
    return json.loads(result.stdout)


class TestSyncHighlightBehavior:
    """The contract the #7752 fix delivers, observed end-to-end via node-eval."""

    _snapshot: dict = {}

    @classmethod
    def setup_class(cls):
        # Run the node-eval harness once per class. The behavioral snapshot
        # is deterministic — no setup/teardown needed per test.
        cls._snapshot = _snapshot_via_node()

    def test_first_render_highlights_and_marks_block(self):
        """Sanity: the pre-existing highlightCode() still works after the
        cache-instrumentation edit. The first render must produce a
        data-highlighted marker and a tokenized innerHTML."""
        s = self._snapshot
        assert s["firstMarked"] == "1", (
            "highlightCode() must still set data-highlighted='1' on the first "
            "render — the cache-edit must not have regressed the basic path."
        )
        assert "<span" in s["firstHighlighted"], (
            "highlightCode() must still produce tokenized HTML (Prism stub "
            "emits a wrapping <span class=\"token\">) on the first render."
        )

    def test_rebuild_with_same_text_is_highlighted_synchronously(self):
        """This is the actual #7752 regression: a block rebuilt with the
        same source text must come out of _applyCachedCodeHighlights()
        already highlighted (data-highlighted set + cached innerHTML
        applied)."""
        s = self._snapshot
        assert s["applied"] == 1, (
            f"_applyCachedCodeHighlights() must report exactly 1 applied hit "
            f"for a single rebuilt block whose source was previously "
            f"highlighted, got applied={s['applied']}."
        )
        assert s["rebuildMarked"] == "1", (
            "The rebuilt <pre><code> must be stamped data-highlighted='1' by "
            "_applyCachedCodeHighlights() — the absence of this marker is "
            "the #7752 bug: the rAF pass would then re-tokenize it next "
            "frame and the block paints unhighlighted in between."
        )
        assert s["rebuildInnerMatches"], (
            "The rebuilt block's innerHTML must match the previously-cached "
            "highlighted innerHTML — that is what the user sees on the first "
            "frame after the rebuild."
        )
        assert s["rebuildInnerDiffersFromRaw"], (
            "The rebuilt block's innerHTML must NOT be the raw source text — "
            "if it is, the sync pass did nothing and the block is still "
            "unhighlighted (the bug)."
        )

    def test_genuinely_new_block_is_left_for_raf(self):
        """Pin the negative contract: a code block whose text is NOT in the
        cache must NOT be touched by _applyCachedCodeHighlights(). The rAF
        post-process handles first-appearance blocks — the sync pass must
        not duplicate that work or, worse, write a wrong cache entry."""
        s = self._snapshot
        assert s["applied3"] == 0, (
            f"_applyCachedCodeHighlights() must report 0 applied hits for a "
            f"block whose source is not in the cache, got "
            f"applied={s['applied3']}."
        )
        assert s["thirdMarked"] is None, (
            "A genuinely-new code block (no cache entry) must NOT be stamped "
            "data-highlighted by the sync pass — the deferred rAF "
            "post-process is responsible for first-appearance blocks, and "
            "premature stamping would break the per-block highlight contract."
        )
        assert s["thirdInnerUntouched"], (
            "A genuinely-new code block's innerHTML must remain the raw "
            "source after the sync pass — the rAF will tokenize it next "
            "frame exactly as it would have before this fix."
        )

    def test_cache_key_isolates_by_language(self):
        """Two blocks with identical textContent but different languages must
        produce different cache keys — otherwise the same string in two
        languages would cross-render with the wrong token set."""
        s = self._snapshot
        assert s["keyJsIsolatedFromPy"], (
            "_codeHighlightCacheKey must include the language in the cache "
            "key — same textContent in two different languages must NOT "
            "collide on the same entry."
        )

    def test_cache_key_is_stable(self):
        """Same input must always produce the same key — otherwise the
        cache hit rate is zero and the fix is a no-op."""
        s = self._snapshot
        assert s["keyJsStable"], (
            "_codeHighlightCacheKey must be deterministic — two blocks with "
            "the same className+textContent must hit the same cache entry, "
            "or the sync pass never fires."
        )


# ── Negative / regression-prevention checks ──────────────────────────────────


class TestFixDoesNotRegressExistingBehavior:
    """The fix must not change the EXISTING highlight contract: blocks that
    are already highlighted must still be skipped by highlightCode, the
    deferred rAF post-process must still run for genuinely new blocks, and
    the rebuild path must still schedule it."""

    def test_highlight_code_still_skips_already_marked_blocks(self):
        """The `pre code:not([data-highlighted])` selector is the perf
        guarantee that #7677 etc. rely on. The cache must not weaken it."""
        src = _read_ui_js()
        body = _extract_function_body(src, "function highlightCode(")
        assert "pre code:not([data-highlighted])" in body, (
            "highlightCode must keep the `pre code:not([data-highlighted])` "
            "selector — the cache edit must not weaken the skip-already-"
            "highlighted perf guarantee."
        )

    def test_rebuild_path_still_schedules_raf_post_process(self):
        """The deferred rAF post-process is by design (#20052 / #20082
        comments) and must keep running for genuinely new code blocks."""
        src = _read_ui_js()
        assert (
            "requestAnimationFrame(()=>_postProcessWithAnchorSuppression(inner))"
            in src
        ), (
            "The rebuild path must still schedule the rAF post-process — "
            "the #7752 fix adds a sync pre-pass BEFORE it, it does not "
            "replace it."
        )

    def test_helper_is_perf_bounded(self):
        """The helper must skip on empty containers (no querySelectorAll
        walk of the full message tree when there is nothing to do)."""
        src = _read_ui_js()
        body = _extract_function_body(src, "function _applyCachedCodeHighlights(")
        assert "blocks.length === 0" in body, (
            "_applyCachedCodeHighlights must early-return on an empty "
            "selector result — otherwise it walks the full message tree on "
            "every render even when no unhighlighted blocks exist, "
            "regressing scroll perf."
        )
        assert "querySelectorAll" in body, (
            "_applyCachedCodeHighlights must use querySelectorAll (not a "
            "manual walk) so it stays O(n) and matches highlightCode's "
            "selector."
        )
