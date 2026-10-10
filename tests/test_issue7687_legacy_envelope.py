"""#7687 re-gate — legacy writer envelopes keep their final-answer preview.

The round-3 re-gate found that the legacy ``## Prompt`` branch was deleted
when the framed-run rule landed, so unframed documents fall into the
fence-aware first-heading scan. Two real on-disk shapes hit that path:

* the Agent wrote ``## Prompt`` plus a response frame with **no prompt
  stamp** until ``00bbc5b64fc`` (2026-09-30);
* failed runs are never prompt-stamped at all
  (``cron/scheduler.py:_run_doc_header``).

Whenever such a document's prompt contains an unclosed fence or HTML
block, the scanner carries that prompt state across the writer's own
``## Response`` delimiter and returns the first 600 characters of
context instead of the answer — verified over real HTTP: master and
``25642a74098f`` display "Backup completed for 3 volumes" where the
pre-fix head shows prompt text with ``has_response_boundary=false``.

For these envelopes the writer's delimiter is authoritative, so the
boundary is the LAST canonical ``## Response``, resolved without fence
tracking — the same rule the pre-``00bbc5b64fc`` reader used. Framed
documents still go through frame validation first and never reach this
path, so a truncated or mismatched frame is still not promoted.
"""
from __future__ import annotations

import textwrap

from api.cron_output_parser import parse_cron_output


def test_unclosed_prompt_fence_keeps_the_final_answer_preview():
    """The exact reproduction: a legacy ``## Prompt`` envelope whose prompt
    opens a fence it never closes must still project the writer's answer,
    not the prompt text."""
    text = textwrap.dedent(
        """\
        # Cron Job: backup

        ## Prompt

        Run the nightly volume backup. Sample config:

        ```json
        {"volumes": ["data", "media"]}

        ## Response

        Backup completed for 3 volumes.
        """
    )
    out = parse_cron_output(text)
    assert out.has_response_boundary is True, (
        "a legacy writer envelope must keep its response boundary"
    )
    assert out.response == "Backup completed for 3 volumes.", (
        f"the final-answer preview regressed to context: {out.response!r}"
    )


def test_unclosed_prompt_html_block_keeps_the_final_answer_preview():
    """Same shape with an unclosed HTML block instead of a fence — the
    ordered-tag walk must not swallow the writer's delimiter either."""
    text = textwrap.dedent(
        """\
        # Cron Job: backup

        ## Prompt

        Dump the status page snippet for the archive:

        <pre><code>
        status: ok

        ## Response

        Backup completed for 3 volumes.
        """
    )
    out = parse_cron_output(text)
    assert out.has_response_boundary is True
    assert out.response == "Backup completed for 3 volumes.", (
        f"the final-answer preview regressed to context: {out.response!r}"
    )


def test_last_response_wins_when_the_prompt_quotes_the_heading():
    """The prompt half may legitimately quote a literal ``## Response``
    (a skill documenting its output format). The writer's own reader
    resolves the LAST one, so the projection must too."""
    text = textwrap.dedent(
        """\
        # Cron Job: report

        ## Prompt

        Emit the report. When a skill documents its format it may quote:

        ## Response

        <example answer placeholder>

        ## Response

        Real answer for 7 volumes.
        """
    )
    out = parse_cron_output(text)
    assert out.has_response_boundary is True
    assert out.response == "Real answer for 7 volumes.", (
        f"the quoted heading must not win over the real one: {out.response!r}"
    )


def test_framed_document_still_goes_through_frame_validation():
    """Control: a framed document must not silently take the legacy
    envelope path — its lengths remain the only authoritative boundary,
    and an invalid frame still fails closed."""
    text = textwrap.dedent(
        """\
        **Prompt Characters:** 10
        ## Prompt

        the prompt

        **Response Characters:** 10
        ## Response

        the answer
        """
    )
    out = parse_cron_output(text)
    assert out.has_response_boundary is True
    assert out.response == "the answer"

    # A truncated write whose length does not match must NOT be promoted
    # through either path.
    broken = textwrap.dedent(
        """\
        **Prompt Characters:** 10
        ## Prompt

        the prompt

        **Response Characters:** 99
        ## Response

        the answer
        """
    )
    out_broken = parse_cron_output(broken)
    assert out_broken.has_response_boundary is False, (
        "a frame whose length does not validate must stay raw-primary"
    )


def test_unframed_document_without_prompt_heading_is_untouched():
    """Control: a plain transcript with no ``## Prompt`` envelope keeps
    the conservative first-heading scan — the new branch must not widen
    what counts as a cron artifact."""
    text = textwrap.dedent(
        """\
        # Cron Job: adhoc

        Some preamble the user pasted.

        ## Response

        first heading wins here.
        """
    )
    out = parse_cron_output(text)
    assert out.has_response_boundary is True
    assert out.response == "first heading wins here.", (
        f"an unframed document without the envelope changed behavior: {out.response!r}"
    )
