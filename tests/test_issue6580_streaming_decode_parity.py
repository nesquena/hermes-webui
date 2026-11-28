"""#6580 follow-up: quoted ``MEDIA:`` refs are normalised exactly once in the
streaming scanner, before the shared media helper, and the live renderer agrees
with the settled renderer for encoded paths.

Item 2 of the port is "a bare path keeps its bytes verbatim, while a quoted
token is normalised exactly once (raw space and already-``%20`` converge)".
The settled path does that in ``renderMd()`` (``decodeURIComponent`` on the
quoted capture, then ``_inlineMediaHtmlForRef`` encodes once for the wire).
The streaming scanner (``_smdMediaAwareAddText`` and the tail flush in
``static/messages.js``) hands the quoted ref to the same helper *without*
that single decode, so an encoded quoted ref resolves to a different file live
than it does once the turn settles.

The cases below drive the real smd parser with the real
``_smdMediaAwareAddText`` wrapper in both renderer modes (safe and fade), and
the real ``renderMd()`` from ``static/ui.js`` for the settled arm.  The helper
``_inlineMediaHtmlForRef`` is stubbed in the live harness only so the test can
read the exact ref the production code decided to hand it.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import unittest
from urllib.parse import unquote

REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MESSAGES_JS = (REPO_ROOT / "static" / "messages.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

_PORT_SPEC = importlib.util.spec_from_file_location(
    "q6580_port",
    pathlib.Path(__file__).with_name("test_issue6580_quoted_media_port.py"),
)
_PORT = importlib.util.module_from_spec(_PORT_SPEC)
_PORT_SPEC.loader.exec_module(_PORT)

_extract_js_function = _PORT._extract_js_function
_SMD_HELPERS = _PORT._SMD_HELPERS


_LIVE_CASES = [
    # (case id, chunks)
    ("tailEncoded", ['MEDIA:"/tmp/my%20screenshot.png"']),
    ("tailEncodedUnicode", ['MEDIA:"/tmp/caf%C3%A9.png"']),
    ("normalEncoded", ['MEDIA:"/tmp/my%20screenshot.png" done']),
    ("normalEncodedSingle", ["MEDIA:'/tmp/my%20screenshot.png' done"]),
    ("splitEncoded", ['MEDIA:"/tmp/my%20scr', 'eenshot.png" done']),
    ("splitEncodedAfterSlash", ['MEDIA:"/tmp/my%20', 'screenshot.png" done']),
    ("normalRawSpace", ['MEDIA:"/tmp/my screenshot.png" done']),
    ("normalDoubleEncoded", ['MEDIA:"/tmp/my%2520screenshot.png" done']),
    ("normalEncodedSlash", ['MEDIA:"/tmp/odd%2Fdir.png" done']),
    ("malformedPercent", ['MEDIA:"/tmp/100%.png" done']),
    ("bareEncodedStaysVerbatim", ["MEDIA:/tmp/report%20final.png done"]),
]

_PARITY_REFS = [
    "/tmp/my screenshot.png",
    "/tmp/my%20screenshot.png",
    "/tmp/caf%C3%A9.png",
    "/tmp/odd%2Fdir.png",
    "/tmp/my%2520screenshot.png",
    "/tmp/interior space.png",
]


def _live_harness() -> dict:
    helpers = "\n".join(_extract_js_function(MESSAGES_JS, name) for name in _SMD_HELPERS)
    ui_helpers = _extract_js_function(UI_JS, "_mediaTokenParts")
    cases = json.dumps([{"id": cid, "chunks": chunks} for cid, chunks in _LIVE_CASES])
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
        "function _smdLinkHref(value){ return String(value || ''); }\n"
        "function _postProcessWithAnchorSuppression(root){}\n"
        "function _applyMediaPlaybackPreferences(){}\n"
        "function esc(value){ return String(value ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c])); }\n"
        "function _inlineMediaHtmlForRef(ref){\n"
        "  const raw = String(ref || '');\n"
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
        "  const node=document.createElement('span');\n"
        "  node.setAttribute('class', 'media-node');\n"
        "  const ref=(html.match(/data-ref=\"([^\"]*)\"/)||[])[1]||'';\n"
        "  if(ref) node.setAttribute('data-ref', ref);\n"
        "  host.appendChild(node);\n"
        "  return { body: { firstChild: host } };\n"
        "} };\n"
        f"{ui_helpers}\n{helpers}\n"
        "function renderChunks(chunks, mode){\n"
        "  const root=document.createElement('div');\n"
        "  const baseRenderer=mode==='fade' ? _streamFadeRenderer(root) : _safeSmdRenderer(root);\n"
        "  const renderer=_smdRendererWithoutUnderscoreEmphasis(baseRenderer);\n"
        "  const parser=smd.parser(renderer);\n"
        "  _smdBindParserIdentity(renderer, parser, root);\n"
        "  for(const chunk of chunks) smd.parser_write(parser, chunk);\n"
        "  smd.parser_end(parser);\n"
        "  _smdMediaTailFlush(parser);\n"
        "  _smdMediaTailClear(parser);\n"
        "  return { refs: root.querySelectorAll('.media-node').map(n=>n.getAttribute('data-ref')), text: root.textContent };\n"
        "}\n"
        f"const cases = {cases};\n"
        "const out = {};\n"
        "for(const c of cases){\n"
        "  out[c.id] = { safe: renderChunks(c.chunks, 'safe'), fade: renderChunks(c.chunks, 'fade') };\n"
        "}\n"
        "console.log(JSON.stringify(out));\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        encoding="utf-8",
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"node streaming driver failed: {completed.stderr[:2000]}")
    return json.loads(completed.stdout)


_LIVE: dict | None = None


def _live() -> dict:
    global _LIVE
    if _LIVE is None:
        _LIVE = _live_harness()
    return _LIVE


def _settled_ref(path: str) -> str:
    """The ref ``renderMd()`` hands to the shared helper, recovered from the wire."""
    markup = _PORT._render(f'MEDIA:"{path}"').strip()
    match = re.search(r"api/media\?path=([^\"&]+)", markup)
    assert match, f"settled renderer produced no media node for {path!r}: {markup!r}"
    return unquote(match.group(1))


class TestStreamingQuotedRefDecodedOnce(unittest.TestCase):
    """The live scanner decodes a quoted ref exactly once before the helper."""

    def test_tail_flush_decodes_encoded_quoted_ref(self):
        for mode in ("safe", "fade"):
            case = _live()["tailEncoded"][mode]
            self.assertEqual(
                ["/tmp/my screenshot.png"],
                case["refs"],
                f"{mode}: tail flush must decode once and drop the quotes: {case}",
            )

    def test_tail_flush_decodes_unicode_escape(self):
        for mode in ("safe", "fade"):
            case = _live()["tailEncodedUnicode"][mode]
            self.assertEqual(["/tmp/café.png"], case["refs"], f"{mode}: {case}")

    def test_normal_emission_decodes_encoded_quoted_ref(self):
        for mode in ("safe", "fade"):
            case = _live()["normalEncoded"][mode]
            self.assertEqual(["/tmp/my screenshot.png"], case["refs"], f"{mode}: {case}")
            self.assertNotIn("MEDIA:", case["text"], f"{mode}: {case}")
            self.assertIn("done", case["text"], f"{mode}: {case}")

    def test_normal_emission_decodes_single_quoted_ref(self):
        for mode in ("safe", "fade"):
            case = _live()["normalEncodedSingle"][mode]
            self.assertEqual(["/tmp/my screenshot.png"], case["refs"], f"{mode}: {case}")

    def test_split_ref_is_decoded_exactly_once(self):
        for mode in ("safe", "fade"):
            case = _live()["splitEncoded"][mode]
            self.assertEqual(["/tmp/my screenshot.png"], case["refs"], f"{mode}: {case}")
            case = _live()["splitEncodedAfterSlash"][mode]
            self.assertEqual(["/tmp/my screenshot.png"], case["refs"], f"{mode}: {case}")

    def test_double_encoded_quoted_ref_decodes_once(self):
        # %2520 is the literal text "%20" after one decode; a second decode
        # would turn it into a space and name a different file.
        for mode in ("safe", "fade"):
            case = _live()["normalDoubleEncoded"][mode]
            self.assertEqual(["/tmp/my%20screenshot.png"], case["refs"], f"{mode}: {case}")

    def test_encoded_separator_decodes_once(self):
        for mode in ("safe", "fade"):
            case = _live()["normalEncodedSlash"][mode]
            self.assertEqual(["/tmp/odd/dir.png"], case["refs"], f"{mode}: {case}")

    def test_malformed_escape_keeps_the_raw_bytes(self):
        # decodeURIComponent throws on "100%.": the quoted ref must survive
        # verbatim instead of losing the character or being dropped.
        for mode in ("safe", "fade"):
            case = _live()["malformedPercent"][mode]
            self.assertEqual(["/tmp/100%.png"], case["refs"], f"{mode}: {case}")


class TestStreamingControlsStayUnchanged(unittest.TestCase):
    """Bare refs stay verbatim and quoted refs without escapes are untouched."""

    def test_bare_percent_sequence_is_not_decoded(self):
        for mode in ("safe", "fade"):
            case = _live()["bareEncodedStaysVerbatim"][mode]
            self.assertEqual(["/tmp/report%20final.png"], case["refs"], f"{mode}: {case}")

    def test_quoted_ref_with_raw_space_is_unchanged(self):
        for mode in ("safe", "fade"):
            case = _live()["normalRawSpace"][mode]
            self.assertEqual(["/tmp/my screenshot.png"], case["refs"], f"{mode}: {case}")


class TestEncodedPathLiveSettledParity(unittest.TestCase):
    """Live and settled agree on which file an encoded quoted ref names."""

    def test_live_ref_matches_settled_ref_for_encoded_paths(self):
        for ref in _PARITY_REFS:
            settled = _settled_ref(ref)
            live_case = _run_single(f'MEDIA:"{ref}" done')
            self.assertEqual(
                [settled],
                live_case["adhoc"]["safe"]["refs"],
                f"{ref!r}: live and settled must name the same file",
            )
            self.assertEqual(
                [settled],
                live_case["adhoc"]["fade"]["refs"],
                f"{ref!r}: fade mode must agree with settled",
            )


_SINGLE_CACHE: dict = {}


def _run_single(markup: str) -> dict:
    if markup not in _SINGLE_CACHE:
        _SINGLE_CACHE[markup] = _run_extra([markup])
    return _SINGLE_CACHE[markup]


def _run_extra(chunks: list[str]) -> dict:
    """Render one ad-hoc chunk list through the same real harness."""
    global _LIVE_CASES
    saved = _LIVE_CASES
    try:
        _LIVE_CASES = [("adhoc", chunks)]
        return _live_harness()
    finally:
        _LIVE_CASES = saved


if __name__ == "__main__":
    unittest.main()
