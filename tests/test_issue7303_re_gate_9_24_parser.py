"""#7303 re-gate 9/24 — three parser regressions closed.

The 9/24 maintainer review on ``e4050150`` flagged three defects in
:mod:`api.cron_output_parser` (items 4, 5 and 6 of the review). Each is
pinned here by a behavioural test that constructs the exact input the
maintainer probed with:

**Finding 4 — a reply past line 2,000 lost its boundary.** The scan
stopped at ``_MAX_PROBE_LINES`` (2,000), so a run whose ``## Response``
heading landed on line 2,001 came back ``has_response_boundary=False``.
The caller then rendered the raw artifact, and the collapsed preview
showed the first 600 chars of front-matter instead of the reply — a
regression against the old preview, which scanned everything. The cap is
gone; the whole already-read artifact is scanned.

**Finding 5 — adjacent tags left the second one untracked.** A line
like ``<pre>one</pre><code>`` was processed by counting *all* closes
before *any* open, so the ``<code>`` that opened after the ``</pre>``
was never counted and the block closed one tag early. A ``## Response``
on the next line (inside the still-open ``<code>``) was then accepted as
the boundary. Tags are now walked in token order.

**Finding 6 — no-boundary previews lost their ``.strip()``.**
``response_snippet`` sliced the raw body without trimming first, so an
artifact with 610 leading spaces produced a 600-space (blank) preview.
The trim is restored before the slice.

Each test below asserts the *buggy* behaviour described by the
maintainer, so it goes RED on ``e4050150`` and GREEN on the fix.
"""
from __future__ import annotations

import textwrap

from api.cron_output_parser import parse_cron_output, response_snippet


# ── Finding 4 — reply past line 2,000 ────────────────────────────────


def test_response_heading_past_line_2000_is_found():
    """The exact 2,001-line probe from the review.

    2,000 filler lines put the ``## Response`` heading on line 2,001,
    which the old ``_MAX_PROBE_LINES`` cap excluded, so the projection
    reported no boundary and the UI fell back to the raw context.
    """
    padding = "\n".join(f"tool dump line {i}" for i in range(2000))
    text = (
        "---\nrun_id: 2001-lines\n---\n\n"
        f"{padding}\n\n"
        "## Response\n\n"
        "All 12 nodes are healthy after the long tool dump.\n"
    )
    # Sanity: the heading really is past the old cap.
    heading_line = text.count("\n", 0, text.index("## Response")) + 1
    assert heading_line > 2001, heading_line
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True, (
        "a ## Response heading on line 2,001 must still be found — the "
        "probe cap dropped it and the preview showed front-matter instead"
    )
    assert projection.response_line == heading_line
    assert projection.response.startswith("All 12 nodes are healthy")
    assert "run_id: 2001-lines" in projection.context


def test_scan_is_not_bounded_by_a_line_limit_constant():
    """The cap constant is gone, so no future edit can quietly reintroduce
    it by tuning a number. Guards are structural (fence + exact heading),
    not positional."""
    import api.cron_output_parser as parser

    assert not hasattr(parser, "_MAX_PROBE_LINES"), (
        "the line-count probe cap must be removed, not raised — a reply "
        "past any fixed cap regresses the preview to front-matter"
    )


def test_deep_fence_guard_still_applies_past_line_2000():
    """Removing the cap must not weaken the fail-closed guards: a
    heading-shaped line inside a fence that starts before line 2,000 and
    ends after it is still not a boundary."""
    deep_fence = "\n".join(f"quoted line {i}" for i in range(3000))
    text = (
        "---\nrun_id: deep-fence\n---\n\n"
        "````markdown\n"
        f"{deep_fence}\n"
        "## Response\n"
        "(this heading is inside a fence that spans the old cap)\n"
        "````\n"
        "\n"
        "## Response\n"
        "\n"
        "The real reply, after the deep fence.\n"
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == "The real reply, after the deep fence."
    assert "this heading is inside a fence" in projection.context


# ── Finding 5 — adjacent tags on one line ────────────────────────────


def test_adjacent_close_then_open_tags_leave_the_code_open():
    """``<pre>one</pre><code>`` must leave the ``<code>`` open.

    The old implementation counted every close on the line before any
    open, so the trailing ``<code>`` was dropped from the depth and the
    block closed one tag early — accepting the ``## Response`` on the
    next line as the boundary even though it sits inside the still-open
    ``<code>``.
    """
    text = textwrap.dedent(
        """\
        ---
        run_id: adjacent-tags
        ---

        <pre>one</pre><code>
        ## Response
        (this heading is inside the still-open <code> and must be ignored)
        </code>

        ## Response

        The real response after the adjacent-tag line.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == "The real response after the adjacent-tag line."
    assert "must be ignored" not in projection.response, (
        "the heading inside the still-open <code> was accepted as the "
        "boundary — adjacent tags were not processed in token order"
    )
    assert "must be ignored" in projection.context


def test_adjacent_open_then_close_tags_still_close():
    """The same walk in the other direction: ``<code>one</code><pre>``
    opens a ``<pre>`` that stays open for the following lines, so a
    heading inside it is not a boundary."""
    text = textwrap.dedent(
        """\
        <code>one</code><pre>
        ## Response
        (inside the <pre> opened by the adjacent tag)
        </pre>

        ## Response

        The real response.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == "The real response."
    assert "inside the <pre> opened" in projection.context


def test_prose_mention_of_a_tag_inside_open_block_does_not_reopen():
    """Token order must not come at the cost of the prose guard: while
    a ``<pre>`` block is open, a later line that merely *mentions*
    ``<pre>`` mid-sentence must not open a second ``<pre>`` and keep the
    block alive forever."""
    text = textwrap.dedent(
        """\
        <pre>
        first block
        </pre>
        This line mentions the open <pre> tag in prose.
        ## Response

        The real response after the prose mention.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True, (
        "a prose mention of <pre> must not re-open the HTML block and "
        "swallow the real ## Response heading"
    )
    assert projection.response == "The real response after the prose mention."


# ── Finding 6 — no-boundary preview lost its .strip() ────────────────


def test_no_boundary_preview_trims_leading_whitespace_before_slicing():
    """The exact probe from the review: 610 leading spaces.

    The snippet sliced the untrimmed body, so the 600-char limit was
    consumed entirely by spaces and the preview rendered blank — the row
    looked like it had no output at all.
    """
    text = (" " * 610) + "front-matter: real content starts here\n"
    snippet = response_snippet(text, limit=600)
    assert snippet.strip() != "", (
        "a preview made entirely of leading spaces is blank — the trim "
        "before the slice was lost"
    )
    assert snippet.startswith("front-matter:"), snippet[:40]
    assert len(snippet) <= 600


def test_no_boundary_preview_trims_trailing_whitespace_too():
    """The trim applies to the body used for the no-boundary preview, so a
    run whose only content is trailing whitespace still renders the
    legacy ``(empty)`` placeholder instead of spaces."""
    text = ("\n" * 10) + ("   " * 200)
    snippet = response_snippet(text, limit=600)
    assert snippet == "(empty)", snippet[:40]
