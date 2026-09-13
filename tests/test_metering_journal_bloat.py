"""Run-journal telemetry bloat regression tests.

A long, reasoning-heavy live run journals ``metering`` snapshots at ~10 Hz.
Because every journaled frame is later replayed to a reconnecting browser
(hard refresh mid-run), a 40-minute run can produce a multi-MB journal that is
overwhelmingly transient telemetry — the replay burst then kills the tab
(observed in production: 18 MB / 39k events, 97% metering + reasoning deltas;
browser OOM on refresh while the server stayed up).

The fix has two halves, each with a pinned invariant:

1. **Write time** — ``RunJournalWriter.append_sse_event`` (the shared
   chokepoint for every SSE frame streamed.py / gateway_chat.py journal via
   ``put()``) skips ``metering`` frames entirely. Metering is live-UI
   telemetry (TPS label, usage indicator): nothing reconstructs transcript
   state from it, so journaling it has no recovery value.
2. **Replay time** — legacy journals already contain metering rows, and the
   replay readers' cursor/coverage math MUST keep seeing them (filtering
   inside the readers would break ``_run_journal_covers_offline_gap``'s seq
   counting and ``read_session_run_events``' ``cursor_event_missing`` bound).
   So filtering happens at the SSE emit sites only.

This suite pins: the writer skip, the replay-visible predicate, the
readers-must-not-filter contract (cursor math on a legacy journal), and the
emit-site filters.
"""
from pathlib import Path

from api.run_journal import (
    RunJournalWriter,
    journal_replay_visible,
    read_run_events,
    read_session_run_events,
)


ROOT = Path(__file__).resolve().parents[1]
ROUTES_SRC = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")


def _write_legacy_journal_with_metering(tmp_path) -> Path:
    """Hand-write a journal shaped like pre-fix production data: metering
    rows interleaved with token rows, contiguous seqs."""
    import json

    journal_dir = tmp_path / "_run_journal" / "session_1"
    journal_dir.mkdir(parents=True)
    rows = []
    for seq in range(1, 7):
        if seq % 2 == 0:
            rows.append(
                {"event": "metering", "seq": seq, "event_id": f"run_1:{seq}",
                 "run_id": "run_1", "session_id": "session_1", "payload": {"tps": 1}}
            )
        else:
            rows.append(
                {"event": "token", "seq": seq, "event_id": f"run_1:{seq}",
                 "run_id": "run_1", "session_id": "session_1", "payload": {"text": f"t{seq}"}}
            )
    path = journal_dir / "run_1.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    return path


# ── Write-time skip ──────────────────────────────────────────────────────────


def test_writer_does_not_journal_metering(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    journaled_metering = writer.append_sse_event("metering", {"tps": 12.5})
    first = writer.append_sse_event("token", {"text": "hello"})
    journaled_metering_2 = writer.append_sse_event("metering", {"tps": 10.1})
    second = writer.append_sse_event("token", {"text": "world"})
    done = writer.append_sse_event("done", {"session": {"session_id": "session_1"}})

    # Metering frames return a not-journaled marker (falsy, no event_id) so
    # put() callers leave their journal-id plumbing untouched.
    assert not journaled_metering
    assert not journaled_metering_2
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert done["seq"] == 3
    assert done["terminal"] is True

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)
    names = [event["event"] for event in journal["events"]]

    # Content and terminal events survive; telemetry does not.
    assert names == ["token", "token", "done"]
    # Seqs stay gapless and contiguous after the skips.
    assert [event["seq"] for event in journal["events"]] == [1, 2, 3]


# ── Replay-visible predicate ─────────────────────────────────────────────────


def test_journal_replay_visible_skips_only_metering():
    assert journal_replay_visible({"event": "metering"}) is False
    assert journal_replay_visible({"event": "token"}) is True
    assert journal_replay_visible({"event": "reasoning"}) is True
    assert journal_replay_visible({"event": "done"}) is True
    # Fail open to content: unknown / missing names stay visible.
    assert journal_replay_visible({"event": "future_event"}) is True
    assert journal_replay_visible({}) is True
    assert journal_replay_visible("not-a-dict") is True


# ── Readers must NOT filter (cursor + coverage contract) ─────────────────────


def test_per_run_reader_keeps_metering_rows_for_cursor_math(tmp_path):
    """read_run_events feeds _run_journal_covers_offline_gap, which counts
    seqs in (floor, cutoff] to prove an offline gap is backfilled. Filtering
    metering rows here would make coverage falsely fail on legacy journals."""
    _write_legacy_journal_with_metering(tmp_path)

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)
    assert [event["seq"] for event in journal["events"]] == [1, 2, 3, 4, 5, 6]
    assert journal["events"][1]["event"] == "metering"


def test_session_replay_reader_keeps_metering_rows_for_cursor_math(tmp_path):
    """read_session_run_events must resolve a cursor pointing at a HIGH seq
    even when most rows below it are metering (the production fat journal:
    18k metering rows interleaved with content). Filtering would trip
    cursor_event_missing and break resume entirely."""
    _write_legacy_journal_with_metering(tmp_path)

    result = read_session_run_events(
        "session_1", after_event_id="run_1:5", session_dir=tmp_path
    )
    assert result["status"] == "ok"
    assert [event["seq"] for event in result["events"]] == [6]


# ── Emit-site filters (static contract, mirrors repo's static-test style) ────


def test_per_run_replay_emitter_filters_metering():
    """_replay_run_journal serves the dead-stream replay and the offline-gap
    replay; its emit loop must skip metering rows."""
    replay_idx = ROUTES_SRC.index("def _replay_run_journal(")
    replay_end = ROUTES_SRC.index("def _run_journal_same_run_seq(", replay_idx)
    replay_block = ROUTES_SRC[replay_idx:replay_end]
    assert "journal_replay_visible" in replay_block, (
        "_replay_run_journal must filter rows via journal_replay_visible"
    )


def test_cross_run_replay_emitter_filters_metering():
    """emit_replay (cross-run session replay in the session-events SSE
    handler) must skip metering rows before writing SSE frames."""
    emit_idx = ROUTES_SRC.index("def emit_replay(")
    emit_end = ROUTES_SRC.index("def emit_session_snapshot(", emit_idx)
    emit_block = ROUTES_SRC[emit_idx:emit_end]
    assert "journal_replay_visible" in emit_block, (
        "emit_replay must filter rows via journal_replay_visible"
    )


def test_writer_skip_is_in_the_shared_chokepoint():
    """The write-time skip must live in RunJournalWriter.append_sse_event —
    the one place both streaming.put() and gateway_chat.put_gateway_event()
    journal through — not duplicated at each producer."""
    journal_src = (ROOT / "api" / "run_journal.py").read_text(encoding="utf-8")
    append_idx = journal_src.index("def append_sse_event(self, event_name")
    body_end = journal_src.index("def read_run_events(", append_idx)
    append_block = journal_src[append_idx:body_end]
    assert "REPLAY_SKIPPED_SSE_EVENTS" in append_block, (
        "append_sse_event must skip journaling REPLAY_SKIPPED_SSE_EVENTS"
    )
