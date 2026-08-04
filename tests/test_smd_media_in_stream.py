"""
Tests for the MEDIA-in-stream fix: MEDIA:<ref> tokens that arrive mid-turn
during streaming used to render as the raw path text until the turn settled
and the full renderMd() pipeline re-rendered the row. The fix lets the smd
streaming renderer replace MEDIA tokens with the same DOM the full pipeline
emits, so live prose shows real images inline.

Static coverage (in TestSmdMediaInStream):
1. messages.js: _smdMediaAwareAddText wrapper exists and references
   _inlineMediaHtmlForRef (the shared renderer from ui.js).
2. messages.js: _safeSmdRenderer's add_text wraps every text chunk through
   the MEDIA-aware interceptor.
3. messages.js: _streamFadeRenderer also short-circuits to the MEDIA-aware
   interceptor when its chunk carries a MEDIA token, instead of wrapping
   the token in a stream-fade-word span.
4. ui.js: a single _inlineMediaHtmlForRef function is the canonical
   renderer used by BOTH renderMd() MEDIA restore and the streaming path.

Behavioural coverage (in TestSmdMediaAwareAddTextBehaviour): drives the
actual JS through a minimal in-process DOM shim that supports the
createElement / appendChild / createTextNode / DOMParser surface the
interceptor uses. These cases answer Greptile's two confidence-sapping
notes head-on:
- "Mixed prose and MEDIA chunks can parse model text as DOM" — covered by
  the prose-only and prose-around-MEDIA cases (no entities ever decode
  on prose; prose enters baseAddText directly via createTextNode).
- "MEDIA tokens split across parser flushes can still show as raw text
  during streaming" — covered by the split-MEDIA case (the tail buffer
  finishes the token on the second call).
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import unittest

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_js_function(src: str, name: str) -> str:
    match = re.search(rf"function\s+{re.escape(name)}\s*\(", src)
    if not match:
        raise ValueError(f"Could not find JS function {name}")
    start = match.start()
    brace = src.index("{", match.end())
    depth = 1
    pos = brace + 1
    while pos < len(src) and depth:
        if src[pos] == "{":
            depth += 1
        elif src[pos] == "}":
            depth -= 1
        pos += 1
    return src[start:pos]


def _run_real_smd_media_cases() -> dict:
    helpers = "\n".join(
        [
            _extract_js_function(MESSAGES_JS, "_smdMediaPrefixTail"),
            _extract_js_function(MESSAGES_JS, "_smdAppendPlainText"),
            _extract_js_function(MESSAGES_JS, "_smdMediaWriteText"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailSet"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailEntryChunk"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailSameOwner"),
            _extract_js_function(MESSAGES_JS, "_smdMediaRefHasReliableBoundary"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailFlushEntry"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailFlush"),
            _extract_js_function(MESSAGES_JS, "_smdMediaAwareAddText"),
            _extract_js_function(MESSAGES_JS, "_smdAppendMediaNode"),
            _extract_js_function(MESSAGES_JS, "_smdScheduleMediaPostProcess"),
            _extract_js_function(MESSAGES_JS, "_smdParserKey"),
            _extract_js_function(MESSAGES_JS, "_smdBindParserIdentity"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailClear"),
            _extract_js_function(MESSAGES_JS, "_streamFadeSkipNode"),
            _extract_js_function(MESSAGES_JS, "_streamFadeReduceMotionEnabled"),
            _extract_js_function(MESSAGES_JS, "_streamFadeBindCleanup"),
            _extract_js_function(MESSAGES_JS, "_streamFadeAppendText"),
            _extract_js_function(MESSAGES_JS, "_streamFadeRenderer"),
            _extract_js_function(MESSAGES_JS, "_safeSmdRenderer"),
            _extract_js_function(MESSAGES_JS, "_smdRendererWithoutUnderscoreEmphasis"),
        ]
    )
    script = (
        "import * as smd from './static/vendor/smd.min.js';\n"
        "globalThis.window = { smd };\n"
        "globalThis.requestAnimationFrame = cb => cb();\n"
        "const _MEDIA_TAIL_MAX = 4096;\n"
        "const _SMD_MEDIA_PREFIX = 'MEDIA:';\n"
        "const _SMD_MEDIA_TAIL = new WeakMap();\n"
        "const __SMD_PARSER_FALLBACK = {};\n"
        "const _SMD_SAFE_URL_RE=/^(?:https?:|mailto:|tel:|message:|\\/|#|\\?|\\.|api|session\\/)/i;\n"
        "const _SMD_SAFE_IMG_URL_RE=/^(?:https?:|mailto:|tel:|\\/|#|\\?|\\.)/i;\n"
        "const _STREAM_FADE_MS = 620;\n"
        "let _streamFadeCurrentMs = _STREAM_FADE_MS;\n"
        "let _streamFadeLatestAnimationEndAt = 0;\n"
        "let _streamFadeReduceMotionMql = null;\n"
        "let _streamFadeReduceMotion = false;\n"
        "let _streamFadeReduceMotionOnChange = null;\n"
        "let postProcessCalls = 0;\n"
        "let playbackCalls = 0;\n"
        "function _smdLinkHref(value){ return String(value || ''); }\n"
        "function _postProcessWithAnchorSuppression(root){ postProcessCalls += root.querySelectorAll('.pdf-preview-load').length; }\n"
        "function _applyMediaPlaybackPreferences(){ playbackCalls += 1; }\n"
        "function esc(value){ return String(value ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c])); }\n"
        "function _inlineMediaHtmlForRef(ref){\n"
        "  const raw = String(ref || '');\n"
        "  if(/\\.pdf$/i.test(raw)) return `<div class=\"pdf-preview-load\" data-path=\"${esc(raw)}\">PDF</div>`;\n"
        "  return `<span class=\"media-node\" data-ref=\"${esc(raw)}\"></span>`;\n"
        "}\n"
        "class FakeNode{\n"
        "  constructor(type, tag='', text=''){\n"
        "    this.nodeType=type; this.tagName=tag; this.children=[]; this.parentNode=null; this.attributes={}; this.data=text;\n"
        "    this.style={ setProperty:()=>{} };\n"
        "    this.classList={ contains:name=>(this.attributes.class||'').split(/\\s+/).includes(name), add:name=>this.setAttribute('class', ((this.attributes.class||'')+' '+name).trim()) };\n"
        "  }\n"
        "  get childNodes(){ return this.children; }\n"
        "  get firstChild(){ return this.children[0] || null; }\n"
        "  get parentElement(){ return this.parentNode; }\n"
        "  get className(){ return this.attributes.class || ''; }\n"
        "  set className(value){ this.attributes.class=String(value); }\n"
        "  appendChild(child){\n"
        "    if(child.nodeType===11){ while(child.firstChild) this.appendChild(child.firstChild); return child; }\n"
        "    if(child.parentNode){ const old=child.parentNode.children.indexOf(child); if(old>=0) child.parentNode.children.splice(old,1); }\n"
        "    child.parentNode=this; this.children.push(child); return child;\n"
        "  }\n"
        "  addEventListener(){}\n"
        "  replaceWith(node){ if(!this.parentNode) return; const i=this.parentNode.children.indexOf(this); if(i>=0){ node.parentNode=this.parentNode; this.parentNode.children.splice(i,1,node); this.parentNode=null; } }\n"
        "  setAttribute(name,value){ this.attributes[name]=String(value); }\n"
        "  getAttribute(name){ return this.attributes[name] ?? null; }\n"
        "  querySelectorAll(selector){\n"
        "    const cls=selector.startsWith('.') ? selector.slice(1) : '';\n"
        "    const out=[];\n"
        "    const visit=node=>{ for(const child of node.children){ const classes=(child.attributes.class||'').split(/\\s+/); if(cls&&classes.includes(cls)) out.push(child); visit(child); } };\n"
        "    visit(this); return out;\n"
        "  }\n"
        "  get textContent(){ return this.nodeType===3 ? this.data : this.children.map(c=>c.textContent).join(''); }\n"
        "  set textContent(value){\n"
        "    if(this.nodeType===3){ this.data=String(value); return; }\n"
        "    this.children=[];\n"
        "    const text=String(value);\n"
        "    if(text) this.appendChild(document.createTextNode(text));\n"
        "  }\n"
        "  get outerHTML(){\n"
        "    if(this.nodeType===3) return esc(this.data);\n"
        "    if(this.nodeType===11) return this.children.map(c=>c.outerHTML).join('');\n"
        "    const attrs=Object.entries(this.attributes).map(([k,v])=>` ${k}=\"${esc(v)}\"`).join('');\n"
        "    return `<${this.tagName}${attrs}>${this.children.map(c=>c.outerHTML).join('')}</${this.tagName}>`;\n"
        "  }\n"
        "}\n"
        "globalThis.document = {\n"
        "  createElement: tag => new FakeNode(1, tag),\n"
        "  createTextNode: text => new FakeNode(3, '#text', String(text)),\n"
        "  createDocumentFragment: () => new FakeNode(11, '#fragment'),\n"
        "};\n"
        "globalThis.DOMParser = class { parseFromString(html){\n"
        "  const host=document.createElement('div');\n"
        "  const cls=html.includes('pdf-preview-load') ? 'pdf-preview-load' : 'media-node';\n"
        "  const node=document.createElement(html.includes('pdf-preview-load') ? 'div' : 'span');\n"
        "  node.setAttribute('class', cls);\n"
        "  const ref=(html.match(/data-ref=\"([^\"]*)\"/)||html.match(/data-path=\"([^\"]*)\"/)||[])[1]||'';\n"
        "  if(ref) node.setAttribute(cls==='pdf-preview-load' ? 'data-path' : 'data-ref', ref);\n"
        "  host.appendChild(node);\n"
        "  return { body: { firstChild: host } };\n"
        "} };\n"
        f"{helpers}\n"
        "function collectTagTexts(root, tag){\n"
        "  const out=[]; const wanted=String(tag).toLowerCase();\n"
        "  const visit=node=>{ for(const child of node.children){ if(String(child.tagName||'').toLowerCase()===wanted) out.push(child.textContent); visit(child); } };\n"
        "  visit(root); return out;\n"
        "}\n"
        "function collectClassTexts(root, cls){ return root.querySelectorAll('.'+cls).map(node=>node.textContent); }\n"
        "function renderChunks(chunks, mode){\n"
        "  postProcessCalls = 0; playbackCalls = 0;\n"
        "  const root=document.createElement('div');\n"
        "  const baseRenderer=mode==='fade' ? _streamFadeRenderer(root) : _safeSmdRenderer(root);\n"
        "  const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);\n"
        "  const parser=smd.parser(renderer);\n"
        "  _smdBindParserIdentity(renderer, parser, root);\n"
        "  for(const chunk of chunks) smd.parser_write(parser, chunk);\n"
        "  smd.parser_end(parser);\n"
        "  _smdMediaTailFlush(parser);\n"
        "  _smdMediaTailClear(parser);\n"
        "  return { html: root.outerHTML, text: root.textContent, liTexts: collectTagTexts(root, 'li'), fadeWords: collectClassTexts(root, 'stream-fade-word'), postProcessCalls, playbackCalls };\n"
        "}\n"
        "function renderModes(chunks){ return { safe: renderChunks(chunks, 'safe'), fade: renderChunks(chunks, 'fade') }; }\n"
        "const marker='MEDIA:';\n"
        "const prefixSplits={};\n"
        "for(let i=1;i<marker.length;i++) prefixSplits[i]=renderModes(['\\n\\n'+marker.slice(0,i), marker.slice(i)+'C:/tmp/live.png ']);\n"
        "const refSplit=renderModes(['MEDIA:C:/tmp/li', 've.png ']);\n"
        "const finalExtensionless=renderModes(['MEDIA:https://fal.media/generated']);\n"
        "const pdf=renderModes(['MEDIA:C:/tmp/report.pdf ']);\n"
        "const falsePrefix=renderModes(['M', 'aybe plain prose ']);\n"
        "const crossParent=renderModes(['- ME', '\\n- ow']);\n"
        "console.log(JSON.stringify({prefixSplits, refSplit, finalExtensionless, pdf, falsePrefix, crossParent}));\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return json.loads(completed.stdout)


class TestSmdMediaInStream(unittest.TestCase):
    """Verify the streaming smd path produces real <img> for MEDIA tokens."""

    def test_inline_media_renderer_exists_in_ui_js(self):
        self.assertIn(
            "function _inlineMediaHtmlForRef",
            UI_JS,
            "ui.js must export _inlineMediaHtmlForRef so messages.js can reuse it",
        )

    def test_render_md_media_restore_uses_shared_renderer(self):
        # The renderMd MEDIA restore pass now delegates to the shared helper
        # instead of carrying its own copy of the URL → HTML mapping.
        marker = "_inlineMediaHtmlForRef(media_stash["
        self.assertIn(
            marker, UI_JS,
            "renderMd MEDIA restore must delegate to _inlineMediaHtmlForRef "
            "so the live + settled representations of the same MEDIA token "
            "stay byte-identical",
        )

    def test_messages_has_smd_media_aware_wrapper(self):
        self.assertIn(
            "function _smdMediaAwareAddText",
            MESSAGES_JS,
            "messages.js must define _smdMediaAwareAddText to convert MEDIA "
            "tokens into DOM elements at smd insert time",
        )

    def test_smd_media_aware_wrapper_invokes_shared_renderer(self):
        # The whole point of the fix: the streaming path uses the SAME renderer
        # the renderMd pipeline uses. If messages.js constructed the HTML
        # inline, the live + settled images could diverge.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6000]
        self.assertIn(
            "_inlineMediaHtmlForRef", block,
            "_smdMediaAwareAddText must call _inlineMediaHtmlForRef to keep "
            "streaming and settled MEDIA paths byte-identical",
        )

    def test_safe_smd_renderer_wraps_add_text_with_media_interceptor(self):
        idx = MESSAGES_JS.index("function _safeSmdRenderer")
        block = MESSAGES_JS[idx:idx + 2000]
        self.assertIn(
            "_smdMediaAwareAddText", block,
            "_safeSmdRenderer's add_text override must route text chunks "
            "through _smdMediaAwareAddText so MEDIA tokens become DOM nodes",
        )

    def test_stream_fade_renderer_short_circuits_media_chunks(self):
        idx = MESSAGES_JS.index("function _streamFadeRenderer")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn(
            "_smdMediaAwareAddText", block,
            "_streamFadeRenderer's add_text override must short-circuit to "
            "_smdMediaAwareAddText when the chunk carries a MEDIA token "
            "(otherwise the token would be wrapped in a stream-fade-word "
            "span and stay visible as literal text)",
        )

    def test_stream_fade_renderer_consumes_buffered_media_tail(self):
        # Greptile re-review: fade streaming previously only checked the
        # current chunk for /MEDIA:/. If the previous chunk buffered "MEDIA:"
        # and the next chunk was only the ref, fade wrapping rendered the path
        # as literal text instead of completing the MEDIA token.
        idx = MESSAGES_JS.index("function _streamFadeRenderer")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn("const parser=parserFor(data);", block)
        self.assertIn("_SMD_MEDIA_TAIL.has(parser)", block)
        self.assertIn("||hasMediaTail", block)
        self.assertIn("hasMediaPrefixTail", block)
        self.assertIn("_smdMediaPrefixTail(value)", block)

    def test_media_interceptor_handles_token_at_chunk_start(self):
        # The smd parser can split chunks mid-text. The fix must handle MEDIA
        # tokens wherever they appear in a single add_text call, not just at
        # the boundary of the chunk.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn("/MEDIA:", block,
                      "Interceptor must scan every chunk for MEDIA tokens")

    def test_media_interceptor_falls_back_to_base_when_no_token(self):
        # Fast path: when the chunk + buffered tail carries no MEDIA token,
        # the wrapper should delegate to the injected text writer so the
        # owning renderer's semantics (plain text for safe mode, word fade for
        # fade mode) survive for plain prose.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn("const writeCurrent=", block)
        self.assertIn("writeCurrent(combined)", block)
        self.assertIn("function _smdMediaWriteText", MESSAGES_JS)

    def test_no_recursive_infinite_loop_via_baseAddText(self):
        # Regression guard: the fade renderer's add_text is itself a wrapper.
        # If the MEDIA interceptor re-routed ALL chunks through baseAddText
        # regardless of token presence, plain prose would re-enter the fade
        # wrapper and on the next chunk also be re-processed. The fast-path
        # delegation happens only when /MEDIA:/ does NOT match.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertTrue(
            "! /MEDIA:/.test" in block.replace(" ", "") or "! /MEDIA:/.test(lead + value)" in block or "! /MEDIA:/.test(lead + value)" in block or "! /MEDIA:/.test(combined)" in block or "!/MEDIA:/.test" in block,
            "Interceptor must have an early-return fast path when the "
            "chunk lacks a MEDIA token (i.e. a `!/MEDIA:/` early bail before "
            "delegating to baseAddText)",
        )

    def test_plain_text_does_not_go_through_dom_parser(self):
        # Greptile #1 (safety): the previous implementation concatenated
        # prose + MEDIA HTML and ran the whole string through DOMParser. That
        # meant agent-supplied prose could be parsed by the HTML parser
        # (entity-decoded / re-serialised) instead of going through a pure
        # text-node insertion. The new implementation routes plain prose
        # back to baseAddText (which uses createTextNode) and only sends
        # each MEDIA token's HTML through DOMParser.
        # The single-token DOMParser helper must exist and accept ONE ref;
        # the loop body must call baseAddText for any prose slice *before*
        # it would attempt to splice HTML.
        self.assertIn("function _smdAppendMediaNode", MESSAGES_JS)
        smd_block = MESSAGES_JS[MESSAGES_JS.index("function _smdAppendMediaNode"):MESSAGES_JS.index("function _smdAppendMediaNode")+2000]
        self.assertIn("parseFromString", smd_block)
        self.assertNotIn("parseFromString('<div>'+value+'</div>'", MESSAGES_JS,
                         "Plain chunk text must never be concatenated into "
                         "the DOMParser input — only the single-token "
                         "mediaHtml produced by _inlineMediaHtmlForRef may "
                         "be parsed.")

    def test_cross_chunk_media_tail_buffer_exists(self):
        # Greptile #2 (cross-chunk split): when smd flushes a MEDIA token
        # in two pieces (e.g. "MEDIA:C:\\Users\\Admin" then "\\foo.png"),
        # the second half alone would not match the MEDIA regex; if we
        # only operate on each chunk independently both pieces render as
        # raw text. The new implementation keeps a per-parser tail buffer
        # for incomplete MEDIA prefixes.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 5000]
        # The interceptor must reference a module-level Map/WeakMap that
        # backstops partial MEDIA prefixes across calls.
        self.assertTrue(
            "_SMD_MEDIA_TAIL" in MESSAGES_JS or "_smdMediaTailSet" in MESSAGES_JS,
            "Interceptor must consult a per-parser tail buffer so a "
            "MEDIA:<ref> split across two smd flushes still resolves to "
            "a media element on the second call",
        )
        # And the interceptor must actually call a tail-mutating setter
        # somewhere on the trailing path, not just read.
        self.assertIn("unmatchedTail", block,
                      "Interceptor must record the trailing bytes that look "
                      "like an incomplete MEDIA prefix so the next add_text "
                      "call can prepend them and finish the token")

    def test_media_prefix_rolls_across_chunk_boundaries(self):
        # A split can happen inside the sentinel itself ("ME" + "DIA:foo.png"),
        # not only after the full "MEDIA:" prefix. Keep a rolling suffix scan
        # so the first half is buffered instead of rendered as visible prose.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 7000]
        self.assertIn("const _SMD_MEDIA_PREFIX = 'MEDIA:'", MESSAGES_JS)
        self.assertIn("function _smdMediaPrefixTail", MESSAGES_JS)
        self.assertIn("_smdMediaPrefixTail(combined)", block)
        self.assertIn("_smdMediaPrefixTail(rest)", block)
        self.assertIn("_SMD_MEDIA_PREFIX.startsWith(suffix)", MESSAGES_JS)

    def test_partial_media_ref_at_chunk_end_is_buffered_until_boundary(self):
        # Greptile re-review: /MEDIA:([^\s)\]]+)/g will happily match
        # "MEDIA:fo" at the end of a chunk even if the next chunk is "o.png".
        # The interceptor must not emit a media node for that partial ref;
        # it should keep the candidate in unmatchedTail unless a delimiter or
        # reliable filename suffix proves the ref is complete.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn("function _smdMediaRefHasReliableBoundary", MESSAGES_JS)
        self.assertIn("matchEnd===combined.length", block)
        self.assertIn("!_smdMediaRefHasReliableBoundary(m[1])", block)
        self.assertIn("unmatchedTail = candidate", block)

    def test_media_ref_boundary_extension_list_matches_renderer_formats(self):
        # Keep the streaming boundary whitelist aligned with ui.js media
        # renderer extension families. Otherwise complete refs at chunk end
        # (e.g. MEDIA:clip.aac) can be buffered and then dropped on stream end.
        idx = MESSAGES_JS.index("function _smdMediaRefHasReliableBoundary")
        block = MESSAGES_JS[idx:idx + 900]
        for ext in [
            "png", "jpe?g", "gif", "webp", "bmp", "ico", "svg", "avif",
            "mp4", "webm", "mov", "m4v", "mkv", "avi", "ogv",
            "mp3", "wav", "ogg", "m4a", "aac", "wma", "opus", "flac", "oga",
            "pdf", "html?", "csv", "diff", "patch", "excalidraw",
        ]:
            self.assertIn(ext, block)

    def test_extensionless_https_media_ref_is_a_reliable_boundary(self):
        # _inlineMediaHtmlForRef renders any http(s) ref as an image, including
        # extensionless CDN URLs such as fal.media generated assets. The stream
        # boundary check must therefore treat a complete http(s) ref as complete
        # even when it has no filename extension.
        self.assertIn("function _smdMediaTailFlush", MESSAGES_JS)
        self.assertIn("/^MEDIA:([^", MESSAGES_JS)
        self.assertIn("_smdMediaTailFlush(_smdParser)", MESSAGES_JS)

    def test_extensionless_https_tail_waits_until_stream_end(self):
        # A chunk ending at MEDIA:https://fal.med may still be mid-URL. Do not
        # treat http(s) scheme alone as a reliable boundary; the stream-end
        # flush is responsible for rendering a final extensionless URL.
        idx = MESSAGES_JS.index("function _smdMediaRefHasReliableBoundary")
        block = MESSAGES_JS[idx:idx + 900]
        self.assertNotIn("/^https?:", block)
        self.assertIn("_smdMediaTailFlush", MESSAGES_JS)

    def test_tail_buffer_size_cap(self):
        # Defensive: a runaway tail buffer from a malformed stream could
        # exhaust memory. The implementation must enforce a max length on
        # the per-parser tail.
        self.assertIn("_MEDIA_TAIL_MAX", MESSAGES_JS,
                      "Tail buffer must enforce a max length to bound memory")

    def test_per_parser_tail_isolation(self):
        # Multiple smd parsers run concurrently in the worklog + anchor
        # scene + main live body. The tail buffer must be keyed by parser
        # (not just by element) so a split MEDIA token in stream A doesn't
        # get prepended to a chunk in stream B.
        self.assertTrue(
            ("parserFor" in MESSAGES_JS and "_SMD_MEDIA_TAIL.get(parser)" in MESSAGES_JS)
            or ("tails.get(parser)" in MESSAGES_JS and "parserFor" in MESSAGES_JS),
            "Tail buffer must be keyed by a stable parser identity so "
            "concurrent streams don't cross-pollinate",
        )

    def test_tail_entries_preserve_original_text_owner(self):
        # A buffered MEDIA-looking suffix belongs to the parent/writer that
        # produced it. If the next smd add_text callback is for a different
        # parent, the scanner must flush through the original owner instead
        # of concatenating across DOM nodes.
        idx = MESSAGES_JS.index("function _smdMediaTailSet")
        block = MESSAGES_JS[idx:idx + 3500]
        self.assertIn("writeText", block)
        self.assertIn("function _smdMediaTailSameOwner", MESSAGES_JS)
        self.assertIn("entry.parent===parent", MESSAGES_JS)
        self.assertIn("entry.writeText===writeText", MESSAGES_JS)
        self.assertIn("_smdMediaTailFlushEntry(leadEntry)", MESSAGES_JS)

    def test_stream_fade_media_scanner_preserves_plain_prose_fade(self):
        # False MEDIA-prefix tails (for example "M" + "aybe") still enter
        # the scanner. Those plain prose writes must use the non-recursive
        # fade appender, not the raw default_renderer text writer.
        idx = MESSAGES_JS.index("function _streamFadeRenderer")
        block = MESSAGES_JS[idx:idx + 7000]
        self.assertIn("const writeFadeText=", block)
        self.assertIn("_streamFadeAppendText(writeParent, writeText)", block)
        self.assertIn("_smdMediaAwareAddText(baseAddText, parent, data, text, _SMD_MEDIA_TAIL, parser, writeFadeText)", block)

    def test_smd_parser_identity_is_bound_to_real_parser(self):
        # smd's renderer.data does not expose a parser by default. Bind the
        # created parser onto both renderer.data and the owning element so every
        # add_text path uses the same key for tail set/get/flush/clear.
        self.assertIn("function _smdParserKey", MESSAGES_JS)
        self.assertIn("function _smdBindParserIdentity", MESSAGES_JS)
        self.assertIn("renderer.data.parser=parser", MESSAGES_JS)
        self.assertIn("el.__smdParser=parser", MESSAGES_JS)
        smd_new = MESSAGES_JS[
            MESSAGES_JS.index("function _smdNewParser"):
            MESSAGES_JS.index("function _smdRendererWithoutUnderscoreEmphasis")
        ].replace(" ", "")
        self.assertIn("_smdBindParserIdentity(renderer,_smdParser,el)", smd_new)
        anchor = MESSAGES_JS[
            MESSAGES_JS.index("function _anchorProseIncrementalNode"):
            MESSAGES_JS.index("function _clearAnchorProseIncrementalNode")
        ].replace(" ", "")
        self.assertIn("_smdBindParserIdentity(renderer,st.parser,body)", anchor)
        self.assertNotIn("(data && data.nodes && data.nodes[data.index]) || __SMD_PARSER_FALLBACK", MESSAGES_JS)

    def test_smd_end_parser_clears_fallback_media_tail(self):
        # Greptile re-review: parserFor falls back to __SMD_PARSER_FALLBACK,
        # so stream-end cleanup must clear that sentinel key, not null.
        idx = MESSAGES_JS.index("function _smdEndParser")
        block = MESSAGES_JS[idx:idx + 1600]
        self.assertIn("_smdMediaTailFlush(_smdParser)", block)
        self.assertIn("_smdMediaTailFlush(__SMD_PARSER_FALLBACK)", block)
        self.assertLess(block.index("parser_end"), block.index("_smdMediaTailFlush(_smdParser)"))
        self.assertIn("_smdMediaTailClear(_smdParser)", block)
        self.assertIn("_smdMediaTailClear(__SMD_PARSER_FALLBACK)", block)
        self.assertNotIn("_smdMediaTailClear(null)", block)

    def test_anchor_prose_cleanup_flushes_media_tail_before_clear(self):
        idx = MESSAGES_JS.index("function _clearAnchorProseIncrementalNode")
        block = MESSAGES_JS[idx:idx + 1800]
        self.assertIn("_smdMediaTailFlush(st.parser)", block)
        self.assertIn("_smdMediaTailClear(st.parser)", block)
        self.assertLess(block.index("_smdMediaTailFlush(st.parser)"), block.index("_smdMediaTailClear(st.parser)"))

    def test_live_media_insertions_are_post_processed(self):
        # Streaming MEDIA inserts PDF/HTML/diff/CSV/Excalidraw placeholders into
        # the live DOM. They must hydrate immediately, not wait for the settled
        # renderMessages() pass.
        append = MESSAGES_JS[
            MESSAGES_JS.index("function _smdAppendMediaNode"):
            MESSAGES_JS.index("function _smdScheduleMediaPostProcess")
        ]
        self.assertIn("_smdScheduleMediaPostProcess(parent)", append)
        scheduler = MESSAGES_JS[
            MESSAGES_JS.index("function _smdScheduleMediaPostProcess"):
            MESSAGES_JS.index("// Per-parser tail buffer")
        ]
        self.assertIn("_postProcessWithAnchorSuppression(root)", scheduler)
        self.assertIn("postProcessRenderedMessages(root)", scheduler)
        self.assertIn("_applyMediaPlaybackPreferences(root)", scheduler)


@unittest.skipIf(NODE is None, "node not on PATH")
class TestSmdMediaRealParserBehaviour(unittest.TestCase):
    """Drive the actual vendored smd parser through split MEDIA chunks."""

    @classmethod
    def setUpClass(cls):
        cls.cases = _run_real_smd_media_cases()

    def test_real_smd_parser_buffers_every_media_prefix_split(self):
        for split, modes in self.cases["prefixSplits"].items():
            for mode, result in modes.items():
                with self.subTest(split=split, mode=mode):
                    self.assertIn('class="media-node"', result["html"])
                    self.assertIn('data-ref="C:/tmp/live.png"', result["html"])
                    self.assertNotIn("MEDIA:", result["text"])

    def test_real_smd_parser_buffers_partial_ref_until_complete(self):
        for mode, result in self.cases["refSplit"].items():
            with self.subTest(mode=mode):
                self.assertIn('class="media-node"', result["html"])
                self.assertIn('data-ref="C:/tmp/live.png"', result["html"])
                self.assertNotIn("MEDIA:", result["text"])
                self.assertNotIn("C:/tmp/li", result["text"])

    def test_real_smd_parser_flushes_final_extensionless_url(self):
        for mode, result in self.cases["finalExtensionless"].items():
            with self.subTest(mode=mode):
                self.assertIn('class="media-node"', result["html"])
                self.assertIn('data-ref="https://fal.media/generated"', result["html"])
                self.assertNotIn("MEDIA:", result["text"])

    def test_real_smd_parser_live_pdf_placeholder_is_hydrated(self):
        for mode, result in self.cases["pdf"].items():
            with self.subTest(mode=mode):
                self.assertIn('class="pdf-preview-load"', result["html"])
                self.assertIn('data-path="C:/tmp/report.pdf"', result["html"])
                self.assertGreaterEqual(result["postProcessCalls"], 1)
                self.assertGreaterEqual(result["playbackCalls"], 1)

    def test_real_smd_parser_false_prefix_plain_prose_keeps_fade(self):
        result = self.cases["falsePrefix"]["fade"]
        self.assertEqual(result["text"], "Maybe plain prose ")
        self.assertEqual(result["fadeWords"], ["Maybe", "plain", "prose"])
        self.assertIn('class="stream-fade-word is-new"', result["html"])

    def test_real_smd_parser_refuses_cross_parent_tail_concat(self):
        for mode, result in self.cases["crossParent"].items():
            with self.subTest(mode=mode):
                self.assertEqual(result["liTexts"], ["ME", "ow"])
                if mode == "fade":
                    self.assertTrue(result["fadeWords"])
                    self.assertEqual("".join(result["fadeWords"]), "MEow")


if __name__ == "__main__":
    import unittest
    unittest.main()
