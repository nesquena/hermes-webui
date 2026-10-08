"""#7303 review 10/06 — protected-context isolation and writer-frame parsing.

The follow-up review on ``6efeaab9c`` re-gated the response-first run
view on three new runtime defects. ``api/cron_output_parser`` owns the
first two; the third is a client-side stale-owner cache leak in
``static/panels.js`` (covered by the node-driver test at the bottom).

**Finding A — protected contexts poison one another.** The fence path
ran before the active-HTML check and the HTML path before the active
fence check, so either quoted shape outlived the other:

* a literal ``<pre>`` inside a fenced HTML sample left PRE state alive
  after the fence closed and swallowed the real ``## Response``;
* a literal fence inside a ``<pre>`` left fence state alive after the
  ``<pre>`` closed and swallowed the heading the same way.

Inside a fence only a valid fence closer may be inspected; inside an
HTML block only its ordered tags may be processed (never a new Markdown
fence opening).

**Finding 2 — the parser ignored the producer's framed envelope.** The
writer assembles ``## Prompt`` + prompt bytes + ``## Response`` +
response, and its own reader (``_archive_answer``) treats the LAST
``## Response`` as the boundary because the prompt half may legitimately
quote a literal ``## Response`` example. The parser instead took the
first heading, so:

* a heading example embedded in the framed prompt was projected as the
  response instead of the authoritative real answer;
* an unclosed fence inside the prompt stranded the whole scan and hid a
  separately valid framed answer;
* a truncated/empty response frame (which the producer reader rejects)
  was promoted to a recognized partial answer.

For writer-framed artifacts we validate the frame and skip the prompt
bytes (boundary = last ``## Response``); a framed envelope with no
usable answer stays raw-primary, and an explicit conservative legacy
unframed path keeps the previous fail-closed guards.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from api.cron_output_parser import (
    _PROMPT_FRAME,
    _PROMPT_HEADING,
    _PROMPT_SEPARATOR,
    _RESPONSE_FRAME,
    _RESPONSE_HEADING,
    _RESPONSE_TERMINATOR,
    parse_cron_output,
)

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = REPO_ROOT / "static" / "panels.js"
DRIVER_JS = Path(__file__).parent / "_cron_run_body_driver.js"

NODE = shutil.which("node")
import pytest

_requires_node = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ---------------------------------------------------------------------------
# Finding 1 — protected contexts must not leak past their own terminator
# ---------------------------------------------------------------------------


def test_literal_fence_inside_pre_must_not_outlive_the_pre():
    """A Markdown fence opened *inside* a <pre> must not survive the
    ``</pre>``: previously the fence-before-HTML ordering left fence
    state alive after the PRE closed, so the real ``## Response`` heading
    (now outside every container) was skipped and the run fell back to
    raw.
    """
    text = textwrap.dedent(
        """\
        # Cron Job: backup

        <pre>
        ```text
        quoted literal fence inside the pre block
        </pre>

        ## Response

        The real result after the pre.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True, (
        "a literal fence inside <pre> must not keep the fence open past "
        "</pre> and hide the real ## Response"
    )
    assert projection.response == "The real result after the pre."
    assert "quoted literal fence" in projection.context


def test_literal_pre_inside_fence_must_not_outlive_the_fence():
    """A literal <pre> inside a fenced HTML sample must not open HTML
    state that survives the fence: the HTML-branch ordering read the
    ``<pre>`` while the fence was still open, so after the fence closed
    the block stayed ``in_html_pre`` and swallowed the real heading.
    """
    text = textwrap.dedent(
        """\
        ```markdown
        <pre>
        unclosed html sample quoted inside a fence
        ```

        ## Response

        The real reply after the fence.
        """
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True, (
        "a literal <pre> inside a fence must not leave HTML state alive "
        "past the fence close and hide the real ## Response"
    )
    assert projection.response == "The real reply after the fence."
    assert "unclosed html sample" in projection.context


# ---------------------------------------------------------------------------
# Finding 2: the writer-framed envelope keys off the LAST ## Response.
# ---------------------------------------------------------------------------


def _framed(body_after_prompt: str, *, prompt: str | None = None) -> str:
    """Build a run document the way the WRITER does, length frames included.

    The 10/08 review found these fixtures never used the frame lines: they
    carried a bare ``## Prompt`` heading and nothing else, so they exercised the
    legacy unframed path while claiming to test the framed one. The writer
    (``cron.scheduler.run_job``) stamps the byte length of the prompt and of the
    response outside the user-owned text — see ``_PROMPT_FRAME`` /
    ``_RESPONSE_FRAME`` in ``api/cron_output_parser`` and the identical
    constants in hermes-agent's ``cron/scheduler_prompt.py``.

    The prompt body defaults to the text the old helper passed as
    ``body_after_prompt`` so the scenarios stay comparable.
    """
    if prompt is None:
        prompt = (
            "You are an SRE bot. The expected report format is:\n"
            f"{body_after_prompt}"
        )
    return (
        "# Cron Job: sre\n"
        "\n"
        "**Job ID:** abc\n"
        "\n"
        f"{_PROMPT_FRAME}{len(prompt)}\n"
        f"{_PROMPT_HEADING}"
        f"{prompt}"
        f"{_PROMPT_SEPARATOR}"
    )


def _framed_with_response(prompt_body: str, answer: str) -> str:
    """A complete framed document: prompt frame, response frame, terminator."""
    return (
        _framed(prompt_body)
        + f"{_RESPONSE_FRAME}{len(answer)}\n"
        f"{_RESPONSE_HEADING}"
        f"{answer}"
        f"{_RESPONSE_TERMINATOR}"
    )


def test_prompt_example_heading_does_not_beat_the_real_answer():
    """A ``## Response`` documented inside the assembled prompt (for
    example a skill describing its own output format, or an injected
    previous answer quoting it) must NOT be the boundary.

    The prompt is skipped by its stamped LENGTH, so a heading quoted inside it
    can never become the boundary — the writer's own reader
    (``_archive_answer``) jumps past the prompt the same way.
    """
    answer = "All 12 nodes are healthy. p99 = 142 ms."
    text = _framed_with_response(
        "## Response\n"
        "EXAMPLE ONLY - a documented format, not the actual reply.\n"
        "\n"
        "Please summarise the cluster state.\n"
        "\n"
        "## Response\n"
        "\n"
        "This second example is quoted too.\n",
        answer,
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == answer, (
        "a ## Response quoted inside the prompt was promoted as the response - "
        "the prompt must be skipped by its length frame"
    )
    assert "EXAMPLE ONLY" not in projection.response
    assert "documented format" in projection.context


def test_answer_containing_its_own_response_heading_is_not_cut():
    """The 10/08 [MUST-FIX]: an answer that contains a ``## Response`` line.

    The old code split on the LAST ``## Response``, so an answer with a section
    of that title (an output-format example, say) was cut at that line: the view
    labelled only the tail as Response and moved the real start of the answer
    into context. The producer's reader returns the whole answer.
    """
    answer = (
        "Here is the report.\n"
        "\n"
        "## Response\n"
        "\n"
        "The section above the fold, which the last-heading rule dropped.\n"
        "\n"
        "And the tail, which it kept."
    )
    text = _framed_with_response("Summarise the cluster state.", answer)
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == answer, (
        "the answer was cut at a ## Response line inside the answer itself"
    )
    assert projection.response.count("## Response") == 1
    assert "The section above the fold" in projection.response
    assert "Summarise the cluster state." in projection.context


def test_unclosed_prompt_fence_does_not_hide_a_valid_framed_answer():
    """Scanning the prompt half must not strand the parser when the
    prompt carries an unclosed Markdown fence; the writer-owned
    length frame still delineates a valid framed answer.

    The prompt is skipped by length, so its fence state is never even computed.
    """
    answer = "Backup completed for 3 volumes."
    text = _framed_with_response(
        "```bash\n"
        "# this fence is intentionally never closed by a matching one\n"
        "status --full\n",
        answer,
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True, (
        "an unclosed fence inside the prompt must not hide the writer's "
        "framed answer"
    )
    assert projection.response == answer
    assert "status --full" in projection.context


def test_empty_framed_terminator_stays_raw_primary():
    """A truncated / empty response frame (what the producer reader
    rejects) must not be promoted to a recognized partial answer: the
    header alone with no usable body keeps the artifact raw-primary.
    """
    text = _framed(
        "# Report\n"
        "\n"
        "## Response\n"
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is False, (
        "a framed envelope whose response body is empty must not be "
        "recognised as a partial answer - it stays raw-primary"
    )
    assert projection.response == ""
    assert projection.context == text


def test_truncated_response_length_stays_raw_primary():
    """The 10/08 [MUST-FIX], second half: a truncated response.

    If the ``Response Characters`` length does not match (an interrupted write),
    the producer returns no usable answer. The old code showed the partial text
    as the Response whenever it was non-empty, and its comment claimed the
    opposite. The frame must be validated, not assumed.
    """
    full_answer = "All 12 nodes are healthy. p99 = 142 ms."
    text = _framed_with_response("Summarise the cluster state.", full_answer)
    # Simulate an interrupted write: the answer is cut short but the frame still
    # stamps the full length.
    truncated = full_answer[: len(full_answer) // 2]
    broken = text.replace(
        f"{_RESPONSE_FRAME}{len(full_answer)}", f"{_RESPONSE_FRAME}{len(full_answer)}"
    ).replace(full_answer + _RESPONSE_TERMINATOR, truncated + _RESPONSE_TERMINATOR)
    assert broken != text, "the fixture must actually be truncated"

    projection = parse_cron_output(broken)
    assert projection.has_response_boundary is False, (
        "a response whose stamped length does not match must not be promoted "
        "to a partial answer - the producer reader rejects it too"
    )
    assert projection.response == ""
    assert projection.context == broken


def test_framed_document_matches_the_producer_reader():
    """Cross-check the projection against hermes-agent's own reader.

    The producer (``cron/scheduler_prompt.py::_archive_answer``) is the
    authority on what a framed document means, so the parser must agree with it
    on the answer text for the same input. The producer's algorithm is
    reproduced here verbatim so a drift on either side fails this test.
    """
    import re as _re

    prompt_frame_re = _re.compile(
        rf"(?m)^{_re.escape(_PROMPT_FRAME)}(\d+)\n{_re.escape(_PROMPT_HEADING)}"
    )
    response_frame_re = _re.compile(
        rf"(?m)^{_re.escape(_RESPONSE_FRAME)}(\d+)\n{_re.escape(_RESPONSE_HEADING)}"
    )

    def producer_answer(archive: str) -> str | None:
        prompt_frame = prompt_frame_re.search(archive)
        if prompt_frame is None:
            return None
        response_start = (
            prompt_frame.end()
            + int(prompt_frame.group(1))
            + len(_PROMPT_SEPARATOR)
        )
        frame = response_frame_re.match(archive, response_start)
        tail = archive[frame.end():] if frame is not None else ""
        if frame is None:
            return None
        if len(tail) != int(frame.group(1)) + len(_RESPONSE_TERMINATOR):
            return None
        if not tail.endswith(_RESPONSE_TERMINATOR):
            return None
        return tail[: -len(_RESPONSE_TERMINATOR)].strip()

    cases = [
        _framed_with_response("Summarise the cluster state.", "All 12 nodes healthy."),
        # An answer that quotes its own boundary.
        _framed_with_response(
            "Show the format.",
            "## Response\n\nThe whole answer, including this heading.\n",
        ),
        # A prompt that quotes a whole framed block.
        _framed_with_response(
            "Previous run was:\n"
            "**Prompt Characters:** 5\n"
            "## Prompt\n\nhello\n\n"
            "**Response Characters:** 2\n"
            "## Response\n\nhi\n",
            "The new answer.\n",
        ),
    ]
    for text in cases:
        expected = producer_answer(text)
        assert expected is not None, "the fixture must be a valid framed document"
        projection = parse_cron_output(text)
        assert projection.response == expected, (
            "the parser disagrees with the producer's own reader on the answer"
        )
        assert projection.has_response_boundary is True


# ---------------------------------------------------------------------------
# Finding 3: CORE — an in-flight load must not repopulate a reset cache.
# ---------------------------------------------------------------------------


def _run_driver(scenario: dict) -> dict:
    assert NODE is not None, "node must be on PATH for the renderer tests"
    result = subprocess.run(
        [NODE, str(DRIVER_JS), str(PANELS_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


@_requires_node
def test_stale_inflight_load_does_not_repopulate_replaced_cache():
    """CORE: replacing the run list resets ``_cronRunBodyCache``; an
    in-flight ``_loadRunContent`` that started against the OLD list must
    not write the old answer back under the same job/filename key, or a
    newer connected row would mount it until its own fetch settles.
    """
    payload_a = {
        "content": "## Response\n\nA PRIVATE ANSWER for an earlier run.\n",
        "snippet": "A PRIVATE ANSWER",
        "parsed": {
            "response": "A PRIVATE ANSWER for an earlier run.",
            "has_response_boundary": True,
        },
        "usage": None,
    }
    scenario = {
        "mode": "stale-write",
        "jobId": "job",
        "filename": "2026-10-06_000000.md",
        "payload": payload_a,
        # Keep the fetch pending until after the list-replacement reset so
        # the stale load's resolve provably runs *after* the cache is new.
        "pendingFetch": True,
    }
    out = _run_driver(scenario)
    assert out["cacheAfterStale"] is False, (
        "the replaced list owns the job/filename key - an in-flight load "
        "from the previous list must not repopulate the cache with an "
        "old run's payload"
    )
    assert out["fetchSettled"] is True