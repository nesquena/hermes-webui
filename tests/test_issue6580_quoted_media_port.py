"""#6580 port: quoted ``MEDIA:`` references with spaces, plus the three
corrective items the maintainer's 2026-10-15 review of ``fix/filename-spaces``
asked for, expressed against the current master renderer.

The feature being ported: a quoted ``MEDIA:"/path/with space.png"`` (and the
single-quoted / quoted ``file://`` forms) is a media reference even though it
contains whitespace, and it is authorised by the same session allow-list that
gates the bare form.

The three review items:

1. Widening a Markdown destination so it can carry spaces must not swallow the
   CommonMark title: ``[docs](https://example.com/a "the title")`` links to
   ``https://example.com/a`` and leaves ``the title`` as text.
2. A bare path keeps its bytes verbatim: ``MEDIA:/tmp/report%20final.png``
   refers to the file literally named ``report%20final.png``, while a quoted
   token is normalised exactly once (raw space and already-``%20`` converge).
3. In the streaming scanner an unterminated quoted candidate that hits an EOL
   flushes literally up to that EOL, so following prose is never swallowed by a
   later unrelated quote.

Coverage here is behavioural only: the settled cases drive the real
``renderMd()`` (with the real ``_inlineMediaHtmlForRef``) out of ``static/ui.js``
through node, the streaming cases drive the real smd parser and the real
``_smdMediaAwareAddText`` wrapper out of ``static/messages.js``, and the
authorisation cases (in ``tests/test_media_inline.py``) drive the real route
helper.
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


# ---------------------------------------------------------------------------
# Settled renderer: drive the real renderMd() + the real _inlineMediaHtmlForRef
# ---------------------------------------------------------------------------

_SETTLED_DRIVER_SRC = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
global.window = {};
global.document = { createElement: () => ({ innerHTML: '', textContent: '' }), baseURI: 'http://localhost/app/' };
function _sessionUrlForSid(sid) { return '/app/session/' + encodeURIComponent(String(sid || '')); }
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const _IMAGE_EXTS=/\.(png|jpg|jpeg|gif|webp|bmp|ico|avif)$/i;
const _PDF_EXTS=/\.pdf$/i;
const _HTML_EXTS=/\.(html?|htm)$/i;
const _SVG_EXTS=/\.svg$/i;
const _AUDIO_EXTS=/\.(mp3|ogg|wav|m4a|aac|flac|wma|opus|webm|oga)$/i;
const _VIDEO_EXTS=/\.(mp4|webm|mkv|mov|avi|ogv|m4v)$/i;
const _CSV_EXTS=/\.csv$/i;
const _EXCALIDRAW_EXTS=/\.excalidraw$/i;
function _mediaKindForName(name=''){
  const clean=String(name||'').split('?')[0].toLowerCase();
  if(_VIDEO_EXTS.test(clean)) return 'video';
  if(_AUDIO_EXTS.test(clean)) return 'audio';
  if(_IMAGE_EXTS.test(clean)) return 'image';
  return '';
}
function _mediaPlayerHtml(kind, url, name){ return `<audio class="msg-media-audio" src="${esc(url)}"></audio>`; }
function _dataImageHtml(){ return ''; }
function _remoteImageAllowed(){ return true; }
function _remoteImagePlaceholderHtml(u){ return `<a class="remote-media" href="${esc(u)}">remote</a>`; }
const t = key => key;
const S = { session: { session_id: '' } };

function extractFunc(name) {
  const re = new RegExp('function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
eval(extractFunc('_matchBacktickFenceLine'));
eval(extractFunc('_isBacktickFenceClose'));
eval(extractFunc('_mediaTokenParts'));
eval(extractFunc('_inlineMediaHtmlForRef'));
eval(extractFunc('renderMd'));

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', c => { buf += c; });
process.stdin.on('end', () => { process.stdout.write(renderMd(buf)); });
"""


def _render(markdown: str) -> str:
    """Run the real renderMd() (and the real media renderer) against ``markdown``."""
    driver = pathlib.Path(__file__).parent / "_tmp_issue6580_settled_driver.js"
    try:
        driver.write_text(_SETTLED_DRIVER_SRC, encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js")],
            input=markdown,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
    finally:
        driver.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return result.stdout


class TestQuotedMediaSettledRenderer(unittest.TestCase):
    """Quoted MEDIA/file:// refs and the destination rules, through renderMd()."""

    def test_double_quoted_media_with_space_renders_media(self):
        out = _render('MEDIA:"/tmp/my screenshot.png" done')
        self.assertIn('src="api/media?path=%2Ftmp%2Fmy%20screenshot.png"', out)
        self.assertNotIn("MEDIA:", out, f"token must be consumed, got {out!r}")
        self.assertIn("done", out)

    def test_single_quoted_media_with_space_renders_media(self):
        out = _render("MEDIA:'/tmp/my screenshot.png' done")
        self.assertIn('src="api/media?path=%2Ftmp%2Fmy%20screenshot.png"', out)
        self.assertNotIn("MEDIA:", out, f"token must be consumed, got {out!r}")

    def test_quoted_already_encoded_ref_is_encoded_once(self):
        # A quoted token that already carries %20 converges on the same query
        # path as the raw-space form; it must never become %2520.
        raw = _render('MEDIA:"/tmp/my screenshot.png"')
        encoded = _render('MEDIA:"/tmp/my%20screenshot.png"')
        self.assertIn("%2Ftmp%2Fmy%20screenshot.png", raw)
        self.assertIn("%2Ftmp%2Fmy%20screenshot.png", encoded)
        self.assertNotIn("%2520", encoded, f"double-encoded ref: {encoded!r}")

    def test_bare_percent_sequence_stays_literal(self):
        # Item 2: bare paths are verbatim. A literal %20 in a bare token names a
        # file that contains "%20", so it is encoded once (-> %2520 on the wire,
        # decoded once by the server back to "%20").
        out = _render("MEDIA:/tmp/report%20final.png done")
        self.assertIn("%2Ftmp%2Freport%2520final.png", out)
        self.assertIn("done", out)

    def test_quoted_file_url_with_space_renders_media(self):
        out = _render('file://"/tmp/my screenshot.png" done')
        self.assertIn("api/media?path=", out)
        self.assertIn("my%20screenshot.png", out)
        self.assertNotIn("file:///tmp/my%20screenshot.png", out)

    def test_titled_markdown_link_does_not_swallow_the_title(self):
        # Item 1: the widened destination must stop before the title.
        out = _render('[docs](https://example.com/a "the title") tail')
        self.assertIn('href="https://example.com/a"', out)
        self.assertNotIn("%22the%20title%22", out, f"title swallowed: {out!r}")
        self.assertIn("the title", out, f"title must stay text: {out!r}")

    def test_titled_markdown_link_with_space_in_destination(self):
        out = _render('[docs](https://example.com/my file "the title") tail')
        self.assertIn('href="https://example.com/my%20file"', out)
        self.assertNotIn("the%20title", out, f"title swallowed: {out!r}")

    def test_titled_markdown_image_does_not_swallow_the_title(self):
        out = _render('![shot](https://example.com/i.png "the title") tail')
        self.assertIn('src="https://example.com/i.png"', out)
        self.assertNotIn("the%20title", out, f"title swallowed: {out!r}")

    def test_unterminated_quoted_media_stays_literal(self):
        out = _render('MEDIA:"/tmp/a b.png')
        self.assertNotIn("api/media?path=", out, f"no media expected: {out!r}")
        self.assertIn('MEDIA:"/tmp/a b.png', out)


class TestPlainParagraphDestinationBoundaries(unittest.TestCase):
    """The outer plain-paragraph passes stop a destination at a line ending."""

    def test_markdown_image_destination_does_not_span_lines(self):
        for sep in ("\n", "\r", "\r\n"):
            out = _render(f"![shot](https://example.com/a{sep}b.png) tail")
            self.assertIn("![shot](", out, f"literal markdown must survive: {out!r}")
            self.assertIn("b.png) tail", out, f"following line must stay prose: {out!r}")
            self.assertNotIn("msg-media-img", out, f"no media node expected: {out!r}")

    def test_markdown_link_destination_does_not_span_lines(self):
        for sep in ("\n", "\r", "\r\n"):
            out = _render(f"[doc](file:///tmp/a{sep}b.txt) tail")
            self.assertIn("[doc](", out, f"literal markdown must survive: {out!r}")
            self.assertIn("b.txt) tail", out, f"following line must stay prose: {out!r}")
            self.assertNotIn("%0A", out, f"swallowed newline must not be encoded: {out!r}")

    def test_same_line_markdown_image_with_space_still_renders_media(self):
        out = _render("![shot](https://example.com/my shot.png) tail")
        self.assertIn('src="https://example.com/my%20shot.png"', out)
        self.assertIn("tail", out)

    def test_same_line_markdown_link_with_space_still_renders_anchor(self):
        out = _render("[doc](file:///tmp/my file.txt) tail")
        self.assertIn("api/media?path=%2Ftmp%2Fmy%20file.txt", out)
        self.assertIn(">doc</a>", out)


# ---------------------------------------------------------------------------
# Streaming: real smd parser + real _smdMediaAwareAddText wrapper
# ---------------------------------------------------------------------------

_SMD_HELPERS = [
    "_smdMediaPrefixTail",
    "_smdAppendPlainText",
    "_smdMediaWriteText",
    "_smdMediaTailSet",
    "_smdMediaTailEntryChunk",
    "_smdMediaTailSameOwner",
    "_smdMediaTokenParts",
    "_smdMediaTailFlushEntry",
    "_smdMediaTailFlush",
    "_smdMediaAwareAddText",
    "_smdAppendMediaNode",
    "_smdScheduleMediaPostProcess",
    "_smdParserKey",
    "_smdBindParserIdentity",
    "_smdMediaTailClear",
    "_streamFadeSkipNode",
    "_streamFadeReduceMotionEnabled",
    "_streamFadeBindCleanup",
    "_streamFadeAppendText",
    "_streamFadeRenderer",
    "_safeSmdRenderer",
    "_smdRendererWithoutUnderscoreEmphasis",
]


def _run_real_smd_media_cases() -> dict:
    helpers = "\n".join(_extract_js_function(MESSAGES_JS, name) for name in _SMD_HELPERS)
    ui_helpers = _extract_js_function(UI_JS, "_mediaTokenParts")
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
        "  return { html: root.outerHTML, text: root.textContent, refs: root.querySelectorAll('.media-node').map(n=>n.getAttribute('data-ref')) };\n"
        "}\n"
        "function renderModes(chunks){ return { safe: renderChunks(chunks, 'safe'), fade: renderChunks(chunks, 'fade') }; }\n"
        "const out={};\n"
        "out.bare=renderModes(['MEDIA:/tmp/a.png done']);\n"
        "out.quotedComplete=renderModes(['MEDIA:\"/tmp/my screenshot.png\" done']);\n"
        "out.quotedSplit=renderModes(['MEDIA:\"/tmp/my scre', 'enshot.png\" done']);\n"
        "out.quotedSplitAfterSpace=renderModes(['MEDIA:\"/tmp/my ', 'screenshot.png\" done']);\n"
        "out.quotedSingle=renderModes([\"MEDIA:'/tmp/my screenshot.png' done\"]);\n"
        "out.quotedUnclosed=renderModes(['MEDIA:\"/tmp/my screenshot.png']);\n"
        "out.unclosedThenProse=renderModes(['MEDIA:\"/tmp/my screenshot.png\\nnext \"q\" done']);\n"
        "out.unclosedThenProseSplit=renderModes(['MEDIA:\"/tmp/my screenshot.png\\n', 'next \"q\" done']);\n"
        "out.multipleTokens=renderModes(['MEDIA:/tmp/a.png and MEDIA:\"/tmp/b c.png\" done']);\n"
        "console.log(JSON.stringify(out));\n"
    )
    completed = subprocess.run(
        [NODE, "--input-type=module", "-e", script],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"node streaming driver failed: {completed.stderr[:2000]}")
    return json.loads(completed.stdout)


_CASES: dict | None = None


def _cases() -> dict:
    global _CASES
    if _CASES is None:
        _CASES = _run_real_smd_media_cases()
    return _CASES


class TestQuotedMediaStreaming(unittest.TestCase):
    """The live streaming renderer honours quoted refs and fails closed on EOL."""

    def test_quoted_token_emits_exact_ref(self):
        for mode in ("safe", "fade"):
            case = _cases()["quotedComplete"][mode]
            self.assertIn("/tmp/my screenshot.png", case["refs"], f"{mode}: {case}")
            self.assertNotIn("MEDIA:", case["text"], f"{mode}: {case}")
            self.assertIn("done", case["text"], f"{mode}: {case}")

    def test_quoted_token_split_mid_ref(self):
        for mode in ("safe", "fade"):
            case = _cases()["quotedSplit"][mode]
            self.assertIn("/tmp/my screenshot.png", case["refs"], f"{mode}: {case}")

    def test_quoted_token_split_after_space(self):
        for mode in ("safe", "fade"):
            case = _cases()["quotedSplitAfterSpace"][mode]
            self.assertIn("/tmp/my screenshot.png", case["refs"], f"{mode}: {case}")

    def test_single_quoted_token_emits_exact_ref(self):
        for mode in ("safe", "fade"):
            case = _cases()["quotedSingle"][mode]
            self.assertIn("/tmp/my screenshot.png", case["refs"], f"{mode}: {case}")

    def test_unclosed_quote_flushes_as_literal_text(self):
        for mode in ("safe", "fade"):
            case = _cases()["quotedUnclosed"][mode]
            self.assertEqual([], case["refs"], f"{mode}: no media node expected: {case}")
            self.assertIn("/tmp/my screenshot.png", case["text"], f"{mode}: {case}")

    def test_unclosed_quote_across_lines_does_not_swallow_prose(self):
        # Item 3: an unterminated quote that reaches an EOL flushes literally and
        # scanning resumes after it, so the next line's quote is never paired.
        for mode in ("safe", "fade"):
            case = _cases()["unclosedThenProse"][mode]
            self.assertEqual([], case["refs"], f"{mode}: no media node expected: {case}")
            self.assertIn('next "q" done', case["text"], f"{mode}: prose swallowed: {case}")

    def test_unclosed_quote_across_chunks_does_not_swallow_prose(self):
        for mode in ("safe", "fade"):
            case = _cases()["unclosedThenProseSplit"][mode]
            self.assertEqual([], case["refs"], f"{mode}: no media node expected: {case}")
            self.assertIn('next "q" done', case["text"], f"{mode}: prose swallowed: {case}")

    def test_bare_token_still_emits_media(self):
        for mode in ("safe", "fade"):
            case = _cases()["bare"][mode]
            self.assertIn("/tmp/a.png", case["refs"], f"{mode}: {case}")

    def test_bare_and_quoted_tokens_in_one_chunk(self):
        for mode in ("safe", "fade"):
            case = _cases()["multipleTokens"][mode]
            self.assertIn("/tmp/a.png", case["refs"], f"{mode}: {case}")
            self.assertIn("/tmp/b c.png", case["refs"], f"{mode}: {case}")


if __name__ == "__main__":
    unittest.main()
