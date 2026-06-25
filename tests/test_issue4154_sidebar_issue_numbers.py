"""Regression tests for #4154: sidebar strips GitHub issue numbers from titles.

The _sessionTitleTags regex must NOT treat purely numeric #NNNN patterns as
tags.  Only known attention/session-control tags like #approval, #clarify,
#attention should be extracted.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = ROOT / "static" / "sessions.js"


def _read_sessions_js():
    return SESSIONS_JS.read_text(encoding="utf-8")


def test_sessionTitleTags_function_exists():
    """The _sessionTitleTags function must be present in sessions.js."""
    src = _read_sessions_js()
    assert "function _sessionTitleTags" in src


def test_regex_excludes_numeric_issue_numbers():
    """The regex in _sessionTitleTags must use a negative lookahead to skip
    purely numeric #NNNN patterns (GitHub issue references)."""
    src = _read_sessions_js()
    # The fix replaces /#[\w-]+/g with /#(?!\d+\b)[\w-]+/g
    assert r"#(?!\d+\b)[\w-]+" in src, (
        "Expected negative lookahead regex /#(??!\\d+\\b)[\\w-]+/ in _sessionTitleTags"
    )
    # The old broad pattern must NOT be the only pattern present
    # (the negative lookahead variant must exist)


def test_old_broad_regex_not_still_present():
    r"""The old /#[\w-]+/g pattern (without negative lookahead) must not appear
    in _sessionTitleTags or _renderOneSession tag-stripping code."""
    src = _read_sessions_js()
    # Find _sessionTitleTags function body
    match = re.search(
        r"function _sessionTitleTags\(rawTitle\)\{(.*?)\n\}",
        src,
        re.DOTALL,
    )
    assert match, "Could not locate _sessionTitleTags function body"
    body = match.group(1)
    # The function body must not contain the old broad regex
    assert "/#[\\w-]+/g" not in body.replace(" ", ""), (
        "_sessionTitleTags still uses the old broad /#[\\w-]+/g regex"
    )


def test_regex_applied_in_renderOneSession_too():
    """The tag-stripping regex in _renderOneSession must also use the
    negative lookahead so visible titles keep issue numbers."""
    src = _read_sessions_js()
    # Find the cleanTitle line
    assert r"#(?!\d+\b)[\w-]+" in src
    # Confirm it appears at least twice (once in _sessionTitleTags, once in
    # _renderOneSession)
    count = src.count(r"#(?!\d+\b)[\w-]+")
    assert count >= 2, (
        f"Expected negative lookahead regex in at least 2 locations "
        f"(_sessionTitleTags + _renderOneSession), found {count}"
    )
