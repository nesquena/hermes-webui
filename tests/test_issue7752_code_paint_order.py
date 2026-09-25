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
        # The cache write may be inlined (legacy) or delegated to a writer
        # helper (#7778). Accept either as long as the cache is populated
        # from highlightCode — that is the actual contract.
        writes_cache = (
            "_codeHighlightCache.set(" in body
            or "_writeCodeHighlightCache(" in body
        )
        assert writes_cache, (
            "highlightCode must populate _codeHighlightCache (either inline "
            "via _codeHighlightCache.set or via a writer helper like "
            "_writeCodeHighlightCache) — that is the only way to record "
            "each block it highlights for the next virtualized rebuild "
            "(#7752 + #7778)."
        )
        # If the writer-helper path is taken, the helper itself must do the
        # set() — otherwise the contract is broken.
        if "_writeCodeHighlightCache(" in body and "_codeHighlightCache.set(" not in body:
            helper = _extract_function_body(src, "function _writeCodeHighlightCache(")
            assert "_codeHighlightCache.set(" in helper, (
                "_writeCodeHighlightCache must write to _codeHighlightCache "
                "— it is the writer called from highlightCode()."
            )

    def test_cache_has_size_cap(self):
        """A naive unbounded cache would grow for the lifetime of the page.
        Pin a cap so a long-lived instance with many distinct code blocks
        cannot blow up memory."""
        src = _read_ui_js()
        # The cap check may live in highlightCode (legacy) or in the writer
        # helper called from highlightCode (#7778). Accept either.
        for sig in ("function highlightCode(", "function _writeCodeHighlightCache("):
            body = _extract_function_body(src, sig)
            if "_CODE_HIGHLIGHT_CACHE_MAX" in body:
                break
        else:
            body = ""
        assert "_CODE_HIGHLIGHT_CACHE_MAX" in body, (
            "Either highlightCode or _writeCodeHighlightCache must bound "
            "_codeHighlightCache size via _CODE_HIGHLIGHT_CACHE_MAX to "
            "prevent unbounded growth across the lifetime of a page with "
            "many distinct code blocks."
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
    # #7778: cache write and Prism-hooks registration are now in helper
    # functions called from highlightCode. Pull them so the node harness
    # can resolve the names.
    write_fn = extract("function _writeCodeHighlightCache(")
    register_hook_fn = extract("function _maybeRegisterPrismCompleteHook(")

    # Extract the cache constant + map declarations. They sit on two lines
    # ABOVE `_codeHighlightCacheKey` in the source; capture both lines by
    # looking for the unique `_CODE_HIGHLIGHT_CACHE_MAX = ` token.
    cache_consts = []
    for marker in (
        "_CODE_HIGHLIGHT_CACHE_MAX = ",
        "const _codeHighlightCache = new Map();",
        # #7778: extra state for the tokenized-form gate and the hook
        # registration idempotency flag.
        "let _prismCompleteHookRegistered = false;",
        "const _CODE_HIGHLIGHT_TOKEN_RE = ",
    ):
        idx = src.find(marker)
        if idx == -1:
            raise AssertionError(
                f"cache setup token {marker!r} not found in static/ui.js — "
                f"the cache-instrumentation edit must be present for #7752 / #7778."
            )
        # Capture the line end (assume single-line declarations; verify).
        end = src.index("\n", idx)
        cache_consts.append(src[idx:end])

    cache_setup = "\n".join(cache_consts)

    # Minimal Prism stub: highlightElement sets data-highlighted on the
    # element and wraps the contents in a single token span so the cache
    # payload differs from the unhighlighted innerHTML. We also stub
    # `Prism.hooks.add` as a no-op so _maybeRegisterPrismCompleteHook
    # can complete cleanly without touching a real Prism instance.
    prism_stub = """
    globalThis.Prism = {
      hooks: {
        // No-op hook registry. The #7778 path tries to register a
        // 'complete' hook; we don't need it to do anything in this
        // happy-path test (the cache is populated synchronously by
        // the writer helper because highlightElement tokenizes right
        // here). The autoloader-deferred scenario is exercised by the
        // dedicated TestAutoloaderDeferred class below.
        add() {},
        run() {},
      },
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
{write_fn}
{register_hook_fn}
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


# ── #7778 regression: only cache actually-tokenized HTML ────────────────────


# Module-level sandbox re-declarations for the #7778 node harness. The
# happy-path harness above (TestSyncHighlightBehavior) is sufficient for
# that scenario, but the autoloader-deferred case needs a *different*
# Prism stub — one that defers when the language isn't loaded and then
# fires 'complete' after the async re-highlight. Re-declaring the DOM
# helpers here (rather than reusing the ones above) keeps the two
# harnesses independent so a future DOM-stub change does not silently
# affect the regression test.
_AUTOLOADER_DOM_STUB = r"""
function _autoloaderParsePreCode(html) {
  const m = String(html).match(/^<pre>([\s\S]*?)<\/pre>$/);
  if (!m) return null;
  const inner = m[1];
  const cm = inner.match(/^<code(?:\s+class="([^"]*)")?>([\s\S]*)<\/code>$/);
  if (!cm) return null;
  const code = _autoloaderMakeEl('code');
  code._className = cm[1] || '';
  // textContent stays the underlying source — the cache key must match
  // the raw form so a rebuild hits the same entry.
  code._textContent = cm[2];
  code._children = [];
  const pre = _autoloaderMakeEl('pre');
  pre._children = [code];
  pre._textContent = '';
  return pre;
}
function _autoloaderSerializeEl(el) {
  if (!el || el.tagName === undefined) return '';
  const tag = el.tagName.toLowerCase();
  const cls = el._className ? ` class="${el._className}"` : '';
  if (el._children.length === 0) return `<${tag}${cls}>${el._textContent}</${tag}>`;
  const body = el._children.map(c => _autoloaderSerializeEl(c)).join('');
  return `<${tag}${cls}>${body}</${tag}>`;
}
function _autoloaderMakeEl(tag) {
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
      if (this._children.length === 0) return this._textContent;
      return this._children.map(c => _autoloaderSerializeEl(c)).join('');
    },
    set innerHTML(v) {
      this._textContent = '';
      this._children = [];
      if (v === '') return;
      // Accept <pre><code>...</code></pre> (raw or tokenized).
      const pre = _autoloaderParsePreCode(v);
      if (pre) { this._children = [pre]; return; }
      // Accept a flat <span>T</span> (Prism's tokenized form, when the
      // <code> element's own innerHTML is read directly).
      const spanMatch = String(v).match(/^<span(?:\s+class="([^"]*)")?>([\s\S]*)<\/span>$/);
      if (spanMatch) {
        const span = _autoloaderMakeEl('span');
        span._className = spanMatch[1] || '';
        span._textContent = spanMatch[2];
        span._children = [];
        this._children = [span];
        return;
      }
      this._textContent = v;
    },
    get dataset() { return this._dataset || (this._dataset = _autoloaderMakeDataset()); },
    querySelectorAll(sel) {
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
function _autoloaderMakeDataset() {
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
  });
}
globalThis.document = {
  createElement(tag) { return _autoloaderMakeEl(tag); },
};
"""


def _autoloader_snapshot_via_node() -> dict:
    """Run the autoloader-deferred scenario in a node vm sandbox.

    Steps:
      1. Build c1 with a code block. Run highlightCode(c1) — the Prism
         stub defers (no grammar loaded) so the block is NOT tokenized
         and the cache MUST stay empty.
      2. Simulate the autoloader finishing its async fetch: install a
         grammar into Prism.languages and re-run highlightElement on
         the same block. The stub now tokenizes and fires the
         'complete' hook, which must populate the cache.
      3. Build c2 with the SAME source text (simulating a virtualized
         rebuild). Call _applyCachedCodeHighlights(c2) — the rebuilt
         block must come out with the tokenized innerHTML and
         data-highlighted='1' on the very first frame.
    """
    assert NODE, "node is required for #7778 behavioral test"
    src = _read_ui_js()

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
    write_fn = extract("function _writeCodeHighlightCache(")
    register_hook_fn = extract("function _maybeRegisterPrismCompleteHook(")
    apply_fn = extract("function _applyCachedCodeHighlights(")
    highlight_fn = extract("function highlightCode(")

    cache_consts = []
    for marker in (
        "_CODE_HIGHLIGHT_CACHE_MAX = ",
        "const _codeHighlightCache = new Map();",
        "let _prismCompleteHookRegistered = false;",
        "const _CODE_HIGHLIGHT_TOKEN_RE = ",
    ):
        idx = src.find(marker)
        if idx == -1:
            raise AssertionError(
                f"cache setup token {marker!r} not found in static/ui.js — "
                f"the #7778 cache-instrumentation edit must be present."
            )
        end = src.index("\n", idx)
        cache_consts.append(src[idx:end])
    cache_setup = "\n".join(cache_consts)

    # Autoloader-simulating Prism stub.
    #
    #   * highlightElement(el): if the block's language is in
    #     Prism.languages, tokenize the innerHTML with a real token
    #     span and fire Prism.hooks.run('complete', { element: el }).
    #     If the language is NOT in Prism.languages, do nothing (the
    #     autoloader would defer to a CDN fetch and return the raw
    #     source unchanged).
    #   * The 'complete' hook is what writes the cache with the
    #     post-defer tokenized HTML. This is the exact contract the
    #     production code now relies on.
    prism_stub = """
    const _autoloaderHookRegistry = {};
    globalThis.Prism = {
      languages: {},  // starts empty — autoloader hasn't fetched yet
      hooks: {
        add(name, fn) {
          (_autoloaderHookRegistry[name] = _autoloaderHookRegistry[name] || []).push(fn);
        },
        run(name, env) {
          for (const fn of (_autoloaderHookRegistry[name] || [])) fn(env);
        },
      },
      highlightElement(el) {
        const m = (el.className || '').match(/language-([\\w-]+)/);
        const lang = m ? m[1] : '';
        // Autoloader behavior: defer when the grammar isn't loaded.
        // The element is unchanged (raw text), and Prism does NOT
        // fire 'complete' — the autoloader will fire it later, after
        // the CDN fetch resolves.
        if (!globalThis.Prism.languages[lang]) return;
        // Grammar loaded: tokenize by wrapping the existing source in
        // a single token span. textContent is preserved (we serialize
        // the same string the element already has), which is critical
        // for the cache key to match the raw form on rebuild.
        el.innerHTML = '<span class="token">' + el.textContent + '</span>';
        globalThis.Prism.hooks.run('complete', { element: el });
      },
    };
    """

    # We re-call highlightElement on c1's block AFTER the grammar
    # is installed, to simulate the autoloader's deferred re-highlight
    # firing on the original element. (In real Prism the autoloader
    # would do this internally; we drive it explicitly so the test
    # is deterministic and doesn't depend on real CDN timing.)
    script = f"""
{_AUTOLOADER_DOM_STUB}
{prism_stub}
{cache_setup}
{cache_key_fn}
{write_fn}
{register_hook_fn}
{apply_fn}
{highlight_fn}

// 1. First render with an unloaded grammar. highlightCode stamps
//    data-highlighted='1' and calls _writeCodeHighlightCache, but
//    Prism.highlightElement deferred, so the innerHTML is still raw
//    and the writer refuses the entry.
const c1 = document.createElement('div');
c1.innerHTML = '<pre><code class="language-rust">fn main(){{}}</code></pre>';
highlightCode(c1);
const c1Code = c1.querySelector('code');
const c1InnerAfterDefer = c1Code.innerHTML;
const c1MarkedAfterDefer = c1Code.dataset.highlighted;
const cacheSizeAfterDefer = _codeHighlightCache.size;

// 2. Autoloader finishes: install the grammar and re-run the
//    autoloader's deferred re-highlight on the same block.
globalThis.Prism.languages['rust'] = {{ /* grammar */ }};
globalThis.Prism.highlightElement(c1Code);  // autoloader's re-highlight
const c1InnerAfterAutoload = c1Code.innerHTML;
const cacheSizeAfterAutoload = _codeHighlightCache.size;
const cacheKeyAfterAutoload = (() => {{
  let k = null, v = null;
  for (const [kk, vv] of _codeHighlightCache) {{ k = kk; v = vv; break; }}
  return {{ k, v }};
}})();

// 3. Virtualized rebuild with the same source text.
const c2 = document.createElement('div');
c2.innerHTML = '<pre><code class="language-rust">fn main(){{}}</code></pre>';
const applied = _applyCachedCodeHighlights(c2);
const c2Code = c2.querySelector('code');
const c2Inner = c2Code.innerHTML;
const c2Marked = c2Code.dataset.highlighted;

console.log(JSON.stringify({{
  c1InnerAfterDefer,
  c1MarkedAfterDefer,
  cacheSizeAfterDefer,
  c1InnerAfterAutoload,
  c1InnerIsTokenized: c1InnerAfterAutoload.includes('class="token'),
  cacheSizeAfterAutoload,
  cacheKeyAfterAutoload,
  cacheValueIsTokenized: cacheKeyAfterAutoload.v !== null && cacheKeyAfterAutoload.v.includes('class="token'),
  applied,
  c2Inner,
  c2InnerIsTokenized: c2Inner.includes('class="token'),
  c2InnerDiffersFromRaw: c2Inner !== 'fn main(){{}}',
  c2Marked,
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


class TestAutoloaderDeferred:
    """The #7778 regression: Prism's autoloader asynchronously fetches
    language grammars from a CDN. The first ``Prism.highlightElement``
    call for an unloaded language does NOT tokenize (the autoloader
    defers) but DOES return. Pre-fix, the cache writer would then
    store the raw innerHTML and stamp ``data-highlighted='1'``,
    permanently locking the block as "highlighted" with no tokens
    — the rAF post-process would skip it forever and the user would
    see un-tokenized code indefinitely.

    Post-fix, three guards prevent this:
      1. The cache writer refuses entries without ``class="token"``
         spans, so the pre-autoload raw form never lands.
      2. A ``Prism.hooks.add('complete', …)`` listener re-writes
         the cache when the autoloader's deferred re-highlight
         finishes.
      3. ``_applyCachedCodeHighlights`` refuses to apply a cache
         entry that lacks token spans.

    These tests pin the contract end-to-end via node-eval.
    """

    _snapshot: dict = {}

    @classmethod
    def setup_class(cls):
        cls._snapshot = _autoloader_snapshot_via_node()

    def test_pre_autoload_does_not_populate_cache(self):
        """The first highlightCode() call (autoloader deferred) must
        NOT populate the cache — that's the bug. Stamping
        data-highlighted='1' is OK (that's our contract); caching the
        raw innerHTML is the failure mode (#7778)."""
        s = self._snapshot
        assert s["c1MarkedAfterDefer"] == "1", (
            "highlightCode must still stamp data-highlighted='1' on the "
            "first call — the contract is that the block is one of ours, "
            "the autoloader just hasn't tokenized it yet."
        )
        assert s["c1InnerAfterDefer"] == "fn main(){}", (
            "With the autoloader deferred, the block's innerHTML must "
            "still be the raw source — that's the pre-#7778 state the "
            "cache writer must refuse."
        )
        assert s["cacheSizeAfterDefer"] == 0, (
            f"Cache must be EMPTY after the deferred first call — the "
            f"writer must refuse the raw innerHTML. Got cache size "
            f"{s['cacheSizeAfterDefer']}. This is the exact #7778 bug: "
            f"caching the pre-autoload raw form locks the block as "
            f"'highlighted' with no tokens."
        )

    def test_complete_hook_populates_cache_after_autoload(self):
        """The 'complete' hook is Prism's canonical signal that
        tokenization finished. When the autoloader's deferred
        re-highlight fires it, the cache must be updated with the
        post-tokenization innerHTML."""
        s = self._snapshot
        assert s["c1InnerIsTokenized"], (
            "After the autoloader's re-highlight, the block's innerHTML "
            "must contain `class=\"token\"` spans — the autoloader "
            "scenario is only meaningful if Prism actually tokenized."
        )
        assert s["cacheSizeAfterAutoload"] == 1, (
            f"Cache size must be 1 after the autoloader's re-highlight "
            f"(the 'complete' hook should have written one entry). "
            f"Got {s['cacheSizeAfterAutoload']}. Without this, the "
            f"sync-rebuild pass on the next virtualized rebuild has "
            f"nothing to apply."
        )
        assert s["cacheValueIsTokenized"], (
            "The cache entry written by the 'complete' hook must "
            "contain `class=\"token\"` spans — caching raw HTML is "
            "the #7778 bug. The hook must call _writeCodeHighlightCache "
            "(which enforces this invariant) rather than writing "
            "directly to the Map."
        )

    def test_rebuild_after_autoload_applies_tokenized_cache(self):
        """The full bug-and-fix loop: after the autoloader finishes,
        a virtualized rebuild of the same code block must come out
        of _applyCachedCodeHighlights() with the tokenized innerHTML
        and data-highlighted='1' on the very first frame. Pre-#7778,
        this would be the raw source (or no entry at all if the
        raw entry was refused on the first call)."""
        s = self._snapshot
        assert s["applied"] == 1, (
            f"_applyCachedCodeHighlights must report exactly 1 applied "
            f"hit for a rebuilt block whose source was previously "
            f"tokenized (post-autoload). Got applied={s['applied']}."
        )
        assert s["c2InnerIsTokenized"], (
            "The rebuilt block's innerHTML must contain "
            "`class=\"token\"` spans — the sync-rebuild pass must "
            "apply the actually-tokenized cache entry, not the raw "
            "source. Pre-#7778 the cache either held the raw form "
            "(locked un-highlighted) or no entry (regressed to "
            "pre-#7752 behavior)."
        )
        assert s["c2InnerDiffersFromRaw"], (
            "The rebuilt block's innerHTML must differ from the raw "
            "source — a cache hit that returns the raw form is the "
            "#7778 bug."
        )
        assert s["c2Marked"] == "1", (
            "The rebuilt block must be stamped data-highlighted='1' "
            "by the sync pass — without this, the rAF post-process "
            "would re-tokenize and the one-frame flash (the original "
            "#7752 bug) would come back. The cache entry from the "
            "autoloader-deferred path must be valid for the sync "
            "pass to apply it correctly."
        )


# ── Cache key isolation: copy button / line number / decoration pollution ────


def _cache_key_isolation_snapshot() -> dict:
    """Verify the cache key is built ONLY from the language-xxx class
    and the block's own textContent — not from sibling decorations.

    Specifically:
      * A copy button in the <pre> (or a header before <pre>) is a
        SIBLING of <code>, so its textContent does NOT bleed into
        the <code>'s textContent. Two blocks with the same code but
        different copy-button states must hash to the same key.
      * The block's className may include other classes beyond
        "language-xxx" (e.g. "hljs", "code-block", editor wrappers).
        Only the first language-xxx match is used; the rest are
        ignored.
      * Whitespace inside the className (multiple spaces) is fine.
    """
    assert NODE, "node is required for cache-key isolation test"
    src = _read_ui_js()

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

    # Pure-function probe — no DOM, no Prism, no cache state.
    # We fabricate block-like objects and call the key function
    # directly, asserting the resulting keys match (or differ) as
    # expected.
    script = f"""
{cache_key_fn}

// Two blocks with the SAME code but different surrounding decorations.
// addCopyButtons() places the copy button as a SIBLING of <code> (inside
// <pre> or in a header before <pre>), NOT inside <code>. So the
// textContent of the <code> block is unaffected — both blocks must
// hash to the same key.
const plain = {{ className: 'language-javascript', textContent: 'const x = 1;' }};
const withCopyBtn = {{ className: 'language-javascript', textContent: 'const x = 1;' }};
// (The copy button is a sibling; it does not modify textContent.)
const withExtraClasses = {{
  className: 'hljs language-javascript code-block some-other-class',
  textContent: 'const x = 1;',
}};
const withMultiSpace = {{
  className: 'language-javascript   foo',
  textContent: 'const x = 1;',
}};
const differentCode = {{ className: 'language-javascript', textContent: 'const y = 2;' }};
const differentLang = {{ className: 'language-python', textContent: 'const x = 1;' }};

const keyPlain = _codeHighlightCacheKey(plain);
const keyWithCopyBtn = _codeHighlightCacheKey(withCopyBtn);
const keyWithExtraClasses = _codeHighlightCacheKey(withExtraClasses);
const keyWithMultiSpace = _codeHighlightCacheKey(withMultiSpace);
const keyDifferentCode = _codeHighlightCacheKey(differentCode);
const keyDifferentLang = _codeHighlightCacheKey(differentLang);

console.log(JSON.stringify({{
  keyPlain,
  keyWithCopyBtn,
  keyWithExtraClasses,
  keyWithMultiSpace,
  keyDifferentCode,
  keyDifferentLang,
  copyButtonDoesNotPollute: keyPlain === keyWithCopyBtn,
  extraClassesDoNotPollute: keyPlain === keyWithExtraClasses,
  multiSpaceOk: keyPlain === keyWithMultiSpace,
  differentCodeIsolated: keyPlain !== keyDifferentCode,
  differentLangIsolated: keyPlain !== keyDifferentLang,
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


class TestCacheKeyIsolation:
    """The cache key MUST be stable across rebuilds and across
    decoration differences. If copy buttons, line numbers, or other
    decorations leaked into the key, the same code would hash to
    different entries on different renders and the sync-rebuild pass
    would never hit (#7752) — or worse, would cross-collide between
    two unrelated blocks (#7778)."""

    _snapshot: dict = {}

    @classmethod
    def setup_class(cls):
        cls._snapshot = _cache_key_isolation_snapshot()

    def test_copy_button_sibling_does_not_pollute_key(self):
        """The copy button lives in <pre> (or in a header before <pre>),
        NOT inside <code>. The cache key is built from the <code>'s
        own textContent, which excludes siblings — so the same code
        with and without a copy button must hash to the same key."""
        s = self._snapshot
        assert s["copyButtonDoesNotPollute"], (
            f"Cache key for a block with the same code (and a sibling "
            f"copy button) must equal the key without the copy button. "
            f"Got plain={s['keyPlain']!r} vs copyBtn="
            f"{s['keyWithCopyBtn']!r}. If the key walks siblings or "
            f"ancestors, a render with and without a copy button would "
            f"miss the cache and the user would see a one-frame flash."
        )

    def test_extra_classes_in_classname_do_not_pollute_key(self):
        """The block's className may include classes other than
        language-xxx (e.g. hljs, code-block, editor wrappers). Only
        the first language-xxx match is used; extra classes are
        ignored. This pins the regex's anchor and capture behavior."""
        s = self._snapshot
        assert s["extraClassesDoNotPollute"], (
            f"Cache key must not change when extra classes (besides "
            f"language-xxx) are added to the block's className. "
            f"Got plain={s['keyPlain']!r} vs extra="
            f"{s['keyWithExtraClasses']!r}. The regex must anchor on "
            f"the first `language-<lang>` token, not on the entire "
            f"className."
        )

    def test_multispace_classname_is_handled(self):
        """The className may contain multiple consecutive spaces
        (e.g. from a templating language). The regex must still
        match the first language-xxx token."""
        s = self._snapshot
        assert s["multiSpaceOk"], (
            f"Cache key must be stable across extra whitespace in "
            f"the className. Got plain={s['keyPlain']!r} vs "
            f"multiSpace={s['keyWithMultiSpace']!r}."
        )

    def test_different_code_produces_different_key(self):
        """Sanity: changing the source text must change the key.
        (Pinned so a future refactor doesn't accidentally drop
        textContent from the key.)"""
        s = self._snapshot
        assert s["differentCodeIsolated"], (
            f"Cache key for different source text must differ. "
            f"Got plain={s['keyPlain']!r} vs diffCode="
            f"{s['keyDifferentCode']!r}."
        )

    def test_different_language_produces_different_key(self):
        """Sanity: same textContent in two different languages must
        produce different keys. (Pinned so a future refactor doesn't
        accidentally drop the language from the key — that would
        cause cross-rendering between languages.)"""
        s = self._snapshot
        assert s["differentLangIsolated"], (
            f"Cache key for same textContent in different languages "
            f"must differ. Got js={s['keyPlain']!r} vs py="
            f"{s['keyDifferentLang']!r}."
        )


# ── #7778 static-source guards ──────────────────────────────────────────────


class TestNoRawHtmlInCacheWrites:
    """The #7778 contract: the cache writer MUST refuse to store
    anything that isn't actually tokenized. This is enforced by a
    shared regex; pin the source so a future refactor can't quietly
    drop the guard."""

    def test_token_regex_constant_declared(self):
        src = _read_ui_js()
        assert "const _CODE_HIGHLIGHT_TOKEN_RE" in src, (
            "_CODE_HIGHLIGHT_TOKEN_RE must be declared as a module-"
            "level const — it is the shared gate that enforces "
            "`class=\"token\"` at both write time and read time (#7778)."
        )
        m = re.search(
            r"const\s+_CODE_HIGHLIGHT_TOKEN_RE\s*=\s*(/[^/]+/[gimsuy]*)",
            src,
        )
        assert m, (
            "_CODE_HIGHLIGHT_TOKEN_RE must be a regex literal — a "
            "string would not enforce the `class=\"token\"` gate."
        )

    def test_writer_helper_enforces_tokenized_form(self):
        """The cache writer must check innerHTML for `class=\"token\"`
        before storing. Without this, the pre-autoload raw form would
        land in the cache and lock the block as 'highlighted' forever
        (#7778)."""
        src = _read_ui_js()
        body = _extract_function_body(src, "function _writeCodeHighlightCache(")
        assert "_CODE_HIGHLIGHT_TOKEN_RE" in body, (
            "_writeCodeHighlightCache must check _CODE_HIGHLIGHT_TOKEN_RE "
            "before writing to the cache — that's the primary #7778 "
            "guard against caching pre-autoload raw innerHTML."
        )
        # The writer must run BEFORE _codeHighlightCache.set() — i.e.
        # the token check must short-circuit the write path.
        check_pos = body.find("_CODE_HIGHLIGHT_TOKEN_RE")
        set_pos = body.find("_codeHighlightCache.set(")
        assert check_pos != -1 and set_pos != -1 and check_pos < set_pos, (
            "The token-form check must appear BEFORE the cache.set() "
            "call in _writeCodeHighlightCache — otherwise raw innerHTML "
            "could land in the cache before the guard runs."
        )

    def test_apply_helper_refuses_untokenized_cache_entries(self):
        """Even if the cache somehow holds a non-tokenized entry
        (legacy state, future regression), _applyCachedCodeHighlights
        must NOT stamp data-highlighted='1' on it. Otherwise the rAF
        post-process would skip the block and the user would see
        un-tokenized code indefinitely (#7778)."""
        src = _read_ui_js()
        body = _extract_function_body(src, "function _applyCachedCodeHighlights(")
        assert "_CODE_HIGHLIGHT_TOKEN_RE" in body, (
            "_applyCachedCodeHighlights must check _CODE_HIGHLIGHT_TOKEN_RE "
            "before applying a cache entry and stamping data-highlighted='1' — "
            "this is the defensive #7778 guard at the read side."
        )
        # The check must run BEFORE the dataset.highlighted='1' stamp —
        # otherwise an untokenized entry would still mark the block.
        check_pos = body.find("_CODE_HIGHLIGHT_TOKEN_RE")
        stamp_pos = body.find("dataset.highlighted = '1'")
        assert check_pos != -1 and stamp_pos != -1 and check_pos < stamp_pos, (
            "The token-form check must appear BEFORE the "
            "data-highlighted='1' stamp in _applyCachedCodeHighlights — "
            "otherwise the rAF post-process would skip the block "
            "indefinitely (#7778)."
        )

    def test_prism_complete_hook_is_registered(self):
        """The autoloader scenario requires a 'complete' hook listener
        that re-writes the cache when the deferred re-highlight
        finishes. Pin the registration site."""
        src = _read_ui_js()
        body = _extract_function_body(src, "function _maybeRegisterPrismCompleteHook(")
        assert "Prism.hooks.add" in body, (
            "_maybeRegisterPrismCompleteHook must call Prism.hooks.add "
            "to register the 'complete' listener — that's how the "
            "autoloader's deferred re-highlight updates the cache (#7778)."
        )
        assert "'complete'" in body, (
            "The hook must subscribe to the 'complete' event — that's "
            "Prism's canonical 'tokenization done' signal."
        )
        assert "_writeCodeHighlightCache" in body, (
            "The hook must call _writeCodeHighlightCache (not the Map "
            "directly) so the tokenized-form invariant is enforced on "
            "the autoloader path too."
        )

    def test_highlight_code_invokes_hook_registration(self):
        """The hook must be registered on the first highlightCode()
        call (lazy init). Without this call site, the autoloader
        scenario is never healed."""
        src = _read_ui_js()
        body = _extract_function_body(src, "function highlightCode(")
        assert "_maybeRegisterPrismCompleteHook" in body, (
            "highlightCode must call _maybeRegisterPrismCompleteHook — "
            "that's how the 'complete' hook gets installed. The "
            "registration is lazy/idempotent so this is safe to call "
            "on every render."
        )
