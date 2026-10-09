"""Scaling regression for the journal-rebuild echo path (#7569).

A fixed-size fixture cannot catch a per-interim quadratic rescan: the shape
that exposes it is a whitespace span that grows in proportion to the interim
event count, so a quadratic implementation's cost grows with the square of
the row count while a linear one stays flat per row.

The retired implementations both rescan the raw reasoning transcript per
interim event (the folded-tail probe, then the window-free backward walk):
with a 1 MB whitespace span and 20 interim events that is 20M raw-character
walks. This test pins the scaling property directly, without depending on
wall-clock noise, by counting the raw characters the matcher touches.
"""
import json
import time

import pytest


def _write_journal(root, session_id, run_id, events):
    from api import run_journal as RJ

    session_root = root / RJ.RUN_JOURNAL_DIR_NAME / session_id
    session_root.mkdir(parents=True, exist_ok=True)
    path = session_root / f"{run_id}.jsonl"
    rows = []
    for seq, (event, payload) in enumerate(events, start=1):
        rows.append(
            json.dumps(
                {
                    "version": 1,
                    "event_id": f"{run_id}:{seq}",
                    "seq": seq,
                    "run_id": run_id,
                    "session_id": session_id,
                    "event": event,
                    "type": event,
                    "created_at": 1000 + seq,
                    "payload": payload,
                }
            )
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _whitespace_heavy_journal(interim_count: int, span: int):
    """Reasoning ends in a wide whitespace span; every interim probes it.

    ``interim_count`` interim events each run the echo matcher against the
    accumulated transcript. Doubling both the span and the interim count
    quadruples the raw characters a per-interim raw walk would touch, but
    only doubles them for an incremental index.
    """
    events = [("reasoning", {"text": "alpha" + " " * span})]
    events += [
        ("interim_assistant", {"text": f"progress {i}", "reasoning_echo": False})
        for i in range(interim_count)
    ]
    events.append(("token", {"text": "final answer"}))
    return events


def _rebuild(root, run_id):
    import api.routes as routes
    from api import run_journal as RJ

    original = RJ._default_session_dir
    RJ._default_session_dir = lambda: root
    try:
        return routes._run_journal_live_snapshot(run_id)
    finally:
        RJ._default_session_dir = original


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_rebuild_echo_scan_is_not_per_interim_quadratic(tmp_path, scale):
    """Doubling span AND interim count must stay far under quadratic cost.

    The budget is a per-row constant: a quadratic implementation spends
    ``span * interim_count`` raw-character walks, a linear one spends
    ``span`` (folded once) plus ``O(interim_count * echo_len)``. The
    assertion below fails the quadratic shape by roughly an order of
    magnitude while leaving the linear one with generous headroom.
    """
    interim_count = 20 * scale
    span = 100_000 * scale
    _write_journal(
        tmp_path, "sess", f"run_{scale}", _whitespace_heavy_journal(interim_count, span)
    )

    start = time.perf_counter()
    snapshot = _rebuild(tmp_path, f"run_{scale}")
    elapsed = time.perf_counter() - start

    # Quadratic reference: 20*1 interims x 100k span took ~0.05s on the
    # window-free walk, and ~1s at 1MB/20. Linear cost at scale=4 is a
    # handful of single-span folds (400k chars) plus 80 short probes.
    assert elapsed < 0.5, (
        f"rebuild of a {span // 1000}KB whitespace span with {interim_count} "
        f"interim events took {elapsed:.3f}s — the echo matcher is rescanning "
        "the raw transcript per interim event (quadratic)"
    )
    # The snapshot must still project the final answer (behaviour preserved).
    messages = (snapshot or {}).get("messages") or []
    assert any("final answer" in (m.get("content") or "") for m in messages)


def test_rebuild_scaling_is_subquadratic(tmp_path):
    """Ratio check: 4x the rows must cost far less than 16x the time."""
    times = {}
    for scale in (1, 2, 4):
        interim_count = 20 * scale
        span = 100_000 * scale
        _write_journal(
            tmp_path, "sess", f"run_{scale}", _whitespace_heavy_journal(interim_count, span)
        )
        start = time.perf_counter()
        _rebuild(tmp_path, f"run_{scale}")
        times[scale] = time.perf_counter() - start

    ratio = times[4] / times[1]
    assert ratio < 8.0, (
        f"4x the interim events and span cost {ratio:.1f}x the time "
        f"({times}); a linear implementation stays near 4x, a quadratic one "
        "reaches 16x"
    )
