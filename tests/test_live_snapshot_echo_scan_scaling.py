"""Scaling regression for the journal-rebuild echo path (#7569).

A fixed-size fixture cannot catch a per-interim quadratic rescan: the shape
that exposes it is a whitespace span that grows in proportion to the interim
event count, so a quadratic implementation's cost grows with the square of
the row count while a linear one stays flat per row.

The retired implementations both rescan the raw reasoning transcript per
interim event (the folded-tail probe, then the window-free backward walk):
with a 1 MB whitespace span and 20 interim events that is 20M raw-character
walks.

Timing samples are taken as the FASTEST of several rebuilds of the same
journal. Scheduler interference (a shared CI runner, a GC pause, an
unrelated process) can only ever ADD wall-clock time, never remove it, so
the minimum is the estimator that is robust to that noise — whereas a
single sample at the small scale (a few ms) is entirely at the mercy of it.
A one-shot sample of the 1x journal measured ~6ms locally but ~683ms on a
loaded runner, which inverted a healthy linear ratio into a false
regression. Each shape is also rebuilt once as an unrecorded warm-up: the
first call pays module-import and page-cache costs, and it lands on the 1x
measurement — the denominator of the ratio — so leaving it in would pad
the baseline and MASK a real quadratic regression.

``LIVE_SNAPSHOT_SCALING_BITE=quadratic-echo-scan`` swaps the indexed
matcher back to the per-probe raw backward walk, so the gate's detection
power can be proved on demand rather than assumed.
"""
import json
import os
import time

import pytest

# Fastest-of-N sampling. 5 rebuilds of the largest (4x) shape cost ~110ms on
# the indexed path, so this stays cheap while pulling the estimator well clear
# of one-off scheduler noise.
_TIMING_SAMPLES = 5

# 4x the rows costs ~4x the time when linear and ~16x when quadratic, so a
# mid-point threshold separates the two shapes with roughly equal headroom.
_RATIO_LIMIT = 8.0


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


class _QuadraticEchoIndex:
    """Bite-only stand-in reproducing the pre-fix per-probe raw rescan.

    The retired matcher walked the raw transcript backwards once per interim
    event, so its cost was ``O(interim_count x whitespace_span)``. Enabled with
    ``LIVE_SNAPSHOT_SCALING_BITE=quadratic-echo-scan`` to prove the ratio gate
    below actually fails on that shape rather than merely looking tight on a
    healthy one.
    """

    def __init__(self) -> None:
        from api.streaming import _find_compact_echo_suffix_start

        self._raw: list[str] = []
        self._walk = _find_compact_echo_suffix_start

    def append(self, text: str) -> None:
        if text:
            self._raw.append(str(text))

    def reset(self) -> None:
        self._raw.clear()

    def matches_tail(self, suffix: str) -> bool:
        return self._walk("".join(self._raw), suffix) is not None

    def cut_to(self, folded_length: int) -> int | None:  # pragma: no cover
        return None


def _install_bite(monkeypatch):
    """Swap the indexed matcher for the quadratic one when the bite is armed."""
    if os.environ.get("LIVE_SNAPSHOT_SCALING_BITE", "").strip() != "quadratic-echo-scan":
        return False
    import api.routes as routes

    monkeypatch.setattr(routes, "_CompactEchoIndex", _QuadraticEchoIndex, raising=True)
    return True


def _fastest_rebuild_seconds(root, run_id, samples=_TIMING_SAMPLES):
    """Return the FASTEST of ``samples`` rebuilds of one journal shape.

    Interference can only add wall-clock time, so the minimum is the noise-
    robust estimator. The first rebuild is an unrecorded warm-up so import and
    page-cache costs never land in the 1x baseline — that sample is the ratio's
    denominator, and padding it would mask a real regression.
    """
    _rebuild(root, run_id)
    best = float("inf")
    for _ in range(samples):
        start = time.perf_counter()
        _rebuild(root, run_id)
        elapsed = time.perf_counter() - start
        if elapsed < best:
            best = elapsed
    return best


@pytest.mark.parametrize("scale", [1, 2, 4])
def test_rebuild_echo_scan_is_not_per_interim_quadratic(tmp_path, scale, monkeypatch):
    """Doubling span AND interim count must stay far under quadratic cost.

    The budget is a per-row constant: a quadratic implementation spends
    ``span * interim_count`` raw-character walks, a linear one spends
    ``span`` (folded once) plus ``O(interim_count * echo_len)``. The
    assertion below fails the quadratic shape by roughly an order of
    magnitude while leaving the linear one with generous headroom.
    """
    _install_bite(monkeypatch)
    interim_count = 20 * scale
    span = 100_000 * scale
    _write_journal(
        tmp_path, "sess", f"run_{scale}", _whitespace_heavy_journal(interim_count, span)
    )

    # Fastest-of-N, not a single sample: this gate is an absolute budget and a
    # loaded runner's one-off pause must not masquerade as a quadratic rescan.
    elapsed = _fastest_rebuild_seconds(tmp_path, f"run_{scale}")
    snapshot = _rebuild(tmp_path, f"run_{scale}")

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


def test_rebuild_scaling_is_subquadratic(tmp_path, monkeypatch):
    """Ratio check: 4x the rows must cost far less than 16x the time."""
    _install_bite(monkeypatch)
    times = {}
    for scale in (1, 2, 4):
        interim_count = 20 * scale
        span = 100_000 * scale
        _write_journal(
            tmp_path, "sess", f"run_{scale}", _whitespace_heavy_journal(interim_count, span)
        )
        times[scale] = _fastest_rebuild_seconds(tmp_path, f"run_{scale}")

    # A sub-millisecond baseline cannot support a ratio at all: timer
    # resolution and scheduler jitter would dominate both terms. Fail with a
    # clear reason rather than reporting a meaningless pass or a phantom
    # regression.
    assert times[1] >= 0.001, (
        f"the 1x baseline rebuild measured {times[1] * 1000:.3f}ms, too short to "
        "support a timing ratio; the fixture is not exercising the real path"
    )

    ratio = times[4] / times[1]
    assert ratio < _RATIO_LIMIT, (
        f"4x the interim events and span cost {ratio:.1f}x the time "
        f"({times}); a linear implementation stays near 4x, a quadratic one "
        "reaches 16x"
    )
