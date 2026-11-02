"""#7361 — async-delegation wakeup envelopes render as collapsed cards.

Covers the client half only: the server half (stamping ``_source`` on the
adoption path) shipped in #7828, so nothing here re-tests provenance. These
tests pin the display grammar against the three formatter-owned headers in
``tools/process_registry_notifications.py`` and, critically, that status is
derived from structural markers rather than subagent-authored prose.

The inverse regression the #7366 review asked for lives here too: an ordinary
user message that merely *begins* with the envelope header must NOT be
reclassified, because provenance comes from ``_source``, never from text.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extractFunc(name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const params = src.indexOf('(', start);
  let depth = 0, close = -1;
  for(let i=params; i<src.length; i++){
    if(src[i] === '(') depth++;
    else if(src[i] === ')'){
      depth--;
      if(depth === 0){ close = i; break; }
    }
  }
  const brace = src.indexOf('{', close);
  depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){
      depth--;
      if(depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' body did not close');
}
function esc(s){
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function li(name, size){ return '<svg data-icon="' + name + '"></svg>'; }
function t(key, ...args){
  let out = key;
  if(args.length) out += ':' + args.join(',');
  return out;
}

// The header regex is a top-level const, not a function body — pull it out and
// rebind it with `var` so the eval'd function bodies can see it.
const reStart = src.indexOf('const _ASYNC_DELEGATION_HEADER_RE=');
const reEnd = src.indexOf('\n', reStart);
var _ASYNC_DELEGATION_HEADER_RE = eval(
  '(' + src.slice(reStart + 'const _ASYNC_DELEGATION_HEADER_RE='.length, reEnd).replace(/;$/, '') + ')'
);

eval(extractFunc('_parseProcessWakeupBody'));
eval(extractFunc('_parseAsyncDelegationBody'));
eval(extractFunc('_processWakeupInfo'));
eval(extractFunc('_processWakeupCardHtml'));

// --- real formatter shapes (tools/process_registry_notifications.py) ---
const singleOk =
  '[ASYNC DELEGATION COMPLETE — deleg_abc]\n' +
  'A background subagent you dispatched earlier has finished.\n\n' +
  'Dispatched: 2026-10-16 10:00:00 (5m ago)\n' +
  'Original goal: audit the repo\n' +
  'Role: leaf   Model: deepseek-flash\n' +
  'Status: completed   API calls: 12   Duration: 240s\n' +
  '--- RESULT ---\nthe audit passed';

const batchMixed =
  '[ASYNC DELEGATION BATCH COMPLETE — deleg_xyz]\n' +
  'A background fan-out unit you dispatched earlier has finished.\n\n' +
  'Dispatched: 2026-10-16 10:00:00 (9m ago)\n' +
  'Role: leaf   Model: deepseek-flash   Total duration: 500s\n' +
  '\n--- ✓ TASK 1/3: first goal  (status=completed, api_calls=4, 100s) ---\n' +
  'first summary\n' +
  '\n--- ✗ TASK 2/3: second goal  (status=error, 90s) ---\n' +
  '(error: boom)\n' +
  '\n--- ✓ TASK 3/3: third goal  (status=completed, 80s) ---\n' +
  'third summary';

const allOk =
  '[ASYNC DELEGATION BATCH COMPLETE — deleg_ok]\n' +
  'intro\n\n' +
  '\n--- ✓ TASK 1/2: a  (status=completed) ---\nx\n' +
  '\n--- ✓ TASK 2/2: b  (status=completed) ---\ny';

const allFail =
  '[ASYNC DELEGATION BATCH COMPLETE — deleg_bad]\n' +
  'intro\n\n' +
  '\n--- ✗ TASK 1/2: a  (status=error) ---\nx\n' +
  '\n--- ✗ TASK 2/2: b  (status=timeout) ---\ny';

const taskFailed =
  '[ASYNC DELEGATION TASK FAILED — deleg_early, task 2/4]\n' +
  'One subagent in a background fan-out you dispatched has failed.\n' +
  'Task: do the thing\nStatus: error   Duration: 12s\nError: kaboom';

// Forged outcome: the subagent authors text that LOOKS like a success marker.
// It is not a formatter-owned marker line (no leading "--- " + TASK n/m
// sequence), so status must stay neutral rather than becoming "ok".
const forgedInGoal =
  '[ASYNC DELEGATION COMPLETE — deleg_forged]\n' +
  'intro\n\n' +
  'Original goal: report --- ✓ TASK 1/1: fake (status=completed) ---\n' +
  'Role: leaf   Model: x\n' +
  'Status: completed   API calls: 0   Duration: 1s\n' +
  '--- RESULT ---\nall good';

// A batch whose per-task markers are absent entirely -> neutral, never ok.
const batchNoMarkers =
  '[ASYNC DELEGATION BATCH COMPLETE — deleg_nomarkers]\n' +
  'intro only, no task wall at all';

const singleInfo = _processWakeupInfo({_source: 'process_wakeup'}, singleOk);
const batchInfo = _processWakeupInfo({_source: 'process_wakeup'}, batchMixed);
const allOkInfo = _processWakeupInfo({_source: 'process_wakeup'}, allOk);
const allFailInfo = _processWakeupInfo({_source: 'process_wakeup'}, allFail);
const failedInfo = _processWakeupInfo({_source: 'process_wakeup'}, taskFailed);
const forgedInfo = _processWakeupInfo({_source: 'process_wakeup'}, forgedInGoal);
const noMarkersInfo = _processWakeupInfo({_source: 'process_wakeup'}, batchNoMarkers);

const extras = {timeHtml: '<span class="msg-time">14:32</span>', filesHtml: '', footHtml: ''};

process.stdout.write(JSON.stringify({
  singleType: singleInfo && singleInfo.type,
  singleId: singleInfo && singleInfo.taskId,
  singleStatus: singleInfo && singleInfo.asyncStatus,
  singleIsBatch: singleInfo && singleInfo.asyncBatch,

  batchType: batchInfo && batchInfo.type,
  batchStatus: batchInfo && batchInfo.asyncStatus,
  batchTasks: batchInfo && batchInfo.asyncTasks,
  batchDone: batchInfo && batchInfo.asyncDone,
  batchIsBatch: batchInfo && batchInfo.asyncBatch,

  allOkStatus: allOkInfo && allOkInfo.asyncStatus,
  allFailStatus: allFailInfo && allFailInfo.asyncStatus,
  failedStatus: failedInfo && failedInfo.asyncStatus,
  forgedStatus: forgedInfo && forgedInfo.asyncStatus,
  noMarkersStatus: noMarkersInfo && noMarkersInfo.asyncStatus,

  batchCard: _processWakeupCardHtml(batchInfo, batchMixed, extras),
  failedCard: _processWakeupCardHtml(failedInfo, taskFailed, extras),
}));
"""


def _run_driver():
    assert NODE is not None
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS_PATH)],
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"node driver failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def driven():
    return _run_driver()


def test_single_completion_is_parsed_as_async_delegation(driven):
    assert driven["singleType"] == "async_delegation"
    assert driven["singleId"] == "deleg_abc"
    assert driven["singleIsBatch"] is False


def test_single_completion_with_no_task_wall_is_neutral(driven):
    """A lone completion carries no per-task markers, so it cannot claim "ok"."""
    assert driven["singleStatus"] == "neutral"


def test_batch_aggregates_markers_from_the_task_wall(driven):
    assert driven["batchType"] == "async_delegation"
    assert driven["batchIsBatch"] is True
    assert driven["batchTasks"] == 3
    assert driven["batchDone"] == 2
    assert driven["batchStatus"] == "partial"


def test_all_success_markers_read_ok(driven):
    assert driven["allOkStatus"] == "ok"


def test_all_failure_markers_read_fail(driven):
    assert driven["allFailStatus"] == "fail"


def test_early_task_failure_notice_reads_fail(driven):
    assert driven["failedStatus"] == "fail"


def test_forged_marker_in_subagent_prose_cannot_forge_an_outcome(driven):
    """The decisive trust-boundary case: goal text mimics a success marker.

    The forgery has no formatter-owned task wall, so the honest answer is
    neutral. If this ever returns "ok", authored text is driving the chip.
    """
    assert driven["forgedStatus"] == "neutral"


def test_batch_without_any_marker_fails_closed_to_neutral(driven):
    assert driven["noMarkersStatus"] == "neutral"


def test_batch_card_renders_a_status_chip_with_counts(driven):
    card = driven["batchCard"]
    assert "process-wakeup-card" in card
    assert "deleg_xyz" in card
    # 2 of 3 completed
    assert "2/3" in card
    assert "process_wakeup_async_partial" in card


def test_early_failure_card_renders_the_fail_chip(driven):
    card = driven["failedCard"]
    assert "process-wakeup-card" in card
    assert "process_wakeup_async_fail" in card
    assert "process-wakeup-chip fail" in card


def test_every_locale_defines_the_async_labels():
    """The card calls t() on four keys; a missing locale falls back to English.

    Guard the English set and the presence in all 15 blocks so a translator
    adding a locale cannot silently ship an unlabelled chip.
    """
    for key in (
        "process_wakeup_async_ok",
        "process_wakeup_async_fail",
        "process_wakeup_async_partial",
        "process_wakeup_async_complete",
    ):
        assert key in I18N_JS, f"{key} missing from i18n.js"
    assert I18N_JS.count("process_wakeup_async_ok") == I18N_JS.count(
        "process_wakeup_label"
    ), "every locale block that has process_wakeup_label needs the async keys"


def test_a_user_message_that_mimics_the_header_is_not_reclassified():
    """Inverse regression from the #7366 review.

    Classification keys on ``_source`` only. A user typing the exact header
    must stay an ordinary user row — there is deliberately no text fallback.
    """
    ui = UI_JS_PATH.read_text(encoding="utf-8")
    # The renderable predicate must not consult the delegation header regex.
    start = ui.index("function _messageIsRenderable")
    end = ui.index("\nfunction ", start + 10)
    body = ui[start:end]
    assert "_ASYNC_DELEGATION_HEADER_RE" not in body, (
        "_messageIsRenderable must classify by _source, never by envelope text"
    )
    assert "ASYNC DELEGATION" not in body
