"""#7303 re-gate 9/21 — two correctness/accessibility gaps closed.

The 9/21 review on commit ``0bedadda`` flagged two specific items
that this PR ships together:

**Gap 1 (parser, correctness):** the HTML-block guard tracked a
single ``in_html_pre`` boolean for both ``<pre>`` and ``<code>``,
two independently nestable elements. For a ``<pre><code>...</code></pre>``
shape, a ``</code>`` inside the still-open ``<pre>`` cleared the
flag and a later ``## Response`` heading (inside the quoted HTML)
was accepted as the boundary. The parser must respect both elements
independently so the fail-closed claim holds.

**Gap 2 (CSS, accessibility):** the new context disclosure removed
its only focus indicator (``outline:none`` on the ``<summary>``).
The native ``<details>`` remains keyboard-operable but the focus
position is invisible to keyboard users. Re-add a focus-visible
ring using the same ``2px solid var(--focus-ring)`` pattern the
rest of the stylesheet already uses for custom controls.

The CHANGELOG.md direct edit is also reverted in this PR (see
``CONTRIBUTING.md:116,143`` — contributors must not edit the
changelog directly).
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path



REPO = Path(__file__).resolve().parents[1]
PARSER_PY = REPO / "api" / "cron_output_parser.py"
STYLE_CSS = (REPO / "static" / "style.css").read_text(encoding="utf-8")

from api.cron_output_parser import parse_cron_output


# ---------------------------------------------------------------------------
# Gap 1 — nested <pre>/<code> does not split a quoted-HTML response
# ---------------------------------------------------------------------------


def test_pre_code_nested_does_not_split_fake_response_heading():
    """The motivating case: a ``<pre><code>`` shell snippet that
    contains ``</code>`` mid-block, a fake exact ``## Response``
    heading inside the still-open ``<pre>``, and ``</pre>`` before
    the real heading. The parser must not accept the fake heading.
    """
    text = textwrap.dedent(
        """\
        ---
        run_id: nested-html
        ---

        ## Prompt

        Show the raw HTML of an error:

        <pre><code class="language-html">
        <html>
          <body>
            <h1>Some old doc that quotes our own messages</h1>
            <p>The trace starts with this exact text.</p>
            ## Response
            <p>(this is NOT the cron response — it is HTML the skill is quoting)</p>
          </body>
        </html>
        </code></pre>

        Some other context after the HTML block.

        ## Response

        The real cron response: 12 nodes healthy, p99 = 142 ms.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True, (
        "parser must still find the real ## Response outside the <pre> block"
    )
    # The real response must NOT include the fake heading inside the
    # quoted HTML; that is the whole point of the fail-closed claim.
    assert "12 nodes healthy" in projection.response
    assert "this is NOT the cron response" not in projection.response, (
        "the fake ## Response inside the still-open <pre> must be ignored; "
        "the parser accepted it as the boundary, which means the depth "
        "tracking regressed"
    )


def test_pre_then_code_does_not_clear_pre_depth():
    """A ``<pre>`` followed by a separate ``<code>`` must keep the
    pre-block guard open across the code block. The pre-block boolean
    must not be cleared by the lone ``</code>`` close.
    """
    text = textwrap.dedent(
        """\
        <pre>
        <code>
        inside
        </code>
        ## Response
        (this heading is still inside the open <pre> and must be ignored)
        </pre>

        ## Response

        The real response.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert "The real response" in projection.response
    assert "this heading is still inside" not in projection.response, (
        "the </code> close must not clear the <pre> depth — the parser "
        "treated </code> as closing both, which is the regression"
    )


def test_mixed_pre_open_close_on_one_line_keeps_element_open():
    """A single line like ``<pre><code>foo</code></pre>`` that opens
    and closes both elements on one line must leave the depth
    accounting correct for the rest of the document. The headline
    case is: a line later in the document that contains a real
    ``## Response`` must be accepted as the boundary, and no line
    inside the line-spanning block is mistaken for the response.
    """
    text = textwrap.dedent(
        """\
        <pre><code>not a response yet</code></pre>

        ## Response

        The real response after a same-line open/close.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert "not a response yet" in projection.context
    assert "The real response" in projection.response


def test_pre_code_depth_tracking_is_separate_in_parser_source():
    """Pin the source-level contract: the parser must track
    ``<pre>`` and ``<code>`` depths independently and avoid
    double-counting plain-text mentions of those tags as if they
    were real opens. We assert the source shape rather than the
    runtime behaviour alone so a future "simplify" cannot regress
    to a single boolean without breaking the test.
    """
    src = PARSER_PY.read_text(encoding="utf-8")
    # Depth counters must be initialised at the top of the parse
    # function, not derived from a single boolean.
    assert "_pre_depth" in src, (
        "parser must track a separate <pre> depth counter (round 9/21 "
        "correctness gap)"
    )
    assert "_code_depth" in src, (
        "parser must track a separate <code> depth counter (round 9/21 "
        "correctness gap)"
    )
    # Entry is gated on a line-start ``<pre`` or ``<code`` regex so a
    # bare mention mid-line ("the open <pre> tag") cannot open the
    # HTML block by accident. Look for an anchored match that combines
    # ``^\\s*<`` with the open tag.
    assert re.search(r"re\.match\([^,]+,\s*line,\s*re\.IGNORECASE\)", src), (
        "parser must use re.match (anchored) against the current line "
        "to detect the HTML block entry, not an unanchored search"
    )
    # Close tokens are detected via re.findall on the line while the
    # block is open.
    assert '</pre>' in src and 're.findall' in src, (
        "parser must scan the line for </pre> close tokens via "
        "re.findall while the block is open"
    )
    assert '</code>' in src, (
        "parser must scan the line for </code> close tokens via "
        "re.findall while the block is open"
    )


# ---------------------------------------------------------------------------
# Gap 2 — context disclosure must show a focus indicator
# ---------------------------------------------------------------------------


def test_context_disclosure_has_focus_visible_ring():
    """The summary inside ``.cron-run-context-disclosure`` must show
    a visible focus ring on keyboard focus. The native ``<details>``
    is still keyboard-operable without it, but a focus indicator is
    a baseline accessibility requirement.
    """
    pattern = re.compile(
        r"\.cron-run-context-disclosure>summary:focus-visible\s*\{[^}]*"
        r"outline:\s*[^;}]+",
        re.DOTALL,
    )
    assert pattern.search(STYLE_CSS), (
        "the context disclosure summary must have a :focus-visible rule "
        "with a visible outline (round 9/21 accessibility gap)"
    )
    # The pattern should be the same as the rest of the stylesheet
    # uses: 2px solid var(--focus-ring) with outline-offset 2px.
    focus_rule = re.search(
        r"\.cron-run-context-disclosure>summary:focus-visible\s*\{([^}]+)\}",
        STYLE_CSS,
    )
    assert focus_rule is not None
    body = focus_rule.group(1)
    assert "var(--focus-ring)" in body, (
        "the focus-visible ring should reuse var(--focus-ring) for "
        "consistency with the rest of the stylesheet"
    )
    assert "outline-offset" in body, (
        "the focus-visible ring should set outline-offset so the ring "
        "sits outside the summary, matching the rest of the stylesheet"
    )
