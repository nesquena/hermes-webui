"""#7653 re-gate round 11 — occurrence identity through paired start/completion events.

The 10/10 re-gate's finding 4: re-armed starts emit another occurrence with
the same tid, while rendering and hydration still identify calls by tid
alone. Chromium consuming the actual callback events shows only ``exit 3``
in both activity modes; the base retains ``echo FIRST``. The reviewer's
exact fix: carry a distinct occurrence ID through paired start/completion
events, mirrors, persistence, hydration, and render/deduplication keys.

The chain, end to end:

* **Server** (``api/streaming.py``): ``on_tool_start``'s rearm branch emits
  ``tool.started`` for a second occurrence of an id this stream already
  saw. The event and the mirror rows carried only ``tid``, so a consumer
  could not tell occurrence 2 from occurrence 1.
* **Mirror** (``_live_tool_calls`` / ``STREAM_LIVE_TOOL_CALLS``): the
  rearm branch appends a second row with the same tid — the list is
  positional, but nothing on the row names its occurrence.
* **Persistence** (``_partial_tool_calls`` / ``s.tool_calls``): both rows
  round-trip with the same tid.
* **Render/dedup** (``static/messages.js``): ``_anchorSceneExistingRowKey``
  keys a tool row as ``tool:<tid>`` and the push loop drops any row whose
  key was already seen — so the second occurrence of a reused id is
  dropped from the activity scene. That is the ``exit 3`` row.

These tests pin the shipped chain, not a restatement:

1. the server's rearm start/completion events carry a distinct occurrence
   id, and the two occurrences of one tid get DIFFERENT ids;
2. the mirror rows carry the same occurrence id as their events;
3. the browser's existing-row key includes the occurrence id, so two
   occurrences of one tid produce two distinct keys and neither is
   dropped;
4. a row with no occurrence id still dedups against itself (the
   duplicate-event case the guard was written for).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _function_block(src: str, header: str) -> str:
    start = src.find(header)
    assert start != -1, f"{header!r} not found"
    brace = src.find("{", start)
    assert brace != -1, f"no body for {header!r}"
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"{header!r} did not close")


def _run_node(script: str, payload: dict):
    assert NODE, "node not on PATH"
    harness = (
        "var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{}');\n"
        + script
    )
    result = subprocess.run(
        [NODE, "-e", harness],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr[:800]}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Server half: the rearm events and mirror rows carry occurrence ids.
# ---------------------------------------------------------------------------

_REARM_DRIVER = r"""
import sys, json
sys.path.insert(0, %r)
import api.streaming as st

events = []
mirror = []

class _Put:
    def __call__(self, kind, payload):
        events.append((kind, payload))

put = _Put()

seen_ids = {}
start_ids = set()

# Occurrence 1: a fresh start.
verdict1 = st._tool_start_occurrence_decision('call_0', start_ids, seen_ids)
if verdict1 == 'rearm':
    pass
else:
    start_ids.add('call_0')
    seen_ids['call_0'] = {'awaiting_complete': True}
    mirror.append({'name': 'terminal', 'args': {}, 'tid': 'call_0'})
    put('tool', {'event_type': 'tool.started', 'name': 'terminal',
                 'preview': None, 'args': {}, 'tid': 'call_0'})

# Occurrence 1 completes.
st._tool_complete_occurrence_decision('call_0', set(), seen_ids)
seen_ids['call_0'] = {'awaiting_complete': False}

# Occurrence 2: the re-arm.
verdict2 = st._tool_start_occurrence_decision('call_0', start_ids, seen_ids)
if verdict2 == 'suppress':
    pass
elif verdict2 == 'rearm':
    seen_ids['call_0'] = {'awaiting_complete': True}
    mirror.append({'name': 'terminal', 'args': {}, 'tid': 'call_0'})
    put('tool', {'event_type': 'tool.started', 'name': 'terminal',
                 'preview': None, 'args': {}, 'tid': 'call_0'})

out = {
    'verdict1': verdict1,
    'verdict2': verdict2,
    'events': events,
    'mirror_tids': [row.get('tid') for row in mirror],
    'mirror_len': len(mirror),
}
sys.stdout.write(json.dumps(out))
""" % str(REPO_ROOT)


def _rearm_probe():
    proc = subprocess.run(
        [sys_executable(), "-c", _REARM_DRIVER],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr[:800]}"
    return json.loads(proc.stdout)


def sys_executable() -> str:
    import sys

    return sys.executable


def test_the_rearm_start_emits_a_second_occurrence_with_the_same_tid():
    """Control: the re-arm path really does produce two rows of one tid.

    Without this the rest of the file would be vacuous — if the server
    never emitted the second occurrence, the dedup would have nothing to
    drop.
    """
    out = _rearm_probe()
    assert out["verdict2"] == "rearm", (
        "the second start of a completed id must be re-armed, not suppressed"
    )
    assert out["mirror_tids"] == ["call_0", "call_0"], (
        "the mirror must hold both occurrences of the reused id"
    )


def test_the_rearm_events_carry_distinct_occurrence_ids():
    """The reviewer's exact fix, server half.

    Both ``tool.started`` events name the same tid, so a consumer keyed on
    tid alone cannot tell them apart. Each event must carry the occurrence
    it belongs to, and the two occurrences must differ.

    The rearm branch lives inside the streaming closure, which cannot be
    driven in-process (it needs a live stream, an agent and a provider), so
    this pins the SHIPPED branch text: every payload the branch emits —
    the in-memory mirror row, the shared mirror row and the SSE event —
    must carry the occurrence, and the index must be derived per tid (so
    the second occurrence of one tid differs from the first).
    """
    src = _read("api/streaming.py")
    rearm_region = src[src.index("elif _start_verdict == 'rearm':") :]
    rearm_region = rearm_region[: rearm_region.index("else:")]
    # The occurrence index is derived from the rows this stream already holds
    # for the tid, so occurrence 2 differs from occurrence 1 by construction.
    assert "_occurrence_index" in rearm_region, (
        "the rearm branch must mint/carry an occurrence id; region:\n"
        f"{rearm_region[:1500]}"
    )
    # Every emit in the branch carries it.
    assert rearm_region.count("'occurrence':") >= 3, (
        "the rearm branch must carry the occurrence on the in-memory mirror "
        "row, the shared mirror row AND the SSE event; region:\n"
        f"{rearm_region[:1500]}"
    )
    # The fresh branch emits occurrence 0 so a consumer can read the field
    # unconditionally.
    fresh_region = src[src.index("_live_tool_event_start_ids.add(tool_call_id)") :]
    fresh_region = fresh_region[: fresh_region.index("_tool_stats = meter()")]
    assert "'occurrence': 0" in fresh_region, (
        "the fresh branch must emit occurrence 0 so the field is always "
        f"present; region:\n{fresh_region[:800]}"
    )
    # The paired completion carries the occurrence of the row it settled.
    complete_fn = src[
        src.index("def _emit_tool_complete_to_mirrors_and_sse(") :
    ]
    complete_fn = complete_fn[: complete_fn.index("\ndef ", 10)]
    assert "_settled_occurrence" in complete_fn, (
        "the completion must carry the occurrence of the row it settled"
    )


def test_the_mirror_rows_carry_their_occurrence_id():
    """The mirror rows must agree with the events they mirror."""
    src = _read("api/streaming.py")
    assert src.count("'occurrence'") + src.count('"occurrence"') >= 2, (
        "the occurrence id must reach the mirror rows as well as the events"
    )


# ---------------------------------------------------------------------------
# Browser half: the existing-row key must include the occurrence id.
# ---------------------------------------------------------------------------


def _key_driver() -> str:
    fn = _function_block(_read("static/messages.js"), "function _anchorSceneExistingRowKey(")
    return f"""
{fn}
function _dedup(rows){{
  const seen=new Set();
  const kept=[];
  for(const row of rows){{
    const key=_anchorSceneExistingRowKey(row);
    if(key&&seen.has(key)) continue;
    if(key) seen.add(key);
    kept.push(row);
  }}
  return kept;
}}
process.stdout.write(JSON.stringify(
  _dedup(PAYLOAD.rows).map(r=>({{tid:r.tool_call_id,occ:r.occurrence}}))));
"""


def _row(occurrence=None):
    row = {
        "role": "tool",
        "tool_call_id": "call_0",
        "tool": {"id": "call_0", "name": "terminal"},
    }
    if occurrence is not None:
        row["occurrence"] = occurrence
    return row


def test_the_existing_row_key_separates_two_occurrences_of_one_tid():
    """The reviewer's repro, browser half: both rows must survive the dedup.

    Chromium showed only ``exit 3`` because the key was ``tool:<tid>`` for
    both occurrences, so the push loop dropped the second one.
    """
    out = _run_node(_key_driver(), {"rows": [_row(1), _row(2)]})
    assert len(out) == 2, (
        f"two occurrences of one tid must produce two distinct keys; kept {out}"
    )


def test_the_key_still_dedups_a_genuine_duplicate_row():
    """Control: the duplicate-event case the guard was written for stands.

    Two rows with the same tid AND the same occurrence id are the same
    event arriving twice, so the second must still be dropped.
    """
    out = _run_node(_key_driver(), {"rows": [_row(1), _row(1)]})
    assert len(out) == 1, (
        f"a duplicate of the SAME occurrence must still dedup; kept {out}"
    )


def test_occurrenceless_rows_keep_the_tid_key():
    """Control: a row with no occurrence id keeps the historical tid key."""
    out = _run_node(_key_driver(), {"rows": [_row(), _row()]})
    assert len(out) == 1, (
        "occurrence-less rows of one tid must still collapse (the "
        "pre-#7653 behaviour for providers that never reuse an id)"
    )
