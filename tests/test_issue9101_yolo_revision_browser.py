"""#9101 follow-up: the browser orders YOLO answers by the server's per-session
revision, sends a conditional /yolo, and drops answers from a server run it
has already moved past.

The server side (#9193) already answers GET/POST /api/session/yolo with
{yolo_enabled, yolo_revision, yolo_epoch} and rejects a conditional POST with
409 yolo_changed carrying the current state. This module pins the client
contract that consumes it:

  a. ordering  — a read answered at an older revision than the view holds is
                 a late answer and must not roll the flag back; an answer from
                 a run this tab already confirmed and moved past is dropped,
                 while an answer naming a run no answer has named yet (the
                 tab requested across a restart) is the server's current
                 state and is accepted.
  b. conditional— /yolo and the pill send expected_revision (the revision the
                 toggle was computed from); a 409 yolo_changed refreshes the
                 view from the payload's current state instead of failing
                 blind or toggling again. expected_epoch is deliberately NOT
                 sent: it would reject a legitimate post-restart disable
                 (pinned by r8b in the pill-authority suite).
  c. confirm   — an applied explicit write is followed by a fire-and-forget
                 read, because the write's answer was computed when the server
                 handled it and another client may have moved the flag since.

The behavioural evidence lives in tests/test_issue9101_yolo_pill_session_authority.py
(the scenarios that were strict xfails on master: r2/r2b/r2c/r2e revision
ordering, r7 conditional /yolo, r8c/r8d/r9b/r11c/r12 run/epoch handling); the
source pins below keep the wiring from silently regressing.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "issue9101_yolo_pill_harness.cjs"
STATIC = ROOT / "static"
NODE = shutil.which("node")


def _read(name):
    return (STATIC / name).read_text(encoding="utf-8")


def _function_body(src, name):
    match = re.search(r"^(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src, re.M)
    assert match, f"{name} not found"
    end = src.find("\n}\n", match.start())
    assert end > 0, f"{name} has no column-zero close"
    return src[match.start():end + 2]


def test_yolo_view_tracks_the_server_revision_and_run():
    """The view carries the answer's revision and server run, and remembers
    every run it confirmed so a late old-run answer can be recognised."""
    src = _read("messages.js")
    assert "let _yoloView = {sid: null, enabled: false, gen: 0, seq: 0, rev: 0, epoch: null, reqGen: 0};" in src
    assert "let _yoloKnownRuns = new Set();" in src
    body = _function_body(src, "_applyYoloState")
    # Same run: an older revision never rolls the view back — for a read AND
    # for a write (a write answer is not covered by the read sequence rule).
    assert (
        "if (aRun && _yoloView.epoch === aRun && aRev !== null && aRev < _yoloView.rev) return rejectAnswer();"
        in body
    )
    # Another run: only one this tab has NOT confirmed is the current state.
    assert "if (_yoloKnownRuns.has(aRun)) return rejectAnswer();" in body
    # A session install keeps the confirmed run and restarts the revision.
    assert "rev: 0, epoch: _yoloView.epoch, reqGen: _yoloView.reqGen};" in body
    assert "if (aRun) _yoloKnownRuns.add(aRun);" in body


def test_a_rejected_write_answer_still_confirms_the_server_state():
    """Dropping a stale WRITE answer must not skip the confirmation read that
    follows the write: the write settled, so the flag may have moved on since
    the answer was computed. A rejected READ needs no confirmation."""
    body = _function_body(_read("messages.js"), "_applyYoloState")
    assert "const rejectAnswer = () => {" in body
    assert (
        "if (!isRead && typeof _fetchYoloState === 'function') _fetchYoloState(sid, {gen});"
        in body
    )
    assert "return false;" in body


def test_yolo_answers_are_ordered_by_the_request_that_produced_them():
    """A late answer must not replace what a NEWER request confirmed: every
    YOLO request takes a generation when it is SENT, and the answer carries
    it back. Two requests sent around a server restart can be delivered in
    any order — the answer of the request sent FIRST is the older one."""
    src = _read("messages.js")
    assert "let _yoloReqGen = 0;" in src
    assert "function _nextYoloReqGen() {" in src
    body = _function_body(src, "_applyYoloState")
    # An answer of an OLDER request is a leftover of the previous run.
    assert (
        "if (aReq !== null && _yoloView.reqGen > 0 && aReq < _yoloView.reqGen) {"
        in body
    )
    # The read path takes its generation when the request is sent.
    read = _function_body(src, "_fetchYoloState")
    assert "reqGen: ++_yoloReqGen," in read
    # Every write path takes one too: /yolo, the pill's disable and the
    # approval card's projection.
    cmd = _function_body(_read("commands.js"), "cmdYolo")
    pill = _function_body(_read("commands.js"), "disableYoloFromPill")
    assert "const reqGen=(typeof _nextYoloReqGen==='function')?_nextYoloReqGen():null;" in cmd
    assert "const reqGen=(typeof _nextYoloReqGen==='function')?_nextYoloReqGen():null;" in pill
    assert "reqGen:reqGen," in cmd
    assert "reqGen:reqGen," in pill
    # The card takes its generation when its request is SENT, not when its
    # answer arrives, and hands it to the projection.
    respond = _function_body(src, "respondApproval")
    assert "const reqGen=options.yolo&&typeof _nextYoloReqGen==='function'?_nextYoloReqGen():null;" in respond
    card = _function_body(src, "_applyApprovalYoloProjection")
    assert "reqGen: typeof reqGen === \"number\" ? reqGen : null," in card


def test_yolo_write_answer_is_confirmed_by_a_follow_up_read():
    """An applied explicit write re-reads the flag: its answer was computed
    when the server handled it and another client may have moved it since."""
    body = _function_body(_read("messages.js"), "_applyYoloState")
    assert "if (!isRead && typeof _fetchYoloState === 'function') _fetchYoloState(sid, {gen});" in body


def test_fetch_yolo_state_threads_revision_and_run_into_the_view():
    """The read path passes the answer's revision/run to the single writer."""
    body = _function_body(_read("messages.js"), "_fetchYoloState")
    assert "meta.rev = typeof data.yolo_revision === 'number' ? data.yolo_revision : null;" in body
    assert "meta.epoch = typeof data.yolo_epoch === 'string' ? data.yolo_epoch : null;" in body


def test_yolo_command_posts_the_revision_it_computed_the_toggle_from():
    """/yolo sends expected_revision taken from the status read's answer (the
    state the toggle was computed from), not from a stale view."""
    body = _function_body(_read("commands.js"), "cmdYolo")
    assert "const expectedRevision=typeof status.yolo_revision==='number'?status.yolo_revision" in body
    assert "body:JSON.stringify({session_id:sid,enabled:enable,expected_revision:expectedRevision})" in body
    # expected_epoch is intentionally absent: it would reject a legitimate
    # post-restart disable (r8b pins that end state).
    assert "expected_epoch" not in body


def test_pill_disable_posts_its_expected_revision():
    """The pill's disable is conditional too, from the revision the visible
    pill was painted from."""
    body = _function_body(_read("commands.js"), "disableYoloFromPill")
    assert "const expectedRevision=(_yoloView.sid===sid&&typeof _yoloView.rev==='number')?_yoloView.rev:0;" in body
    assert "body:JSON.stringify({session_id:sid,enabled:false,expected_revision:expectedRevision})" in body
    assert "expected_epoch" not in body


def test_conflict_payload_refreshes_the_view_instead_of_failing_blind():
    """A 409 yolo_changed carries the server's CURRENT state; both write
    paths apply it (revision/run included) rather than dropping the answer."""
    cmd = _function_body(_read("commands.js"), "cmdYolo")
    pill = _function_body(_read("commands.js"), "disableYoloFromPill")
    for body in (cmd, pill):
        applied = re.search(
            r"_applyYoloState\(sid,errorPayload\.yolo_enabled,\{(.*?)\}\)",
            body,
            re.S,
        )
        assert applied, "no errorPayload application in the write path"
        meta = applied.group(1)
        assert "errorPayload.yolo_revision" in meta
        assert "errorPayload.yolo_epoch" in meta
    # The pill never re-toggles on a conflict: no second POST is composed.
    assert body.count("/api/session/yolo',{") == 1


@pytest.mark.skipif(NODE is None, reason="node required")
@pytest.mark.parametrize(
    "name",
    [
        # (a) revision ordering: an older write answer never overrides a newer
        # change; the write's follow-up confirmation read carries it.
        "r2_older_write_answer_never_overrides_newer_frame",
        # (b) conditional /yolo: a stale read cannot re-enable after the pill
        # disabled (the 409 refreshes the view from the current state).
        "r7_stale_yolo_read_cannot_undo_later_pill_disable",
        # (c) run/epoch: late answers from a run the tab moved past are
        # dropped instead of overriding the current run's answer.
        "r11c_old_process_answers_keep_their_revision_order",
        # (d) a late answer of an OLDER request must not replace what a newer
        # request confirmed (the first sync overlapping a restart).
        "r22_late_old_process_answer_never_blocks_the_current_process",
        # (e) an older write answer must not roll back the confirmed revision.
        "r23_older_write_answer_never_rolls_back_a_confirmed_revision",
        # (f) a rejected older write answer must still confirm the server.
        "r24_rejected_older_write_answer_still_confirms_the_server_state",
    ],
)
def test_revision_browser_scenarios_hold(name):
    proc = subprocess.run(
        [NODE, str(DRIVER), name, str(ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = proc.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1])
    except (IndexError, ValueError):
        out = {"raw_stdout": proc.stdout, "stderr": proc.stderr}
    assert proc.returncode == 0, f"{out.get('message')}\n{json.dumps(out.get('detail'), indent=1)}"
