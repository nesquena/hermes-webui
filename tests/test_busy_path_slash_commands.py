"""Regression test for the busy-path slash-command allowlist (#6597, PR #6613).

The Oct 1 review asked for a harness around the **real** busy branch instead of a
test that copies the allowlist and the routing into the test file. So this module
loads the shipped ``send()`` from ``static/messages.js`` and the shipped
``parseCommand()`` from ``static/commands.js`` into a fresh ``vm`` context per
scenario and drives that production code. The only stubs are UI/dependency seams
(composer element, ``COMMANDS`` handler recorders, queue/steer/cancel recorders).

Contract under test: while the agent is busy (or a manual compression is running)
every busy-control command listed in the intercept must execute its handler once,
with the parsed argument, clearing the composer, and must NOT reach steer, queue or
cancel. Commands outside that list must keep normal routing. Reverting the
allowlist entry or the ``return`` in the intercept turns the allowlisted cases into
queued/steered text, which these assertions reject.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = ROOT / "static" / "messages.js"
COMMANDS_JS = ROOT / "static" / "commands.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# Busy-control commands: the historical entries, /stop (#6951) and the two
# action-only commands this PR adds, /btw and /background (#6597).
ALLOWLISTED = [
    "steer",
    "interrupt",
    "queue",
    "terminal",
    "goal",
    "yolo",
    "stop",
    "btw",
    "background",
]

# Documented command that is *not* a busy control: it has to reach normal routing.
CONTROL = "help"

ARG = "sample-arg"

RUNNER = r"""
const vm = require('vm');
const SEND_SRC = __SEND__;
const PARSE_SRC = __PARSE__;
const SCENARIOS = __SCENARIOS__;

async function runScenario(sc) {
  const rec = {handlers: [], queued: [], steered: [], cancelled: 0, composerAfter: null};
  const composer = {value: sc.text};
  const S = {
    busy: !!sc.busy,
    session: {session_id: 'sid-1'},
    pendingFiles: [],
    activeStreamId: 'stream-1',
    activeProfile: 'default',
  };
  const ctx = {
    console: console,
    S: S,
    window: {_defaultMessageMode: sc.mode},
    document: {querySelector: function () { return null; }},
    $: function (sel) { return sel === 'msg' ? composer : null; },
    COMMANDS: sc.commands.map(function (name) {
      return {name: name, fn: async function (args) { rec.handlers.push([name, args]); }};
    }),
    autoResize: function () {},
    isCompressionUiRunning: function () { return !!sc.compression; },
    _clearStaleBusyStateBeforeSend: function () {},
    _flushSelectionBlocksToComposer: function () {},
    _takeRestoredDraftGoalContinuationId: function () { return null; },
    _normalizeGoalContinuationId: function (v) { return String(v || '').trim(); },
    _setRestoredGoalContinuationDraft: function () {},
    _clearRestoredGoalContinuationDraft: function () {},
    clearInflightState: function () {},
    clearOptimisticSessionStreaming: function () {},
    stopApprovalPolling: function () {},
    stopClarifyPolling: function () {},
    removeThinking: function () {},
    setBusy: function () {},
    setComposerStatus: function () {},
    updateSendBtn: function () {},
    _chatPayloadModelState: function () { return {model: 'm', model_provider: 'p'}; },
    queueSessionMessage: function (sid, entry) { rec.queued.push([sid, entry]); },
    _clearComposerAfterQueuedSelectionSend: function () {},
    updateQueueBadge: function () {},
    showToast: function () {},
    renderTray: function () {},
    t: function (k) { return String(k); },
    _trySteer: async function () { rec.steered.push(['steer']); return true; },
    cancelStream: async function () { rec.cancelled += 1; return true; },
    _goalContinuationId: '',
    _sendInProgress: false,
    _sendInProgressSid: null,
    _pendingSelections: [],
    INFLIGHT: {},
  };
  vm.createContext(ctx);
  vm.runInContext(PARSE_SRC, ctx);
  vm.runInContext(SEND_SRC, ctx);
  await ctx.send();
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
    commands = ALLOWLISTED + [CONTROL]
    scenarios = []
    for cmd in ALLOWLISTED:
        for state in ("busy", "compression"):
            scenarios.append(
                {
                    "id": f"{cmd}::{state}",
                    "text": f"/{cmd} {ARG}",
                    "busy": state == "busy",
                    "compression": state == "compression",
                    "mode": "steer",
                    "commands": commands,
                }
            )
    for mode in ("queue", "steer"):
        scenarios.append(
            {
                "id": f"control::{mode}",
                "text": f"/{CONTROL}",
                "busy": True,
                "compression": False,
                "mode": mode,
                "commands": commands,
            }
        )
    return scenarios


def _run_harness(tmp_path: Path) -> dict:
    send_src = extract_function(
        MESSAGES_JS.read_text(encoding="utf-8"), "send", prefix="async function"
    )
    parse_src = extract_function(COMMANDS_JS.read_text(encoding="utf-8"), "parseCommand")
    script = (
        RUNNER.replace("__SEND__", json.dumps(send_src))
        .replace("__PARSE__", json.dumps(parse_src))
        .replace("__SCENARIOS__", json.dumps(_scenarios()))
    )
    path = tmp_path / "busy_allowlist_6597.js"
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
    return _run_harness(tmp_path_factory.mktemp("busy_allowlist_6597"))


def test_every_allowlisted_command_runs_immediately_while_busy(harness):
    for cmd in ALLOWLISTED:
        for state in ("busy", "compression"):
            rec = harness[f"{cmd}::{state}"]
            where = f"/{cmd} while {state}"
            assert rec["handlers"] == [[cmd, ARG]], f"{where} did not run its handler: {rec}"
            assert rec["composerAfter"] == "", f"{where} left the composer text in place"
            assert rec["queued"] == [], f"{where} was queued instead of executed"
            assert rec["steered"] == [], f"{where} was steered instead of executed"
            assert rec["cancelled"] == 0, f"{where} cancelled the active stream"


def test_command_outside_the_allowlist_keeps_normal_routing(harness):
    queued = harness["control::queue"]
    assert queued["handlers"] == []
    assert len(queued["queued"]) == 1
    assert queued["queued"][0][1]["text"] == f"/{CONTROL}"
    assert queued["steered"] == []
    assert queued["cancelled"] == 0

    steered = harness["control::steer"]
    assert steered["handlers"] == []
    assert steered["queued"] == []
    assert steered["steered"] == [["steer"]]
    assert steered["cancelled"] == 0
