import json


def test_read_session_message_tail_reads_only_recent_rows_from_large_sidecar(tmp_path, monkeypatch):
    from api import models

    sid = "tail_reader_large"
    path = tmp_path / f"{sid}.json"
    messages = [
        {"role": "user", "content": f"old-{idx}"}
        for idx in range(200)
    ] + [
        {"role": "assistant", "content": "new-1", "timestamp": 201.0},
        {"role": "tool", "content": "result", "tool_call_id": "call-1"},
        {"role": "assistant", "content": "new-2", "timestamp": 203.0},
    ]
    path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "large",
                "message_count": len(messages),
                "messages": messages,
                "_db_persisted": True,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    tail, offset = models.read_session_message_tail(sid, 3)

    assert [message["content"] for message in tail] == ["new-1", "result", "new-2"]
    assert offset == 200


def test_read_session_message_tail_does_not_use_full_session_loader(tmp_path, monkeypatch):
    from api import models

    sid = "tail_reader_no_full_load"
    path = tmp_path / f"{sid}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "title": "large",
                "message_count": 4,
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                    {"role": "user", "content": "three"},
                    {"role": "assistant", "content": "four"},
                ],
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(
        models.Session,
        "load",
        classmethod(lambda cls, _sid: (_ for _ in ()).throw(AssertionError("full load used"))),
    )

    tail, offset = models.read_session_message_tail(sid, 2)

    assert [message["content"] for message in tail] == ["three", "four"]
    assert offset == 2
