"""#7687 re-gate round 4 — the two legacy shapes that previewed worse than master.

The 10-12 review re-ran the parity sweep and found two legacy envelope
shapes where this branch showed a WORSE preview than master
(``api/routes.py::_cron_output_snippet`` — first canonical ``## Response``
heading, whole document when none is found):

| legacy archive | master preview | pre-fix branch |
|---|---|---|
| answer contains its own ``## Response`` heading (inside a fenced template) | whole answer | ``Example text`` + a stray fence |
| prompt quotes a framed example (``**Prompt Characters:** 12`` + ``## Prompt``) | whole answer | no boundary; the preview was the prompt text |

Both rows are pinned here as regression fixtures.

Finding 1 (row 1) is fixed by the exactly-one-candidate rule: an envelope
whose answer carries its own ``## Response`` has more than one candidate,
so the projection declines to guess and keeps master's snippet plus the
raw-primary view instead of cutting the answer at the wrong line.

Finding 2 (row 2) is fixed by ``_has_own_prompt_frame``: a frame quoted
inside the prompt no longer trips the fail-closed guard that used to fire
for ANY prompt frame, so the document falls through to the legacy
envelope rule and shows the real answer.
"""

from __future__ import annotations

import textwrap

from api.cron_output_parser import (
    _has_own_prompt_frame,
    _has_prompt_frame,
    parse_cron_output,
)


def test_answer_with_its_own_response_heading_keeps_the_whole_answer():
    """Row 1: the answer contains a fenced template with a ``## Response``
    heading inside it.

    The envelope has TWO canonical ``## Response`` headings, so the
    envelope path must not guess: it declines to promote the last one
    (which cut the answer to the fenced example). The document then
    reaches the fence-aware unframed scan, which correctly skips the
    heading inside the fenced block and resolves the writer's real
    delimiter — the whole answer, matching master's preview.
    """
    text = textwrap.dedent(
        """\
        **Prompt Characters:** 12

        ## Prompt

        Say hi plz

        **Response Characters:** 0

        ## Response

        Backup completed for 3 volumes.

        Example template:

        ```md
        ## Response

        Example text
        ```
        """
    )
    out = parse_cron_output(text)
    assert out.has_response_boundary is True
    assert out.response == (
        "Backup completed for 3 volumes.\n\nExample template:\n\n```md\n"
        "## Response\n\nExample text\n```"
    ), f"the answer was cut at the wrong boundary: {out.response!r}"
    assert out.response_line == 9, (
        "the boundary must be the writer's own heading, not the one quoted "
        f"inside the fenced example: line {out.response_line}"
    )


def test_quoted_frame_inside_the_prompt_is_not_the_documents_own_frame():
    """Row 2: the prompt quotes a framed run document as an example.

    The quoted ``**Prompt Characters:** 12`` + ``## Prompt`` pair matches
    the prompt-frame regex, but it is NOT the document's own frame — the
    writer's own ``## Prompt`` comes first. The fail-closed guard must
    only fire for an OWN frame, so this document falls through to the
    legacy envelope rule and shows the real answer.
    """
    text = textwrap.dedent(
        """\
        **Prompt Characters:** 90

        ## Prompt

        Please answer.

        Example of a framed run document:

        **Prompt Characters:** 12
        ## Prompt

        quoted prompt

        **Response Characters:** 5
        ## Response

        Backup completed for 3 volumes.
        """
    )
    # The precondition the review reproduced: a frame regex hit that is
    # NOT the document's own frame.
    assert _has_prompt_frame(text) is True
    assert _has_own_prompt_frame(text) is False

    out = parse_cron_output(text)
    assert out.has_response_boundary is True, (
        "a quoted frame must not trip the fail-closed guard: "
        f"response={out.response!r}"
    )
    assert out.response == "Backup completed for 3 volumes.", (
        "the quoted frame must not hide the real answer: "
        f"{out.response!r}"
    )


def test_own_frame_that_fails_validation_still_fails_closed():
    """Control: the guard's original intent is preserved.

    A document whose OWN frame announces the first ``## Prompt`` but whose
    lengths do not validate is still never guessed at with the legacy
    heading rule — only a QUOTED frame falls through.
    """
    text = textwrap.dedent(
        """\
        **Prompt Characters:** 10
        ## Prompt

        the prompt

        **Response Characters:** 99
        ## Response

        the answer
        """
    )
    assert _has_own_prompt_frame(text) is True
    out = parse_cron_output(text)
    assert out.has_response_boundary is False, (
        "an own frame whose length does not validate must stay raw-primary"
    )
    assert out.response == ""


def test_single_candidate_envelope_still_resolves_the_writer_delimiter():
    """Control: the exactly-one-candidate rule keeps the round-3 repairs.

    A legacy envelope with exactly one canonical ``## Response`` — the
    writer's own delimiter — is still promoted, including the unclosed
    prompt fence shape that motivated the legacy branch.
    """
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
    assert out.has_response_boundary is True
    assert out.response == "Backup completed for 3 volumes."
