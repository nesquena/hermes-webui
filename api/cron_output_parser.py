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
4. The line is within the first ``MAX_PROBE_LINES`` lines of the
   file (front-matter + system context). Real artifacts carry several
   hundred context lines before the reply, so the cap is generous;
   it exists only to stop an unbounded scan of pathological input.

The fail-closed guarantee that a quoted ``## Response`` inside an
agent transcript is *not* taken as the boundary comes from the fence
and ``<pre>`` tracking plus the exact heading match, not from the
probe cap.

If no boundary is found, ``has_response_boundary`` is False and
``response`` is the empty string. The caller is expected to render the
raw artifact in that case (the current behaviour for script/no-agent
runs, malformed files, and missing markers).
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Cap how far into the file we look for a response boundary. Real cron
# artifacts routinely carry several hundred lines of front-matter,
# system context, skill dumps and tool output before the agent replies
# — the motivating #7303 artifact has ~320 context lines. The probe
# range must therefore cover them; the guards that actually keep the
# boundary fail-closed are the fence / <pre> tracking and the exact
# heading match below.
_MAX_PROBE_LINES = 2000

# A boundary is a markdown ATX heading of the right level with the
# canonical title. We match both ``## Response`` and ``# Response`` to
# tolerate minor inconsistency between agent runs.
_RESPONSE_HEADING_RE = re.compile(r"^#{1,2}\s+Response\s*$")

# A fenced code block starts with ``` or ~~~ (optionally with a language
# tag) and ends with the same fence on its own line. We track fence
# character AND opening delimiter length so a ```` ```` ``` ```` ```` line
# cannot close a four-backtick block early (skill dumps nest fences).
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")


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
    response_idx: int | None = None
    fence_char: str | None = None
    fence_len = 0

    for i, line in enumerate(lines):
        if i >= _MAX_PROBE_LINES:
            break
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
        if re.search(r"<pre\b|<code\b", line, re.IGNORECASE):
            in_html_pre = True
        if in_html_pre and re.search(r"</pre>|</code>", line, re.IGNORECASE):
            in_html_pre = False
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
    return body[:limit] or "(empty)"
