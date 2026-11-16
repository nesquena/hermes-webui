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
