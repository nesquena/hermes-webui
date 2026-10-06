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

from api.cron_output_parser import parse_cron_output

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


def _framed(body_after_prompt: str) -> str:
    return (
        "# Cron Job: sre\n"
        "\n"
        "**Job ID:** abc\n"
        "\n"
        "## Prompt\n"
        "\n"
        "You are an SRE bot. The expected report format is:\n"
        f"{body_after_prompt}"
    )


def test_prompt_example_heading_does_not_beat_the_real_answer():
    """A ``## Response`` documented inside the assembled prompt (for
    example a skill describing its own output format, or an injected
    previous answer quoting it) must NOT be the boundary. The producer's
    own reader keys off the LAST ``## Response``; mirror that so the real
    reply wins.
    """
    text = _framed(
        "## Response\n"
        "EXAMPLE ONLY - a documented format, not the actual reply.\n"
        "\n"
        "Please summarise the cluster state.\n"
        "\n"
        "## Response\n"
        "\n"
        "All 12 nodes are healthy. p99 = 142 ms.\n"
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True
    assert projection.response == "All 12 nodes are healthy. p99 = 142 ms.", (
        "the prompted EXAMPLE ONLY heading was promoted as the response - "
        "the writer's boundary is the LAST ## Response"
    )
    assert "EXAMPLE ONLY" not in projection.response
    assert "documented format" in projection.context


def test_unclosed_prompt_fence_does_not_hide_a_valid_framed_answer():
    """Scanning the prompt half must not strand the parser when the
    prompt carries an unclosed Markdown fence; the writer-owned
    terminator still delineates a valid framed answer.
    """
    text = _framed(
        "```bash\n"
        "# this fence is intentionally never closed by a matching one\n"
        "status --full\n"
        "\n"
        "## Response\n"
        "\n"
        "Backup completed for 3 volumes.\n"
    )
    projection = parse_cron_output(text)
    assert projection.has_response_boundary is True, (
        "an unclosed fence inside the prompt must not hide the writer's "
        "framed ## Response answer"
    )
    assert projection.response == "Backup completed for 3 volumes."
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