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
                "compression_anchor_details": {"messages": [{"role": "assistant", "content": "not the transcript"}]},
                "messages": messages,
                "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}],
                "anchor_activity_scenes": {"scene-1": {"message_index": 202}},
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
    aux = models.read_session_auxiliary_metadata(sid)
    assert aux["tool_calls"][0]["id"] == "call-1"
    assert aux["anchor_activity_scenes"]["scene-1"]["message_index"] == 202


def test_read_session_message_tail_expands_when_recent_value_crosses_initial_window(tmp_path, monkeypatch):
    from api import models

    sid = "tail_reader_large_value"
    path = tmp_path / f"{sid}.json"
    path.write_text(
        json.dumps(
            {
                "session_id": sid,
                "message_count": 2,
                "messages": [
                    {"role": "user", "content": "old"},
                    {"role": "assistant", "content": "x" * (1024 * 1024 + 128 * 1024)},
                ],
                "tool_calls": [],
                "anchor_activity_scenes": {},
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)

    tail, offset = models.read_session_message_tail(sid, 1)

    assert len(tail) == 1
    assert len(tail[0]["content"]) == 1024 * 1024 + 128 * 1024
    assert offset == 1

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


def test_read_session_message_tail_handles_multibyte_metadata_and_top_level_auxiliary_fields(tmp_path, monkeypatch):
    from api import models

    sid = "tail_reader_multibyte_metadata"
    path = tmp_path / f"{sid}.json"
    path.write_text(
        json.dumps(
            {
                "title": "😀" * 20 + "[",
                "message_count": 3,
                "messages": [
                    {"role": "user", "content": "one"},
                    {"role": "assistant", "content": "two"},
                    {"role": "assistant", "content": "three"},
                ],
                "tool_calls": [],
                "anchor_activity_scenes": {"real": {"message_index": 2}},
                "extra": {"anchor_activity_scenes": {"fake": {"message_index": 0}}},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)

    tail, offset = models.read_session_message_tail(sid, 3)
    auxiliary = models.read_session_auxiliary_metadata(sid)

    assert [message["content"] for message in tail] == ["one", "two", "three"]
    assert offset == 0
    assert auxiliary["anchor_activity_scenes"] == {"real": {"message_index": 2}}
