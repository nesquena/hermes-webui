import base64
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

import api.run_journal as run_journal
from api.run_journal import (
    RunJournalWriter as _RunJournalWriter,
    append_run_event as _append_run_event,
    find_run_summary,
    latest_run_summary,
    read_run_events,
    stale_interrupted_event,
)


def _activate(session_id, session_dir, *, reactivate_retired=False):
    return run_journal.activate_run_journal_session(
        session_id,
        session_dir=session_dir,
        reactivate_retired=reactivate_retired,
    )


def RunJournalWriter(session_id, run_id, *, session_dir=None, reactivate_retired=False):
    return _RunJournalWriter(
        session_id,
        run_id,
        session_dir=session_dir,
        incarnation=_activate(
            session_id,
            session_dir,
            reactivate_retired=reactivate_retired,
        ),
    )


def append_run_event(session_id, run_id, event_name, payload=None, **kwargs):
    session_dir = kwargs.get("session_dir")
    return _append_run_event(
        session_id,
        run_id,
        event_name,
        payload,
        **kwargs,
        _incarnation=_activate(session_id, session_dir),
    )


def test_run_journal_appends_monotonic_seq_and_reads_after_cursor(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    first = writer.append_sse_event("token", {"text": "hello"})
    second = writer.append_sse_event("done", {"session": {"session_id": "session_1"}})

    assert first["seq"] == 1
    assert first["event_id"] == "run_1:1"
    assert first["terminal"] is False
    assert second["seq"] == 2
    assert second["terminal"] is True
    assert second["terminal_state"] == "completed"

    journal = read_run_events("session_1", "run_1", after_seq=1, session_dir=tmp_path)
    assert [event["event"] for event in journal["events"]] == ["done"]


def test_run_journal_reads_bounded_replay_window(tmp_path):
    writer = RunJournalWriter("session_1", "run_1", session_dir=tmp_path)

    writer.append_sse_event("token", {"text": "one"})
    writer.append_sse_event("token", {"text": "two"})
    writer.append_sse_event("token", {"text": "three"})
    writer.append_sse_event("token", {"text": "four"})

    journal = read_run_events(
        "session_1",
        "run_1",
        after_seq=1,
        max_seq=3,
        session_dir=tmp_path,
    )

    assert [event["seq"] for event in journal["events"]] == [2, 3]
    assert [event["payload"]["text"] for event in journal["events"]] == ["two", "three"]


def test_run_journal_legacy_overcap_terminal_respects_exhausted_row_cap(tmp_path):
    session_id = "session_legacy_overcap_row_cap"
    run_id = "run_legacy_overcap_row_cap"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    ordinary = {
        "version": 1, "event_id": f"{run_id}:1", "seq": 1, "run_id": run_id,
        "session_id": session_id, "event": "token", "type": "token", "payload": {"text": "first"},
    }
    oversized_terminal = {
        "version": 1, "event_id": f"{run_id}:2", "seq": 2, "run_id": run_id,
        "session_id": session_id, "event": "done", "type": "done", "terminal": True,
        "terminal_state": "completed", "payload": {"session": {"messages": [{"content": "x" * 1000}]}},
    }
    path.write_text(
        json.dumps(ordinary, separators=(",", ":")) + "\n"
        + json.dumps(oversized_terminal, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=512, max_rows=1,
    )

    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["complete"] is False
    assert journal["limit_reason"] == "replay_limit_rows"
    assert journal["malformed"] == [{"line": 2, "reason": "replay_limit_rows"}]


def test_run_journal_bounded_reader_keeps_suffix_after_large_prefix(tmp_path):
    session_id = "session_bounded_suffix"
    run_id = "run_bounded_suffix"
    append_run_event(session_id, run_id, "token", {"text": "x" * 2048}, session_dir=tmp_path, seq=1)
    append_run_event(session_id, run_id, "done", {"session": {}}, session_dir=tmp_path, seq=2)

    journal = read_run_events(
        session_id, run_id, after_seq=1, session_dir=tmp_path, max_bytes=512, max_rows=1,
    )

    assert journal["complete"] is True
    assert [event["seq"] for event in journal["events"]] == [2]


def test_run_journal_recovery_marker_is_inclusive_of_byte_cap(tmp_path):
    session_id = "session_marker_cap"
    run_id = "run_marker_cap"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    oversized_terminal = {
        "version": 1, "event_id": f"{run_id}:1", "seq": 1, "run_id": run_id,
        "session_id": session_id, "event": "done", "type": "done", "terminal": True,
        "terminal_state": "completed", "payload": {"session": {"messages": [{"content": "x" * 1000}]}},
    }
    path.write_text(json.dumps(oversized_terminal) + "\n", encoding="utf-8")

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=512, max_rows=1,
    )

    assert journal["complete"] is True
    assert journal["events"][0]["payload"]["terminal_disposition"]["kind"] == "consumed_non_materializable"
    assert run_journal._serialized_event_size(journal["events"][0]) <= 512


def test_run_journal_bounded_reader_advances_past_oversized_nonterminal_row(tmp_path):
    session_id = "session_bounded_cursor"
    run_id = "run_bounded_cursor"
    append_run_event(session_id, run_id, "token", {"text": "x" * 2048}, session_dir=tmp_path, seq=1)
    append_run_event(session_id, run_id, "done", {"session": {}}, session_dir=tmp_path, seq=2)

    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=64, max_rows=1,
    )
    second_page = read_run_events(
        session_id, run_id, after_seq=first_page["next_after_seq"],
        session_dir=tmp_path, max_bytes=512, max_rows=1,
    )

    assert first_page["events"] == []
    assert first_page["complete"] is False
    assert first_page["limit_reason"] == "replay_limit_bytes"
    assert first_page["next_after_seq"] == 1
    assert [event["seq"] for event in second_page["events"]] == [2]


def test_run_journal_bounded_reader_ignores_overcap_row_outside_window(tmp_path):
    session_id = "session_bounded_window"
    run_id = "run_bounded_window"
    append_run_event(session_id, run_id, "token", {"text": "ok"}, session_dir=tmp_path, seq=1)
    append_run_event(session_id, run_id, "done", {"session": {"messages": [{"content": "x" * 1000}]}}, session_dir=tmp_path, seq=2)

    journal = read_run_events(
        session_id, run_id, max_seq=1, session_dir=tmp_path, max_bytes=512, max_rows=1,
    )

    assert journal["complete"] is True
    assert journal["limit_reason"] is None
    assert [event["seq"] for event in journal["events"]] == [1]


def test_run_journal_bounded_reader_limits_line_read_before_decode(tmp_path, monkeypatch):
    session_id = "session_bounded_read"
    run_id = "run_bounded_read"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    oversized_row = (
        b'{"version":1,"event_id":"'
        + f"{run_id}:1".encode("utf-8")
        + b'","seq":1,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","event":"token","type":"token","payload":{"text":"'
        + b"x" * (run_journal._LEGACY_TERMINAL_RECOVERY_MAX_BYTES + 1)
        + b'"}}\n'
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(oversized_row)

    class BoundedReader:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def readline(self, limit):
            assert 0 < limit <= run_journal._SESSION_REPLAY_READ_CHUNK_BYTES
            return self._fh.readline(limit)

    real_open = Path.open

    def patched_open(candidate, *args, **kwargs):
        fh = real_open(candidate, *args, **kwargs)
        return BoundedReader(fh) if candidate == path else fh

    monkeypatch.setattr(Path, "open", patched_open)
    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=512,
    )

    assert journal["complete"] is False
    assert journal["limit_reason"] == "replay_limit_bytes"
    assert journal["next_after_seq"] == 1


def _bounded_event(session_id, run_id, seq, text):
    return {
        "version": 1,
        "event_id": f"{run_id}:{seq}",
        "seq": seq,
        "run_id": run_id,
        "session_id": session_id,
        "event": "token",
        "type": "token",
        "terminal": False,
        "terminal_state": None,
        "payload": {"text": text},
    }


def _write_bounded_events(path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"".join(
            json.dumps(event, separators=(",", ":")).encode("utf-8") + b"\n"
            for event in events
        )
    )


def _tamper_resume_token(token, **changes):
    payload_token, separator, signature = token.partition(".")
    padding = "=" * (-len(payload_token) % 4)
    payload = json.loads(
        base64.urlsafe_b64decode(payload_token + padding).decode("ascii")
    )
    payload.update(changes)
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).rstrip(b"=").decode("ascii")
    return f"{encoded}.{signature}" if separator else encoded


def test_run_journal_bounded_reader_drains_actual_hard_row_before_next_page(tmp_path):
    session_id = "session_hard_row_resume"
    run_id = "run_hard_row_resume"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    hard_row = _bounded_event(
        session_id,
        run_id,
        1,
        "x" * (run_journal._LEGACY_TERMINAL_RECOVERY_MAX_BYTES + 1),
    )
    following = _bounded_event(session_id, run_id, 2, "following")
    _write_bounded_events(path, [hard_row, following])

    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=512, max_rows=1,
    )
    second_page = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        session_dir=tmp_path,
        max_bytes=512,
        max_rows=1,
    )

    assert first_page["events"] == []
    assert first_page["complete"] is False
    assert first_page["limit_reason"] == "replay_limit_bytes"
    assert first_page["next_after_seq"] == 1
    assert [event["seq"] for event in second_page["events"]] == [2]
    assert second_page["complete"] is True


def test_run_journal_bounded_reader_row_pages_do_not_skip_candidate(tmp_path):
    session_id = "session_row_pages"
    run_id = "run_row_pages"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "one"),
            _bounded_event(session_id, run_id, 2, "two"),
            _bounded_event(session_id, run_id, 3, "three"),
        ],
    )

    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_rows=1,
    )
    second_page = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        session_dir=tmp_path,
        max_rows=1,
    )
    third_page = read_run_events(
        session_id,
        run_id,
        after_seq=second_page["next_after_seq"],
        session_dir=tmp_path,
        max_rows=1,
    )

    assert [event["seq"] for event in first_page["events"]] == [1]
    assert first_page["complete"] is False
    assert first_page["limit_reason"] == "replay_limit_rows"
    assert first_page["next_after_seq"] == 1
    assert [event["seq"] for event in second_page["events"]] == [2]
    assert second_page["complete"] is False
    assert second_page["limit_reason"] == "replay_limit_rows"
    assert second_page["next_after_seq"] == 2
    assert [event["seq"] for event in third_page["events"]] == [3]
    assert third_page["complete"] is True


def test_run_journal_bounded_reader_byte_pages_do_not_skip_candidate(tmp_path):
    for case_name in ("ordinary", "terminal"):
        session_id = f"session_byte_pages_{case_name}"
        run_id = f"run_byte_pages_{case_name}"
        path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
        first = _bounded_event(session_id, run_id, 1, "a" * 80)
        second = _bounded_event(session_id, run_id, 2, "b" * 80)
        if case_name == "terminal":
            second.update({
                "event": "done",
                "type": "done",
                "terminal": True,
                "terminal_state": "completed",
                "payload": {"session": {"messages": [{"content": "b" * 80}]}},
            })
        _write_bounded_events(path, [first, second])
        page_bytes = max(
            run_journal._serialized_event_size(first),
            run_journal._serialized_event_size(second),
        )

        first_page = read_run_events(
            session_id, run_id, session_dir=tmp_path, max_bytes=page_bytes,
        )
        second_page = read_run_events(
            session_id,
            run_id,
            after_seq=first_page["next_after_seq"],
            session_dir=tmp_path,
            max_bytes=page_bytes,
        )

        assert [event["seq"] for event in first_page["events"]] == [1]
        assert first_page["complete"] is False
        assert first_page["limit_reason"] == "replay_limit_bytes"
        assert first_page["next_after_seq"] == 1
        assert [event["seq"] for event in second_page["events"]] == [2]
        assert second_page["next_after_seq"] == 2
        assert second_page["complete"] is True
        if case_name == "terminal":
            assert "terminal_disposition" not in second_page["events"][0]["payload"]


def test_run_journal_bounded_reader_filters_hard_row_at_after_seq(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 256)
    session_id = "session_hard_after"
    run_id = "run_hard_after"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "x" * 512),
            _bounded_event(session_id, run_id, 2, "visible"),
        ],
    )

    journal = read_run_events(
        session_id,
        run_id,
        after_seq=1,
        session_dir=tmp_path,
        max_bytes=512,
        max_rows=1,
    )

    assert [event["seq"] for event in journal["events"]] == [2]
    assert journal["complete"] is True


def test_run_journal_bounded_reader_filters_hard_row_above_max_seq(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 256)
    session_id = "session_hard_ceiling"
    run_id = "run_hard_ceiling"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "visible"),
            _bounded_event(session_id, run_id, 2, "x" * 512),
        ],
    )

    journal = read_run_events(
        session_id,
        run_id,
        max_seq=1,
        session_dir=tmp_path,
        max_bytes=512,
        max_rows=1,
    )

    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["complete"] is True
    assert journal["limit_reason"] is None


def test_run_journal_bounded_reader_ignores_nested_seq_in_hard_row(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 256)
    session_id = "session_nested_seq"
    run_id = "run_nested_seq"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    hard_row = (
        b'{"payload":{"seq":999,"text":"'
        + b"x" * 512
        + b'"},"version":1,"event_id":"'
        + f"{run_id}:1".encode("utf-8")
        + b'","seq":1,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","event":"token","type":"token"}\n'
    )
    following = _bounded_event(session_id, run_id, 2, "following")
    path.write_bytes(
        hard_row
        + json.dumps(following, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=128,
    )
    second_page = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        session_dir=tmp_path,
        max_bytes=512,
    )

    assert first_page["next_after_seq"] == 1
    assert [event["seq"] for event in second_page["events"]] == [2]


def test_run_journal_bounded_reader_rejects_malformed_hard_row_before_cursor_advance(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 256)
    session_id = "session_malformed_hard_row"
    run_id = "run_malformed_hard_row"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    malformed = (
        b'{"version":1,"event_id":"'
        + f"{run_id}:999".encode("utf-8")
        + b'","seq":999,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","event":"token","type":"token","payload":{"text":"'
        + b"x" * 512
        + b'"},}\n'
    )
    valid = _bounded_event(session_id, run_id, 1, "valid")
    path.write_bytes(
        malformed
        + json.dumps(valid, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=4096, max_rows=2,
    )

    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["malformed"] == [
        {"line": 1, "reason": "replay_invalid_envelope"},
    ]
    assert journal["next_after_seq"] == 1
    assert journal["complete"] is True


@pytest.mark.parametrize(
    ("payload", "root_suffix"),
    [
        (b'{"text":"' + b"x" * 512 + b'",}', b',"event":"token"}\n'),
        (b'["' + b"x" * 512 + b'",]', b',"event":"token"}\n'),
        (b'{"text":"' + b"x" * 512 + b'" "other":1}', b"}\n"),
        (b'{"text":"' + b"x" * 512 + b'","other":}', b"}\n"),
        (b'{"text":"' + b"x" * 512 + b'","other":truth}', b"}\n"),
        (b'{"text":"' + b"x" * 512 + b'\\q"}', b"}\n"),
        (b'{"text":"' + b"x" * 512 + b'\xff"}', b"}\n"),
        (b'{"text":"' + b"x" * 512 + b'"', b"\n"),
        (b'{"text":"' + b"x" * 512 + b'"}', b',"seq":998}\n'),
    ],
    ids=[
        "trailing-object-comma",
        "trailing-array-comma",
        "missing-separator",
        "missing-value",
        "invalid-literal",
        "invalid-escape",
        "invalid-utf8",
        "truncated-container",
        "duplicate-authority",
    ],
)
def test_run_journal_bounded_reader_rejects_invalid_hard_row_grammar(
    tmp_path,
    monkeypatch,
    payload,
    root_suffix,
):
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 256)
    session_id = "session_invalid_hard_grammar"
    run_id = "run_invalid_hard_grammar"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    malformed = (
        b'{"version":1,"event_id":"'
        + f"{run_id}:999".encode("utf-8")
        + b'","seq":999,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","payload":'
        + payload
        + root_suffix
    )
    valid = _bounded_event(session_id, run_id, 1, "valid")
    path.write_bytes(
        malformed
        + json.dumps(valid, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=4096, max_rows=2,
    )

    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["malformed"][0]["line"] == 1
    assert journal["next_after_seq"] == 1
    assert journal["complete"] is True


def test_run_journal_bounded_reader_pages_hard_terminal_recovery_marker(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 256)
    session_id = "session_hard_terminal_marker"
    run_id = "run_hard_terminal_marker"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    first = _bounded_event(session_id, run_id, 1, "first")
    terminal = _bounded_event(session_id, run_id, 2, "unused")
    terminal.update({
        "event": "done",
        "type": "done",
        "terminal": True,
        "terminal_state": "completed",
        "payload": {"session": {"messages": [{"content": "x" * 512}]}},
    })
    _write_bounded_events(path, [first, terminal])
    marker = run_journal._recover_legacy_overcap_terminal_event(
        terminal,
        session_id=session_id,
        run_id=run_id,
        max_seq=None,
    )
    page_bytes = max(
        run_journal._serialized_event_size(first),
        run_journal._serialized_event_size(marker),
    )

    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=page_bytes,
    )
    second_page = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        session_dir=tmp_path,
        max_bytes=page_bytes,
    )

    assert [event["seq"] for event in first_page["events"]] == [1]
    assert first_page["complete"] is False
    assert first_page["limit_reason"] == "replay_limit_bytes"
    assert first_page["next_after_seq"] == 1
    assert [event["seq"] for event in second_page["events"]] == [2]
    assert second_page["events"][0]["payload"]["terminal_disposition"] == {
        "version": "terminal_disposition_v1",
        "kind": "consumed_non_materializable",
        "reason": "legacy_terminal_payload_too_large",
        "session_id": session_id,
        "run_id": run_id,
        "stream_id": run_id,
    }
    assert second_page["complete"] is True
    assert second_page["next_after_seq"] == 2


@pytest.mark.parametrize("hard_row", [False, True], ids=["retained-row", "hard-row"])
def test_run_journal_bounded_reader_retries_terminal_marker_after_too_small_page(
    tmp_path,
    monkeypatch,
    hard_row,
):
    threshold = 256 if hard_row else 4096
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", threshold)
    session_id = f"session_terminal_retry_{hard_row}"
    run_id = f"run_terminal_retry_{hard_row}"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    terminal = _bounded_event(session_id, run_id, 1, "unused")
    terminal.update({
        "event": "done",
        "type": "done",
        "terminal": True,
        "terminal_state": "completed",
        "payload": {"session": {"messages": [{"content": "x" * 1024}]}},
    })
    _write_bounded_events(path, [terminal])
    marker = run_journal._recover_legacy_overcap_terminal_event(
        terminal,
        session_id=session_id,
        run_id=run_id,
        max_seq=None,
    )
    marker_size = run_journal._serialized_event_size(marker)

    first_page = read_run_events(
        session_id,
        run_id,
        session_dir=tmp_path,
        max_bytes=marker_size - 1,
        max_rows=1,
    )
    second_page = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_bytes=marker_size,
        max_rows=1,
    )

    assert first_page["events"] == []
    assert first_page["complete"] is False
    assert first_page["limit_reason"] == "replay_limit_bytes"
    assert first_page["next_after_seq"] == 0
    assert first_page["resume_token"]
    assert [event["seq"] for event in second_page["events"]] == [1]
    assert second_page["events"][0]["payload"]["terminal_disposition"]["kind"] == (
        "consumed_non_materializable"
    )
    assert second_page["complete"] is True


def test_run_journal_bounded_reader_resume_token_skips_delivered_prefix(tmp_path, monkeypatch):
    session_id = "session_resume_offset"
    run_id = "run_resume_offset"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    first = _bounded_event(session_id, run_id, 1, "one")
    second = _bounded_event(session_id, run_id, 2, "two")
    _write_bounded_events(path, [first, second])
    expected_offset = len(json.dumps(first, separators=(",", ":")).encode("utf-8")) + 1

    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_rows=1,
    )
    starts = []
    original_reader = run_journal._read_bounded_physical_row

    def tracked_reader(fh, *args, **kwargs):
        starts.append(fh.tell())
        return original_reader(fh, *args, **kwargs)

    monkeypatch.setattr(run_journal, "_read_bounded_physical_row", tracked_reader)
    second_page = read_run_events(
        session_id,
        run_id,
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_rows=1,
    )

    assert starts[0] == expected_offset
    assert [event["seq"] for event in second_page["events"]] == [2]
    assert second_page["complete"] is True


def test_run_journal_bounded_reader_resume_token_survives_append(tmp_path):
    session_id = "session_resume_append"
    run_id = "run_resume_append"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    first = _bounded_event(session_id, run_id, 1, "one")
    second = _bounded_event(session_id, run_id, 2, "two")
    third = _bounded_event(session_id, run_id, 3, "three")
    _write_bounded_events(path, [first, second])
    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_rows=1,
    )
    with path.open("ab") as fh:
        fh.write(json.dumps(third, separators=(",", ":")).encode("utf-8") + b"\n")

    second_page = read_run_events(
        session_id,
        run_id,
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_rows=2,
    )

    assert [event["seq"] for event in second_page["events"]] == [2, 3]
    assert second_page["complete"] is True


def test_run_journal_resume_token_missing_journal_fails_closed(tmp_path):
    session_id = "session_resume_missing_journal"
    run_id = "run_resume_missing_journal"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "one"),
            _bounded_event(session_id, run_id, 2, "two"),
        ],
    )
    first_page = read_run_events(session_id, run_id, session_dir=tmp_path, max_rows=1)
    path.unlink()

    resumed = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_rows=1,
    )

    assert resumed["events"] == []
    assert resumed["complete"] is False
    assert resumed["limit_reason"] == "replay_cursor_invalid"
    assert resumed["resume_token"] is None


def test_run_journal_resume_token_missing_generation_fails_closed(tmp_path):
    session_id = "session_resume_missing_generation"
    run_id = "run_resume_missing_generation"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "one"),
            _bounded_event(session_id, run_id, 2, "two"),
        ],
    )
    first_page = read_run_events(session_id, run_id, session_dir=tmp_path, max_rows=1)
    run_journal._run_generation_path(path).unlink()

    resumed = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_rows=1,
    )

    assert resumed["events"] == []
    assert resumed["complete"] is False
    assert resumed["limit_reason"] == "replay_cursor_invalid"
    assert resumed["resume_token"] is None


def test_run_journal_append_quiesces_before_concurrent_delete(tmp_path, monkeypatch):
    session_id = "session_append_delete_barrier"
    run_id = "run_append_delete_barrier"
    append_ready = threading.Event()
    allow_seq = threading.Event()
    delete_started = threading.Event()
    delete_attempted = threading.Event()
    delete_done = threading.Event()
    writes_after_delete: list[bool] = []
    append_result: list[dict] = []
    append_errors: list[BaseException] = []

    real_next_seq = run_journal._next_seq

    def blocked_next_seq(path):
        append_ready.set()
        assert allow_seq.wait(timeout=10)
        return real_next_seq(path)

    monkeypatch.setattr(run_journal, "_next_seq", blocked_next_seq)
    real_fdopen = run_journal.os.fdopen

    def tracked_fdopen(fd, mode="r", *args, **kwargs):
        fh = real_fdopen(fd, mode, *args, **kwargs)
        if mode != "a":
            return fh

        class TrackedFile:
            def __enter__(self):
                fh.__enter__()
                return self

            def __exit__(self, *exc_info):
                return fh.__exit__(*exc_info)

            def write(self, data):
                if delete_done.is_set():
                    writes_after_delete.append(True)
                return fh.write(data)

            def __getattr__(self, name):
                return getattr(fh, name)

        return TrackedFile()

    monkeypatch.setattr(run_journal.os, "fdopen", tracked_fdopen)
    real_authority = run_journal._run_journal_lifecycle_authority

    @contextmanager
    def tracked_authority(path):
        if path.name == ".delete.jsonl":
            delete_attempted.set()
        with real_authority(path):
            yield

    monkeypatch.setattr(run_journal, "_run_journal_lifecycle_authority", tracked_authority)

    def append():
        try:
            append_result.append(
                append_run_event(
                    session_id,
                    run_id,
                    "token",
                    {"text": "append"},
                    session_dir=tmp_path,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - test records worker failure
            append_errors.append(exc)

    def delete():
        delete_started.set()
        run_journal.delete_run_journal(session_id, session_dir=tmp_path)
        delete_done.set()

    append_thread = threading.Thread(target=append)
    append_thread.start()
    assert append_ready.wait(timeout=10)
    delete_thread = threading.Thread(target=delete)
    delete_thread.start()
    assert delete_started.wait(timeout=10)
    assert delete_attempted.wait(timeout=10)
    assert not delete_done.is_set(), "delete must wait for the append transaction"
    allow_seq.set()
    append_thread.join(timeout=10)
    delete_thread.join(timeout=10)

    assert not append_thread.is_alive()
    assert not delete_thread.is_alive()
    assert not append_errors
    assert append_result and append_result[0]["seq"] == 1
    assert not writes_after_delete


def test_run_journal_delete_retires_writer_admitted_before_delete(
    tmp_path, monkeypatch
):
    session_id = "session_writer_recreate_barrier"
    run_id = "run_writer_recreate_barrier"
    append_run_event(
        session_id,
        run_id,
        "token",
        {"text": "seed"},
        session_dir=tmp_path,
        seq=1,
    )
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    with run_journal._SEQ_CACHE_LOCK:
        run_journal._SEQ_CACHE.pop(str(path), None)

    old_ready = threading.Event()
    allow_old = threading.Event()
    delete_started = threading.Event()
    delete_attempted = threading.Event()
    delete_acquired = threading.Event()
    old_validated = threading.Event()
    old_result: list[dict] = []
    errors: list[BaseException] = []
    real_next_seq = run_journal._next_seq
    real_authority = run_journal._run_journal_lifecycle_authority
    real_discard_summary = run_journal._discard_cached_summary

    def blocked_next_seq(candidate_path):
        old_ready.set()
        assert allow_old.wait(timeout=10)
        return real_next_seq(candidate_path)

    monkeypatch.setattr(run_journal, "_next_seq", blocked_next_seq)

    @contextmanager
    def tracked_authority(candidate_path):
        is_delete = candidate_path.name == ".delete.jsonl"
        if is_delete:
            delete_attempted.set()
        with real_authority(candidate_path):
            if is_delete:
                delete_acquired.set()
            yield

    monkeypatch.setattr(run_journal, "_run_journal_lifecycle_authority", tracked_authority)

    def tracked_discard_summary(candidate_path):
        if candidate_path == path:
            old_validated.set()
        real_discard_summary(candidate_path)

    monkeypatch.setattr(run_journal, "_discard_cached_summary", tracked_discard_summary)
    old_writer_obj = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    recreated_writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)

    def old_writer():
        try:
            old_result.append(
                old_writer_obj.append_sse_event("token", {"text": "old"})
            )
        except BaseException as exc:  # noqa: BLE001 - test records worker failure
            errors.append(exc)

    def delete():
        delete_started.set()
        run_journal.delete_run_journal(session_id, session_dir=tmp_path)

    old_thread = threading.Thread(target=old_writer)
    old_thread.start()
    assert old_ready.wait(timeout=10)
    delete_thread = threading.Thread(target=delete)
    delete_thread.start()
    assert delete_started.wait(timeout=10)
    assert delete_attempted.wait(timeout=10)
    assert not delete_acquired.is_set()
    allow_old.set()
    old_thread.join(timeout=10)
    delete_thread.join(timeout=10)

    assert not old_thread.is_alive()
    assert not delete_thread.is_alive()
    assert not errors
    assert delete_acquired.is_set()
    assert old_validated.is_set()
    assert old_result and old_result[0]["seq"] == 2
    with pytest.raises(RuntimeError, match="run journal writer incarnation retired"):
        recreated_writer.append_sse_event("token", {"text": "stale"})
    assert not path.parent.exists()
    with run_journal._WRITER_LOCKS_GUARD:
        assert not any(key[0] == str(path.parent) for key in run_journal._WRITER_LOCKS)
    with run_journal._SEQ_CACHE_LOCK:
        assert str(path) not in run_journal._SEQ_CACHE
    with run_journal._SUMMARY_CACHE_LOCK:
        assert str(path) not in run_journal._SUMMARY_CACHE

    with pytest.raises(RuntimeError, match="run journal writer incarnation retired"):
        RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    fresh_writer = RunJournalWriter(
        session_id,
        run_id,
        session_dir=tmp_path,
        reactivate_retired=True,
    )
    new_event = fresh_writer.append_sse_event("token", {"text": "new"})
    assert new_event["seq"] == 1
    final_events = read_run_events(session_id, run_id, session_dir=tmp_path)["events"]
    assert [event["seq"] for event in final_events] == [1]
    assert final_events[0]["payload"]["text"] == "new"


def test_run_journal_delete_retires_writer_admitted_in_another_process(tmp_path):
    session_id = "session_cross_process_writer_retirement"
    run_id = "run_cross_process_writer_retirement"
    coordination = tmp_path / "writer_retirement_coordination"
    coordination.mkdir()
    writer_process = r'''
import sys
import time
from pathlib import Path

from api.run_journal import RunJournalWriter, activate_run_journal_session

root = Path(sys.argv[1])
coordination = Path(sys.argv[2])
incarnation = activate_run_journal_session(sys.argv[3], session_dir=root)
writer = RunJournalWriter(
    sys.argv[3], sys.argv[4], session_dir=root, incarnation=incarnation
)
(coordination / "writer_ready").touch()
deadline = time.monotonic() + 15
while not (coordination / "append_after_delete").exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("append_after_delete")
    time.sleep(0.01)
try:
    writer.append_sse_event("token", {"text": "stale"})
except RuntimeError as exc:
    if str(exc) != "run journal writer incarnation retired":
        raise
    (coordination / "writer_retired").touch()
else:
    raise AssertionError("pre-delete writer recreated a deleted journal")
'''

    def wait_for(name, timeout=15):
        deadline = time.monotonic() + timeout
        marker = coordination / name
        while not marker.exists():
            if time.monotonic() >= deadline:
                pytest.fail(f"timed out waiting for {name}")
            time.sleep(0.01)

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            writer_process,
            str(tmp_path),
            str(coordination),
            session_id,
            run_id,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for("writer_ready")
    path = run_journal._run_path(session_id, run_id, session_dir=tmp_path)
    RunJournalWriter(session_id, run_id, session_dir=tmp_path).append_sse_event(
        "token", {"text": "seed"}
    )
    assert run_journal.delete_run_journal(session_id, session_dir=tmp_path) is True
    assert not path.parent.exists()

    (coordination / "append_after_delete").touch()
    output, error = process.communicate(timeout=20)
    assert process.returncode == 0, (output, error)
    assert (coordination / "writer_retired").exists()
    assert not path.parent.exists()

    with pytest.raises(RuntimeError, match="run journal writer incarnation retired"):
        RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    fresh_event = RunJournalWriter(
        session_id,
        run_id,
        session_dir=tmp_path,
        reactivate_retired=True,
    ).append_sse_event("token", {"text": "fresh"})
    assert fresh_event["seq"] == 1


def test_run_journal_failed_final_validation_discards_post_write_caches(
    tmp_path, monkeypatch
):
    session_id = "session_failed_final_validation"
    run_id = "run_failed_final_validation"
    writer = RunJournalWriter(session_id, run_id, session_dir=tmp_path)
    writer.append_sse_event("token", {"text": "seed"})
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    latest_run_summary(session_id, run_id, session_dir=tmp_path)
    with run_journal._SEQ_CACHE_LOCK:
        assert run_journal._SEQ_CACHE[str(path)][2] == 2

    real_read_generation = run_journal._read_run_generation_record
    generation_reads = 0

    def return_mismatched_generation(candidate_path):
        nonlocal generation_reads
        record = real_read_generation(candidate_path)
        if candidate_path == path:
            generation_reads += 1
            if generation_reads == 2:
                assert record is not None
                wrong_generation = "0" * 32
                if record[0] == wrong_generation:
                    wrong_generation = "1" * 32
                return wrong_generation, record[1]
        return record

    monkeypatch.setattr(
        run_journal, "_read_run_generation_record", return_mismatched_generation
    )
    with pytest.raises(OSError, match="generation changed"):
        writer.append_sse_event("token", {"text": "written-before-failure"})

    with run_journal._SEQ_CACHE_LOCK:
        assert str(path) not in run_journal._SEQ_CACHE
    assert latest_run_summary(session_id, run_id, session_dir=tmp_path)["event_count"] == 2

    retry = writer.append_sse_event("token", {"text": "retry"})
    assert retry["seq"] == 3
    events = read_run_events(session_id, run_id, session_dir=tmp_path)["events"]
    assert [event["seq"] for event in events] == [1, 2, 3]


def test_run_journal_bounded_reader_rejects_tampered_resume_authority(tmp_path):
    session_id = "session_tampered_resume"
    run_id = "run_tampered_resume"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    events = [
        _bounded_event(session_id, run_id, 1, "one"),
        _bounded_event(session_id, run_id, 2, "two"),
        _bounded_event(session_id, run_id, 3, "three"),
    ]
    _write_bounded_events(path, events)
    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_rows=1,
    )
    second_row_end = sum(
        len(json.dumps(event, separators=(",", ":")).encode("utf-8")) + 1
        for event in events[:2]
    )
    tampered = _tamper_resume_token(
        first_page["resume_token"],
        o=second_row_end,
        p=2,
        l=2,
    )

    resumed = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        resume_token=tampered,
        session_dir=tmp_path,
        max_rows=2,
    )

    assert resumed["events"] == []
    assert resumed["complete"] is False
    assert resumed["limit_reason"] == "replay_cursor_invalid"
    assert resumed["next_after_seq"] == first_page["next_after_seq"]


def test_run_journal_bounded_reader_rejects_resume_token_after_window_change(tmp_path):
    session_id = "session_resume_window"
    run_id = "run_resume_window"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "one"),
            _bounded_event(session_id, run_id, 2, "two"),
            _bounded_event(session_id, run_id, 3, "three"),
        ],
    )
    first_page = read_run_events(
        session_id, run_id, max_seq=2, session_dir=tmp_path, max_rows=1,
    )

    resumed = read_run_events(
        session_id,
        run_id,
        max_seq=3,
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_rows=1,
    )

    assert resumed["events"] == []
    assert resumed["complete"] is False
    assert resumed["limit_reason"] == "replay_cursor_invalid"


def test_run_journal_bounded_reader_rejects_resume_token_after_file_replacement(tmp_path):
    session_id = "session_replaced_resume"
    run_id = "run_replaced_resume"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    events = [
        _bounded_event(session_id, run_id, 1, "one"),
        _bounded_event(session_id, run_id, 2, "two"),
    ]
    _write_bounded_events(path, events)
    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_rows=1,
    )
    replacement = path.with_suffix(".replacement")
    _write_bounded_events(replacement, events)
    os.replace(replacement, path)

    resumed = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_rows=1,
    )

    assert resumed["events"] == []
    assert resumed["complete"] is False
    assert resumed["limit_reason"] == "replay_cursor_invalid"
    assert resumed["next_after_seq"] == first_page["next_after_seq"]


def test_run_journal_bounded_reader_caps_physical_scan_before_row_end(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", 256, raising=False)
    session_id = "session_scan_byte_cap"
    run_id = "run_scan_byte_cap"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [_bounded_event(session_id, run_id, 1, "x" * 1024)],
    )
    real_open = Path.open
    physical_bytes_read = 0

    class CountingReader:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def readline(self, limit=-1):
            nonlocal physical_bytes_read
            chunk = self._fh.readline(limit)
            physical_bytes_read += len(chunk)
            return chunk

    def tracked_open(candidate, *args, **kwargs):
        fh = real_open(candidate, *args, **kwargs)
        return CountingReader(fh) if candidate == path else fh

    monkeypatch.setattr(Path, "open", tracked_open)
    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=512, max_rows=1,
    )

    assert physical_bytes_read == 256
    assert journal["events"] == []
    assert journal["complete"] is False
    assert journal["limit_reason"] == "replay_scan_limit_bytes"
    assert journal["next_after_seq"] == 0
    assert journal["scanned_bytes"] == 256


def test_run_journal_bounded_reader_resumes_at_row_after_scan_budget_prefix(
    tmp_path,
    monkeypatch,
):
    session_id = "session_scan_prefix_resume"
    run_id = "run_scan_prefix_resume"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    first = _bounded_event(session_id, run_id, 1, "one")
    second = _bounded_event(session_id, run_id, 2, "x" * 1024)
    _write_bounded_events(path, [first, second])
    first_size = len(json.dumps(first, separators=(",", ":")).encode("utf-8")) + 1
    monkeypatch.setattr(
        run_journal,
        "_BOUNDED_REPLAY_MAX_SCAN_BYTES",
        first_size + 64,
    )

    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=4096, max_rows=2,
    )
    assert [event["seq"] for event in first_page["events"]] == [1]
    assert first_page["complete"] is False
    assert first_page["limit_reason"] == "replay_scan_limit_bytes"
    assert first_page["next_after_seq"] == 1
    assert first_page["resume_token"]

    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", 4096)
    starts = []
    original_reader = run_journal._read_bounded_physical_row

    def tracked_reader(fh, *args, **kwargs):
        starts.append(fh.tell())
        return original_reader(fh, *args, **kwargs)

    monkeypatch.setattr(run_journal, "_read_bounded_physical_row", tracked_reader)
    second_page = read_run_events(
        session_id,
        run_id,
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_bytes=4096,
        max_rows=2,
    )

    assert starts[0] == first_size
    assert [event["seq"] for event in second_page["events"]] == [2]
    assert second_page["complete"] is True


def test_run_journal_bounded_resume_charges_boundary_read_to_scan_budget(
    tmp_path,
    monkeypatch,
):
    scan_cap = 256
    session_id = "session_resume_scan_budget"
    run_id = "run_resume_scan_budget"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "one"),
            _bounded_event(session_id, run_id, 2, "x" * 1024),
        ],
    )
    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_rows=1,
    )
    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", scan_cap)
    real_open = Path.open
    physical_bytes_read = 0

    class CountingReader:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._fh, name)

        def read(self, size=-1):
            nonlocal physical_bytes_read
            chunk = self._fh.read(size)
            physical_bytes_read += len(chunk)
            return chunk

        def readline(self, limit=-1):
            nonlocal physical_bytes_read
            chunk = self._fh.readline(limit)
            physical_bytes_read += len(chunk)
            return chunk

    def tracked_open(candidate, *args, **kwargs):
        fh = real_open(candidate, *args, **kwargs)
        return CountingReader(fh) if candidate == path else fh

    monkeypatch.setattr(Path, "open", tracked_open)
    journal = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_bytes=4096,
        max_rows=1,
    )

    assert physical_bytes_read == scan_cap
    assert journal["scanned_bytes"] == scan_cap
    assert journal["limit_reason"] == "replay_scan_limit_bytes"


def test_run_journal_bounded_reader_caps_malformed_flood_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_ROWS", 3, raising=False)
    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_MALFORMED", 2, raising=False)
    session_id = "session_malformed_flood"
    run_id = "run_malformed_flood"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"{bad}\n" * 10)

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=512, max_rows=1,
    )

    assert journal["events"] == []
    assert journal["complete"] is False
    assert journal["limit_reason"] == "replay_scan_limit_rows"
    assert journal["next_after_seq"] == 0
    assert journal["scanned_rows"] == 3
    assert journal["malformed_count"] == 3
    assert len(journal["malformed"]) == 2
    assert journal["resume_token"]


def test_run_journal_bounded_reader_fails_closed_on_invalid_sequences(tmp_path):
    session_id = "session_invalid_sequences"
    run_id = "run_invalid_sequences"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    missing = _bounded_event(session_id, run_id, 1, "missing")
    missing.pop("seq")
    nonpositive = _bounded_event(session_id, run_id, 0, "zero")
    string_seq = _bounded_event(session_id, run_id, 1, "string")
    string_seq["seq"] = "1"
    valid = _bounded_event(session_id, run_id, 1, "valid")
    _write_bounded_events(path, [missing, nonpositive, string_seq, valid])

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=4096, max_rows=4,
    )

    assert [event["payload"]["text"] for event in journal["events"]] == ["valid"]
    assert [entry["reason"] for entry in journal["malformed"]] == [
        "replay_invalid_seq",
        "replay_invalid_seq",
        "replay_invalid_seq",
    ]
    assert journal["next_after_seq"] == 1
    assert journal["complete"] is True


def test_run_journal_bounded_reader_fails_closed_on_duplicate_and_out_of_order_seq(tmp_path):
    session_id = "session_seq_order"
    run_id = "run_seq_order"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "one"),
            _bounded_event(session_id, run_id, 1, "duplicate"),
            _bounded_event(session_id, run_id, 3, "three"),
            _bounded_event(session_id, run_id, 2, "out-of-order"),
            _bounded_event(session_id, run_id, 4, "four"),
        ],
    )

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=4096, max_rows=5,
    )

    assert [event["seq"] for event in journal["events"]] == [1, 3, 4]
    assert [entry["reason"] for entry in journal["malformed"]] == [
        "replay_invalid_seq_order",
        "replay_invalid_seq_order",
    ]
    assert journal["next_after_seq"] == 4
    assert journal["complete"] is True


def test_run_journal_bounded_reader_invalid_json_cannot_advance_seq_authority(tmp_path):
    session_id = "session_invalid_json_order"
    run_id = "run_invalid_json_order"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    malformed = (
        b'{"version":1,"event_id":"'
        + f"{run_id}:999".encode("utf-8")
        + b'","seq":999,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","event":"token","type":"token","payload":{,,,,}}\n'
    )
    valid = _bounded_event(session_id, run_id, 1, "valid")
    path.write_bytes(
        malformed
        + json.dumps(valid, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=4096, max_rows=2,
    )

    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["malformed"] == [{"line": 1, "raw": ""}]
    assert journal["next_after_seq"] == 1
    assert journal["complete"] is True


def test_run_journal_bounded_reader_resumes_inside_oversized_row_and_reaches_suffix(
    tmp_path,
    monkeypatch,
):
    scan_cap = 384
    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", scan_cap)
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 128)
    session_id = "session_mid_row_resume"
    run_id = "run_mid_row_resume"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "x" * 2048),
            _bounded_event(session_id, run_id, 2, "reachable"),
        ],
    )

    token = None
    after_seq = None
    seen = []
    incomplete_prefix_pages = 0
    for _page_number in range(12):
        page = read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            resume_token=token,
            session_dir=tmp_path,
            max_bytes=4096,
            max_rows=2,
        )
        assert page["scanned_bytes"] <= scan_cap
        seen.extend(page["events"])
        if page["complete"]:
            break
        assert page["resume_token"]
        if page["next_after_seq"] == 0:
            incomplete_prefix_pages += 1
        token = page["resume_token"]
        after_seq = page["next_after_seq"]
    else:
        pytest.fail("bounded replay never reached the row after the oversized prefix")

    assert incomplete_prefix_pages >= 2
    assert [event["seq"] for event in seen] == [2]
    assert page["next_after_seq"] == 2


def test_run_journal_mid_row_token_restores_late_authority_and_partial_unicode_escape(
    tmp_path,
    monkeypatch,
):
    session_id = "session_late_mid_row_authority"
    run_id = "run_late_mid_row_authority"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    nested_depth = 40
    payload_prefix = (
        b'{"payload":'
        + (b"[" * nested_depth)
        + b'{"text":"'
        + (b"x" * 120)
    )
    scan_cap = len(payload_prefix) + len(b"\\u2")
    late_authority_row = (
        payload_prefix
        + b"\\u2603"  # first scan ends inside this JSON unicode escape
        + b'"}'
        + (b"]" * nested_depth)
        + b',"version":1,"event_id":"'
        + f"{run_id}:1".encode("utf-8")
        + b'","seq":1,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","event":"token","type":"token","terminal":false,'
        + b'"terminal_state":null}\n'
    )
    suffix = _bounded_event(session_id, run_id, 2, "reachable-after-late-authority")
    path.write_bytes(
        late_authority_row
        + json.dumps(suffix, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )
    monkeypatch.setattr(run_journal, "_LEGACY_TERMINAL_RECOVERY_MAX_BYTES", 64)
    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", scan_cap)

    token = None
    after_seq = None
    seen = []
    prefix_pages = 0
    for _page_number in range(12):
        page = read_run_events(
            session_id,
            run_id,
            after_seq=after_seq,
            resume_token=token,
            session_dir=tmp_path,
            max_bytes=4096,
            max_rows=2,
        )
        seen.extend(page["events"])
        if page["complete"]:
            break
        assert page["resume_token"]
        assert len(page["resume_token"]) <= run_journal._REPLAY_RESUME_TOKEN_MAX_CHARS
        token = page["resume_token"]
        after_seq = page["next_after_seq"]
        if after_seq == 0:
            prefix_pages += 1
        else:
            monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", 4096)
    else:
        pytest.fail("late-authority oversized row never reached its suffix")

    assert prefix_pages >= 2
    assert [event["seq"] for event in seen] == [2]
    assert page["next_after_seq"] == 2


def test_run_journal_bounded_resume_accepts_logical_cursor_ahead_of_physical_seq(
    tmp_path,
    monkeypatch,
):
    session_id = "session_logical_physical_cursor"
    run_id = "run_logical_physical_cursor"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    first = _bounded_event(session_id, run_id, 4, "before-floor")
    later = _bounded_event(session_id, run_id, 6, "after-floor")
    _write_bounded_events(path, [first, later])
    first_size = len(json.dumps(first, separators=(",", ":")).encode("utf-8")) + 1
    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", first_size)

    first_page = read_run_events(
        session_id,
        run_id,
        after_seq=5,
        session_dir=tmp_path,
        max_bytes=4096,
        max_rows=1,
    )
    assert first_page["events"] == []
    assert first_page["next_after_seq"] == 5
    assert first_page["resume_token"]

    monkeypatch.setattr(run_journal, "_BOUNDED_REPLAY_MAX_SCAN_BYTES", 4096)
    resumed = read_run_events(
        session_id,
        run_id,
        after_seq=5,
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_bytes=4096,
        max_rows=1,
    )

    assert [event["seq"] for event in resumed["events"]] == [6]
    assert resumed["complete"] is True
    assert resumed["next_after_seq"] == 6


def test_run_journal_bounded_reader_continues_after_over_digit_limit_integer(tmp_path):
    session_id = "session_over_digit_integer"
    run_id = "run_over_digit_integer"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    oversized_integer = (
        b'{"version":1,"event_id":"'
        + f"{run_id}:999".encode("utf-8")
        + b'","seq":999,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","event":"token","type":"token","payload":{"number":'
        + (b"9" * 5000)
        + b"}}\n"
    )
    valid = _bounded_event(session_id, run_id, 1, "valid")
    path.write_bytes(
        oversized_integer
        + json.dumps(valid, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=8192, max_rows=2,
    )

    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["malformed"] == [
        {"line": 1, "reason": "replay_invalid_json_value"},
    ]
    assert journal["next_after_seq"] == 1
    assert journal["complete"] is True


def test_run_journal_bounded_resume_rejects_recreated_generation_with_same_inode(
    tmp_path,
    monkeypatch,
):
    session_id = "session_recreated_generation"
    run_id = "run_recreated_generation"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    append_run_event(
        session_id, run_id, "token", {"text": "old-one"},
        session_dir=tmp_path, seq=1, created_at=1.0,
    )
    append_run_event(
        session_id, run_id, "token", {"text": "old-two"},
        session_dir=tmp_path, seq=2, created_at=2.0,
    )
    original_stat = path.stat()
    first_page = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_rows=1,
    )

    path.unlink()
    append_run_event(
        session_id, run_id, "token", {"text": "new-one"},
        session_dir=tmp_path, seq=1, created_at=1.0,
    )
    append_run_event(
        session_id, run_id, "token", {"text": "new-two"},
        session_dir=tmp_path, seq=2, created_at=2.0,
    )

    real_fstat = os.fstat

    class SameInodeStat:
        def __init__(self, current):
            self._current = current
            self.st_dev = original_stat.st_dev
            self.st_ino = original_stat.st_ino

        def __getattr__(self, name):
            return getattr(self._current, name)

    monkeypatch.setattr(
        run_journal.os,
        "fstat",
        lambda fd: SameInodeStat(real_fstat(fd)),
    )
    resumed = read_run_events(
        session_id,
        run_id,
        after_seq=first_page["next_after_seq"],
        resume_token=first_page["resume_token"],
        session_dir=tmp_path,
        max_rows=1,
    )

    assert resumed["events"] == []
    assert resumed["complete"] is False
    assert resumed["limit_reason"] == "replay_cursor_invalid"


def test_run_journal_generation_failure_cannot_leave_a_reported_failed_event(
    tmp_path,
    monkeypatch,
):
    session_id = "session_generation_write_failure"
    run_id = "run_generation_write_failure"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"

    def fail_generation_write(*_args, **_kwargs):
        raise OSError("generation-sidecar-write-failed")

    monkeypatch.setattr(
        run_journal,
        "_write_run_generation_record",
        fail_generation_write,
    )

    with pytest.raises(OSError, match="generation-sidecar-write-failed"):
        append_run_event(
            session_id,
            run_id,
            "token",
            {"text": "must-not-be-reported-failed-after-append"},
            session_dir=tmp_path,
            seq=1,
            created_at=1.0,
        )

    assert not path.exists() or path.read_bytes() == b""


def test_run_journal_reader_fails_closed_when_open_file_identity_diverges(
    tmp_path,
    monkeypatch,
):
    session_id = "session_open_file_generation"
    run_id = "run_open_file_generation"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(
        path,
        [
            _bounded_event(session_id, run_id, 1, "one"),
            _bounded_event(session_id, run_id, 2, "two"),
        ],
    )
    real_identity = run_journal._run_file_identity(path)
    assert real_identity is not None
    monkeypatch.setattr(
        run_journal,
        "_run_file_identity",
        lambda _path: (real_identity[0], real_identity[1] + 1),
    )

    page = read_run_events(
        session_id,
        run_id,
        session_dir=tmp_path,
        max_rows=1,
    )

    assert page["events"] == []
    assert page["complete"] is False
    assert page["limit_reason"] == "replay_cursor_invalid"
    assert page["resume_token"] is None


def test_run_journal_generation_is_stable_across_concurrent_processes(tmp_path):
    session_id = "session_process_generation"
    run_id = "run_process_generation"
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    worker = r'''
import sys
import time
from pathlib import Path

import api.run_journal as journal

root = Path(sys.argv[1])
coordination = Path(sys.argv[2])
role = sys.argv[3]
session_id = sys.argv[4]
run_id = sys.argv[5]
original_write = journal._write_run_generation_record
incarnation = sys.argv[6]

def wait_for(name):
    deadline = time.monotonic() + 15
    marker = coordination / name
    while not marker.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(name)
        time.sleep(0.01)

if role == "a":
    def gated_write(*args, **kwargs):
        (coordination / "a_writer_ready").touch()
        wait_for("allow_a_write")
        return original_write(*args, **kwargs)
    journal._write_run_generation_record = gated_write
    journal.append_run_event(
        session_id, run_id, "token", {"text": "x" * 2048},
        session_dir=root, seq=1, created_at=1.0, _incarnation=incarnation,
    )
    generation, _identity = journal._read_run_generation_record(
        journal._run_path(session_id, run_id, session_dir=root)
    )
    (coordination / "a_generation").write_text(generation, encoding="ascii")
    (coordination / "a_generation_ready").touch()
else:
    (coordination / "b_process_started").touch()
    def gated_write(*args, **kwargs):
        (coordination / "b_writer_ready").touch()
        wait_for("a_generation_ready")
        return original_write(*args, **kwargs)
    journal._write_run_generation_record = gated_write
    journal.append_run_event(
        session_id, run_id, "token", {"text": "suffix"},
        session_dir=root, seq=2, created_at=2.0, _incarnation=incarnation,
    )
'''

    def wait_for(name, timeout=15):
        deadline = time.monotonic() + timeout
        marker = coordination / name
        while not marker.exists():
            if time.monotonic() >= deadline:
                pytest.fail(f"timed out waiting for {name}")
            time.sleep(0.01)

    incarnation = _activate(session_id, tmp_path)
    common = [str(tmp_path), str(coordination), session_id, run_id, incarnation]
    process_a = subprocess.Popen(
        [sys.executable, "-c", worker, common[0], common[1], "a", common[2], common[3], common[4]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for("a_writer_ready")
    process_b = subprocess.Popen(
        [sys.executable, "-c", worker, common[0], common[1], "b", common[2], common[3], common[4]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for("b_process_started")
    time.sleep(0.25)
    (coordination / "allow_a_write").touch()
    wait_for("a_generation_ready")
    output_a, error_a = process_a.communicate(timeout=20)
    output_b, error_b = process_b.communicate(timeout=20)
    assert process_a.returncode == 0, (output_a, error_a)
    assert process_b.returncode == 0, (output_b, error_b)

    path = run_journal._run_path(session_id, run_id, session_dir=tmp_path)
    final_generation, _identity = run_journal._read_run_generation_record(path)
    assert final_generation == (coordination / "a_generation").read_text(encoding="ascii")


def test_run_journal_generation_lock_inode_survives_session_delete(tmp_path):
    session_id = "session_stable_generation_lock"
    run_id = "run_stable_generation_lock"
    append_run_event(
        session_id,
        run_id,
        "token",
        {"text": "before-delete"},
        session_dir=tmp_path,
        seq=1,
    )
    path = run_journal._run_path(session_id, run_id, session_dir=tmp_path)
    lock_path = run_journal._run_generation_lock_path(path)
    lock_identity = (lock_path.stat().st_dev, lock_path.stat().st_ino)

    coordination = tmp_path / "delete_coordination"
    coordination.mkdir()
    holder = r'''
import sys
import time
from pathlib import Path
import api.run_journal as journal
root, coordination = Path(sys.argv[1]), Path(sys.argv[2])
path = journal._run_path(sys.argv[3], sys.argv[4], session_dir=root)
with journal._run_generation_process_lock(path):
    (coordination / "held").touch()
    deadline = time.monotonic() + 15
    while not (coordination / "release").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("release")
        time.sleep(0.01)
'''
    deleter = r'''
import sys
from pathlib import Path
import api.run_journal as journal
root, coordination = Path(sys.argv[1]), Path(sys.argv[2])
journal.delete_run_journal(sys.argv[3], session_dir=root)
(coordination / "deleted").touch()
'''

    def wait_for(name, timeout=15):
        deadline = time.monotonic() + timeout
        marker = coordination / name
        while not marker.exists():
            if time.monotonic() >= deadline:
                pytest.fail(f"timed out waiting for {name}")
            time.sleep(0.01)

    holder_process = subprocess.Popen(
        [sys.executable, "-c", holder, str(tmp_path), str(coordination), session_id, run_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    wait_for("held")
    deleter_process = subprocess.Popen(
        [sys.executable, "-c", deleter, str(tmp_path), str(coordination), session_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(0.25)
    assert not (coordination / "deleted").exists()
    (coordination / "release").touch()
    holder_output, holder_error = holder_process.communicate(timeout=20)
    deleter_output, deleter_error = deleter_process.communicate(timeout=20)
    assert holder_process.returncode == 0, (holder_output, holder_error)
    assert deleter_process.returncode == 0, (deleter_output, deleter_error)
    wait_for("deleted")

    assert lock_path.exists()
    assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == lock_identity

    incarnation = _activate(session_id, tmp_path, reactivate_retired=True)
    _append_run_event(
        session_id,
        run_id,
        "token",
        {"text": "after-delete"},
        session_dir=tmp_path,
        seq=1,
        _incarnation=incarnation,
    )
    assert run_journal._run_generation_lock_path(path) == lock_path
    assert (lock_path.stat().st_dev, lock_path.stat().st_ino) == lock_identity


@pytest.mark.parametrize("nonfinite", [b"NaN", b"Infinity", b"-Infinity"])
def test_run_journal_bounded_reader_rejects_nonfinite_json_before_seq_authority(
    tmp_path,
    nonfinite,
):
    session_id = "session_nonfinite_json"
    run_id = "run_nonfinite_json"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True)
    malformed = (
        b'{"version":1,"event_id":"'
        + f"{run_id}:999".encode("utf-8")
        + b'","seq":999,"run_id":"'
        + run_id.encode("utf-8")
        + b'","session_id":"'
        + session_id.encode("utf-8")
        + b'","event":"token","type":"token","payload":{"number":'
        + nonfinite
        + b"}}\n"
    )
    valid = _bounded_event(session_id, run_id, 1, "valid")
    path.write_bytes(
        malformed
        + json.dumps(valid, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )

    journal = read_run_events(
        session_id, run_id, session_dir=tmp_path, max_bytes=4096, max_rows=2,
    )

    assert [event["seq"] for event in journal["events"]] == [1]
    assert journal["malformed"] == [
        {"line": 1, "reason": "replay_invalid_envelope"},
    ]
    assert journal["next_after_seq"] == 1
    assert journal["complete"] is True


@pytest.mark.parametrize("max_rows", [0, -1])
def test_run_journal_bounded_reader_rejects_nonpositive_row_cap(tmp_path, max_rows):
    session_id = "session_zero_row_cap"
    run_id = "run_zero_row_cap"
    path = tmp_path / "_run_journal" / session_id / f"{run_id}.jsonl"
    _write_bounded_events(path, [_bounded_event(session_id, run_id, 1, "one")])

    for _attempt in range(2):
        with pytest.raises(ValueError, match="max_rows must be at least 1"):
            read_run_events(
                session_id, run_id, session_dir=tmp_path, max_rows=max_rows,
            )


def test_run_journal_default_fsyncs_terminal_events_only(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.delenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", raising=False)
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert fsync_calls == []

    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    assert len(fsync_calls) == 1


def test_run_journal_eager_fsync_mode_fsyncs_non_terminal_events(tmp_path, monkeypatch):
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    path.parent.mkdir(parents=True)
    path.touch()
    fsync_calls = []
    monkeypatch.setenv("HERMES_WEBUI_RUN_JOURNAL_FSYNC", "eager")
    monkeypatch.setattr("api.run_journal.os.fsync", lambda fd: fsync_calls.append(fd))

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)

    assert len(fsync_calls) == 1


def test_run_journal_tolerates_malformed_lines(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = tmp_path / "_run_journal" / "session_1" / "run_1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
        fh.write(json.dumps(["wrong-shape"]) + "\n")

    journal = read_run_events("session_1", "run_1", session_dir=tmp_path)

    assert len(journal["events"]) == 1
    assert len(journal["malformed"]) == 2


def test_latest_summary_and_find_run_summary_classify_terminal_state(tmp_path):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    found = find_run_summary("run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["terminal_state"] == "interrupted-by-user"
    assert summary["last_seq"] == 2
    assert found["session_id"] == "session_1"
    assert found["terminal_state"] == "interrupted-by-user"


def test_latest_summary_reuses_unchanged_journal_summary_without_reparsing(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    monkeypatch.setattr(
        "api.run_journal._read_jsonl",
        lambda _path: (_ for _ in ()).throw(AssertionError("unchanged journal was reparsed")),
    )
    repeated = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert repeated == first


def test_summary_cache_invalidates_on_same_size_rewrite_with_restored_mtime(tmp_path, monkeypatch):
    # A same-inode, same-size rewrite that restores the original mtime_ns (e.g. an
    # atomic replace, or a tool that preserves mtime) must still invalidate the
    # cached summary. The signature includes st_ctime_ns — which advances on any
    # content/metadata change and cannot be forged back — so device/inode/size/
    # mtime collisions alone can never serve a stale summary. Proven at the
    # signature level (the enforced TOCTOU precondition for the cache) with a
    # deterministic stat where ONLY ctime differs.
    import api.run_journal as run_journal

    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    path = run_journal._run_path("session_1", "run_1", session_dir=tmp_path)
    real = path.stat()

    class _Stat:
        st_dev = real.st_dev
        st_ino = real.st_ino
        st_size = real.st_size
        st_mtime_ns = real.st_mtime_ns
        st_ctime_ns = real.st_ctime_ns  # overwritten per-call below

    seq = {"ctime": real.st_ctime_ns}

    def fake_stat(self, *a, **k):
        s = _Stat()
        s.st_ctime_ns = seq["ctime"]
        return s

    monkeypatch.setattr(Path, "stat", fake_stat)
    sig_before = run_journal._summary_cache_signature(path)
    # Same dev/inode/size/mtime, but a same-size in-place rewrite advanced ctime.
    seq["ctime"] = real.st_ctime_ns + 1
    sig_after = run_journal._summary_cache_signature(path)

    assert sig_after is not None and sig_before is not None
    assert sig_after != sig_before, "signature must change when only ctime advances"
    assert sig_before[:4] == sig_after[:4], "dev/inode/size/mtime_ns unexpectedly changed"


def test_summary_cache_does_not_store_result_when_journal_changes_during_read(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "ok"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl

    def append_after_read(path):
        events, malformed = original_read(path)
        append_run_event(
            "session_1",
            "run_1",
            "cancel",
            {"message": "Cancelled by user"},
            session_dir=tmp_path,
        )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_read)

    first = latest_run_summary("session_1", "run_1", session_dir=tmp_path)
    second = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert first["terminal_state"] == "completed"
    assert second["terminal_state"] == "interrupted-by-user"



def test_summary_cache_rejects_first_append_that_races_missing_journal_read(tmp_path, monkeypatch):
    import api.run_journal as run_journal

    original_read = run_journal._read_jsonl
    appended = False

    def append_after_missing_read(path):
        nonlocal appended
        events, malformed = original_read(path)
        if not appended:
            appended = True
            append_run_event(
                "session_1",
                "run_first_append",
                "done",
                {"session": {}},
                session_dir=tmp_path,
            )
        return events, malformed

    monkeypatch.setattr(run_journal, "_read_jsonl", append_after_missing_read)

    raced = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)
    refreshed = latest_run_summary("session_1", "run_first_append", session_dir=tmp_path)

    assert raced["terminal_state"] == "unknown"
    assert refreshed["terminal_state"] == "completed"
    assert refreshed["last_seq"] == 1
    assert refreshed["last_event_id"] == "run_first_append:1"


def test_terminal_state_classification_distinguishes_crash_from_user_cancel(tmp_path):
    append_run_event("session_1", "run_cancelled", "cancel", {"message": "Cancelled by user"}, session_dir=tmp_path)
    append_run_event("session_1", "run_crashed", "apperror", {"type": "interrupted"}, session_dir=tmp_path)
    append_run_event("session_1", "run_failed", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit", "apperror", {"type": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_tool_limit_done", "done", {"terminal_state": "tool_limit_reached"}, session_dir=tmp_path)
    append_run_event("session_1", "run_unknown_done", "done", {"terminal_state": "future_unknown_state"}, session_dir=tmp_path)
    append_run_event("session_1", "run_done", "done", {"session": {}}, session_dir=tmp_path)

    assert latest_run_summary("session_1", "run_cancelled", session_dir=tmp_path)["terminal_state"] == "interrupted-by-user"
    assert latest_run_summary("session_1", "run_crashed", session_dir=tmp_path)["terminal_state"] == "interrupted-by-crash"
    assert latest_run_summary("session_1", "run_failed", session_dir=tmp_path)["terminal_state"] == "errored"
    assert latest_run_summary("session_1", "run_tool_limit", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_tool_limit_done", session_dir=tmp_path)["terminal_state"] == "tool_limit_reached"
    assert latest_run_summary("session_1", "run_unknown_done", session_dir=tmp_path)["terminal_state"] == "completed"
    assert latest_run_summary("session_1", "run_done", session_dir=tmp_path)["terminal_state"] == "completed"


def test_summary_keeps_logical_terminal_state_when_stream_end_follows(tmp_path):
    append_run_event("session_1", "run_1", "apperror", {"type": "auth_mismatch"}, session_dir=tmp_path)
    append_run_event("session_1", "run_1", "stream_end", {"session_id": "session_1"}, session_dir=tmp_path)

    summary = latest_run_summary("session_1", "run_1", session_dir=tmp_path)

    assert summary["terminal"] is True
    assert summary["last_event"] == "stream_end"
    assert summary["terminal_state"] == "errored"


def test_stale_interrupted_event_reports_non_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "token", {"text": "partial"}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)
    event = stale_interrupted_event("session_1", "run_1")
    assert event is not None

    assert event["event"] == "apperror"
    assert event["seq"] == 2
    assert event["terminal_state"] == "lost-worker-bookkeeping"
    assert event["payload"]["type"] == "interrupted"
    assert "last journaled event" in event["payload"]["hint"]
    assert "process restarted" not in event["payload"]["message"]
    assert "lost the live worker" not in event["payload"]["message"]
    assert "live worker stopped" in event["payload"]["message"]


def test_stale_interrupted_event_skips_terminal_journal(tmp_path, monkeypatch):
    append_run_event("session_1", "run_1", "done", {"session": {}}, session_dir=tmp_path)

    monkeypatch.setattr("api.run_journal._default_session_dir", lambda: tmp_path)

    assert stale_interrupted_event("session_1", "run_1") is None
