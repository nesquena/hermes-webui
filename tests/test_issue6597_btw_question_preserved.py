"""A refused ``/btw`` must keep the question in the composer (#6597, PR #6613).

Re-review 2026-11-23T04:13:26Z: the busy branch in ``static/messages.js`` clears
the composer before calling ``cmdBtw`` and the real ``/api/btw`` route answers
``409 "session already has an active stream"`` whenever the parent stream is
live, so the typed question was lost. The accepted resolution ships
``/background`` alone in the busy intercept and requires ``/btw`` to preserve the
question on failure, "guarded against overwriting newer composer input or
another session's draft".

This module drives the shipped ``cmdBtw`` and its ``/btw`` restore helper
(``static/commands.js``) against
recorded route results — the real 409 rejection shape and a 200 ``{error}``
body — with the shipped ``_restoreComposerDraftAfterFailedSend``
(``static/messages.js``) as the only composer writer. Nothing about the restore
rule is restated here: the guards under test are the production ones.

Cases: two failures that must restore, plus three controls that must not
clobber (newer typed input, a different visible session, and the success path).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
COMMANDS_JS = ROOT / "static" / "commands.js"
MESSAGES_JS = ROOT / "static" / "messages.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

QUESTION = "why is the sky blue"
COMMAND_TEXT = f"/btw {QUESTION}"
BUSY_MESSAGE = "session already has an active stream"

RUNNER = r"""
const vm = require('vm');
const CMD_SRC = __CMD__;
const HELPER_SRC = __HELPER__;
const RESTORE_SRC = __RESTORE__;
const SCENARIOS = __SCENARIOS__;

async function runScenario(sc) {
  const rec = {toasts: [], drafts: [], attached: [], composerAfter: null};
  const composer = {value: sc.composer};
  const S = {
    busy: false,
    session: {session_id: sc.visibleSid},
    pendingFiles: [],
    activeProfile: 'default',
  };
  const ctx = {
    console: console,
    S: S,
    $: function (sel) { return sel === 'msg' ? composer : null; },
    api: async function () {
      // The user can switch chats while the request is in flight: the composer
      // then belongs to the session they moved to, not to the one that asked.
      if (sc.switchTo) {
        S.session.session_id = sc.switchTo;
        composer.value = sc.composerAfterSwitch || '';
      }
      if (sc.fail === 'reject') {
        const err = new Error(sc.message || 'failed');
        err.status = sc.status || 409;
        throw err;
      }
      return sc.result;
    },
    showToast: function (msg) { rec.toasts.push(String(msg)); },
    t: function (k) { return String(k); },
    autoResize: function () {},
    updateSendBtn: function () {},
    renderTray: function () {},
    attachBtwStream: function (parentSid, streamId, question) {
      rec.attached.push([parentSid, streamId, question]);
    },
    _normalizeGoalContinuationId: function (v) { return String(v || '').trim(); },
    _setRestoredGoalContinuationDraft: function () {},
    _clearRestoredGoalContinuationDraft: function () {},
    _saveComposerDraftNow: function (sid, text, files) {
      rec.drafts.push([sid, text, Array.isArray(files) ? files.slice() : []]);
    },
    _handleComposerDraftPostFailure: function () {},
  };
  vm.createContext(ctx);
  vm.runInContext(RESTORE_SRC, ctx);
  vm.runInContext(HELPER_SRC, ctx);
  vm.runInContext(CMD_SRC, ctx);
  await ctx.cmdBtw(sc.args);
  rec.composerAfter = composer.value;
  return rec;
}

(async function () {
  const out = {};
  for (const sc of SCENARIOS) out[sc.id] = await runScenario(sc);
  console.log(JSON.stringify(out));
})().catch(function (e) { console.error(String(e && e.stack || e)); process.exit(1); });
"""


def _scenarios() -> list[dict]:
    common = {"args": QUESTION, "visibleSid": "sid-1", "composer": ""}
    return [
        # Defect: the real 409 from a live parent stream, composer already cleared.
        dict(common, id="refused_409_restores_the_question", fail="reject", status=409,
             message=BUSY_MESSAGE),
        # Defect: the route can also answer 200 with an error body.
        dict(common, id="error_body_restores_the_question", fail="result",
             result={"error": "gateway backend"}),
        # Control: newer input typed while the request was in flight is authoritative.
        dict(common, id="newer_input_is_not_overwritten", fail="reject", status=409,
             message=BUSY_MESSAGE, composer="a newer draft"),
        # Control: the user switched chats mid-request — never inject the question into
        # the session they moved to; the asking session keeps its own draft.
        dict(common, id="another_visible_session_is_not_touched", fail="reject", status=409,
             message=BUSY_MESSAGE, switchTo="sid-2"),
        # Control: success keeps the composer clear and streams the answer.
        dict(common, id="success_does_not_restore", fail="result",
             result={"stream_id": "stream-9", "parent_session_id": "sid-1"}),
    ]


def _run_harness(tmp_path: Path) -> dict:
    cmd_src = extract_function(
        COMMANDS_JS.read_text(encoding="utf-8"), "cmdBtw", prefix="async function"
    )
    helper_src = extract_function(
        COMMANDS_JS.read_text(encoding="utf-8"), "_restoreBtwQuestionAfterFailure"
    )
    restore_src = extract_function(
        MESSAGES_JS.read_text(encoding="utf-8"), "_restoreComposerDraftAfterFailedSend"
    )
    script = (
        RUNNER.replace("__CMD__", json.dumps(cmd_src))
        .replace("__HELPER__", json.dumps(helper_src))
        .replace("__RESTORE__", json.dumps(restore_src))
        .replace("__SCENARIOS__", json.dumps(_scenarios()))
    )
    path = tmp_path / "btw_question_6597.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> dict:
    return _run_harness(tmp_path_factory.mktemp("btw_question_6597"))


@pytest.mark.parametrize("case", ["refused_409_restores_the_question", "error_body_restores_the_question"])
def test_a_refused_btw_keeps_the_question_in_the_composer(harness, case):
    rec = harness[case]
    assert rec["attached"] == [], f"{case}: a failed request must not attach a stream"
    assert rec["toasts"], f"{case}: the refusal must still be reported"
    assert rec["composerAfter"] == COMMAND_TEXT, f"{case}: the question was lost"
    assert rec["drafts"] == [["sid-1", COMMAND_TEXT, []]], (
        f"{case}: the preserved question must be the owning session's draft"
    )


def test_newer_composer_input_wins_over_the_restore(harness):
    rec = harness["newer_input_is_not_overwritten"]
    assert rec["composerAfter"] == "a newer draft"
    assert rec["attached"] == []


def test_a_different_visible_session_is_never_given_the_question(harness):
    rec = harness["another_visible_session_is_not_touched"]
    assert rec["composerAfter"] == "", "restore leaked into another session's composer"
    assert rec["attached"] == []
    # The owning session still keeps its copy: the draft is persisted for sid-1.
    assert rec["drafts"] == [["sid-1", COMMAND_TEXT, []]]


def test_a_successful_btw_still_streams_and_leaves_the_composer_empty(harness):
    rec = harness["success_does_not_restore"]
    assert rec["attached"] == [["sid-1", "stream-9", QUESTION]]
    assert rec["composerAfter"] == ""
    assert rec["drafts"] == []
