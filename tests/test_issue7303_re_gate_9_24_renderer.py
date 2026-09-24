"""#7303 re-gate 9/24 — behavioural tests for the cron run renderer.

The source-presence assertions in ``test_issue2661_2629_frontend.py``
pin the *shape* of ``_renderCronRunBody`` / ``toggleCronRunExpanded``
but pass even when a runtime detail is wrong. This module drives the
real functions from ``static/panels.js`` through a Node harness
(``tests/_cron_run_body_driver.js``) against a simulated DOM, so each
finding from the 9/24 maintainer review is asserted on actual rendered
output:

CORE 1  — expanded response-first runs must keep a "View raw output"
          control that mounts ``data.content`` verbatim (the projection
          drops the ``## Response`` heading and trims both sides, so the
          response block + context disclosure cannot rebuild the file).
CORE 2  — clicking the expand toggle while the run fetch is still
          pending must NOT close the row (the fallback ``_loadRunContent``
          call treated the already-open row as a collapse request).
SILENT 3— a script (``no_agent``) job whose stdout contains
          ``## Response`` must stay on the raw view, and an empty agent
          response must not render a blank primary block.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = REPO_ROOT / "static" / "panels.js"
DRIVER_JS = Path(__file__).parent / "_cron_run_body_driver.js"

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_driver(scenario: dict) -> dict:
    """Run the panels.js cron run renderer against a scenario."""
    result = subprocess.run(
        [NODE, str(DRIVER_JS), str(PANELS_JS), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed: {result.stderr}")
    return json.loads(result.stdout)


# A realistic /api/crons/run payload for an agent run with a recognized
# response boundary, where the raw artifact carries front-matter and a
# prompt section that the projection strips.
RESPONSE_FIRST_PAYLOAD = {
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
    """Render a run body for *job* at the given expansion state."""
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


def _kinds(rendered):
    return [entry["kind"] for entry in rendered]


def _entry(rendered, kind):
    for entry in rendered:
        if entry["kind"] == kind:
            return entry
    return None


# ── CORE 1 — the raw artifact stays reachable ────────────────────────


def test_expanded_response_first_run_has_view_raw_output_control():
    """CORE 1: the expanded response-first view must offer a control that
    renders ``data.content`` as literal text. Without it the original
    artifact is unrecoverable: the projection dropped the
    ``## Response`` heading and trimmed both sides."""
    out = _render(RESPONSE_FIRST_PAYLOAD, job=AGENT_JOB, expanded=True)
    button = _entry(out["rendered"], "button")
    assert button is not None, (
        "expanded response-first runs must keep a raw-output control; "
        f"got kinds {_kinds(out['rendered'])}"
    )
    assert button["label"].lower().find("raw output") >= 0, button


def test_view_raw_output_renders_verbatim_content_including_heading():
    """CORE 1: clicking the control must mount the entire artifact — the
    ``## Response`` heading included — as literal preformatted text."""
    out = _run_driver(
        {
            "mode": "render-then-click-raw",
            "jobId": AGENT_JOB["id"],
            "filename": "2026-09-24_120000.md",
            "expanded": True,
            "currentCronDetail": AGENT_JOB,
            "payload": RESPONSE_FIRST_PAYLOAD,
        }
    )
    assert out["hasRawButton"] is True
    pre = _entry(out["afterRawClick"], "pre")
    assert pre is not None, out["afterRawClick"]
    assert pre["text"] == RESPONSE_FIRST_PAYLOAD["content"], (
        "the raw control must render data.content verbatim so the "
        "artifact can be reconstructed exactly"
    )
    # And the response view is restorable without another fetch.
    assert out["hasBackButton"] is True
    back_pre = _entry(out["afterBackClick"], "response-block")
    assert back_pre is not None, out["afterBackClick"]
    assert "All 12 nodes are healthy" in back_pre["text"]


def test_collapsed_response_first_run_keeps_snippet_only():
    """The collapsed state is unchanged by the fix: snippet, no raw
    control (the row is not expanded, so there is nothing to expand)."""
    out = _render(RESPONSE_FIRST_PAYLOAD, job=AGENT_JOB, expanded=False)
    pre = _entry(out["rendered"], "pre")
    assert pre is not None
    assert pre["text"] == RESPONSE_FIRST_PAYLOAD["snippet"]
    assert _entry(out["rendered"], "button") is None, (
        "the raw control belongs to the expanded view only"
    )


# ── CORE 2 — pending fetch must not collapse the row ─────────────────


def test_toggle_while_fetch_pending_keeps_row_open():
    """CORE 2: on a cache miss the toggle used to fall back to
    ``_loadRunContent()``, which saw the row already ``.open`` and
    treated the click as a collapse request — closing the row the user
    just opened."""
    out = _run_driver(
        {
            "mode": "toggle-pending",
            "jobId": AGENT_JOB["id"],
            "filename": "2026-09-24_120000.md",
            "pendingFetch": True,
            "resolveFetch": True,
            "currentCronDetail": AGENT_JOB,
            "payload": RESPONSE_FIRST_PAYLOAD,
        }
    )
    assert out["openAfterLoad"] is True, "opening the row must mark it open"
    assert out["openAfterToggle"] is True, (
        "clicking expand while the fetch is pending must not close the row"
    )
    assert out["storedExpanded"] is True, (
        "the toggle must persist the expanded state"
    )
    assert out["fetchAfterToggle"] == out["fetchAfterLoad"], (
        "the toggle must not fire a second fetch while one is pending"
    )


def test_pending_fetch_resolves_into_the_expanded_state():
    """CORE 2: dropping the fallback is only safe because the pending
    fetch reads the current expansion state when it resolves — pin that
    the resolved render honours the state the toggle set."""
    out = _run_driver(
        {
            "mode": "toggle-pending",
            "jobId": AGENT_JOB["id"],
            "filename": "2026-09-24_120000.md",
            "pendingFetch": True,
            "resolveFetch": True,
            "currentCronDetail": AGENT_JOB,
            "payload": RESPONSE_FIRST_PAYLOAD,
        }
    )
    assert _entry(out["rendered"], "response-block") is not None, (
        "the pending fetch must render the response view when it resolves"
    )
    raw = _entry(out["rendered"], "button")
    assert raw is not None and raw["label"].lower().find("raw output") >= 0, (
        "the resolved expanded view must include the raw-output control"
    )


# ── SILENT 3 — script jobs and empty responses stay raw ──────────────


def test_script_job_with_response_heading_stays_raw_view():
    """SILENT 3: a script job whose stdout happens to contain
    ``## Response`` must keep the established raw view. Its mode is
    known from the job, not guessed from the artifact text."""
    payload = {
        "content": (
            "starting backup\n"
            "## Response\n"
            "3 files copied\n"
            "backup finished in 4.2s\n"
        ),
        "snippet": "3 files copied\nbackup finished in 4.2s\n",
        "parsed": {
            "response": "3 files copied\nbackup finished in 4.2s\n",
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
        "the script job must keep the verbatim raw view"
    )


def test_empty_agent_response_does_not_render_blank_primary_block():
    """SILENT 3: a boundary with an empty response body is not a reply.
    Rendering it response-first shows a blank primary block, so the raw
    view must be used instead."""
    payload = {
        "content": "---\nrun_id: empty\n---\n\n## Response\n\n   \n",
        "snippet": "(empty)",
        "parsed": {
            "response": "",
            "context": "---\nrun_id: empty\n---",
            "has_response_boundary": True,
            "response_line": 4,
        },
        "usage": None,
    }
    out = _render(payload, job=AGENT_JOB, expanded=True)
    assert _entry(out["rendered"], "response-block") is None, (
        "an empty agent response must not enable response-first"
    )
    pre = _entry(out["rendered"], "pre")
    assert pre is not None
    assert pre["text"] == payload["content"], (
        "an empty-response run must fall back to the raw artifact"
    )


def test_agent_run_with_actual_response_still_renders_response_first():
    """The other half of the SILENT 3 contract: gating on script mode and
    non-empty response text must not disable response-first for real
    agent replies."""
    out = _render(RESPONSE_FIRST_PAYLOAD, job=AGENT_JOB, expanded=True)
    response = _entry(out["rendered"], "response-block")
    assert response is not None, (
        "an agent run with a real response must still render response-first"
    )
    assert "All 12 nodes are healthy" in response["text"]
