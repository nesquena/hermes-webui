"""Pin the CSS block scanner used by the #4856 layout guards (tests/_css_blocks.py)."""
from __future__ import annotations

import pytest

from tests._css_blocks import css_block


def _block(css: str) -> str:
    return css_block(css, css.index("{"))


def test_nested_blocks_end_at_the_matching_brace():
    assert _block("@media(x){a{b:c}@supports(d){e{f:g}}}h{i:j}") == "{a{b:c}@supports(d){e{f:g}}}"


@pytest.mark.parametrize("inner", [
    "/* } { */",
    "a{content:'}'}",
    'a{content:"\\"}"}',
    "a{content:'\\'{'}",
    "a{background:url(x}y.png)}",
    "a{background:url( x{y.png )}",
    "a{background:url('x}y.png')}",
    "\\}a{b:c}",
])
def test_braces_in_comments_strings_urls_and_escapes_do_not_count(inner):
    css = "@media(x){" + inner + "}tail{}"
    assert _block(css) == "{" + inner + "}"


def test_a_property_ending_in_url_is_not_a_url_token():
    assert _block("@media(x){a{--my-url(:0}}b{}") == "{a{--my-url(:0}}"


@pytest.mark.parametrize("bad", ["@media(x){a{b:c}", "@media(x){/* }", "@media(x){a{content:'}}"])
def test_malformed_input_fails_loudly(bad):
    with pytest.raises(AssertionError):
        _block(bad)
