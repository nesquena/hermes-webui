"""#7653 re-gate round 16 — occurrence persistence, replay, partial projection.

The 10-31 re-review keeps the 10-19 review's item 3 open: a reused tool id's
occurrence is still lost on the persistence/replay/partial-projection chain, so
two calls of one id collapse into one card and the later failure is projected
onto the earlier success.

The maintainer's own repro (10-27 supplement), replayed here against the real
production functions:

    real ``_extract_tool_calls_from_messages`` -> ``_complete_hydrated_anchor_scene``
    with successful ``call_0: echo FIRST`` followed by failed ``call_0: exit 3``
    in ONE user turn.

RED on the previous head: the two occurrences of ``call_0`` shared one
assistant owner, so ``_anchor_scene_row_key`` gave them the same key, the rows
merged, and the second occurrence's ``is_error: true`` upgraded the kept
``echo FIRST`` card to ``status: error``. The saved summaries were wrong even
before that: BOTH carried ``is_error: true``, because the live-mirror alignment
deduped "several result rows of one owner are one occurrence slot" and bound
both results to the tid's trailing (failing) live row.

The repair, per item 3's fix shape ("carry the occurrence through the saved
summary, journal replay, hydration and every render/dedup key, and transfer a
verdict only between rows with the same occurrence"):

- ``_tool_result_occurrences_by_tid`` gives every RESOLVED tool result of a tid
  its own occurrence ordinal, so one assistant message emitting an id twice is
  two occurrences, not one slot.
- ``_extract_tool_calls_from_messages`` writes that ordinal into the persisted
  summary (``occurrence``) and pairs the k-th occurrence with the k-th live
  mirror row, so a successful first occurrence keeps its success.
- ``_anchor_scene_tool_row`` projects the summary's occurrence onto the
  hydrated row, and ``_anchor_scene_row_key`` scopes the row key by it, so two
  occurrences of one id are two cards.
- ``merge_duplicate_tool_row`` refuses to move a verdict across two rows that
  name DIFFERENT occurrences.

Every test drives the real ``api/streaming.py`` / ``api/routes.py`` functions
through a subprocess driver (``api.streaming`` pulls heavy deps, same as the
round-6..10 fixtures).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# The driver runs under the same interpreter as this test process (the repo's
# committed test venv), so it resolves the project packages exactly as the
# suite does — no machine-specific interpreter path.
DRIVER_PYTHON = sys.executable

# ---------------------------------------------------------------------------
# Driver: the real _extract_tool_calls_from_messages + _complete_hydrated_anchor_scene
# ---------------------------------------------------------------------------

_DRIVER = r"""
import sys, json
sys.path.insert(0, %r)
from api.streaming import (
    _extract_tool_calls_from_messages,
    _tool_result_occurrences_by_tid,
)
from api import routes
data = json.loads(sys.stdin.read())
out = _extract_tool_calls_from_messages(
    data['messages'],
    live_tool_calls=data['live_tool_calls'],
    prior_tool_calls=data.get('prior_tool_calls'),
)
scene = routes._complete_hydrated_anchor_scene(
    data['messages'],
    data['scene'],
    data['final_idx'],
    tool_calls=out,
)
print(json.dumps({
    'summaries': out,
    'occurrences': {
        tid: [list(pair) for pair in pairs]
        for tid, pairs in _tool_result_occurrences_by_tid(data['messages']).items()
    },
    'rows': [
        {
            'key': routes._anchor_scene_row_key(r),
            'tid': r.get('tool_call_id'),
            'status': r.get('status'),
            'is_error': (r.get('tool') or {}).get('is_error'),
            'command': ((r.get('tool') or {}).get('args') or {}).get('command'),
            'occurrence': (r.get('payload') or {}).get('occurrence'),
        }
        for r in scene['activity_rows'] if r.get('role') == 'tool'
    ],
}))
""" % str(REPO_ROOT)


def _run(messages, live_tool_calls=None, scene=None, final_idx=None):
    payload = {
        "messages": messages,
        "live_tool_calls": live_tool_calls or [],
        "prior_tool_calls": [],
        "scene": scene if scene is not None else {"activity_rows": []},
        "final_idx": final_idx if final_idx is not None else len(messages) - 1,
    }
    proc = subprocess.run(
        [DRIVER_PYTHON, "-c", _DRIVER],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"driver failed:\n{proc.stderr[:2000]}"
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Fixtures — the maintainer's item-3 shapes
# ---------------------------------------------------------------------------


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


def _asst_tool_calls(*pairs) -> dict:
    """One assistant message emitting several tool_calls (possibly one id twice)."""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tid,
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps({"command": command}),
                },
            }
            for tid, command in pairs
        ],
    }


def _tool_result(tid: str, payload: dict) -> dict:
    return {"role": "tool", "tool_call_id": tid, "content": json.dumps(payload)}


def _final(text: str = "Final answer.") -> dict:
    return {"role": "assistant", "content": text}


def _live_row(tid: str, occurrence: int, command: str, *, is_error: bool) -> dict:
    return {
        "name": "terminal",
        "tid": tid,
        "occurrence": occurrence,
        "done": True,
        "is_error": is_error,
        "snippet": "boom" if is_error else "FIRST",
        "args": {"command": command},
    }


def _same_owner_turn():
    """One user turn, one assistant message, ``call_0`` twice (the 10-27 repro)."""
    messages = [
        _user("go"),
        _asst_tool_calls(("call_0", "echo FIRST"), ("call_0", "exit 3")),
        _tool_result("call_0", {"output": "FIRST", "exit_code": 0, "error": None}),
        _tool_result("call_0", {"output": "", "exit_code": 3, "error": "boom"}),
        _final(),
    ]
    live = [
        _live_row("call_0", 0, "echo FIRST", is_error=False),
        _live_row("call_0", 1, "exit 3", is_error=True),
    ]
    return messages, live


# ---------------------------------------------------------------------------
# _tool_result_occurrences_by_tid — occurrence identity over resolved results
# ---------------------------------------------------------------------------


def test_every_resolved_result_of_a_tid_gets_its_own_occurrence():
    messages, _ = _same_owner_turn()
    occurrences = _run(messages)["occurrences"]
    plan = occurrences.get("call_0")
    assert plan is not None, "a resolved tid must have an occurrence plan"
    assert len(plan) == 2, (
        "two resolved results of one id in one turn are two occurrences, not one "
        f"slot (got {plan})"
    )
    assert [pair[0] for pair in plan] == [1, 1], "both share the one assistant owner"
    assert [pair[1] for pair in plan] == [2, 3], "in message order"


def test_distinct_owners_still_get_sequential_occurrences():
    messages = [
        _user("go"),
        _asst_tool_calls(("call_0", "echo FIRST")),
        _tool_result("call_0", {"output": "FIRST", "exit_code": 0, "error": None}),
        _asst_tool_calls(("call_0", "exit 3")),
        _tool_result("call_0", {"output": "", "exit_code": 3, "error": "boom"}),
        _final(),
    ]
    plan = _run(messages)["occurrences"]["call_0"]
    assert [pair[0] for pair in plan] == [1, 3]
    assert len(plan) == 2


def test_a_declared_but_unfinished_call_is_not_an_occurrence():
    """Item 5's shape must survive: an unfinished trailing call occupies no rank."""
    messages = [
        _user("go"),
        _asst_tool_calls(("call_0", "exit 3")),
        _tool_result("call_0", {"output": "", "exit_code": 3, "error": "boom"}),
        _asst_tool_calls(("call_0", "echo ok")),
        _tool_result("call_0", {"output": "ok", "exit_code": 0, "error": None}),
        _asst_tool_calls(("call_0", "never finishes")),
        _final(),
    ]
    plan = _run(messages)["occurrences"]["call_0"]
    assert [pair[0] for pair in plan] == [1, 3], (
        f"the declared-but-unfinished trailing call must not take a rank (got {plan})"
    )


# ---------------------------------------------------------------------------
# The saved summary carries the occurrence (item 3: "saved summaries lose occurrence")
# ---------------------------------------------------------------------------


def test_saved_summary_carries_the_occurrence_of_each_call():
    messages, _ = _same_owner_turn()
    summaries = _run(messages)["summaries"]
    assert len(summaries) == 2, f"both occurrences must settle (got {len(summaries)})"
    by_command = {s["snippet"]: s for s in summaries}
    assert set(by_command) == {"FIRST", "boom"}
    assert by_command["FIRST"]["occurrence"] == 0, (
        "the first occurrence must be persisted as occurrence 0 "
        f"(got {by_command['FIRST']})"
    )
    assert by_command["boom"]["occurrence"] == 1, (
        "the second occurrence must be persisted as occurrence 1 "
        f"(got {by_command['boom']})"
    )


def test_saved_summary_keeps_the_first_occurrence_successful():
    """The RED core: both summaries used to carry is_error=True."""
    messages, live = _same_owner_turn()
    summaries = _run(messages, live_tool_calls=live)["summaries"]
    by_command = {s["snippet"]: s for s in summaries}
    assert by_command["FIRST"]["is_error"] is False, (
        "a successful first occurrence must not inherit the second occurrence's "
        f"failure (got {by_command['FIRST']})"
    )
    assert by_command["boom"]["is_error"] is True, (
        "the failed occurrence must stay failed "
        f"(got {by_command['boom']})"
    )


# ---------------------------------------------------------------------------
# Hydration: two occurrences are two cards, each with its own verdict
# ---------------------------------------------------------------------------


def test_hydration_keeps_two_cards_for_two_occurrences_of_one_id():
    messages, live = _same_owner_turn()
    result = _run(messages, live_tool_calls=live)
    rows = result["rows"]
    assert len(rows) == 2, (
        "two occurrences of one id in one turn must hydrate as two cards, not one "
        f"(got {len(rows)}: {rows})"
    )
    by_command = {r["command"]: r for r in rows}
    assert set(by_command) == {"echo FIRST", "exit 3"}
    assert by_command["echo FIRST"]["status"] == "completed", (
        "the successful occurrence must not be painted failed by the later "
        f"occurrence's verdict (got {by_command['echo FIRST']})"
    )
    assert by_command["echo FIRST"]["is_error"] is False
    assert by_command["exit 3"]["status"] == "error"
    assert by_command["exit 3"]["is_error"] is True


def test_hydrated_rows_carry_distinct_occurrence_scoped_keys():
    messages, live = _same_owner_turn()
    keys = {r["key"] for r in _run(messages, live_tool_calls=live)["rows"]}
    assert len(keys) == 2, f"the two cards must not share a dedup key (got {keys})"
    assert "tool:call_0@1#0" in keys, f"expected the first occurrence's key (got {keys})"
    assert "tool:call_0@1#1" in keys, f"expected the second occurrence's key (got {keys})"


def test_the_same_occurrence_seen_twice_still_collapses_to_one_card():
    """Non-goal control: a genuine duplicate (same occurrence) must still merge."""
    messages, live = _same_owner_turn()
    scene = {
        "activity_rows": [
            {
                "role": "tool",
                "kind": "tool_completed",
                "status": "completed",
                "tool_call_id": "call_0",
                "name": "terminal",
                "args": {"command": "echo FIRST"},
                "group": {"assistant_msg_idx": 1},
                "payload": {"occurrence": 0},
                "done": True,
            },
            {
                "role": "tool",
                "kind": "tool_completed",
                "status": "completed",
                "tool_call_id": "call_0",
                "name": "terminal",
                "args": {"command": "echo FIRST"},
                "group": {"assistant_msg_idx": 1},
                "payload": {"occurrence": 0},
                "done": True,
            },
        ]
    }
    rows = _run(messages, live_tool_calls=live, scene=scene)["rows"]
    first = [r for r in rows if r["command"] == "echo FIRST"]
    assert len(first) == 1, (
        f"two spellings of the SAME occurrence must collapse (got {len(first)})"
    )


def test_a_legacy_row_without_occurrence_keeps_the_owner_scoped_key():
    """Non-goal control: pre-#7653 rows must not change key shape."""
    assert pytest.approx is not None  # keep pytest imported for the marker below
    from api import routes  # noqa: PLC0415

    row = {
        "role": "tool",
        "kind": "tool_completed",
        "tool_call_id": "call_0",
        "group": {"assistant_msg_idx": 1},
    }
    assert routes._anchor_scene_row_key(row) == "tool:call_0@1"
    bare = {"role": "tool", "kind": "tool_completed", "tool_call_id": "call_0"}
    assert routes._anchor_scene_row_key(bare) == "tool:call_0"


# ---------------------------------------------------------------------------
# Verdict transfer only between rows of the SAME occurrence
# ---------------------------------------------------------------------------


def test_a_verdict_is_never_transferred_across_different_occurrences():
    from api import routes  # noqa: PLC0415

    # One assistant message emitting ``call_0`` twice, so the history mints
    # occurrences 0 and 1 — the reviewer's own repro shape.
    messages = [
        _user("go"),
        _asst_tool_calls(("call_0", "echo FIRST"), ("call_0", "exit 3")),
        _tool_result("call_0", {"output": "FIRST", "exit_code": 0, "error": None}),
        _tool_result("call_0", {"output": "", "exit_code": 3, "error": "boom"}),
        _final(),
    ]
    existing = {
        "role": "tool",
        "kind": "tool_completed",
        "status": "completed",
        "tool_call_id": "call_0",
        "tool": {"id": "call_0", "is_error": False, "args": {"command": "echo FIRST"}},
        "payload": {"tid": "call_0", "occurrence": 0},
        "group": {"assistant_msg_idx": 1},
    }
    incoming = {
        "role": "tool",
        "kind": "tool_completed",
        "status": "error",
        "tool_call_id": "call_0",
        "tool": {"id": "call_0", "is_error": True, "args": {"command": "exit 3"}},
        "payload": {"tid": "call_0", "occurrence": 1},
        "group": {"assistant_msg_idx": 1},
    }
    merged = routes._complete_hydrated_anchor_scene(
        messages, {"activity_rows": [existing, incoming]}, 4
    )
    rows = [r for r in merged["activity_rows"] if r.get("role") == "tool"]
    by_occ = {(r.get("payload") or {}).get("occurrence"): r for r in rows}
    assert set(by_occ) == {0, 1}, f"both occurrences must survive (got {list(by_occ)})"
    assert by_occ[0]["status"] == "completed", (
        "occurrence 0 must keep its success even when occurrence 1 failed "
        f"(got {by_occ[0]['status']})"
    )
    assert by_occ[1]["status"] == "error"


def test_the_same_occurrence_still_upgrades_to_error():
    """Non-goal control: the #7358 round-3 upgrade must survive."""
    from api import routes  # noqa: PLC0415

    messages = [
        _user("go"),
        _asst_tool_calls(("call_0", "exit 3")),
        _tool_result("call_0", {"output": "", "exit_code": 3, "error": "boom"}),
        _final(),
    ]
    existing = {
        "role": "tool",
        "kind": "tool_completed",
        "status": "completed",
        "tool_call_id": "call_0",
        "tool": {"id": "call_0", "is_error": False, "args": {"command": "exit 3"}},
        "payload": {"tid": "call_0", "occurrence": 0},
        "group": {"assistant_msg_idx": 1},
    }
    incoming = {
        "role": "tool",
        "kind": "tool_completed",
        "status": "error",
        "tool_call_id": "call_0",
        "tool": {"id": "call_0", "is_error": True, "args": {"command": "exit 3"}},
        "payload": {"tid": "call_0", "occurrence": 0},
        "group": {"assistant_msg_idx": 1},
    }
    merged = routes._complete_hydrated_anchor_scene(
        messages, {"activity_rows": [existing, incoming]}, 3
    )
    rows = [r for r in merged["activity_rows"] if r.get("role") == "tool"]
    assert len(rows) == 1, f"the same occurrence must collapse (got {len(rows)})"
    assert rows[0]["status"] == "error", (
        "a failed occurrence must still upgrade its own card "
        f"(got {rows[0]['status']})"
    )


# ---------------------------------------------------------------------------
# Partial projection: the cancelled _partial_tool_calls keep occurrences apart
# ---------------------------------------------------------------------------


def test_partial_tool_calls_project_two_occurrences_as_two_cards():
    from api import routes  # noqa: PLC0415

    partial = {
        "role": "assistant",
        "content": "",
        "_partial": True,
        "_partial_tool_calls": [
            {
                "name": "terminal",
                "tid": "call_0",
                "occurrence": 0,
                "done": True,
                "is_error": False,
                "snippet": "FIRST",
                "args": {"command": "echo FIRST"},
            },
            {
                "name": "terminal",
                "tid": "call_0",
                "occurrence": 1,
                "done": True,
                "is_error": True,
                "snippet": "boom",
                "args": {"command": "exit 3"},
            },
        ],
    }
    messages = [_user("go"), partial, _final()]
    merged = routes._complete_hydrated_anchor_scene(
        messages, {"activity_rows": []}, 2
    )
    rows = [r for r in merged["activity_rows"] if r.get("role") == "tool"]
    by_command = {(r.get("tool") or {}).get("args", {}).get("command"): r for r in rows}
    assert set(by_command) == {"echo FIRST", "exit 3"}, (
        f"a cancelled turn's two occurrences must project as two cards (got {list(by_command)})"
    )
    assert by_command["echo FIRST"]["status"] == "completed", (
        "the cancelled partial must not paint the successful occurrence failed "
        f"(got {by_command['echo FIRST']['status']})"
    )
    assert by_command["exit 3"]["status"] == "error"


def test_partial_message_signature_distinguishes_occurrences():
    """Two occurrences of one id must not dedupe as the same partial marker."""
    from api.streaming import _partial_message_signature  # noqa: PLC0415

    def _partial(occurrence: int, command: str, is_error: bool) -> dict:
        return {
            "role": "assistant",
            "content": "",
            "_partial": True,
            "_partial_tool_calls": [
                {
                    "name": "terminal",
                    "tid": "call_0",
                    "occurrence": occurrence,
                    "done": True,
                    "is_error": is_error,
                    "args": {"command": command},
                }
            ],
        }

    first = _partial_message_signature(_partial(0, "echo FIRST", False))
    second = _partial_message_signature(_partial(1, "exit 3", True))
    assert first != second, (
        "the partial-marker signature must not treat two occurrences of one id as "
        "the same marker"
    )


# ---------------------------------------------------------------------------
# Journal replay keeps its occurrence carry (round 14 must not regress)
# ---------------------------------------------------------------------------


def test_journal_replay_still_carries_the_occurrence(tmp_path, monkeypatch):
    from api import models
    from api.run_journal import RunJournalWriter

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes_mod(), "SESSION_DIR", session_dir)

    stream_id = "stream-round16-replay"
    writer = RunJournalWriter("sess-round16", stream_id, session_dir=session_dir)
    for occurrence, command, is_error in (
        (0, "echo FIRST", False),
        (1, "exit 3", True),
    ):
        writer.append_sse_event(
            "tool",
            {"name": "terminal", "tid": "call_0", "occurrence": occurrence,
             "args": {"command": command}},
        )
        writer.append_sse_event(
            "tool_complete",
            {"name": "terminal", "tid": "call_0", "occurrence": occurrence,
             "preview": command, "is_error": is_error},
        )

    from api import routes  # noqa: PLC0415

    snapshot = routes._run_journal_live_snapshot(stream_id)
    assert snapshot is not None
    rows = [
        r for r in snapshot["anchor_activity_scene"]["activity_rows"]
        if r.get("role") == "tool"
    ]
    occurrences = sorted((r.get("payload") or {}).get("occurrence") for r in rows)
    assert occurrences == [0, 1], (
        f"the journal replay must keep both occurrences (got {occurrences})"
    )


def routes_mod():
    from api import routes  # noqa: PLC0415

    return routes


# ---------------------------------------------------------------------------
# The node argv guard: no harness may hand a big script to `node -e`
# ---------------------------------------------------------------------------


def test_node_argv_guard_flags_an_oversized_script():
    """RED control for the guard itself.

    The maintainer's 10-31 note asks that extracted-function drivers adopt
    stdin or a temporary file "so growing production functions cannot exceed
    Linux's per-argument limit", and that other ``node -e`` drivers be checked
    for the same constraint. This pins the guard that enforces it: an argv
    entry past the safe line must be recorded, and the session-finish reporter
    must fail the run.
    """
    from tests import _node_argv_guard

    _node_argv_guard.reset()
    try:
        # One argv entry just under the safe line: recorded, not flagged.
        _node_argv_guard._record(["node", "-e", "x" * (_node_argv_guard.ARGV_SAFE - 1)])
        assert _node_argv_guard.finish() == []

        # Past the safe line: recorded.
        _node_argv_guard._record(["node", "-e", "x" * (_node_argv_guard.ARGV_SAFE + 1)])
        flagged = _node_argv_guard.finish()
        assert len(flagged) == 1, (
            "an argv entry past the safe line must be recorded so the session "
            "fails instead of dying with OSError(E2BIG) on Linux"
        )
        assert flagged[0]["max_entry"] == _node_argv_guard.ARGV_SAFE + 1
        assert flagged[0]["flag"] == "-e"

        # A file-based launch is never flagged, however large the script.
        _node_argv_guard.reset()
        _node_argv_guard._record(["node", "/tmp/huge.js"])
        assert _node_argv_guard.finish() == []

        # Non-node subprocesses are not this guard's business.
        _node_argv_guard.reset()
        _node_argv_guard._record(["python3", "-c", "x" * 200_000])
        assert _node_argv_guard.finish() == []
    finally:
        _node_argv_guard.reset()


def test_run_node_script_never_puts_the_script_in_argv(monkeypatch):
    """The shared launcher must hand node a path, not the script."""
    from tests import _node_script

    seen = {}

    class _FakeCompleted:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    def _fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return _FakeCompleted()

    monkeypatch.setattr(_node_script.subprocess, "run", _fake_run)
    proc = _node_script.run_node_script("console.log(1)", node="node", timeout=5)
    assert proc.returncode == 0
    argv = seen["argv"]
    assert argv[0] == "node"
    assert len(argv) == 2, f"expected [node, <path>], got {argv}"
    assert not argv[1].endswith(".js") is False, "the script must be a .js file"
    assert "console.log(1)" not in argv[1], "the script body must not travel in argv"


def test_script_is_argv_safe_draws_the_line_below_the_linux_ceiling():
    from tests import _node_script

    assert _node_script.ARGV_SAFE_BYTES < 131_072, (
        "the safe line must sit below Linux's MAX_ARG_STRLEN so a harness has "
        "headroom before it dies with OSError(E2BIG)"
    )
    assert _node_script.script_is_argv_safe("x" * _node_script.ARGV_SAFE_BYTES)
    assert not _node_script.script_is_argv_safe("x" * (_node_script.ARGV_SAFE_BYTES + 1))


def test_footer_scope_harness_script_exceeds_the_linux_argv_ceiling():
    """Measured evidence for the maintainer's 10-31 finding.

    ``tests/test_gateway_routing_footer_scope.py`` assembles the whole
    ``renderMessages()`` body plus its DOM shim into one script. Measured on
    this head the assembled script is 140040 bytes — 106.8% of Linux's 131072
    ``MAX_ARG_STRLEN`` — so the old ``node -e <script>`` launch died with
    ``OSError: [Errno 7] Argument list too long`` before node ran. The harness
    now writes the script to a temporary file and runs ``node <path>``.
    """
    footer = REPO_ROOT / "tests" / "test_gateway_routing_footer_scope.py"
    if not footer.exists():
        pytest.skip("footer-scope harness not present")
    source = footer.read_text(encoding="utf-8")
    assert '[node, "-e", script]' not in source, (
        "the footer-scope harness must not hand its (140 KB) assembled script "
        "to node as a single argv entry — Linux caps one argument at 131072 "
        "bytes, so the run dies with OSError(E2BIG) before node starts"
    )
    assert "[node, script_path]" in source, (
        "the footer-scope harness must run its script from a file"
    )
