"""Autolinking must not rewrite text inside a tag.

renderMd() turns bare http(s) URLs into links. Complete links, images and pre
blocks were set aside first, but an opening tag without its closing tag (an
``<a>`` with no ``</a>``) was not. A bare URL inside an attribute value of
such an unclosed tag was rebuilt in place as ``<a href="...">``; the quote of
the rebuilt link closed the value early and the rest of the URL parsed as new
attribute names. List items, table cells and headings run their own autolink
pass (inlineMd) on the raw text, before the attribute sanitizer, with the same
gap. Both passes now set aside every tag, and inlineMd escapes a raw tag left
without its closing ">" the way the block pass already does, so autolinking
only ever rewrites the text between tags.

Every case checks the exact attribute names of every tag and that no attribute
value holds markup. Ordinary messages render byte-for-byte as before.

Drives the real renderMd() through the harness in test_renderer_js_behaviour.py.
"""

from html.parser import HTMLParser

import pytest

from tests.test_renderer_js_behaviour import NODE, _render, driver_path  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Each path segment of this URL reads like an attribute name or name=value pair.
U = "https://e.com/a/data-x=1//"


class _Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.values = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, [name for name, _value in attrs]))
        self.values.extend(value or "" for _name, value in attrs)

    handle_startendtag = handle_starttag


def _parse(html):
    parser = _Tags()
    parser.feed(html)
    parser.close()
    return parser


def _assert_no_markup_in_attribute_values(html):
    for value in _parse(html).values:
        assert "<" not in value, (value, html)


# ── A URL in an attribute value of an unclosed tag stays where it was written ──
# (markdown, exact rendered HTML, exact tag/attribute names)
_UNCLOSED = {
    'unclosed-anchor-href': (
        f'<a href="{U}">text',
        f'<p><a href="{U}">text</p>',
        [('p', []), ('a', ['href'])],
    ),
    'unclosed-anchor-download': (
        f'<a href="api/x" download="{U}">t',
        f'<p><a href="api/x" download="{U}">t</p>',
        [('p', []), ('a', ['href', 'download'])],
    ),
    'unclosed-anchor-single-quoted': (
        f"<a href='{U}'>text",
        f'<p><a href="{U}">text</p>',
        [('p', []), ('a', ['href'])],
    ),
    'unclosed-anchor-unquoted': (
        f'<a href={U}>text',
        '<p><a href="https://e.com/a/data-x=1/">text</p>',
        [('p', []), ('a', ['href'])],
    ),
    'uppercase-tag': (
        f'<A HREF="{U}">text',
        f'<p><a href="{U}">text</p>',
        [('p', []), ('a', ['href'])],
    ),
    'newline-before-close': (
        f'<a href="{U}"\ntitle="y">t',
        f'<p><a href="{U}">t</p>',
        [('p', []), ('a', ['href'])],
    ),
    'self-closing-form': (
        f'<a href="{U}" />text',
        f'<p><a href="{U}">text</p>',
        [('p', []), ('a', ['href'])],
    ),
    'entity-encoded-tag': (
        f'&lt;a href="{U}"&gt;text',
        f'<p><a href="{U}">text</p>',
        [('p', []), ('a', ['href'])],
    ),
    'after-a-closed-anchor': (
        f'<a href="api/y">ok</a> and <a href="{U}">text',
        f'<p><a href="api/y">ok</a> and <a href="{U}">text</p>',
        [('p', []), ('a', ['href']), ('a', ['href'])],
    ),
    'nested-in-unclosed-anchor': (
        f'<a href="api/y"><a href="{U}">t',
        f'<p><a href="api/y"><a href="{U}">t</p>',
        [('p', []), ('a', ['href']), ('a', ['href'])],
    ),
    'list-item': (
        f'- <a href="{U}">text',
        f'<ul><li><a href="{U}">text</li></ul>',
        [('ul', []), ('li', []), ('a', ['href'])],
    ),
    'list-item-title': (
        f'- <a href="api/x" title="{U}">t',
        '<ul><li><a href="api/x">t</li></ul>',
        [('ul', []), ('li', []), ('a', ['href'])],
    ),
    'list-item-split-tag': (
        f'- <a\n  href="{U}">t',
        f'<ul><li><a href="{U}">t</li></ul>',
        [('ul', []), ('li', []), ('a', ['href'])],
    ),
    'ordered-item': (
        f'1. <a href="{U}">text',
        f'<ol><li value="1"><a href="{U}">text</li></ol>',
        [('ol', []), ('li', ['value']), ('a', ['href'])],
    ),
    'task-item': (
        f'- [ ] <a href="{U}">text',
        f'<ul><li><span class="task-todo">☐</span> <a href="{U}">text</li></ul>',
        [('ul', []), ('li', []), ('span', ['class']), ('a', ['href'])],
    ),
    'table-cell': (
        f'| a | b |\n|---|---|\n| <a href="{U}">t | x |',
        f'\n<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td><a href="{U}">t</td><td>x</td></tr></tbody></table>\n',
        [('table', []), ('thead', []), ('tr', []), ('th', []), ('th', []), ('tbody', []), ('tr', []), ('td', []), ('a', ['href']), ('td', [])],
    ),
    'table-cell-download': (
        f'| a |\n|---|\n| <a href="api/x" download="{U}">t |',
        f'\n<table><thead><tr><th>a</th></tr></thead><tbody><tr><td><a href="api/x" download="{U}">t</td></tr></tbody></table>\n',
        [('table', []), ('thead', []), ('tr', []), ('th', []), ('tbody', []), ('tr', []), ('td', []), ('a', ['href', 'download'])],
    ),
    'heading-title': (
        f'### <a href="api/x" title="{U}">t',
        '<h3><a href="api/x">t</h3>',
        [('h3', []), ('a', ['href'])],
    ),
    'heading-download': (
        f'# <a href="api/x" download="{U}">t',
        f'<h1><a href="api/x" download="{U}">t</h1>',
        [('h1', []), ('a', ['href', 'download'])],
    ),
    'blockquote': (
        f'> <a href="{U}">text',
        f'<blockquote><p><a href="{U}">text</p></blockquote>',
        [('blockquote', []), ('p', []), ('a', ['href'])],
    ),
    'blockquote-list-item': (
        f'> - <a href="{U}">text',
        f'<blockquote><ul><li><a href="{U}">text</li></ul></blockquote>',
        [('blockquote', []), ('ul', []), ('li', []), ('a', ['href'])],
    ),
    'list-item-closed-anchor-text': (
        f'- <a href="api/x">{U}</a> and {U}',
        f'<ul><li><a href="api/x">{U}</a> and <a href="{U}" target="_blank" rel="noopener">{U}</a></li></ul>',
        [('ul', []), ('li', []), ('a', ['href']), ('a', ['href', 'target', 'rel'])],
    ),
}


@pytest.mark.parametrize(
    "markdown,expected_html,expected_tags", list(_UNCLOSED.values()), ids=list(_UNCLOSED)
)
def test_url_in_an_attribute_of_an_unclosed_tag_is_left_as_written(
    driver_path, markdown, expected_html, expected_tags  # noqa: F811
):
    html = _render(driver_path, markdown)
    assert _parse(html).tags == expected_tags, html
    _assert_no_markup_in_attribute_values(html)
    assert html == expected_html


# ── A raw tag missing its closing ">" in a list item, table cell or heading ───
# reads as text, as it already does in a paragraph; only the URL in that text
# is linked, and the rebuilt link never lands inside a tag.

_OPEN_TAG = {
    'list-item': f'- <a href="api/x" download="{U}"',
    'table-cell': f'| a |\n|---|\n| <a href="api/x" download="{U}" |',
    'heading': f'### <a href="api/x" download="{U}"',
}


@pytest.mark.parametrize("markdown", list(_OPEN_TAG.values()), ids=list(_OPEN_TAG))
def test_raw_tag_without_closing_bracket_stays_text_in_inline_blocks(driver_path, markdown):  # noqa: F811
    html = _render(driver_path, markdown)
    assert "&lt;a href=&quot;api/x&quot; download=&quot;" in html, html
    tags = _parse(html).tags
    assert tags[-1] == ("a", ["href", "target", "rel"]), html
    assert all(names == [] for _tag, names in tags[:-1]), html
    _assert_no_markup_in_attribute_values(html)


# ── Attribute URLs the sanitizer or an earlier stash already protected ────────
# (the sanitizer drops a plain link title and every span attribute but a few
# fixed classes; images were already set aside; a paragraph escapes a tag
# without its closing ">")

_ALREADY_KEPT = {
    'paragraph-title': (f'<a href="api/x" title="{U}">t', [('p', []), ('a', ['href'])]),
    'paragraph-span-title': (f'<span title="{U}">t', [('p', []), ('span', [])]),
    'paragraph-img-alt': (f'<img src="api/x" alt="{U}">', [('p', []), ('img', ['src', 'alt'])]),
    'list-item-img-alt': (f'- <img src="api/x" alt="{U}">', [('ul', []), ('li', []), ('img', ['src', 'alt'])]),
    'paragraph-no-close': (f'<a href="api/x" download="{U}"', [('p', []), ('a', ['href', 'target', 'rel'])]),
}


@pytest.mark.parametrize("markdown,expected_tags", list(_ALREADY_KEPT.values()), ids=list(_ALREADY_KEPT))
def test_attribute_urls_already_kept_stay_kept(driver_path, markdown, expected_tags):  # noqa: F811
    html = _render(driver_path, markdown)
    assert _parse(html).tags == expected_tags, html
    _assert_no_markup_in_attribute_values(html)


# ── Text with a stray "<" in a list item, table cell or heading is kept ───────
# It used to merge with the closing tag the renderer added after it and vanish.

_ANGLE_TEXT = {
    'list-item': ('- a<b', '<ul><li>a&lt;b</li></ul>'),
    'list-item-with-url': ('- x<y https://example.com', '<ul><li>x&lt;y <a href="https://example.com" target="_blank" rel="noopener">https://example.com</a></li></ul>'),
    'table-cell': ('| x<y |\n|---|\n| a<b |', '\n<table><thead><tr><th>x&lt;y</th></tr></thead><tbody><tr><td>a&lt;b</td></tr></tbody></table>\n'),
    'heading': ('## Vec<T', '<h2>Vec&lt;T</h2>'),
}


@pytest.mark.parametrize("markdown,expected", list(_ANGLE_TEXT.values()), ids=list(_ANGLE_TEXT))
def test_stray_angle_bracket_text_is_kept_in_inline_blocks(driver_path, markdown, expected):  # noqa: F811
    assert _render(driver_path, markdown) == expected


# ── Ordinary bare URLs autolink exactly as before ─────────────────────────────
# Expected HTML captured from the parent commit with this same harness.

_ORDINARY = [
    ('see https://example.com for details', '<p>see <a href="https://example.com" target="_blank" rel="noopener">https://example.com</a> for details</p>'),
    ('(https://example.com/a)', '<p>(<a href="https://example.com/a" target="_blank" rel="noopener">https://example.com/a</a>)</p>'),
    ('visit https://example.com/path.', '<p>visit <a href="https://example.com/path" target="_blank" rel="noopener">https://example.com/path</a>.</p>'),
    ('visit https://example.com/path, then https://e.org/x; ok!', '<p>visit <a href="https://example.com/path" target="_blank" rel="noopener">https://example.com/path</a>, then <a href="https://e.org/x" target="_blank" rel="noopener">https://e.org/x</a>; ok!</p>'),
    ('**https://example.com/bold**', '<p><strong><a href="https://example.com/bold" target="_blank" rel="noopener">https://example.com/bold</a></strong></p>'),
    ('*https://example.com/it*', '<p><em><a href="https://example.com/it" target="_blank" rel="noopener">https://example.com/it</a></em></p>'),
    ('[docs](https://example.com/docs) and https://example.com/bare', '<p><a href="https://example.com/docs" target="_blank" rel="noopener">docs</a> and <a href="https://example.com/bare" target="_blank" rel="noopener">https://example.com/bare</a></p>'),
    ('<a href="https://example.com">x</a>', '<p><a href="https://example.com">x</a></p>'),
    ('<a href="https://example.com">https://example.com/inner</a> and https://e.org', '<p><a href="https://example.com">https://example.com/inner</a> and <a href="https://e.org" target="_blank" rel="noopener">https://e.org</a></p>'),
    ('<img src="https://example.com/a.png" alt="a">', '<p><img src="https://example.com/a.png" alt="a"></p>'),
    ('`https://example.com/code`', '<p><code><a href="https://example.com/code" target="_blank" rel="noopener">https://example.com/code</a></code></p>'),
    ('```\nhttps://example.com/fence\n```', '<pre><code>https://example.com/fence</code></pre>'),
    ('```bash\ncurl "https://example.com/x?a=1&b=2"\n```', '<div class="pre-header">bash</div><pre><code class="language-bash">curl &quot;https://example.com/x?a=1&amp;b=2&quot;</code></pre>'),
    ('- item https://example.com/li\n- [t](https://example.com/t)', '<ul><li>item <a href="https://example.com/li" target="_blank" rel="noopener">https://example.com/li</a></li><li><a href="https://example.com/t" target="_blank" rel="noopener">t</a></li></ul>'),
    ('1. one https://example.com/1\n2. two', '<ol><li value="1">one <a href="https://example.com/1" target="_blank" rel="noopener">https://example.com/1</a></li><li value="2">two</li></ol>'),
    ('| a | b |\n|---|---|\n| https://example.com/c | [d](https://example.com/d) |', '\n<table><thead><tr><th>a</th><th>b</th></tr></thead><tbody><tr><td><a href="https://example.com/c" target="_blank" rel="noopener">https://example.com/c</a></td><td><a href="https://example.com/d" target="_blank" rel="noopener">d</a></td></tr></tbody></table>\n'),
    ('## Heading https://example.com/h', '<h2>Heading <a href="https://example.com/h" target="_blank" rel="noopener">https://example.com/h</a></h2>'),
    ('> quote https://example.com/q', '<blockquote><p>quote <a href="https://example.com/q" target="_blank" rel="noopener">https://example.com/q</a></p></blockquote>'),
    ('https://example.com/?q=1&r=2#frag', '<p><a href="https://example.com/?q=1&r=2#frag" target="_blank" rel="noopener">https://example.com/?q=1&amp;r=2#frag</a></p>'),
    ('line1\nhttps://example.com/l2\n\npara https://example.com/p', '<p>line1<br><a href="https://example.com/l2" target="_blank" rel="noopener">https://example.com/l2</a></p>\n<p>para <a href="https://example.com/p" target="_blank" rel="noopener">https://example.com/p</a></p>'),
    ('a < b and c > d https://example.com', '<p>a < b and c > d <a href="https://example.com" target="_blank" rel="noopener">https://example.com</a></p>'),
    ('x<y https://example.com', '<p>x&lt;y <a href="https://example.com" target="_blank" rel="noopener">https://example.com</a></p>'),
    ('$x$ https://example.com/m', '<p><span class="katex-inline" data-katex="inline">x</span> <a href="https://example.com/m" target="_blank" rel="noopener">https://example.com/m</a></p>'),
    ('<b>bold</b> https://example.com', '<p><strong>bold</strong> <a href="https://example.com" target="_blank" rel="noopener">https://example.com</a></p>'),
    ('<strong>https://example.com/s</strong>', '<p><strong><a href="https://example.com/s" target="_blank" rel="noopener">https://example.com/s</a></strong></p>'),
    ('- <strong>https://example.com/s</strong>', '<ul><li><strong><a href="https://example.com/s" target="_blank" rel="noopener">https://example.com/s</a></strong></li></ul>'),
    ('- `https://example.com/code`', '<ul><li><code><a href="https://example.com/code" target="_blank" rel="noopener">https://example.com/code</a></code></li></ul>'),
    ('<code>https://example.com/c</code>', '<p>`<a href="https://example.com/c`" target="_blank" rel="noopener">https://example.com/c`</a></p>'),
    ('- <code>https://example.com/c</code>', '<ul><li><code><a href="https://example.com/c" target="_blank" rel="noopener">https://example.com/c</a></code></li></ul>'),
    ('- see [docs](https://example.com/d) or https://example.com/e.', '<ul><li>see <a href="https://example.com/d" target="_blank" rel="noopener">docs</a> or <a href="https://example.com/e" target="_blank" rel="noopener">https://example.com/e</a>.</li></ul>'),
    ('| u |\n|---|\n| (https://example.com/p) |', '\n<table><thead><tr><th>u</th></tr></thead><tbody><tr><td>(<a href="https://example.com/p" target="_blank" rel="noopener">https://example.com/p</a>)</td></tr></tbody></table>\n'),
]


@pytest.mark.parametrize("markdown,expected", _ORDINARY, ids=[str(i) for i in range(len(_ORDINARY))])
def test_ordinary_bare_urls_autolink_exactly_as_before(driver_path, markdown, expected):  # noqa: F811
    assert _render(driver_path, markdown) == expected
