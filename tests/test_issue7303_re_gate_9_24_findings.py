"""#7303 re-gate 9/24 — one focused regression test per finding.

The maintainer's 9/24 re-gate review on commit ``e4050150`` flagged six
defects on the response-first run view (two release-blocker CORE items
and four SILENT items). The other test modules in this directory
(``test_issue7303_re_gate_9_24_parser`` and
``test_issue7303_re_gate_9_24_renderer``) cover each finding with
several behaviour tests, but the ask from the review is explicit:
**one regression test per finding** so a future regression points
directly at the contract that broke.

This file ships exactly six tests, one per finding, mapping:

* ``test_finding_1_core_view_raw_output_renders_verbatim`` — CORE 1
* ``test_finding_2_core_pending_fetch_keeps_row_open`` — CORE 2
* ``test_finding_3_silent_script_job_and_empty_response_stay_raw`` — SILENT 3
* ``test_finding_4_silent_reply_past_line_2000_keeps_boundary`` — SILENT 4
* ``test_finding_5_silent_adjacent_tags_keep_block_open`` — SILENT 5
* ``test_finding_6_silent_no_boundary_preview_is_trimmed`` — SILENT 6

Each test asserts the *buggy* behaviour described by the maintainer, so
it goes RED on ``e4050150`` (the pre-9/24 code) and GREEN on the fix
shipped in ``8f92991e``.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from api.cron_output_parser import parse_cron_output, response_snippet

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = REPO_ROOT / "static" / "panels.js"
DRIVER_JS = Path(__file__).parent / "_cron_run_body_driver.js"

NODE = shutil.which("node")
import pytest

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


# ---------------------------------------------------------------------------
# shared renderer driver (reuses the existing node harness)
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


def _entry(rendered, kind):
    for entry in rendered:
        if entry["kind"] == kind:
            return entry
    return None


# A realistic /api/crons/run payload for an agent run with a recognized
# response boundary, where the raw artifact carries front-matter and a
# prompt section that the projection strips. The renderer tests reuse
# this so the contract being tested is the renderer, not the parser.
AGENT_RUN_PAYLOAD = {
    "content": (
        "---\n"
        "run_id: abc123\n"
        "model: claude-opus-4\n"
        "---\n"
        "\n"
        "## Prompt\n"
        "\n"
        "You are an SRE bot. Check the cluster health.\n"
        "\n"
        "## Response\n"
        "\n"
        "All 12 nodes are healthy. Latency p99 = 142 ms.\n"
    ),
    "snippet": "All 12 nodes are healthy. Latency p99 = 142 ms.",
    "parsed": {
        "response": "All 12 nodes are healthy. Latency p99 = 142 ms.",
        "context": (
            "---\nrun_id: abc123\nmodel: claude-opus-4\n---\n\n"
            "## Prompt\n\nYou are an SRE bot. Check the cluster health."
        ),
        "has_response_boundary": True,
        "response_line": 9,
    },
    "usage": {"input_tokens": 820, "output_tokens": 96, "total_tokens": 916},
}

AGENT_JOB = {"id": "job-agent", "no_agent": False}


def _render(scenario_payload, *, job, expanded):
    return _run_driver(
        {
            "mode": "render",
            "jobId": job["id"],
            "filename": "2026-09-24_120000.md",
            "expanded": expanded,
            "currentCronDetail": job,
            "payload": scenario_payload,
        }
    )


# ---------------------------------------------------------------------------
# Finding 1 — CORE 1: expanded response-first runs keep "View raw output"
# ---------------------------------------------------------------------------


def test_finding_1_core_view_raw_output_renders_verbatim():
    """CORE 1: the projection drops the ``## Response`` heading and trims
    both sides, so the response block + context disclosure cannot rebuild
    the artifact. The expanded response-first view must keep a control
    that mounts ``data.content`` verbatim — that is the only way a user
    can copy the original file or do prompt-engineering forensics on it.

    On the pre-9/24 code the control was the legacy "View full output"
    button, which the response-first renderer explicitly removed; the
    fix is the dedicated raw-output control. This test pins the
    end-to-end contract: the button exists on the expanded view, it
    mounts the verbatim content, and the back button restores the
    response view without a second fetch.
    """
    out = _run_driver(
        {
            "mode": "render-then-click-raw",
            "jobId": AGENT_JOB["id"],
            "filename": "2026-09-24_120000.md",
            "expanded": True,
            "currentCronDetail": AGENT_JOB,
            "payload": AGENT_RUN_PAYLOAD,
        }
    )
    assert out["hasRawButton"] is True, (
        "expanded response-first runs must keep a 'View raw output' "
        "control so the user can recover the original artifact"
    )
    raw_pre = _entry(out["afterRawClick"], "pre")
    assert raw_pre is not None, out["afterRawClick"]
    assert raw_pre["text"] == AGENT_RUN_PAYLOAD["content"], (
        "the raw control must render data.content verbatim — the "
        "## Response heading and surrounding text must be present"
    )
    # Back to the response view from the cache, no second fetch.
    assert out["hasBackButton"] is True
    back_block = _entry(out["afterBackClick"], "response-block")
    assert back_block is not None
    assert "All 12 nodes are healthy" in back_block["text"]


# ---------------------------------------------------------------------------
# Finding 2 — CORE 2: pending fetch must not close the row
# ---------------------------------------------------------------------------


def test_finding_2_core_pending_fetch_keeps_row_open():
    """CORE 2: on a cache miss the toggle used to fall back to
    ``_loadRunContent()``, which saw the row already ``.open`` and
    treated the click as a *collapse* request — closing the row the
    user just opened. The fix is to drop the fallback: the pending
    fetch already reads the current expansion state when it resolves,
    so the just-toggled state is rendered on its own.

    This test pins the two halves of that contract: the row stays open
    across the toggle, and the resolved render shows the expanded view
    (not a stale collapsed state).
    """
    out = _run_driver(
        {
            "mode": "toggle-pending",
            "jobId": AGENT_JOB["id"],
            "filename": "2026-09-24_120000.md",
            "pendingFetch": True,
            "resolveFetch": True,
            "currentCronDetail": AGENT_JOB,
            "payload": AGENT_RUN_PAYLOAD,
        }
    )
    assert out["openAfterLoad"] is True, "opening the row must mark it open"
    assert out["openAfterToggle"] is True, (
        "clicking expand while the fetch is pending must NOT close the "
        "row — the pre-9/24 fallback treated the already-open row as a "
        "collapse request and flipped it shut"
    )
    assert out["storedExpanded"] is True, (
        "the toggle must persist the expanded state for the pending "
        "fetch to read on resolve"
    )
    assert out["fetchAfterToggle"] == out["fetchAfterLoad"], (
        "the toggle must not fire a second fetch while one is pending"
    )
    # The resolved render must honour the toggled state.
    assert _entry(out["rendered"], "response-block") is not None, (
        "the pending fetch must render the response view when it "
        "resolves into the toggled (expanded) state"
    )


# ---------------------------------------------------------------------------
# Finding 3 — SILENT 3: script jobs and empty responses stay raw
# ---------------------------------------------------------------------------


def test_finding_3_silent_script_job_and_empty_response_stay_raw():
    """SILENT 3: two distinct conditions were missing from the gate that
    enables response-first. A script (``no_agent``) job whose stdout
    contains ``## Response`` is still a script — switching it to
    response-first silently changed the established raw view for that
    job. And a boundary with an EMPTY response body is not a reply
    either: rendering it response-first shows a blank primary block.

    The fix gates response-first on both ``!isScriptJob`` and
    ``hasResponseText`` (non-empty response body). This test exercises
    both gates from a single assertion: a script job AND an empty
    response must fall back to the raw artifact, with no response
    block mounted and the verbatim content shown instead.
    """
    payload = {
        "content": "starting backup\n## Response\n3 files copied\nbackup finished\n",
        "snippet": "3 files copied\nbackup finished\n",
        "parsed": {
            "response": "3 files copied\nbackup finished\n",
            "context": "starting backup",
            "has_response_boundary": True,
            "response_line": 2,
        },
        "usage": None,
    }
    script_job = {"id": "job-script", "no_agent": True}
    out = _render(payload, job=script_job, expanded=True)
    assert _entry(out["rendered"], "response-block") is None, (
        "a script job must not switch to response-first no matter what "
        "its stdout contains"
    )
    pre = _entry(out["rendered"], "pre")
    assert pre is not None
    assert pre["text"] == payload["content"], (
        "a script job with an empty response must fall back to the "
        "raw artifact — the pre-9/24 gate did not check job mode"
    )

    # The other half: an agent run with an empty response body must
    # also stay raw, otherwise the user sees a blank primary block.
    empty_payload = {
        "content": "---\nrun_id: empty\n---\n\n## Response\n\n   \n",
        "snippet": "(empty)",
        "parsed": {
            "response": "",
            "context": "---\nrun_id: empty\n---\n",
            "has_response_boundary": True,
            "response_line": 4,
        },
        "usage": None,
    }
    out = _render(empty_payload, job=AGENT_JOB, expanded=True)
    assert _entry(out["rendered"], "response-block") is None, (
        "an empty agent response must not enable response-first — the "
        "pre-9/24 gate checked has_response_boundary only, not the body"
    )
    pre = _entry(out["rendered"], "pre")
    assert pre is not None
    assert pre["text"] == empty_payload["content"], (
        "an empty-response run must fall back to the raw artifact"
    )


# ---------------------------------------------------------------------------
# Finding 4 — SILENT 4: reply past line 2,000 keeps its boundary
# ---------------------------------------------------------------------------


def test_finding_4_silent_reply_past_line_2000_keeps_boundary():
    """SILENT 4: a ``_MAX_PROBE_LINES`` cap of 2,000 stopped the scan
    before the boundary, so a real run whose reply landed on line 2,001
    came back ``has_response_boundary=False`` and the collapsed preview
    regressed to the first 600 chars of front-matter. The cap is gone;
    the whole already-read artifact is scanned, with the fail-closed
    guards (fence + exact heading match) doing the work the cap used
    to do.

    This test pins the exact probe the review used: 2,000 filler
    lines, the heading on line 2,001, the reply on the line that
    follows. The projection must still find the boundary and the
    response must be the reply, not the front-matter.
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
        "pre-9/24 _MAX_PROBE_LINES=2000 cap dropped it and the preview "
        "showed front-matter instead"
    )
    assert projection.response_line == heading_line
    assert projection.response.startswith("All 12 nodes are healthy")
    assert "run_id: 2001-lines" in projection.context

    # The cap constant must be gone entirely — guards against a future
    # "let's just raise the cap" regression that hides the real
    # problem (a probe range is not a security boundary).
    import api.cron_output_parser as parser
    assert not hasattr(parser, "_MAX_PROBE_LINES"), (
        "the line-count probe cap must be removed, not raised — a "
        "reply past any fixed cap regresses the preview to front-matter"
    )


# ---------------------------------------------------------------------------
# Finding 5 — SILENT 5: adjacent tags keep the block open
# ---------------------------------------------------------------------------


def test_finding_5_silent_adjacent_tags_keep_block_open():
    """SILENT 5: ``<pre>one</pre><code>`` on one line was processed by
    counting *all* close tags before *any* open tag, so the trailing
    ``<code>`` was dropped from the depth and the HTML block closed
    one tag early. A ``## Response`` on the next line (inside the
    still-open ``<code>``) was then accepted as the boundary. The fix
    walks the tags in token order: a close that precedes an open on
    the same line does not pre-count against it.

    This test pins the exact probe the review used. The fake heading
    inside the still-open ``<code>`` must NOT be the boundary — the
    real response is the one after the ``</code>``.
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


# ---------------------------------------------------------------------------
# Finding 6 — SILENT 6: no-boundary preview is trimmed before the slice
# ---------------------------------------------------------------------------


def test_finding_6_silent_no_boundary_preview_is_trimmed():
    """SILENT 6: ``response_snippet`` sliced the raw body without
    trimming first, so an artifact with 610 leading spaces produced a
    600-space (blank) preview. The row looked like it had no output
    at all. The fix restores ``.strip()`` before the slice.

    This test pins the exact probe the review used: 610 leading
    spaces followed by real content. The preview must contain the
    real content, not 600 spaces.
    """
    text = (" " * 610) + "front-matter: real content starts here\n"
    snippet = response_snippet(text, limit=600)
    assert snippet.strip() != "", (
        "a preview made entirely of leading spaces is blank — the "
        ".strip() before the slice was lost"
    )
    assert snippet.startswith("front-matter:"), snippet[:40]
    assert len(snippet) <= 600
