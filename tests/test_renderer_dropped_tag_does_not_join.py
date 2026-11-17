"""Text around a tag the sanitizer removes stays text.

renderMd() runs every raw tag through its attribute sanitizer in one global
replace. The sanitizer returns "" for a tag it removes (an unknown name, a
closing tag it does not keep, an image whose src it does not accept), and a
replace never scans its own output again. A "<" written just before such a tag
used to join the text after it into a new tag that was never checked:
"<<x>img src=x title=t>" rendered a raw <img> with its title. The same pass
now turns every "<" it does not hand to the sanitizer into "&lt;", so the text
around a removed tag stays text in paragraphs, list items, headings, table
cells and blockquotes, and a URL written just after a removed tag is linked
with its exact value, never made into a tag.

Each case parses the output and checks that every element and attribute is one
the sanitizer itself writes, that no element was formed from the text around
the removed tag, and that the text (with its "<") is still shown.

Drives the real renderMd() through the harness in test_renderer_js_behaviour.py.
"""

from html.parser import HTMLParser

import pytest

from tests.test_renderer_js_behaviour import NODE, _render, driver_path  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Elements and attributes the sanitizer (_tag) writes. Anything else in the
# output was never checked by it.
_PLAIN = {
    "strong", "em", "del", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol",
    "table", "thead", "tbody", "tr", "th", "td", "blockquote", "p", "br", "hr",
}
_ATTRS = {
    "a": {"href", "target", "rel", "class", "download", "title"},
    "img": {"src", "alt", "class", "loading"},
    "li": {"value"},
    "span": {"class", "data-katex"},
    "div": {"class", "data-mermaid-id", "data-katex"},
    "code": {"class"},
}


class _Doc(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = ""

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, [name for name, _value in attrs], dict(attrs)))

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        self.text += data


def _parse(html):
    doc = _Doc()
    doc.feed(html)
    doc.close()
    return doc


# (markdown, text written after the removed tag(s), which must stay visible text)
_CASES = {
    "image": ("<<x>img src=x title=t>", "img src=x title=t>"),
    "closing-tag-removed": ("<</img>img src=x title=t>", "img src=x title=t>"),
    "bold": ("<<x>b>joined</b>", "b>joined"),
    "italic-after-unknown-closing-tag": ("<</x>i>t", "i>t"),
    "anchor": ('<<x>a href="https://e.com">c</a>', 'a href="https://e.com">c'),
    "nested": ("<<<x>y>b>z", "y>b>z"),
    "uppercase": ("<<X>B>t", "B>t"),
    "removed-image-between": ("<<img src=x>img src=x title=t>", "img src=x title=t>"),
    "tag-name-split-by-removed-tags": ("<</x>sp</x>an title=t>x", "an title=t>x"),
    "url": ("<<x>https://e.com/a/title=t//>hover me", "https://e.com/a/title=t//>hover me"),
    "url-after-closing-tag": ("<</b>https://e.com/a/title=t//>hover me", "https://e.com/a/title=t//>hover me"),
}

_CONTEXTS = {
    "paragraph": lambda md: md,
    "list-item": lambda md: f"- {md}",
    "ordered-item": lambda md: f"1. {md}",
    "heading": lambda md: f"## {md}",
    "table-cell": lambda md: f"| h |\n|---|\n| {md} |",
    "blockquote": lambda md: f"> {md}",
    "blockquote-list-item": lambda md: f"> - {md}",
}

_PARAMS = [
    pytest.param(wrap(md), tail, id=f"{case}-{ctx}")
    for case, (md, tail) in _CASES.items()
    for ctx, wrap in _CONTEXTS.items()
]


@pytest.mark.parametrize("markdown,tail", _PARAMS)
def test_text_around_a_removed_tag_stays_text(driver_path, markdown, tail):  # noqa: F811
    html = _render(driver_path, markdown)
    doc = _parse(html)
    for tag, names, values in doc.tags:
        assert tag in _PLAIN or tag in _ATTRS, (tag, html)
        assert set(names) <= _ATTRS.get(tag, set()), (tag, names, html)
        # No element is formed from the text around the removed tag: no image,
        # no span, and the only links are the ones the bare-URL pass builds.
        assert tag not in ("img", "span"), html
        if tag == "a":
            assert names == ["href", "target", "rel"], html
            assert values["href"] in ("https://e.com", "https://e.com/a/title=t//"), html
    assert tail in doc.text, (doc.text, html)
    assert "<" in doc.text, (doc.text, html)


# The joined URL form: no element whose name starts with the URL's scheme, and
# the URL is linked with its exact value, after a "<" kept as text.
@pytest.mark.parametrize("wrap", list(_CONTEXTS.values()), ids=list(_CONTEXTS))
def test_url_after_a_removed_tag_is_linked_not_made_a_tag(driver_path, wrap):  # noqa: F811
    html = _render(driver_path, wrap("<</b>https://e.com/a/title=t//>hover me"))
    doc = _parse(html)
    assert not any(tag.startswith("https") for tag, _names, _values in doc.tags), html
    links = [(values.get("href"), names) for tag, names, values in doc.tags if tag == "a"]
    assert links == [("https://e.com/a/title=t//", ["href", "target", "rel"])], html
    # A paragraph removes the unknown </b>; list items, headings and cells show
    # it as text. Either way the "<" before it is still shown.
    assert "<" in doc.text.split("https://")[0], (doc.text, html)
    assert "https://e.com/a/title=t//>hover me" in doc.text, (doc.text, html)


# Exact output for the plain forms.
_EXACT = {
    "image": ("<<x>img src=x title=t>", "<p>&lt;img src=x title=t></p>"),
    "bold": ("<<x>b>joined</b>", "<p>&lt;b>joined</p>"),
    "uppercase": ("<<X>B>t", "<p>&lt;B>t</p>"),
    "nested": ("<<<x>y>b>z", "<p>&lt;&lt;y>b>z</p>"),
    "tag-name-split-by-removed-tags": ("<</x>sp</x>an title=t>x", "<p>&lt;span title=t>x</p>"),
    "list-item-closing-tag-removed": ("- <</img>img src=x title=t>", "<ul><li>&lt;img src=x title=t></li></ul>"),
    "heading-image-removed": ("# <<img src=x>img src=x title=t>", "<h1>&lt;img src=x title=t></h1>"),
}


@pytest.mark.parametrize("markdown,expected", list(_EXACT.values()), ids=list(_EXACT))
def test_text_around_a_removed_tag_exact_output(driver_path, markdown, expected):  # noqa: F811
    assert _render(driver_path, markdown) == expected


# An HTML comment written in the message is kept as before.
def test_html_comment_is_kept(driver_path):  # noqa: F811
    assert _render(driver_path, "a <!-- note --> b") == "<p>a <!-- note --> b</p>"


# A "<" written straight after a bare URL still ends the URL, as the raw "<"
# did before the sanitizer pass wrote it as "&lt;": the link keeps its exact
# value and the "<" is shown after it.
_BOUNDARY_CONTEXTS = {
    "paragraph": lambda md: md,
    "blockquote": lambda md: f"> {md}",
    "nested-blockquote": lambda md: f"> > {md}",
    "list-item": lambda md: f"- {md}",
    "heading": lambda md: f"## {md}",
    "table-cell": lambda md: f"| h |\n|---|\n| {md} |",
}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs = []
        self.text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.append(dict(attrs).get("href"))

    def handle_data(self, data):
        self.text += data


@pytest.mark.parametrize("suffix", ["<", "<3"])
@pytest.mark.parametrize("wrap", list(_BOUNDARY_CONTEXTS.values()), ids=list(_BOUNDARY_CONTEXTS))
def test_angle_bracket_after_a_bare_url_ends_the_link(driver_path, wrap, suffix):  # noqa: F811
    html = _render(driver_path, wrap("see https://example.com/" + suffix))
    doc = _Links()
    doc.feed(html)
    doc.close()
    assert doc.hrefs == ["https://example.com/"], html
    assert doc.text.strip() == ("h" if "|" in wrap("") else "") + "see https://example.com/" + suffix, html
    assert "\x00" not in html, html


# Every stash token is put back: a "<" inside a protected block is shown as
# written, and text between two stash tokens is never read as a token.
_RESTORED = {
    "pre": ("<pre>x < 3</pre>", "x < 3"),
    "anchor": ('<a href="api/x">x <3</a>', "x <3"),
    "letter-between-math": ("$a$V$b$", "aVb"),
    "token-shaped-text-after-angle": ("x <B0<", "x <B0<"),
    "token-shaped-text-before-math": ("x <B0$y$", "x <B0y"),
    "token-shaped-text-after-math": ("$a$B5<3", "aB5<3"),
}


@pytest.mark.parametrize("markdown,text", list(_RESTORED.values()), ids=list(_RESTORED))
def test_every_stashed_angle_bracket_is_restored(driver_path, markdown, text):  # noqa: F811
    html = _render(driver_path, markdown)
    assert "\x00" not in html, html
    assert "undefined" not in html, html
    doc = _Links()
    doc.feed(html)
    doc.close()
    assert doc.text == text, html
