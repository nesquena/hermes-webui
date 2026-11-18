"""Stash tokens must never survive inside a rendered attribute value.

renderMd() swaps blockquotes, math and code for ``\\x00...\\x00`` stash tokens,
sanitizes the remaining HTML, and restores the stashed HTML afterwards. A token
that ended up inside an attribute value (a raw ``<img alt>``, for example) was
restored there too. The restored markup's quotes then closed the attribute
early and the rest of it parsed as new attributes. The attribute sanitizer now
drops stash tokens, so a restore never lands inside a tag.

Drives the real renderMd() through the harness in test_renderer_js_behaviour.py.
"""

from html.parser import HTMLParser

import pytest

from tests.test_renderer_js_behaviour import NODE, _render, driver_path  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, [name for name, _value in attrs]))


def _tags(html):
    parser = _Tags()
    parser.feed(html)
    return parser.tags


# A stashed block placed inside an attribute value, one per stash kind.
_TOKEN_IN_ATTRIBUTE = {
    'blockquote-in-img-alt': '<img src="api/x" alt="\n> <a href="https://example.com/x y z">t</a>\n">',
    'math-in-img-alt': '<img src="api/x" alt="$x$">',
    'fence-in-img-alt': '<img src="api/x" alt="\n```\nplain\n```\n">',
}


@pytest.mark.parametrize("markdown", list(_TOKEN_IN_ATTRIBUTE.values()), ids=list(_TOKEN_IN_ATTRIBUTE))
def test_stash_token_never_survives_in_an_attribute(driver_path, markdown):  # noqa: F811
    html = _render(driver_path, markdown)
    assert html == '<p><img src="api/x" alt=""></p>', html
    assert _tags(html) == [("p", []), ("img", ["src", "alt"])], html


# Ordinary content that goes through the same stashes renders exactly as before.
_UNCHANGED = {
    'blockquote': ('> quote line\n> second', '<blockquote><p>quote line<br>second</p></blockquote>'),
    'blockquote-with-link-and-code': ('> see [docs](https://example.com/docs) and `code`', '<blockquote><p>see <a href="https://example.com/docs" target="_blank" rel="noopener">docs</a> and <code>code</code></p></blockquote>'),
    'nested-blockquote': ('> outer\n>> inner **bold**', '<blockquote><p>outer</p>\n<blockquote><p>inner <strong>bold</strong></p></blockquote></blockquote>'),
    'blockquote-with-fence': ('> ```python\n> print("hi")\n> ```', '<blockquote><div class="pre-header">python</div><pre><code class="language-python">print(&quot;hi&quot;)</code></pre></blockquote>'),
    'inline-math': ('Euler: $e^{i\\pi}+1=0$', '<p>Euler: <span class="katex-inline" data-katex="inline">e^{i\\pi}+1=0</span></p>'),
    'raw-img-alt': ('<img src="api/x" alt="plain &amp; simple">', '<p><img src="api/x" alt="plain &amp; simple"></p>'),
    'raw-anchor': ('<a href="https://example.com" title="Example">x</a>', '<p><a href="https://example.com">x</a></p>'),
    'table-with-code-and-link': ('| a | b |\n|---|---|\n| `c` | [d](https://example.com) |', '\n<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td><code>c</code></td><td><a href="https://example.com" target="_blank" rel="noopener">d</a></td></tr></tbody></table>\n'),
}


@pytest.mark.parametrize("markdown,expected", list(_UNCHANGED.values()), ids=list(_UNCHANGED))
def test_ordinary_attributes_and_stashed_blocks_render_unchanged(driver_path, markdown, expected):  # noqa: F811
    assert _render(driver_path, markdown) == expected


# ── Every attribute the sanitizer rebuilds stays a single, plain value ─────────
# Raw NUL characters in the input would let text forge a stash token, and two
# rebuilt attributes (a link's download name, a diagram id) skipped the value
# cleanup. The autolink pass also rewrote a URL inside a diagram id.

_ANCHOR = '<a href="https://example.com/x y z">t</a>'

_ALLOWED_ATTRIBUTES = {
    "src", "alt", "href", "title", "class", "loading", "target", "rel", "download",
    "id", "value", "data-katex", "data-mermaid-id", "data-raw", "data-lang",
}

_FORGED_OR_UNCLEANED = {
    "forged-token-in-autolink": "https://example.com/\x00Q0\x00\n> " + _ANCHOR,
    "forged-token-in-download": '<a href="https://example.com" download="\x00Q0\x00">x</a>\n> ' + _ANCHOR,
    "forged-token-in-diagram-id": '<div data-mermaid-id="\x00Q0\x00"></div>\n> ' + _ANCHOR,
    "forged-token-in-json-fence": '```json\n{"x":"\x00Q0\x00"}\n```\n> ' + _ANCHOR,
    "math-in-download": '<a href="https://example.com" download="$x$">x</a>',
    "math-in-diagram-id": '<div data-mermaid-id="$x$"></div>',
    "url-in-diagram-id": '<div data-mermaid-id="https://example.com/a/b=c"></div>',
}


class _AllAttrs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.attrs.extend((tag, name, value or "") for name, value in attrs)


@pytest.mark.parametrize("markdown", list(_FORGED_OR_UNCLEANED.values()), ids=list(_FORGED_OR_UNCLEANED))
def test_rebuilt_attributes_hold_one_plain_value(driver_path, markdown):  # noqa: F811
    html = _render(driver_path, markdown)
    assert "\x00" not in html, html
    parser = _AllAttrs()
    parser.feed(html)
    for tag, name, value in parser.attrs:
        assert name in _ALLOWED_ATTRIBUTES, (tag, name, html)
        assert "<" not in value, (tag, name, html)


def test_generated_diagram_ids_are_kept(driver_path):  # noqa: F811
    html = _render(driver_path, '<div class="mermaid-block" data-mermaid-id="mermaid-ab12cd34"></div>')
    assert 'data-mermaid-id="mermaid-ab12cd34"' in html, html


# ── Media references whose decoded path carries a NUL render nothing ──────────

def test_media_reference_with_encoded_nul_renders_nothing(tmp_path_factory):
    from tests.test_issue8033_file_uri_windows import UI_JS_PATH, _SETTLED_DRIVER, _run_node_driver

    cases = ["file:///%00Q0%00.png", "file:///notes/a%00b.png", "/a\x00Q0\x00.png"]
    results = _run_node_driver(_SETTLED_DRIVER, UI_JS_PATH, cases, tmp_path_factory)
    for case in cases:
        assert results[case]["html"] == "", (case, results[case])
    ordinary = _run_node_driver(_SETTLED_DRIVER, UI_JS_PATH, ["file:///notes/a%20b.png"], tmp_path_factory)
    assert ordinary["file:///notes/a%20b.png"]["pathParam"] == "/notes/a b.png", ordinary


# ── Autolink and code-tree attributes are built from source text only ────────
# The autolink pass runs while math, media and link stashes are still tokens,
# and a JSON/YAML code tree builds its raw-text attribute while MEDIA references
# are still tokens. Each token is restored after the attribute is built, so the
# rebuilt attribute must never hold one.


class _StartTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, [(name, value or "") for name, value in attrs]))


def _plain_attribute_tags(html):
    """Every attribute value is plain text: no stash token, no restored markup."""
    assert "\x00" not in html, html
    parser = _StartTags()
    parser.feed(html)
    for tag, attrs in parser.tags:
        for name, value in attrs:
            assert name in _ALLOWED_ATTRIBUTES, (tag, name, html)
            assert "<" not in value, (tag, name, html)
    return parser.tags


_ODATA_URL = "https://svc.example/People?$top=2&$skip=1"
_ODATA_HTML = (
    '<a href="https://svc.example/People" target="_blank" rel="noopener">https://svc.example/People</a>'
    '?<span class="katex-inline" data-katex="inline">top=2&amp;</span>skip=1'
)

_AUTOLINK_BEFORE_TOKEN = {
    "math-token-in-paragraph": (_ODATA_URL, "<p>" + _ODATA_HTML + "</p>"),
    "math-token-in-list-item": ("- " + _ODATA_URL, "<ul><li>" + _ODATA_HTML + "</li></ul>"),
    "link-right-after-url": (
        "https://example.com[x](https://example.org/y)",
        '<p><a href="https://example.com" target="_blank" rel="noopener">https://example.com</a>'
        '<a href="https://example.org/y" target="_blank" rel="noopener">x</a></p>',
    ),
}


@pytest.mark.parametrize(
    "markdown,expected", list(_AUTOLINK_BEFORE_TOKEN.values()), ids=list(_AUTOLINK_BEFORE_TOKEN)
)
def test_autolink_href_ends_before_a_stash_token(driver_path, markdown, expected):  # noqa: F811
    html = _render(driver_path, markdown)
    _plain_attribute_tags(html)
    assert html == expected, html


def test_bare_url_then_media_reference_keeps_the_media_after_the_link(driver_path):  # noqa: F811
    link = '<a href="https://example.com/a" target="_blank" rel="noopener">https://example.com/a</a>'
    image = '<img class="msg-media-img" src="api/media?path=%2Ftmp%2Fa.png" alt="image" loading="lazy">'
    for separator in (" ", ""):
        html = _render(driver_path, "https://example.com/a" + separator + "MEDIA:/tmp/a.png")
        _plain_attribute_tags(html)
        assert html == "<p>" + link + separator + image + "</p>", (separator, html)


_CODE_TREE_SOURCES = {
    "json": '{\n  "img": "MEDIA:/tmp/a.png",\n  "n": 1\n}',
    "yaml": "img: MEDIA:/tmp/a.png\nn: 1",
}


@pytest.mark.parametrize("lang", list(_CODE_TREE_SOURCES))
def test_code_tree_raw_attribute_holds_the_fence_source(driver_path, lang):  # noqa: F811
    source = _CODE_TREE_SOURCES[lang]
    html = _render(driver_path, "```" + lang + "\n" + source + "\n```")
    tags = _plain_attribute_tags(html)
    wraps = [attrs for tag, attrs in tags if tag == "div" and ("class", "code-tree-wrap") in attrs]
    assert len(wraps) == 1, html
    attrs = dict(wraps[0])
    assert [name for name, _value in wraps[0]] == ["class", "data-raw", "data-lang", "id"], html
    assert attrs["data-raw"] == source, html
    assert attrs["data-lang"] == lang, html
    assert '<pre class="tree-raw-view"><code class="language-' + lang + '">' in html, html


# ── A raw NUL renders as U+FFFD, never as entity text ─────────────────────────

_RAW_NUL = {
    "plain-text": ("a\x00b", "<p>a\ufffdb</p>"),
    "inline-code": ("`a\x00b`", "<p><code>a\ufffdb</code></p>"),
    "fenced-code": ("```\na\x00b\n```", "<pre><code>a\ufffdb</code></pre>"),
}


@pytest.mark.parametrize("markdown,expected", list(_RAW_NUL.values()), ids=list(_RAW_NUL))
def test_raw_nul_renders_as_replacement_character(driver_path, markdown, expected):  # noqa: F811
    html = _render(driver_path, markdown)
    assert html == expected, html
    assert "#0;" not in html, html


def test_raw_nul_in_code_tree_source_renders_as_replacement_character(driver_path):  # noqa: F811
    html = _render(driver_path, '```json\n{"a":"x\x00y"}\n```')
    assert "#0;" not in html, html
    tags = _plain_attribute_tags(html)
    wraps = [attrs for tag, attrs in tags if tag == "div" and ("class", "code-tree-wrap") in attrs]
    assert len(wraps) == 1, html
    assert dict(wraps[0])["data-raw"] == '{"a":"x\ufffdy"}', html
    assert '<code class="language-json">{&quot;a&quot;:&quot;x\ufffdy&quot;}</code>' in html, html
