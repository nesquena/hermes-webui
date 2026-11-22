"""Public share page: a message the page would partly hide is shown as plain text.

``static/share.js`` renders each message with ``renderMd`` and then compares the
words of the published text with the words the rendered message shows. If any
published word is missing, the whole message is shown as plain text instead.
These tests load the real ``share.js`` under node and exercise the word counting
on small DOM trees. The end-to-end proof, with the real renderer in a real share
page plus a differential run over random messages, is
``tests/browser_share_hidden_text.py`` (run by the browser-smoke workflow).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHARE_JS = ROOT / "static" / "share.js"
SHARE_HTML = ROOT / "static" / "share.html"
WORKFLOW = ROOT / ".github" / "workflows" / "browser-smoke.yml"
NODE = shutil.which("node")

# Loads share.js into a vm context (its DOMContentLoaded hook is a no-op here)
# and evaluates each request. A request is {fn, args}; a DOM argument is a
# compact tree: ["tag", {attrs}, ...children], a string is a text node and
# {"comment": "..."} is a comment node.
_DRIVER = r"""
const fs = require('fs'); const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
// A tiny DOM for _shareShowPlainText: elements, text nodes, classes, attributes.
const el = tag => ({nodeType: 1, tagName: tag.toUpperCase(), className: '', children: [], attrs: {}, _text: '',
  classList: {list: [], add(c){ this.list.push(c); }},
  setAttribute(k, v){ this.attrs[k] = String(v); },
  appendChild(c){ this.children.push(c); return c; },
  set textContent(v){ this.children = []; this._text = String(v); }, get textContent(){ return this._text; }});
const ctx = {console, window: {location: {pathname: '/'}},
  document: {addEventListener(){}, createElement: el, createTextNode: d => ({nodeType: 3, data: String(d)})},
  _isSafeDataImageUri: v => /^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(v)};
vm.createContext(ctx);
// The page loads ui.js first: share.js asks the renderer's own MEDIA: token parser
// (_mediaTokenParts) whether a token is consumed. Load that one function from ui.js.
const uiPath = require('path').join(require('path').dirname(process.argv[2]), 'ui.js');
if (fs.existsSync(uiPath)) {
  const ui = fs.readFileSync(uiPath, 'utf8'), at = ui.indexOf('function _mediaTokenParts(');
  if (at >= 0) vm.runInContext(ui.slice(at, ui.indexOf('\n}\n', at) + 3), ctx);
}
vm.runInContext(src, ctx);
const dump = n => n.nodeType === 3 ? {text: n.data} :
  {tag: n.tagName.toLowerCase(), cls: n.className, attrs: n.attrs, alt: n.alt, src: n.src, text: n._text,
   classes: n.classList.list, children: n.children.map(dump)};
ctx.__plain = source => { const body = el('div'); ctx._shareShowPlainText(body, source); return dump(body); };
// Named references stay as written under node (no textarea decoding here).
ctx.document.createElement = (orig => tag => tag === 'textarea' ? (() => { throw new Error('no dom'); })() : orig(tag))(el);
const build = n => {
  if (typeof n === 'string') return {nodeType: 3, data: n};
  if (n && n.comment !== undefined) return {nodeType: 8, data: n.comment};
  const [tag, attrs, ...kids] = n;
  return {nodeType: 1, tagName: tag.toUpperCase(),
          attributes: Object.entries(attrs || {}).map(([name, value]) => ({name, value})),
          childNodes: kids.map(build)};
};
const out = JSON.parse(fs.readFileSync(0, 'utf8')).map(req => {
  const args = req.args.map(a => (a && a.dom !== undefined) ? {nodeType: 1, tagName: 'DIV', attributes: [], childNodes: a.dom.map(build)} : a);
  const t0 = Date.now();
  let value = ctx[req.fn](...args);
  if (Object.prototype.toString.call(value) === '[object Map]') value = Object.fromEntries(value);  // a Map from the vm realm
  return {value, ms: Date.now() - t0};
});
fs.writeSync(1, JSON.stringify(out));
"""


class _ShareJs:
    """Calls functions of the real share.js under node."""

    def __init__(self, node: str, driver: Path):
        self.node, self.driver = node, driver

    def many(self, requests):
        result = subprocess.run(
            [self.node, str(self.driver), str(SHARE_JS)],
            input=json.dumps(requests), capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout)

    def __call__(self, fn, *args):
        return self.many([{"fn": fn, "args": list(args)}])[0]["value"]


@pytest.fixture(scope="module")
def call(tmp_path_factory):
    node = NODE
    if node is None:
        pytest.skip("node not on PATH")
    driver = tmp_path_factory.mktemp("share_js") / "driver.js"
    driver.write_text(_DRIVER, encoding="utf-8")
    return _ShareJs(node, driver)


def _missing(call, source, *dom):
    return call("_shareMissingWords", source, {"dom": list(dom)})


# ── Counting words ────────────────────────────────────────────────────────────


def test_add_words(call):
    got = call("_shareSourceWords", "Un*believ*able snake_case `co`de AND and")
    assert got == {"unbelievable": 1, "snakecase": 1, "code": 1, "and": 2}


def test_cjk_and_accented_words_survive(call):
    got = call("_shareSourceWords", "中文**粗体**文字 naïve café")
    assert got == {"中文粗体文字": 1, "naïve": 1, "café": 1}


def test_source_drops_markup_that_never_shows_as_words(call):
    got = call(
        "_shareSourceWords",
        "&amp;lt;b&amp;gt;bold&lt;/b&gt; <br> E=mc<sup>2</sup> MEDIA:https://e.com/a.png\n"
        "- [x] done\n```mermaid\ngraph\n```\n[f](file:///tmp/r.txt)",
    )
    assert "lt" not in got and "amp" not in got and "b" not in got and "br" not in got and "sup" not in got
    assert "media" not in got and "x" not in got and "file" not in got
    # A mermaid fence's language shows on the page (the diagram block's class, the code header, or
    # the fence line itself), so it stays a word.
    assert got["mermaid"] == 1
    assert {"bold", "e", "mc2", "https", "com", "a", "png", "done", "graph", "tmp", "r", "txt"} <= set(got)


def test_shown_fence_languages_stay_words_and_hidden_ones_count(call):
    # renderMd shows a \\w-led one-word language in the block header; others are dropped.
    assert call("_shareSourceWords", "```python\nx\n```") == {"python": 1, "x": 1}
    got = call("_shareSourceWords", "```+the-secret-words\ny\n```\n```js two words\nz\n```")
    assert {"the", "secret", "words", "js", "two"} <= set(got)


def test_a_link_labelled_with_its_own_address_counts_it_once(call):
    assert call("_shareSourceWords", "[https://e.com/d](https://e.com/d)") == {"https": 1, "e": 1, "com": 1, "d": 1}
    assert call("_shareSourceWords", '<a href="https://e.com/d">https://e.com/d</a>') == {
        "https": 1, "e": 1, "com": 1, "d": 1}
    # a different label still counts both
    assert call("_shareSourceWords", "[docs](https://e.com/docs)")["docs"] == 2


def test_source_keeps_words_the_page_cannot_show(call):
    got = call(
        "_shareSourceWords",
        '<span data-note="note words" title="tip">t</span> <custom-tag>c</custom-tag> <span odd>o</span>\n'
        "```js two words\nx\n```",
    )
    # attribute values, unknown attribute and tag names, and a multi-word fence label
    assert {"note", "words", "tip", "data", "custom", "odd", "js", "two"} <= set(got)
    assert "span" not in got and "title" not in got


def test_inline_tags_join_and_block_tags_split(call):
    assert call("_shareSourceWords", "x<b>y</b>z") == {"xyz": 1}
    assert call("_shareSourceWords", "x<br>y") == {"x": 1, "y": 1}


def test_numeric_character_references_are_decoded_after_tags(call):
    # &#65; is "A"; &#60;b&#62; decodes to "<b>" only after tags are handled, so
    # it stays text, as the browser shows it.
    assert call("_shareSourceWords", "x&#65;y &#x42;C &#60;b&#62;") == {"xay": 1, "bc": 1, "b": 1}
    assert call("_shareSourceWords", "&#0; &#xD800; &#99999999;") == {}


def test_named_references_stay_as_written_without_a_dom(call):
    # Under node there is no DOM to decode with; the name is kept (a false alarm
    # at worst). The browser gate checks real decoding on the share page.
    assert call("_shareSourceWords", "a&nbsp;b") == {"a": 1, "nbsp": 1, "b": 1}


def test_tag_syntax_inside_a_comment_keeps_its_words(call):
    got = call("_shareSourceWords", "x <!-- <mark> <p title=t> --> <mark>y</mark> <!-- <select>")
    assert got == {"x": 1, "mark": 1, "p": 1, "title": 1, "t": 1, "y": 1, "select": 1}
    assert _missing(call, "a <!-- <mark> --> b", ["p", {}, "a ", {"comment": " <mark> "}, " b"]) == ["mark"]


def test_markers_inside_a_comment_keep_their_words(call):
    got = call("_shareSourceWords", "<!-- MEDIA:x\n- [x] y\n```mermaid\n```zq-secret\nfile://f [d](d) -->")
    assert {"media", "x", "y", "mermaid", "zq", "secret", "file", "f"} <= set(got)
    assert got["d"] == 2


_CODE_HEADER = ["div", {"class": "pre-header"}, "markdown"]


@pytest.mark.parametrize(
    "src,dom,hidden",
    [
        ("`MEDIA: hello` <!-- media -->", ["p", {}, ["code", {}, "MEDIA: hello"], " ", {"comment": " media "}], "media"),
        ("`file:///tmp/report.txt` <!-- file -->",
         ["p", {}, ["code", {}, "file:///tmp/report.txt"], " ", {"comment": " file "}], "file"),
        ("```markdown\n- [x] done\n```\n<!-- x -->",
         [_CODE_HEADER, ["pre", {}, ["code", {}, "- [x] done"]], {"comment": " x "}], "x"),
        ("````markdown\n```mermaid\ngraph TD\n```\n````\n<!-- mermaid -->",
         [_CODE_HEADER, ["pre", {}, ["code", {}, "```mermaid\ngraph TD\n```"]], {"comment": " mermaid "}], "mermaid"),
        ('`<a href="https://example.com/zyxraw">https://example.com/zyxraw</a>` <!-- zyxraw -->',
         ["p", {}, ["code", {}, '<a href="https://example.com/zyxraw">https://example.com/zyxraw</a>'], " ",
          {"comment": " zyxraw "}], "zyxraw"),
        ("`[https://e.com/zqcedar](https://e.com/zqcedar)` <!-- zqcedar -->",
         ["p", {}, ["code", {}, "[https://e.com/zqcedar](https://e.com/zqcedar)"], " ", {"comment": " zqcedar "}],
         "zqcedar"),
    ],
    ids=["media", "file", "task-box", "nested-mermaid", "raw-anchor", "markdown-link"],
)
def test_a_word_shown_in_code_cannot_pay_for_a_hidden_copy(call, src, dom, hidden):
    # Code shows its text as written, so the marker, task-box, mermaid, link and anchor rules
    # must not shorten it: the hidden copy is caught, and without it nothing is missing.
    dom = dom if isinstance(dom[0], list) else [dom]
    assert call("_shareMissingWords", src, {"dom": dom}) == [hidden]
    shown_src = src.rsplit("<!--", 1)[0].rstrip()
    shown_dom = [n for n in dom if not (isinstance(n, dict) and "comment" in n)]
    shown_dom = [[*n[:2], *[c for c in n[2:] if not (isinstance(c, dict) and "comment" in c)]]
                 if isinstance(n, list) else n for n in shown_dom]
    assert call("_shareMissingWords", shown_src, {"dom": shown_dom}) == []


def test_a_link_labelled_with_a_code_span_of_its_address_counts_it_once(call):
    dom = ["p", {}, ["a", {"href": "https://e.com/zqd"}, ["code", {}, "https://e.com/zqd"]]]
    assert _missing(call, "[`https://e.com/zqd`](https://e.com/zqd)", dom) == []
    dom = ["p", {}, ["a", {"href": "https://e.com/zqr"}, ["code", {}, "https://e.com/zqr"]]]
    assert _missing(call, '<a href="https://e.com/zqr">`https://e.com/zqr`</a>', dom) == []


def test_a_link_made_inside_code_gives_no_extra_credit(call):
    # renderMd autolinks an address even inside a code span; that link's address must not pay
    # for a hidden copy of a word the code already shows.
    code = ["code", {}, '<a href="',
            ["a", {"href": 'https://example.com/zqmark">https://example.com/zqmark</a>'},
             "https://example.com/zqmark&quot;&gt;https://example.com/zqmark&lt;/a&gt"], ";"]
    src = '`<a href="https://example.com/zqmark">https://example.com/zqmark</a>`'
    assert _missing(call, src + " <!-- zqmark -->", ["p", {}, code, " ", {"comment": " zqmark "}]) == ["zqmark"]
    assert _missing(call, src, ["p", {}, code]) == []
    # An address written in code still shows in full.
    dom = ["p", {}, "Open ", ["code", {}, ["a", {"href": "https://example.com/docs"}, "https://example.com/docs"]]]
    assert _missing(call, "Open `https://example.com/docs`", dom) == []


def test_a_mermaid_fence_label_counts_once_closed_or_not(call):
    assert call("_shareSourceWords", "```mermaid\ngraph TD\n```") == {"mermaid": 1, "graph": 1, "td": 1}
    assert call("_shareSourceWords", "```mermaid\ngraph TD") == {"mermaid": 1, "graph": 1, "td": 1}


_MERMAID_BLOCK = ["div", {"class": "mermaid-block", "data-mermaid-id": "mermaid-x"}, "graph TD\n  A-->B"]
_MERMAID_TEXT = [["div", {"class": "pre-header"}, "mermaid"],
                 ["pre", {}, ["code", {"class": "language-mermaid"}, "not a diagram"]]]


@pytest.mark.parametrize(
    "src,dom,twin_src,twin_dom,hidden",
    [
        ("```mermaid\ngraph TD\n  A-->B\n```\n<!-- mermaid -->", [_MERMAID_BLOCK, {"comment": " mermaid "}],
         "```mermaid\ngraph TD\n  A-->B\n```", [_MERMAID_BLOCK], "mermaid"),
        ("```mermaid\nnot a diagram\n```\n<!-- mermaid -->", [*_MERMAID_TEXT, {"comment": " mermaid "}],
         "```mermaid\nnot a diagram\n```", _MERMAID_TEXT, "mermaid"),
        ("<!-- mermaid -->\n```mermaid\ngraph TD",
         [["p", {}, {"comment": " mermaid "}, ["br", {}], "```mermaid", ["br", {}], "graph TD"]],
         "```mermaid\ngraph TD", [["p", {}, "```mermaid", ["br", {}], "graph TD"]], "mermaid"),
        ("MEDIA: hello <!-- media -->", [["p", {}, "MEDIA: hello ", {"comment": " media "}]],
         "MEDIA: hello", [["p", {}, "MEDIA: hello"]], "media"),
        ("**MEDIA:** <!-- media -->", [["p", {}, ["strong", {}, "MEDIA:"], " ", {"comment": " media "}]],
         "**MEDIA:**", [["p", {}, ["strong", {}, "MEDIA:"]]], "media"),
        ("See file:// here <!-- file -->", [["p", {}, "See file:// here ", {"comment": " file "}]],
         "See file:// here", [["p", {}, "See file:// here"]], "file"),
        ("Saved (file:///tmp/r.txt) <!-- file -->", [["p", {}, "Saved (file:///tmp/r.txt) ", {"comment": " file "}]],
         "Saved (file:///tmp/r.txt)", [["p", {}, "Saved (file:///tmp/r.txt)"]], "file"),
        ("<pre>see file:///tmp/x.txt</pre>\n<!-- file -->", [["pre", {}, "see file:///tmp/x.txt"], {"comment": " file "}],
         "<pre>see file:///tmp/x.txt</pre>", [["pre", {}, "see file:///tmp/x.txt"]], "file"),
        # The renderer parses MEDIA: before code, so a closing backtick right before it decides the token:
        # here the token is left as text, and `MEDIA` shows once.
        ("`a.`MEDIA:` <!-- media -->", [["p", {}, ["code", {}, "a."], "MEDIA:` ", {"comment": " media "}]],
         "`a.`MEDIA:`", [["p", {}, ["code", {}, "a."], "MEDIA:`"]], "media"),
    ],
    ids=["mermaid-diagram", "mermaid-text", "mermaid-unclosed", "media-space", "media-bold", "file-bare",
         "file-in-parens", "file-in-pre", "media-after-code"],
)
def test_a_marker_the_page_still_shows_cannot_pay_for_a_hidden_copy(call, src, dom, twin_src, twin_dom, hidden):
    # A marker word is removed from the source only where the renderer consumes it. Where the page
    # still shows it, a hidden copy is caught, and without that copy nothing is missing.
    assert call("_shareMissingWords", src, {"dom": dom}) == [hidden]
    assert call("_shareMissingWords", twin_src, {"dom": twin_dom}) == []


@pytest.mark.parametrize(
    "src,dom",
    [
        ("Here: MEDIA:/tmp/chart.png",
         [["p", {}, "Here: ", ["span", {"class": "msg-artifact-image"},
                               ["img", {"class": "msg-media-img", "src": "api/media?path=%2Ftmp%2Fchart.png",
                                        "alt": "chart.png"}]]]]),
        ("Saved to file:///tmp/report.txt",
         [["p", {}, "Saved to ", ["a", {"class": "msg-media-link", "href": "api/media?path=%2Ftmp%2Freport.txt",
                                        "download": "report.txt"}, "📎 report.txt"]]]),
        ("Report: [report](file:///tmp/report.txt)",
         [["p", {}, "Report: ", ["a", {"href": "api/media?path=%2Ftmp%2Freport.txt&inline=1"}, "report"]]]),
    ],
    ids=["media-token", "file-bare", "file-link"],
)
def test_a_marker_the_renderer_consumes_is_not_counted(call, src, dom):
    assert call("_shareMissingWords", src, {"dom": dom}) == []


def test_a_media_token_inside_code_keeps_its_word(call):
    # The renderer turns MEDIA:<ref> into a media chip even inside code. The source side keeps the
    # code's text as written, so a hidden copy of `media` can't hide behind the chip's class name.
    dom = [["p", {}, ["code", {}, ["a", {"class": "msg-media-link", "href": "api/media?path=%2Ftmp%2Fa.png"},
                                   "a.png"]], " ", {"comment": " media "}]]
    assert "media" in call("_shareMissingWords", "`MEDIA:/tmp/a.png` <!-- media -->", {"dom": dom})


@pytest.mark.parametrize(
    "dom",
    [
        ["pre", {}, ["a", {"href": "https://e.com/zqpre"}, "x"]],
        ["code", {}, ["span", {}, ["a", {"href": "https://e.com/zqpre"}, "x"]]],
        ["code", {}, ["img", {"src": "https://e.com/zqpre.png", "alt": "x"}]],
    ],
    ids=["link-in-pre", "link-nested-in-code", "image-in-code"],
)
def test_an_address_inside_code_gives_no_credit_at_any_depth(call, dom):
    # Inside <pre> or <code>, at any depth, a link's or image's address adds no words.
    assert _missing(call, "```\nx\n``` <!-- zqpre -->", dom, {"comment": " zqpre "}) == ["zqpre"]


def test_an_anchor_with_two_addresses_is_not_collapsed(call):
    # The label-equals-address credit applies only to an anchor with exactly one href.
    got = call("_shareSourceWords", '<a href="https://e.com/zz" href="https://e.com/zz">https://e.com/zz</a>')
    assert got["zz"] == 3


# ── One normalization on both sides ───────────────────────────────────────────


def test_a_literal_entity_in_code_cannot_cover_a_hidden_word(call):
    # The page shows `&copy;` as written inside code; both sides decode it, so it
    # gives no "copy" credit to the hidden comment.
    dom = ["p", {}, "Use ", ["code", {}, "&copy;"], " literally; ", {"comment": " copy "}]
    assert _missing(call, "Use `&copy;` literally; <!-- copy -->", dom) == ["copy"]
    dom = ["p", {}, "Use ", ["code", {}, "&copy;"], " literally"]
    assert _missing(call, "Use `&copy;` literally", dom) == []


def test_percent_escapes_count_once_in_one_form(call):
    # A %-escaped address can't credit both its written and its decoded word.
    dom = ["p", {}, ["a", {"href": "https://e.com/%7Aqsecret"}, "docs"], " ", {"comment": " zqsecret "}]
    assert _missing(call, "[docs](https://e.com/%7Aqsecret) <!-- zqsecret -->", dom) == ["zqsecret"]
    dom = ["p", {}, ["a", {"href": "https://e.com/zqsecret/%41"}, "https://e.com/zqsecret/%41"], " ",
           {"comment": " zqsecret "}]
    assert _missing(call, "https://e.com/zqsecret/%41 <!-- zqsecret -->", dom) == ["zqsecret"]


def test_math_spans_break_words(call):
    dom = ["p", {}, "这是", ["span", {"class": "katex-inline", "data-katex": "inline"}, "x"], "的值"]
    assert _missing(call, "这是$x$的值", dom) == []
    # The data-katex attribute alone marks a math span too.
    dom = ["p", {}, "这是", ["span", {"data-katex": "inline"}, "x"], "的值"]
    assert _missing(call, "这是$x$的值", dom) == []


# ── What the rendered message shows ───────────────────────────────────────────


def test_comments_are_not_shown(call):
    assert _missing(call, "a <!-- b --> c", ["p", {}, "a ", {"comment": " b "}, " c"]) == ["b"]


def test_counts_catch_a_hidden_copy_of_a_visible_word(call):
    assert _missing(call, "word <!-- word -->", ["p", {}, "word ", {"comment": " word "}]) == ["word"]


def test_block_elements_split_words_inline_elements_join_them(call):
    assert _missing(call, "- a\n- b", ["ul", {}, ["li", {}, "a"], ["li", {}, "b"]]) == []
    assert _missing(call, "**bo**ld", ["p", {}, ["strong", {}, "bo"], "ld"]) == []


def test_addresses_alt_text_and_renderer_attributes_count(call):
    dom = ["p", {}, ["a", {"href": "https://e.com/docs", "target": "_blank", "rel": "noopener"}, "docs"], " ",
           ["img", {"src": "https://e.com/cat.png", "alt": "a cat", "class": "msg-media-img", "loading": "lazy"}]]
    src = '[docs](https://e.com/docs) <img src="https://e.com/cat.png" class="msg-media-img" alt="a cat" loading="lazy">'
    assert _missing(call, src, dom) == []


def test_a_bare_link_counts_its_address_once(call):
    dom = ["p", {}, ["a", {"href": "https://e.com/word"}, "https://e.com/word"], " ", {"comment": " word "}]
    assert _missing(call, "https://e.com/word <!-- word -->", dom) == ["word"]


def test_percent_encoded_addresses_match_plain_ones(call):
    dom = ["p", {}, ["a", {"href": "https://e.com/a%20b"}, "x"]]
    assert _missing(call, "[x](https://e.com/a b)", dom) == []


def test_a_list_number_counts_only_where_the_browser_shows_it(call):
    # A numbered list shows its item's number in full up to four digits.
    assert _missing(call, '<ol><li value="3">item</li></ol>', ["ol", {}, ["li", {"value": "3"}, "item"]]) == []
    assert _missing(call, "2023. Launched\n2024. Grew",
                    ["ol", {}, ["li", {"value": "2023"}, "Launched"], ["li", {"value": "2024"}, "Grew"]]) == []
    # Outside an <ol>, past four digits (a phone cuts the leading digits), or inside a block that
    # clips the margin where the number is drawn, the number is not shown in full.
    cases = [
        ("731337", '<ul><li value="731337">item</li></ul>', ["ul", {}, ["li", {"value": "731337"}, "item"]]),
        ("731338", '<li value="731338">item</li>', ["li", {"value": "731338"}, "item"]),
        ("99999999999", '<ol><li value="99999999999">item</li></ol>',
         ["ol", {}, ["li", {"value": "99999999999"}, "item"]]),
        ("12345", "12345. item", ["ol", {}, ["li", {"value": "12345"}, "item"]]),
        ("42", '<div class="katex-block"><ol><li value="42">x</li></ol></div>',
         ["div", {"class": "katex-block"}, ["ol", {}, ["li", {"value": "42"}, "x"]]]),
        ("123", '<div class="mermaid-block"><ol><li value="123">x</li></ol></div>',
         ["div", {"class": "mermaid-block"}, ["ol", {}, ["li", {"value": "123"}, "x"]]]),
        ("4321", '<pre><ol><li value="4321">x</li></ol></pre>',
         ["pre", {}, ["ol", {}, ["li", {"value": "4321"}, "x"]]]),
    ]
    for number, src, dom in cases:
        assert _missing(call, src, dom) == [number], src


def test_html_examples_in_code_keep_their_formatting(call):
    src = "HTML: `<span>Hello</span><span>world</span>`. See [documentation](https://example.org/html)."
    dom = ["p", {}, "HTML: ", ["code", {}, "<span>Hello</span><span>world</span>"], ". See ",
           ["a", {"href": "https://example.org/html"}, "documentation"], "."]
    assert _missing(call, src, dom) == []
    src = "```\n<b>bold</b><i>it</i>\n```"
    dom = ["pre", {}, ["code", {}, "<b>bold</b><i>it</i>"]]
    assert _missing(call, src, dom) == []


def test_link_labels_match_their_address_the_same_way_on_both_sides(call):
    # A label that differs from its address only by a trailing slash shows the address once.
    dom = ["p", {}, ["a", {"href": "https://example.com/"}, "https://example.com"]]
    assert _missing(call, "[https://example.com](https://example.com/)", dom) == []
    # So does an address-labelled anchor with extra attributes.
    src = '<a href="https://e.com/d" target="_blank" rel="noopener">https://e.com/d</a>'
    dom = ["p", {}, ["a", {"href": "https://e.com/d", "target": "_blank", "rel": "noopener"}, "https://e.com/d"]]
    assert _missing(call, src, dom) == []
    # A hidden copy of the address word is still caught.
    dom = ["p", {}, ["a", {"href": "https://example.com/"}, "https://example.com"], " ", {"comment": " example "}]
    assert _missing(call, "[https://example.com](https://example.com/) <!-- example -->", dom) == ["example"]
    # The renderer drops the address of a raw anchor with any other scheme, so an encoded label
    # gives no credit for the address's words.
    for href in ["secret-plan", "javascript:secret-plan", "ftp://secret-plan"]:
        src = f'<a href="{href}">%73%65%63%72%65%74-%70%6C%61%6E</a>'
        assert set(_missing(call, src, ["p", {}, ["a", {}, "%73%65%63%72%65%74-%70%6C%61%6E"]])) >= {"secret", "plan"}, href
    # Other attributes of an address-labelled anchor keep their words.
    src = '<a href="https://e.com/zqd3x" title="zqd4x">https://e.com/zqd3x</a>'
    assert _missing(call, src, ["p", {}, ["a", {"href": "https://e.com/zqd3x"}, "https://e.com/zqd3x"]]) == ["zqd4x"]
    src = '<a href="https://e.com/zqd9x"/title="zqe1x">https://e.com/zqd9x</a>'
    assert "zqe1x" in _missing(call, src, ["p", {}, ["a", {"href": "https://e.com/zqd9x"}, "https://e.com/zqd9x"]])


@pytest.mark.parametrize(
    "attrs",
    [{"data-mermaid-id": "secretword"}, {"download": "secretword"}, {"style": "secretword"},
     {"class": "language-secretword"}],
)
def test_free_text_attributes_never_count(call, attrs):
    dom = ["p", {}, ["span", attrs, "x"]]
    assert _missing(call, "x secretword", dom) == ["secretword"]


def test_ordinary_message_has_nothing_missing(call):
    src = "## Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n`Vec<T>` and $x$"
    dom = ["h2", {}, "Title"], ["table", {}, ["tr", {}, ["th", {}, "a"], ["th", {}, "b"]],
                                ["tr", {}, ["td", {}, "1"], ["td", {}, "2"]]], \
          ["p", {}, ["code", {}, "Vec<T>"], " and ", ["span", {"class": "katex-inline"}, "x"]]
    assert _missing(call, src, *dom) == []


# ── Cost ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["<a " * 100_000, "<" * 400_000, '<a x="' + "b " * 200_000, "<a b=" * 80_000, "&amp;" * 100_000,
     "- [x] \n" * 50_000, "```\n" * 100_000, "&copy;" * 100_000, "&#65;" * 100_000, "<!--" * 100_000,
     "<!-- <a> -->" * 40_000, "a" + "\n" * 200_000 + "b"],
    ids=["unclosed_tags", "lt_run", "open_quote", "open_values", "entities", "task_boxes", "fences",
         "named_refs", "numeric_refs", "comment_openers", "comments", "blank_lines"],
)
def test_source_words_stay_linear(call, text):
    out = call.many([{"fn": "_shareSourceWords", "args": [text]}])[0]
    assert out["ms"] < 2000, out["ms"]


# ── Wiring ────────────────────────────────────────────────────────────────────


def test_share_page_checks_each_message_before_highlighting():
    # Behaviour is proven in the browser gate; this pins the exact statement so a
    # disabled or bypassed check fails the ordinary suite too.
    js = SHARE_JS.read_text(encoding="utf-8")
    render = js[js.index("function _shareRenderMessages"):]
    stmt = "if(body&&_shareMissingWords(source,body).length) _shareShowPlainText(body,source);"
    assert render.count(stmt) == 1
    assert render.index("const body=row.querySelector('.share-message-body');") < render.index(stmt)
    assert render.index(stmt) < render.index("wrap.appendChild(row);") < render.index("highlightCode(wrap)")


def test_plain_view_shows_server_embedded_images_as_images(call):
    b64 = "iVBORw0KGgo" + "A" * 4000
    embed = f'<img src="data:image/png;base64,{b64}" class="msg-media-img" alt="shot &amp; notes.png" loading="lazy">'
    other = '<img src="data:image/png;base64,QUJD" class="msg-media-img" alt="x" loading="lazy" title="t">'
    out = call("__plain", f"before {embed} after <!-- hidden --> {other}")
    note, pre = out["children"]
    assert note["attrs"] == {"role": "note"}
    assert pre["cls"] == "share-plain-text"
    kinds = [c.get("tag", "text") for c in pre["children"]]
    assert kinds == ["text", "img", "text"]
    img = pre["children"][1]
    assert img["src"] == f"data:image/png;base64,{b64}" and img["alt"] == "shot & notes.png"
    assert img["cls"] == "msg-media-img share-plain-image"
    shown = "".join(c.get("text", "") for c in pre["children"] if "tag" not in c)
    assert b64 not in shown
    # Everything else, including an embed not in the server's exact form, is shown as text.
    assert shown == f"before  after <!-- hidden --> {other}"


def test_plain_view_keeps_an_unsafe_embed_as_text(call):
    svg = '<img src="data:image/svg+xml;base64,PHN2Zz4=" class="msg-media-img" alt="a" loading="lazy">'
    out = call("__plain", svg)
    pre = out["children"][1]
    assert [c.get("tag", "text") for c in pre["children"]] == ["text"]
    assert pre["children"][0]["text"] == svg


def test_plain_text_view_is_styled_and_uses_text_content():
    js = SHARE_JS.read_text(encoding="utf-8")
    plain = js[js.index("function _shareShowPlainText"):js.index("function _shareRenderMessages")]
    assert "createTextNode(" in plain and "innerHTML" not in plain
    assert "note.setAttribute('role','note');" in plain
    assert "'Shown as plain text because formatting could hide part of this message.'" in plain
    html = SHARE_HTML.read_text(encoding="utf-8")
    assert ".share-plain-note" in html and "pre.share-plain-text" in html


def test_browser_gate_runs_in_ci():
    assert "python tests/browser_share_hidden_text.py" in WORKFLOW.read_text(encoding="utf-8")
