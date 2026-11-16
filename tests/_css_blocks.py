"""Find whole CSS blocks in static/style.css for source-level layout guards.

Guards that look for a rule inside an at-rule block (for example the mobile
``@media(max-width:640px){...}`` block) used to search a fixed number of
characters after the block start. Adding unrelated rules earlier in the block
pushed the rule past the cutoff and the guard failed although the CSS was
unchanged. ``css_block`` returns the whole block instead.
"""
from __future__ import annotations


def css_block(css: str, open_brace: int) -> str:
    """Return ``css[open_brace:]`` through the ``}`` that closes it.

    Braces inside comments, quoted strings (with backslash escapes), unquoted
    ``url(...)`` tokens and backslash escapes outside strings do not count, as
    in CSS tokenization. Malformed input fails loudly with AssertionError.
    """
    assert css[open_brace:open_brace + 1] == "{", "css_block needs the index of a '{'"
    n = len(css)
    depth = 0
    i = open_brace
    while i < n:
        if css.startswith("/*", i):
            end = css.find("*/", i + 2)
            assert end != -1, "unterminated comment in CSS"
            i = end + 2
            continue
        c = css[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and css[j] != c:
                assert css[j] != "\n", "unterminated string in CSS"
                j += 2 if css[j] == "\\" else 1
            assert j < n, "unterminated string in CSS"
            i = j + 1
            continue
        if css[i:i + 4].lower() == "url(" and not (i and (css[i - 1].isalnum() or css[i - 1] in "-_")):
            k = i + 4
            while k < n and css[k] in " \t\r\n\f":
                k += 1
            if k < n and css[k] not in "'\"":
                while k < n and css[k] != ")":
                    k += 2 if css[k] == "\\" else 1
                assert k < n, "unterminated url() in CSS"
                i = k + 1
                continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return css[open_brace:i + 1]
        i += 1
    raise AssertionError("unterminated block in CSS")
