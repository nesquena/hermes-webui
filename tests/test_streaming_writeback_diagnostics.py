from pathlib import Path

import pytest

from api import streaming


class _FakeLog:
    def __init__(self):
        self.debug_calls = []

    def debug(self, *args):
        self.debug_calls.append(args)


def test_stream_writeback_diag_threshold_parsing():
    assert streaming._stream_writeback_diag_threshold_seconds({}) == pytest.approx(0.25)
    assert streaming._stream_writeback_diag_threshold_seconds({
        "HERMES_WEBUI_STREAM_WRITEBACK_DIAG_MS": "0",
    }) == 0.0
    assert streaming._stream_writeback_diag_threshold_seconds({
        "HERMES_WEBUI_STREAM_WRITEBACK_DIAG_MS": "-1",
    }) is None
    assert streaming._stream_writeback_diag_threshold_seconds({
        "HERMES_WEBUI_STREAM_WRITEBACK_DIAG_MS": "not-a-number",
    }) == pytest.approx(0.25)


def test_stream_writeback_stage_records_elapsed_time():
    ticks = iter([10.0, 10.125])
    timings = []

    with streaming._stream_writeback_stage(timings, "session_save", clock=lambda: next(ticks)):
        pass

    assert timings == [("session_save", pytest.approx(0.125))]


def test_stream_writeback_timing_log_respects_threshold():
    log = _FakeLog()

    emitted = streaming._log_stream_writeback_timings(
        "sid",
        "stream",
        [("session_save", 0.125)],
        1.0,
        clock=lambda: 1.2,
        log=log,
        environ={"HERMES_WEBUI_STREAM_WRITEBACK_DIAG_MS": "500"},
    )

    assert emitted is False
    assert log.debug_calls == []

    emitted = streaming._log_stream_writeback_timings(
        "sid",
        "stream",
        [("merge_result", 0.050), ("session_save", 0.125)],
        1.0,
        clock=lambda: 1.3,
        log=log,
        environ={"HERMES_WEBUI_STREAM_WRITEBACK_DIAG_MS": "250"},
    )

    assert emitted is True
    assert len(log.debug_calls) == 1
    args = log.debug_calls[0]
    assert args[0] == "stream final writeback timing session=%s stream=%s total=%.1fms stages=%s"
    assert args[1:4] == ("sid", "stream", pytest.approx(300.0))
    assert "merge_result=50.0ms" in args[4]
    assert "session_save=125.0ms" in args[4]


def test_stream_writeback_diagnostics_cover_final_writeback_stages():
    src = Path("api/streaming.py").read_text(encoding="utf-8")

    expected_stages = [
        "merge_result",
        "session_save",
        "persistent_state_scan",
        "state_sync",
        "done_payload",
    ]
    for stage in expected_stages:
        assert f'_stream_writeback_stage(_writeback_timings, "{stage}")' in src

    assert (
        'with _stream_writeback_stage(_writeback_timings, "session_save"):\n'
        '                    s.save()'
    ) in src
    assert src.index('with _stream_writeback_stage(_writeback_timings, "session_save")') < src.index(
        'with _stream_writeback_stage(_writeback_timings, "state_sync")'
    )
    assert src.index('with _stream_writeback_stage(_writeback_timings, "state_sync")') < src.index(
        'with _stream_writeback_stage(_writeback_timings, "done_payload")'
    )
