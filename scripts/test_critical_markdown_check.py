#!/usr/bin/env python3
"""Tests for scripts/critical_markdown_check.py — the docs-CI critical-markdown gate.

The checker must flag ONLY catastrophic rendering breaks (a newline that splits an
inline-link destination, or an unclosed inline link) and nothing that renders fine.
Each case is asserted against the intended CommonMark behavior. Where markdown-it-py
is installed the test additionally cross-checks the checker's verdict against the
reference parser; when it isn't, the hand-labeled expectations still run.

Run: pytest scripts/test_critical_markdown_check.py -v
"""
import importlib.util
import tempfile
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "critical_markdown_check",
    str(Path(__file__).with_name("critical_markdown_check.py")),
)
cmc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmc)

try:
    from markdown_it import MarkdownIt
    _MD = MarkdownIt("commonmark")
except Exception:  # optional; hand labels still assert
    _MD = None


def _flags(src: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
        tf.write(src)
        p = Path(tf.name)
    try:
        return len(cmc.check_file(p)) > 0
    finally:
        p.unlink()


def _renders(src: str) -> bool:
    html = _MD.render(src)
    return "<a href" in html or "<img" in html


# (name, source, should_flag). should_flag=True means the checker MUST report it broken.
CASES = [
    # legal constructs — must NOT flag (render fine)
    ("newline-before-dest",    "[l](\nhttps://x.com)",            False),
    ("newline-after-dest",     "[l](https://x.com\n)",            False),
    ("newline-in-dq-title",    '[l](https://x.com "a\nb")',       False),
    ("title-separator-nl",     '[x](https://a\n"t")',             False),
    ("single-quote-title-ml",  "[x](url\n'a\nb')",                False),
    ("paren-title-multiline",  "[x](url (multi\nline))",          False),
    ("balanced-parens-dest",   "[x](https://x.com/foo(bar)baz)",  False),
    ("angle-dest",             "[x](<https://x.com>)",            False),
    ("angle-escaped-gt",       "[x](<foo\\>bar>)",                False),
    ("dq-title-inline",        '[x](url "title")',                False),
    ("paren-title-closed",     "[x](foo (title))",                False),
    ("dq-title-then-close",    '[x](foo "title")',                False),
    ("empty-dest",             "[x]()",                           False),
    ("plain-good",             "[l](https://x.com)",              False),
    ("two-links-line",         "[a](x) and [b](y)\n",             False),
    # catastrophic breaks — MUST flag (do not render as links)
    ("newline-inside-dest",    "[l](https://ex\nam.com)",         True),
    ("img-newline-inside",     "![a](img\nf.png)",                True),
    ("bare-text-after-nl",     "[x](url\nmore)",                  True),
    ("quote-glued-in-url",     '[x](exa"part\nmple.com)',         True),
    ("bare-unbalanced-paren",  "[x](foo(\n))",                    True),
    ("unclosed-at-eof",        "[x](https://a",                   True),
    ("unclosed-newline-term",  "[x](https://a\n",                 True),
    ("angle-dest-split",       "[x](<https://x\n.com>)",          True),
    ("angle-escaped-newline",  "[x](<foo\\\n>)",                  True),
    ("paren-title-unclosed",   "[x](foo (title)",                True),
    ("angle-title-unclosed",   "[x](<foo> (title)",              True),
    ("dq-title-unclosed",      '[x](foo "title"',                True),
    ("nested-paren-title",     "[x](foo (a (b) c))",             True),
]

# Bad markdown INSIDE code must never be flagged (it's an intentional example).
CODE_SAFE = [
    ("fenced-example",   "```\n[x](\ny)\n```\n"),
    ("indented-example", "text\n\n    [x](\n    y)\n"),
    ("inline-code",      "Use `[x](` in prose\n"),
]

# Cases where the FORMAL CommonMark/GFM grammar (what GitHub's cmark-gfm renders)
# disagrees with markdown-it-py, which is permissive. We follow the formal spec /
# GitHub behavior (these docs are GitHub-rendered), so these are verdict-asserted but
# excluded from the markdown-it-py cross-check.
SPEC_DIVERGENT = [
    # An escaped newline inside a bare destination is a raw line ending — GFM forbids
    # it (GitHub won't render the link) though markdown-it-py permissively renders it.
    ("bare-escaped-newline", "[x](foo\\\nbar)", True),
]


@pytest.mark.parametrize("name,src,should_flag", CASES, ids=[c[0] for c in CASES])
def test_checker_verdict(name, src, should_flag):
    assert _flags(src) is should_flag


@pytest.mark.parametrize("name,src,should_flag", SPEC_DIVERGENT,
                         ids=[c[0] for c in SPEC_DIVERGENT])
def test_spec_divergent_verdict(name, src, should_flag):
    # Follow the formal GFM grammar / GitHub rendering, not markdown-it-py's permissive
    # behavior — so these are asserted by hand, not cross-checked against the parser.
    assert _flags(src) is should_flag


@pytest.mark.parametrize("name,src,should_flag", CASES, ids=[c[0] for c in CASES])
def test_agrees_with_commonmark(name, src, should_flag):
    if _MD is None:
        pytest.skip("markdown-it-py not installed")
    # The checker should flag exactly the cases that do NOT render as a link/image.
    assert _flags(src) is (not _renders(src))


@pytest.mark.parametrize("name,src", CODE_SAFE, ids=[c[0] for c in CODE_SAFE])
def test_code_spans_never_flagged(name, src):
    assert _flags(src) is False


def test_empty_and_missing_inputs():
    # No files -> ok (exit 0 path).
    assert cmc.main(["prog"]) == 0
    # A file with no links -> no problems.
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as tf:
        tf.write("# Title\n\nJust prose, no links.\n")
        p = Path(tf.name)
    try:
        assert cmc.check_file(p) == []
    finally:
        p.unlink()
