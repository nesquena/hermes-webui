"""Hermes cron-output artifact parser.

Issue #7303: completed agent cron runs persist a markdown artifact with
front-matter followed by a ``## Response`` heading that marks the start
of the agent's reply. The collapsed preview extracted text after the
heading, but the expanded view discarded the projection and showed the
raw file, so the user scrolled past hundreds of lines of prompt and
skill context to reach the result.

This module is the **single shared parser** the maintainer asked for in
the #7303 comment: it returns an explicit ``{response, context, raw,
has_response_boundary, ...}`` shape so the collapsed and expanded
views can render the same projection without re-parsing independently.

## Fail-closed boundaries

The parser is conservative on purpose. A heading-shaped line is only
treated as a response boundary when ALL of the following are true:

1. The line begins with ``## Response`` or ``# Response`` (exact prefix,
   not e.g. ``## Response time``).
2. The line is OUTSIDE a fenced code block (``\`\`\`) — a heading-shaped
   string inside script output or a quoted snippet must not be
   interpreted as a section boundary.
3. The line is OUTSIDE an HTML-style ``<pre>`` / ``<code>`` block.
   Successive open/close tokens on one line are processed in token
   order, so adjacent tags (``<pre>one</pre><code>``) keep the depth
   accounting honest.

4. The whole already-read artifact is scanned. The artifact is fully
   in memory by the time the parser runs, so there is no reason to
   stop at an arbitrary line count: real runs have carried the reply
   past line 2,000, and the legacy preview (which scanned everything)
   showed it. The guards that actually keep the boundary fail-closed
   are the fence / ``<pre>`` tracking and the exact heading match, not
   a probe range.

The fail-closed guarantee that a quoted ``## Response`` inside an
agent transcript is *not* taken as the boundary comes from the fence
and ``<pre>`` tracking plus the exact heading match, not from any
line-range limit.

If no boundary is found, ``has_response_boundary`` is False and
``response`` is the empty string. The caller is expected to render the
raw artifact in that case (the current behaviour for script/no-agent
runs, malformed files, and missing markers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# A boundary is a markdown ATX heading of the right level with the
# canonical title. We match both ``## Response`` and ``# Response`` to
# tolerate minor inconsistency between agent runs.
_RESPONSE_HEADING_RE = re.compile(r"^#{1,2}\s+Response\s*$")

# A fenced code block starts with ``` or ~~~ (optionally with a language
# tag) and ends with the same fence on its own line. We track fence
# character AND opening delimiter length so a ```` ```` ``` ```` ```` line
# cannot close a four-backtick block early (skill dumps nest fences).
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")

# One HTML block-level tag token. Used to walk the tags on a line in
# order so adjacent tags (``<pre>one</pre><code>``) are processed in
# token order rather than by counting every close tag first.
_HTML_TAG_RE = re.compile(r"</?(pre|code)\b", re.IGNORECASE)

# Text that may precede an *open* tag and still make it a real tag:
# indentation only, or the tail of another tag (so ``</pre><code>``
# counts as a real open for the ``<code>``). Anything else in front of
# the tag (letters, punctuation) means the line merely mentions it.
_HTML_LEADING_RE = re.compile(r"^(?:\s*|.*>\s*)$")


def _is_anchored_html_tag(prefix: str) -> bool:
    """True when *prefix* (the text before an open tag) lets that tag
    count as a real open: indentation, or the end of another tag.
    """
    return bool(_HTML_LEADING_RE.match(prefix))


@dataclass
class CronOutputProjection:
    """Result of parsing one cron output artifact.

    Attributes
    ----------
    response
        The agent reply text. Empty when ``has_response_boundary`` is
        False.
    context
        The text BEFORE the response boundary (front-matter, system
        context, prompt, skill text, intermediate tool output, …).
        Useful as a collapsible diagnostics disclosure.
    raw
        The verbatim artifact body, exactly as it appears on disk. The
        response-first view preserves this for the "View raw" affordance.
    has_response_boundary
        True iff the parser located a heading boundary that satisfies
        all four fail-closed checks.
    response_line
        1-indexed line number where the response heading was found.
        0 when ``has_response_boundary`` is False.
    """

    response: str
    context: str
    raw: str
    has_response_boundary: bool
    response_line: int = 0

    def to_dict(self) -> dict:
        return {
            "response": self.response,
            "context": self.context,
            "raw": self.raw,
            "has_response_boundary": self.has_response_boundary,
            "response_line": self.response_line,
        }


def parse_cron_output(text: str) -> CronOutputProjection:
    """Parse a cron output artifact into a response-first projection.

    See module docstring for the fail-closed boundary rules.
    """
    if not text:
        return CronOutputProjection(
            response="",
            context="",
            raw="",
            has_response_boundary=False,
        )

    raw = text
    lines = text.split("\n")
    in_fence = False
    in_html_pre = False
    # #7303 re-gate 9/21: track <pre> and <code> depths separately
    # so a ``</code>`` inside a still-open ``<pre>`` does not clear
    # the HTML-block guard and accept a heading inside the quoted
    # HTML as the response boundary.
    _pre_depth = 0
    _code_depth = 0
    response_idx: int | None = None
    fence_char: str | None = None
    fence_len = 0

    # #7303 re-gate 9/24 (finding 4): the whole already-read artifact is
    # scanned. A run whose reply landed past line 2,000 (a long tool dump
    # before the reply) lost its boundary under the previous cap, so the
    # collapsed preview regressed from the reply to the first 600
    # characters of front-matter. ``lines`` is already fully materialised
    # in memory at this point, so the scan costs no extra I/O and stays
    # bounded by the artifact itself; the guards that keep the boundary
    # fail-closed are the fence / <pre> tracking and the exact heading
    # match below, not a line-range limit.

    for i, line in enumerate(lines):
        # Track fenced code blocks. Toggle on opening AND closing fences
        # of the same character so ``` doesn't re-open. The closing
        # fence must be at least as long as the opening one, otherwise
        # a ``` line inside a ```` block would close it early and a
        # subsequent ``## Response`` inside the still-open block could
        # be mistaken for the real boundary.
        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(1)[0]
            length = len(m.group(1))
            # A closing fence carries no info string; an indented or
            # tagged run of fence characters is a nested opening fence
            # and must not close the current block.
            rest = line[m.end():].strip()
            if not in_fence:
                in_fence = True
                fence_char = fence
                fence_len = length
            elif fence_char == fence and length >= fence_len and not rest:
                in_fence = False
                fence_char = None
                fence_len = 0
            continue
        # Track HTML <pre>/<code> blocks (some skill output uses them
        # for shell snippets and the parser must respect the boundary).
        # #7303 re-gate 9/21 (correctness gap): the previous single
        # ``in_html_pre`` boolean conflates two independently nestable
        # elements — for a ``<pre><code>...</code></pre>`` shape, a
        # ``</code>`` inside the still-open ``<pre>`` would clear the
        # flag and a later ``## Response`` heading (inside the quoted
        # HTML) would be accepted as the boundary.
        #
        # Two-tier detection to keep well-formed artifacts tracking
        # correctly without confusing plain-text mentions like
        # ``the open <pre> tag`` for an actual tag:
        # 1. **Entry** — a line that *starts* (after optional indent)
        #    with ``<pre`` or ``<code`` opens the HTML block, and that
        #    specific opening token is counted as the open. This is
        #    the only place open tokens are recognised.
        # 2. **Inside** — while the HTML block is open, the tags on the
        #    line are walked **in token order**. A close token always
        #    applies; an open token only counts when it is anchored —
        #    it starts the line or directly follows another tag — so
        #    prose that merely mentions ``<pre>`` cannot re-open the
        #    block, while a genuinely adjacent tag (``</pre><code>``)
        #    still does. Once both depths hit zero, the block closes.
        _opening_match = re.match(r"^\s*<(pre|code)\b", line, re.IGNORECASE)
        # Where the ordered token walk starts on this line. On the entry
        # line it begins *after* the token that already opened the block,
        # so that token is not counted twice.
        _tag_scan_start = 0
        if _opening_match and not in_html_pre:
            # Open the HTML block on this single starting token.
            in_html_pre = True
            if _opening_match.group(1).lower() == "pre":
                _pre_depth = 1
                _code_depth = 0
            else:
                _pre_depth = 0
                _code_depth = 1
            _tag_scan_start = _opening_match.end()
        if in_html_pre:
            # Walk the tokens left-to-right so a close that precedes an
            # open on the same line cannot be pre-counted against it.
            for _tag in _HTML_TAG_RE.finditer(line):
                if _tag.start() < _tag_scan_start:
                    continue  # the entry token is already counted
                _name = _tag.group(1).lower()
                _is_close = _tag.group(0).startswith("</")
                if not _is_close and not _is_anchored_html_tag(line[: _tag.start()]):
                    continue  # a prose mention, not a real open
                if _name == "pre":
                    if _is_close:
                        _pre_depth = max(0, _pre_depth - 1)
                    else:
                        _pre_depth += 1
                else:
                    if _is_close:
                        _code_depth = max(0, _code_depth - 1)
                    else:
                        _code_depth += 1
            in_html_pre = _pre_depth > 0 or _code_depth > 0
            if in_fence or in_html_pre:
                continue
        if in_fence or in_html_pre:
            continue
        if _RESPONSE_HEADING_RE.match(line):
            response_idx = i
            break

    if response_idx is None:
        return CronOutputProjection(
            response="",
            context=raw,
            raw=raw,
            has_response_boundary=False,
        )

    response_body = "\n".join(lines[response_idx + 1:]).strip()
    context_body = "\n".join(lines[:response_idx]).strip()
    return CronOutputProjection(
        response=response_body,
        context=context_body,
        raw=raw,
        has_response_boundary=True,
        response_line=response_idx + 1,  # 1-indexed for the UI
    )


def response_snippet(text: str, limit: int = 600) -> str:
    """Backwards-compatible snippet helper used by the existing route
    layer. Equivalent to the old ``_cron_output_snippet`` contract:
    returns the response body (or the full text when no boundary is
    found), truncated to *limit* characters.
    """
    projection = parse_cron_output(text)
    if projection.has_response_boundary:
        body = projection.response
    else:
        # Legacy contract: when no boundary, return the whole text.
        # Front-matter may appear, but the snippet is still bounded by
        # *limit* so the previews are consistent.
        body = projection.context or text
    # Trim before slicing: leading whitespace must not consume the
    # character budget. A run that starts with indented front-matter
    # (600+ leading spaces) otherwise previews as a blank string and
    # the row looks like it has no output at all.
    return (body.strip()[:limit]) or "(empty)"
