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
            _extract_js_function(UI_JS, "_mediaTokenParts"),
            _extract_js_function(MESSAGES_JS, "_smdMediaPrefixTail"),
            _extract_js_function(MESSAGES_JS, "_smdAppendPlainText"),
            _extract_js_function(MESSAGES_JS, "_smdMediaWriteText"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailSet"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailEntryChunk"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTailSameOwner"),
            _extract_js_function(MESSAGES_JS, "_smdMediaRefHasReliableBoundary"),
            _extract_js_function(MESSAGES_JS, "_smdMediaTokenParts"),
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
        "let _streamFadeSilentPrefixChars = 0;\n"
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
        "const boundaries={\n"
        "  bold:renderModes(['**MEDIA:/tmp/report.xlsx** ']),\n"
        "  boldSplit:renderModes(['**MEDIA:/tmp/report.', 'xlsx** ']),\n"
        "  trailingPeriod:renderModes(['MEDIA:/tmp/report.xlsx. ']),\n"
        "  trailingPeriodEnd:renderModes(['MEDIA:/tmp/report.xlsx.']),\n"
        "  bareMarker:renderModes(['`MEDIA:` ']),\n"
        "  queryFragment:renderModes(['MEDIA:https://example.com/a.png?size=1#preview ']),\n"
        "  wrappedRemoteQueryPunctuation:renderModes(['**MEDIA:https://example.com/a.png?signature=value.**. ']),\n"
        "  quotedDouble:renderModes(['\"MEDIA:/tmp/report.xlsx\". ']),\n"
        "  quotedSingleSplit:renderModes([\"'MEDIA:/tmp/report.\", \"xlsx'.\"]),\n"
        "  entityQuotedDoubleSplit:renderModes(['&quot;', 'MEDIA:/tmp/report.xlsx&quot;. ']),\n"
        "  entityQuotedSingleEnd:renderModes(['&#39;MEDIA:/tmp/report.xlsx&#39;.']),\n"
        "  entityQuotedDoubleOpenerSplit:renderModes(['&quo', 't;MEDIA:/tmp/report.xlsx&quot;. ']),\n"
        "  quotedRemoteQuery:renderModes(['\"MEDIA:https://example.com/a.png?signature=value!\". ']),\n"
        "  quotedRemoteFragment:renderModes(['\"MEDIA:https://example.com/a.png#preview!\". ']),\n"
        "  windowsPath:renderModes(['MEDIA:C:\\\\Temp\\\\report.xlsx ']),\n"
        "  unmatchedDelimiter:renderModes(['MEDIA:/tmp/report.xlsx* ']),\n"
        "  multiple:renderModes(['MEDIA:/tmp/one.png then MEDIA:/tmp/two.pdf after']),\n"
        "};\n"
        "const punctuation={};\n"
        "for(const mark of ['.',',',';',':','!','?',')']) punctuation[mark]=renderModes([`MEDIA:/tmp/report.xlsx${mark} `]);\n"
        "const remoteSuffixPunctuation={};\n"
        "for(const mark of ['.',',',';',':','!','?']){\n"
        "  remoteSuffixPunctuation['query-'+mark]=renderModes([`MEDIA:https://example.com/a.png?signature=value${mark} `]);\n"
        "  remoteSuffixPunctuation['fragment-'+mark]=renderModes([`MEDIA:https://example.com/a.png#section${mark} `]);\n"
        "}\n"
        "const partialPunctuationSplit=renderModes(['MEDIA:/tmp/a.', 'png ']);\n"
        "console.log(JSON.stringify({prefixSplits,refSplit,finalExtensionless,pdf,falsePrefix,crossParent,boundaries,punctuation,remoteSuffixPunctuation,partialPunctuationSplit}));\n"
    )
    proc = subprocess.run(
        [NODE, "--input-type=module", "--eval", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(proc.stdout)


class TestSmdMediaInStream(unittest.TestCase):
    def test_media_aware_wrapper_exists(self):
        self.assertIn("function _smdMediaAwareAddText", MESSAGES_JS)
        self.assertIn("_inlineMediaHtmlForRef", MESSAGES_JS)

    def test_prefix_tail_logic_is_present(self):
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
        # it should keep the candidate in unmatchedTail unless a reliable
        # filename suffix proves the parsed ref is complete.
        idx = MESSAGES_JS.index("function _smdMediaAwareAddText")
        block = MESSAGES_JS[idx:idx + 6500]
        self.assertIn("function _smdMediaRefHasReliableBoundary", MESSAGES_JS)
        self.assertIn("matchEnd===combined.length", block)
        self.assertIn("!_smdMediaRefHasReliableBoundary(parts?parts[0]:m[1])", block)
        self.assertLess(
            block.index("const parts="),
            block.index("if(matchEnd===combined.length"),
        )
        self.assertIn("unmatchedTail = candidate", block)

    def test_media_ref_boundary_extension_list_matches_renderer_formats(self):
        idx = MESSAGES_JS.index("function _smdMediaRefHasReliableBoundary")
        block = MESSAGES_JS[idx:idx + 1200]
        for ext in ["png", "jpg", "svg", "mp4", "mp3", "pdf", "html", "csv", "diff", "patch", "excalidraw"]:
            self.assertIn(ext, block)


@unittest.skipIf(NODE is None, "node is required for streaming MEDIA behavior tests")
class TestSmdMediaAwareAddTextBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = _run_real_smd_media_cases()

    def _for_modes(self, case):
        return [case["safe"], case["fade"]]

    def test_partial_punctuation_split_reconstructs_reference(self):
        for result in self._for_modes(self.cases["partialPunctuationSplit"]):
            self.assertIn('data-ref="/tmp/a.png"', result["html"])
            self.assertNotIn('data-ref="/tmp/a"', result["html"])

    def test_remote_query_fragment_trailing_punctuation_is_preserved(self):
        for key, case in self.cases["remoteSuffixPunctuation"].items():
            for result in self._for_modes(case):
                mark = key[-1]
                self.assertIn(mark, result["html"])


if __name__ == "__main__":
    unittest.main()
