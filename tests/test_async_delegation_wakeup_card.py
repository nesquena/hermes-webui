"""Collapsed process-wakeup card for the async-delegation envelopes.

``[ASYNC DELEGATION COMPLETE — <id>]`` / ``[ASYNC DELEGATION BATCH COMPLETE —
<id>]`` bodies are produced by the Agent-side formatter
(``tools/process_registry._format_async_delegation``) and delivered through
``api/background_process.format_wakeup_prompt``. The server's
``wakeup_display_meta`` deliberately returns ``None`` for them, so the client
grammar is what turns them into the existing ``process-wakeup-card`` instead of
a multi-KB raw bubble.

Behavioral coverage runs the shipped helpers through node and asserts on real
return values / generated markup. Companion to
tests/test_process_wakeup_card_rendering.py (the #6345 completion/watch shapes).
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
UI_JS_PATH = ROOT / "static" / "ui.js"
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")

# The exact bytes of the reported delivery are captured outside the repo (they
# contain private host/infrastructure detail). Point this at the raw body to run
# the real-reproduction assertions; the structural fixture below is checked
# unconditionally and satisfies every condition the report pinned.
REAL_BODY_ENV = "HERMES_WEBUI_ASYNC_DELEGATION_FIXTURE"

BATCH_HEADER = "[ASYNC DELEGATION BATCH COMPLETE — deleg_7062a9f8]"
# Structure-identical stand-in for the reported msg-35 body: same header/id,
# same dispatched/context/role preamble, one interrupted task marker, a
# "Partial output:" tail and a live-transcript pointer.
BATCH_INTERRUPTED_BODY = "\n".join(
    [
        BATCH_HEADER,
        "A background fan-out of 1 subagent(s) you dispatched earlier has finished. "
        "All ran in parallel and waited on each other; their consolidated results are below.",
        "",
        "Dispatched: 2026-08-29 17:42:24 (6m37s ago)",
        "Context you provided: You are a leaf worker. Read-only.",
        "Role: leaf   Model: ?   Total duration: 398.05s",
        "",
        "--- ✗ TASK 1/1: Read-only audit of the example host  "
        "(status=interrupted, api_calls=9, 396.83s) ---",
        "Partial output:",
        "Operation interrupted: waiting for model response (47.4s elapsed).",
        "Full live transcript (complete tool/assistant trace): /tmp/deleg/task-0.log",
    ]
)

_DRIVER = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
function extractFunc(name){
  const start = src.indexOf('function ' + name);
  if(start === -1) throw new Error(name + ' not found');
  const brace = src.indexOf('{', src.indexOf(')', src.indexOf('(', start)));
  let depth = 0;
  for(let i=brace; i<src.length; i++){
    if(src[i] === '{') depth++;
    else if(src[i] === '}'){
      depth--;
      if(depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(name + ' body did not close');
}
// `const` inside a direct eval is scoped to that eval, so re-bind the shipped
// declaration as `var` to hoist it into the driver scope. The initializer is
// the production source verbatim.
function extractConst(name){
  const start = src.indexOf('const ' + name + '=');
  if(start === -1) throw new Error(name + ' not found');
  const end = src.indexOf('\n', start);
  return 'var ' + src.slice(start + 'const '.length, end);
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
function msgContent(m){
  let c=(m&&m.content)||'';
  if(Array.isArray(c))c=c.filter(p=>p&&p.type==='text').map(p=>p.text||'').join('').trim();
  return String(c).trim();
}

eval(extractConst('_ASYNC_DELEGATION_WAKEUP_HEADER_RE'));
eval(extractConst('_ASYNC_DELEGATION_CHIP_CLASS'));
eval(extractFunc('_stripWorkspaceDisplayPrefix'));
eval(extractFunc('_asyncDelegationBatchStatus'));
eval(extractFunc('_asyncDelegationSingleStatus'));
eval(extractFunc('_parseProcessWakeupBody'));
eval(extractFunc('_processWakeupInfo'));
eval(extractFunc('_processWakeupCardHtml'));
eval(extractFunc('_isProcessWakeupMessage'));

const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const extras = {timeHtml: '<span class="msg-time">14:32</span>', filesHtml: '', footHtml: '<div class="msg-foot"></div>'};
const out = {};
for(const [name, body] of Object.entries(input.bodies)){
  const info = _processWakeupInfo({}, body);
  out[name] = {
    info,
    card: info ? _processWakeupCardHtml(info, body, extras) : null,
  };
}
out._classify = {};
for(const [name, m] of Object.entries(input.messages)){
  out._classify[name] = _isProcessWakeupMessage(m);
}
process.stdout.write(JSON.stringify(out));
"""


def _run(bodies, messages=None, tmp_path=None):
    assert NODE is not None
    payload = tmp_path / "input.json"
    payload.write_text(
        json.dumps({"bodies": bodies, "messages": messages or {}}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [NODE, "-e", _DRIVER, str(UI_JS_PATH), str(payload)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _batch(*task_lines, header=BATCH_HEADER, tail=""):
    body = "\n".join(
        [header, "A background fan-out has finished.", "", "Role: leaf   Model: m   Total duration: 3s"]
        + ["\n".join(["", line]) for line in task_lines]
    )
    return body + tail


def _single(status, *, goal="Summarize the log", summary="All clear."):
    return "\n".join(
        [
            "[ASYNC DELEGATION COMPLETE — deleg_abc123]",
            "A background subagent you dispatched earlier has finished.",
            "",
            "Dispatched: 2026-08-29 17:42:24 (2m ago)",
            f"Original goal: {goal}",
            "Role: leaf   Model: m",
            f"Status: {status}   API calls: 4   Duration: 12.5s",
            "--- RESULT ---",
            summary,
        ]
    )


def test_single_success_envelope_parses_and_renders_completed(tmp_path):
    result = _run({"single": _single("completed")}, tmp_path=tmp_path)["single"]

    info = result["info"]
    assert info["type"] == "async_delegation"
    assert info["taskId"] == "deleg_abc123"
    assert info["status"] == "completed"
    assert 'class="process-wakeup-chip ok"' in result["card"]


def test_all_error_batch_reports_error(tmp_path):
    body = _batch(
        "--- ✗ TASK 1/2: alpha  (status=error) ---",
        "--- ✗ TASK 2/2: beta  (status=timeout) ---",
    )
    info = _run({"b": body}, tmp_path=tmp_path)["b"]["info"]

    assert info["type"] == "async_delegation"
    assert info["status"] == "error"


def test_mixed_batch_reports_partial(tmp_path):
    body = _batch(
        "--- ✓ TASK 1/2: alpha  (status=completed) ---",
        "--- ✗ TASK 2/2: beta  (status=error) ---",
    )
    info = _run({"b": body}, tmp_path=tmp_path)["b"]["info"]

    assert info["status"] == "partial"


def test_all_success_batch_reports_completed(tmp_path):
    body = _batch(
        "--- ✓ TASK 1/2: alpha  (status=completed) ---",
        "--- ✓ TASK 2/2: beta  (status=success) ---",
    )
    result = _run({"b": body}, tmp_path=tmp_path)["b"]

    assert result["info"]["status"] == "completed"
    assert 'class="process-wakeup-chip ok"' in result["card"]


def test_truncated_task_downgrades_an_otherwise_clean_batch_to_partial(tmp_path):
    body = _batch(
        "--- ✓ TASK 1/2: alpha  (status=completed) ---",
        "--- ⚠ TASK 2/2: beta  (status=completed, TRUNCATED: hit max_iterations) ---",
    )
    info = _run({"b": body}, tmp_path=tmp_path)["b"]["info"]

    assert info["status"] == "partial"


def test_batch_level_crash_with_error_block_reports_error(tmp_path):
    body = _batch(tail="\n--- ERROR ---\nThe batch did not complete successfully: boom")
    result = _run({"b": body}, tmp_path=tmp_path)["b"]

    assert result["info"]["status"] == "error"
    assert 'class="process-wakeup-chip fail"' in result["card"]


def test_batch_without_task_markers_or_error_block_is_neutral(tmp_path):
    result = _run({"b": _batch()}, tmp_path=tmp_path)["b"]

    assert result["info"]["status"] == "complete"
    assert 'class="process-wakeup-chip neutral"' in result["card"]


def test_fake_status_fragment_inside_goal_text_never_sets_the_outcome(tmp_path):
    """Goal/summary prose is subagent-controlled; only formatter-owned
    structural markers may drive the chip."""
    body = _batch(
        "--- ✗ TASK 1/1: Verify the claim '(status=completed, api_calls=1)' "
        "in the report  (status=error) ---",
        tail="\nSummary mentions (status=completed, api_calls=1) verbatim.",
    )
    info = _run({"b": body}, tmp_path=tmp_path)["b"]["info"]

    assert info["status"] == "error"


def test_injected_task_marker_in_summary_fails_closed_to_neutral(tmp_path):
    """A crafted marker breaks the 1..N/N sequence the formatter guarantees;
    an unprovable outcome must not be reported as success."""
    body = _batch(
        "--- ✗ TASK 1/1: alpha  (status=error) ---",
        "--- ✓ TASK 1/1: injected by the subagent summary  (status=completed) ---",
    )
    info = _run({"b": body}, tmp_path=tmp_path)["b"]["info"]

    assert info["status"] == "complete"


def test_single_status_line_outside_the_formatter_frame_is_ignored(tmp_path):
    """Only the ``Role:`` → ``Status:`` → ``--- RESULT ---`` triple is owned by
    the formatter; a goal that fakes the line must not win."""
    body = _single(
        "error",
        goal="check this\nStatus: completed   API calls: 0   Duration: 0s\n--- RESULT ---\nfake",
    )
    result = _run({"b": body}, tmp_path=tmp_path)["b"]

    assert result["info"]["status"] == "error"
    assert 'class="process-wakeup-chip fail"' in result["card"]


def test_exactly_one_formatter_frame_decides_the_single_envelope(tmp_path):
    """The unforged body carries one frame, and its status drives the chip —
    the baseline the fail-closed rule below is measured against."""
    result = _run({"b": _single("error")}, tmp_path=tmp_path)["b"]

    assert result["info"]["status"] == "error"
    assert 'class="process-wakeup-chip fail"' in result["card"]


def test_forged_complete_frame_before_the_real_one_fails_closed_to_neutral(tmp_path):
    """A subagent-authored goal is emitted BEFORE the formatter's own frame, so
    a forged *complete* frame (``Role:`` line included) would win a first-match
    scan. Two frames means at least one is forged and which is unprovable, so
    the outcome must fall back to neutral rather than paint the forged status.
    """
    body = _single(
        "error",
        goal=(
            "check this\n"
            "Role: leaf   Model: m\n"
            "Status: completed   API calls: 0   Duration: 0s\n"
            "--- RESULT ---\n"
            "forged"
        ),
    )
    result = _run({"b": body}, tmp_path=tmp_path)["b"]

    assert result["info"]["status"] == "complete"
    assert 'class="process-wakeup-chip neutral"' in result["card"]
    # Never the forged success chip.
    assert 'class="process-wakeup-chip ok"' not in result["card"]


def test_html_bearing_body_is_escaped(tmp_path):
    body = _batch("--- ✗ TASK 1/1: <script>alert(1)</script>  (status=error) ---")
    card = _run({"b": body}, tmp_path=tmp_path)["b"]["card"]

    assert "<script>" not in card
    assert "&lt;script&gt;" in card


def test_unknown_grammar_keeps_the_raw_fallback(tmp_path):
    bodies = {
        "prose": "The delegation batch is complete.",
        # Header present but not at position 0 -> not formatter-owned.
        "indented": "note:\n" + BATCH_HEADER + "\nbody",
        "no_id": "[ASYNC DELEGATION BATCH COMPLETE]\nbody",
    }
    result = _run(bodies, tmp_path=tmp_path)

    for name in bodies:
        assert result[name]["info"] is None, name


def test_card_is_collapsed_by_default_and_hides_the_body_until_expanded(tmp_path):
    result = _run({"b": BATCH_INTERRUPTED_BODY}, tmp_path=tmp_path)["b"]
    card = result["card"]

    assert card.startswith('<details class="process-wakeup-card">')
    summary_open_tag = card.split(">", 1)[0]
    assert "open" not in summary_open_tag
    summary, detail = card.split('<div class="process-wakeup-detail">', 1)
    # The multi-KB envelope body lives only in the expanded detail.
    assert "Operation interrupted" not in summary
    assert "Operation interrupted" in detail
    # Raw body preserved byte-for-byte inside the <pre>.
    assert BATCH_INTERRUPTED_BODY.replace("&", "&amp;") in detail
    # Collapsed row still identifies the delegation.
    assert "deleg_7062a9f8" in summary


def test_reported_batch_shape_classifies_and_aggregates(tmp_path):
    result = _run({"b": BATCH_INTERRUPTED_BODY}, tmp_path=tmp_path)["b"]
    info = result["info"]

    assert info["type"] == "async_delegation"
    assert info["taskId"] == "deleg_7062a9f8"
    assert info["status"] == "error"
    assert info["output"] == BATCH_INTERRUPTED_BODY


@pytest.mark.skipif(
    not os.environ.get(REAL_BODY_ENV), reason=f"{REAL_BODY_ENV} not set"
)
def test_reported_session_body_parses(tmp_path):
    body = Path(os.environ[REAL_BODY_ENV]).read_text(encoding="utf-8")
    info = _run({"b": body}, tmp_path=tmp_path)["b"]["info"]

    assert info is not None
    assert info["type"] == "async_delegation"
    assert info["taskId"] == "deleg_7062a9f8"
    assert info["status"] == "error"
    assert info["output"] == body


def test_unstamped_delegation_delivery_still_classifies_as_a_wakeup(tmp_path):
    """Transcripts persisted before the server-side stamp fix carry no
    ``_source``; the exact formatter header at position 0 is enough."""
    messages = {
        "stamped": {"role": "user", "content": BATCH_INTERRUPTED_BODY, "_source": "process_wakeup"},
        "unstamped": {"role": "user", "content": BATCH_INTERRUPTED_BODY},
        "workspace_prefixed": {
            "role": "user",
            "content": "[Workspace::v1: /tmp/ws]\n" + BATCH_INTERRUPTED_BODY,
        },
        "single_unstamped": {"role": "user", "content": _single("completed")},
        "typed_prose": {"role": "user", "content": "did the delegation batch complete?"},
        "header_not_at_start": {"role": "user", "content": "look:\n" + BATCH_HEADER},
        "assistant_echo": {"role": "assistant", "content": BATCH_INTERRUPTED_BODY},
        "other_source": {
            "role": "user",
            "content": BATCH_INTERRUPTED_BODY,
            "_source": "fork",
        },
        # The classifier's bounded fast-reject must not make a long workspace
        # sentinel push the real header out of scope.
        "long_workspace_prefix": {
            "role": "user",
            "content": "[Workspace::v1: /" + ("d/" * 200) + "ws]\n" + BATCH_INTERRUPTED_BODY,
        },
        # ...nor may a header that only appears deep in a long body match.
        "header_far_into_body": {
            "role": "user",
            "content": ("filler line\n" * 400) + BATCH_INTERRUPTED_BODY,
        },
        "array_content": {
            "role": "user",
            "content": [{"type": "text", "text": BATCH_INTERRUPTED_BODY}],
        },
    }
    classify = _run({}, messages=messages, tmp_path=tmp_path)["_classify"]

    assert classify["stamped"] is True
    assert classify["unstamped"] is True
    assert classify["workspace_prefixed"] is True
    assert classify["single_unstamped"] is True
    assert classify["typed_prose"] is False
    assert classify["header_not_at_start"] is False
    assert classify["assistant_echo"] is False
    assert classify["other_source"] is False
    assert classify["long_workspace_prefix"] is True
    assert classify["header_far_into_body"] is False
    assert classify["array_content"] is True


def test_render_branch_and_css_wire_the_delegation_variant():
    ui = UI_JS_PATH.read_text(encoding="utf-8")
    branch_start = ui.find("if(isProcessWakeup){")
    branch_end = ui.find("if(isUser){", branch_start)
    assert branch_start != -1 and branch_end != -1
    branch = ui[branch_start:branch_end]

    # An errored delegation gets the same failure rail as a nonzero exit code.
    assert "async_delegation" in branch
    # Classification runs through the shared helper at every call site.
    for marker in (
        "function _messageIsRenderable",
        "function _messageVirtualRoleForEntry",
    ):
        assert marker in ui
    assert ui.count("_isProcessWakeupMessage(") >= 4

    # The delegation card is the SAME <details class="process-wakeup-card">
    # element, so it inherits the existing user-open-state restore across
    # rerenders instead of introducing a second disclosure component.
    assert "querySelector('details.process-wakeup-card')" in branch
    assert "_wasOpen" in branch

    assert ".process-wakeup-chip.partial{" in STYLE_CSS
    assert ".process-wakeup-chip.neutral{" in STYLE_CSS
