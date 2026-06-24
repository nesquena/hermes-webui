import json
from collections import OrderedDict
from types import SimpleNamespace


def _client_anchor_scene_message_ref(message):
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or part.get("input_text") or ""))
            else:
                parts.append(str(part or ""))
        content = "\n".join(parts)
    payload = {
        "role": str(message.get("role") or ""),
        "content": " ".join(str(content or "").split()),
        "timestamp": message.get("_ts") or message.get("timestamp") or "",
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def test_anchor_scene_persistence_round_trip_outside_provider_messages(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session = Session(
        session_id="anchorpersist1",
        title="Anchor persistence",
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "final answer", "timestamp": 10.0},
        ],
    )
    session.save(skip_index=True)

    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "activity_rows": [
            {
                "row_id": "tool-1",
                "role": "tool",
                "tool_call_id": "call-1",
                "tool": {"id": "call-1", "name": "terminal", "args": {"command": "git status"}},
            }
        ],
        "final_answer": "final answer",
    }
    request_body = {
        "session_id": "anchorpersist1",
        "stream_id": "stream-1",
        "message_index": 1,
        "message_ref": "stale-ref-after-content-normalization",
        "scene": scene,
    }

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: request_body)
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        ) or True,
    )

    assert routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path="/api/session/anchor-scene")) is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["message_index"] == 1

    raw = json.loads((session_dir / "anchorpersist1.json").read_text(encoding="utf-8"))
    assert "_anchor_activity_scene" not in raw["messages"][1]
    assert raw["messages"][1]["content"] == "final answer"
    records = raw["anchor_activity_scenes"]
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["message_index"] == 1
    assert record["stream_id"] == "stream-1"
    assert record["scene"]["version"] == "activity_scene_v1"

    loaded = Session.load("anchorpersist1")
    hydrated = routes._hydrate_anchor_activity_scenes(
        loaded.messages,
        loaded.anchor_activity_scenes,
        message_offset=0,
    )
    assert "_anchor_activity_scene" not in loaded.messages[1]
    assert hydrated[1]["_anchor_stream_id"] == "stream-1"
    assert hydrated[1]["_anchor_activity_scene"]["final_answer"] == "final answer"
    assert hydrated[1]["_anchor_activity_scene"]["activity_rows"][0]["tool_call_id"] == "call-1"


def test_anchor_scene_persistence_rejects_cross_profile_write(tmp_path, monkeypatch):
    """#4411 security: /api/session/anchor-scene must not persist a scene onto a
    session that isn't visible to the active request profile. _get_or_materialize_session
    loads by id with no profile scoping, so the handler must apply the same
    _session_visible_to_active_profile guard GET /api/session uses — returning 404
    and leaving anchor_activity_scenes untouched (no cross-profile write)."""
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session = Session(
        session_id="foreignprofile1",
        title="Owned by profile B",
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "final answer", "timestamp": 10.0},
        ],
    )
    session.profile = "profile-b"
    session.save(skip_index=True)

    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "activity_rows": [
            {
                "row_id": "tool-1",
                "role": "tool",
                "tool_call_id": "call-1",
                "tool": {"id": "call-1", "name": "terminal", "args": {"command": "git status"}},
            }
        ],
        "final_answer": "final answer",
    }
    request_body = {
        "session_id": "foreignprofile1",
        "stream_id": "stream-1",
        "message_index": 1,
        "message_ref": "ref",
        "scene": scene,
    }

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: request_body)
    # Request runs under a profile that CANNOT see profile-b's session.
    monkeypatch.setattr(
        routes,
        "_session_visible_to_active_profile",
        lambda session_profile, handler=None: session_profile not in ("profile-b",),
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400, extra_headers=None: captured.update(
            error=msg, status=status
        ) or True,
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        ) or True,
    )

    assert routes.handle_post(
        SimpleNamespace(command="POST"),
        SimpleNamespace(path="/api/session/anchor-scene"),
    ) is True
    assert captured.get("status") == 404
    assert "payload" not in captured  # success j() never called
    raw = json.loads((session_dir / "foreignprofile1.json").read_text(encoding="utf-8"))
    assert not raw.get("anchor_activity_scenes"), (
        "cross-profile request must NOT persist anchor_activity_scenes"
    )


def test_anchor_scene_hydration_skips_ambiguous_ref_match(monkeypatch):
    """#4411 defense-in-depth: when two assistant messages share a ref (byte-identical
    content + identical _ts), the read-side hydration must NOT attach the same scene to
    both (which would render duplicate worklog groups) — mirroring the write-side
    _find_anchor_scene_message ambiguity guard. The ambiguous ref falls through to the
    positional index match instead."""
    from api import routes

    # Two assistant messages that normalize to the SAME ref.
    messages = [
        {"role": "assistant", "content": "dup answer", "timestamp": 5.0},
        {"role": "assistant", "content": "dup answer", "timestamp": 5.0},
    ]
    ref = routes._assistant_anchor_scene_message_ref(messages[0])
    assert ref == routes._assistant_anchor_scene_message_ref(messages[1]), "refs must collide for this test"

    # A single record keyed by that ambiguous ref, index-targeted at message 0.
    records = {
        ref: {
            "version": "anchor_activity_scene_record_v1",
            "message_index": 0,
            "message_ref": ref,
            "scene": {"version": "activity_scene_v1", "activity_rows": [], "final_answer": "dup answer"},
        }
    }
    out = routes._hydrate_anchor_activity_scenes(messages, records, message_offset=0)
    attached = [("_anchor_activity_scene" in m) for m in out]
    # The ambiguous ref must NOT fan the scene out to BOTH messages.
    assert attached.count(True) <= 1, (
        f"ambiguous ref must not double-attach the scene; got {attached}"
    )


def test_anchor_scene_hydration_rejects_stale_index_fallback_when_final_answer_mismatches():
    from api import routes

    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old final"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new final"},
    ]
    records = {
        "stale-ref": {
            "message_index": 3,
            "message_ref": "missing-ref-after-window-shift",
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "old final",
                "activity_rows": [
                    {"row_id": "old-tool", "role": "tool", "tool_call_id": "old-call"}
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)

    assert "_anchor_activity_scene" not in hydrated[3]


def test_anchor_scene_persistence_rejects_invalid_scene(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    Session(
        session_id="anchorpersist2",
        messages=[{"role": "assistant", "content": "answer"}],
    ).save(skip_index=True)

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "session_id": "anchorpersist2",
            "message_index": 0,
            "scene": {"version": "wrong", "activity_rows": []},
        },
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: captured.update(error=msg, status=status) or True,
    )

    assert routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path="/api/session/anchor-scene")) is True
    assert captured["status"] == 400
    assert "activity_scene_v1" in captured["error"]


def test_anchor_scene_persistence_prefers_unique_ref_over_stale_index(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old final"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new final"},
    ]
    Session(session_id="anchorpersist_ref", messages=messages).save(skip_index=True)
    client_ref = _client_anchor_scene_message_ref(messages[3])
    assert client_ref != routes._assistant_anchor_scene_message_ref(messages[3])

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "session_id": "anchorpersist_ref",
            "message_index": 1,
            "message_ref": client_ref,
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [{"row_id": "tool-1", "role": "tool"}],
                "final_answer": "new final",
            },
        },
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        ) or True,
    )

    assert routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path="/api/session/anchor-scene")) is True
    assert captured["payload"]["message_index"] == 3

    raw = json.loads((session_dir / "anchorpersist_ref.json").read_text(encoding="utf-8"))
    record = next(iter(raw["anchor_activity_scenes"].values()))
    assert record["message_index"] == 3
    assert record["scene"]["final_answer"] == "new final"


def test_anchor_scene_persistence_rejects_duplicate_client_ref_over_stale_index(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    messages = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "same final"},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "same final"},
    ]
    Session(session_id="anchorpersist_duplicate_ref", messages=messages).save(skip_index=True)

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "session_id": "anchorpersist_duplicate_ref",
            "message_index": 1,
            "message_ref": _client_anchor_scene_message_ref(messages[3]),
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [{"row_id": "tool-1", "role": "tool"}],
                "final_answer": "same final",
            },
        },
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: captured.update(error=msg, status=status) or True,
    )

    assert routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path="/api/session/anchor-scene")) is True
    assert captured["status"] == 404
    assert captured["error"] == "Assistant message not found"

    raw = json.loads((session_dir / "anchorpersist_duplicate_ref.json").read_text(encoding="utf-8"))
    assert not raw.get("anchor_activity_scenes")


def test_anchor_scene_persistence_converts_window_index_to_full_index(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    Session(
        session_id="anchorpersist_window",
        messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old final"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new final"},
        ],
    ).save(skip_index=True)

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "session_id": "anchorpersist_window",
            "message_index": 1,
            "message_window_index": 1,
            "message_offset": 2,
            "message_ref": "stale-ref-after-content-normalization",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [{"row_id": "tool-1", "role": "tool"}],
                "final_answer": "new final",
            },
        },
    )
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload, status=status
        ) or True,
    )

    assert routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path="/api/session/anchor-scene")) is True
    assert captured["payload"]["message_index"] == 3

    raw = json.loads((session_dir / "anchorpersist_window.json").read_text(encoding="utf-8"))
    record = next(iter(raw["anchor_activity_scenes"].values()))
    assert record["message_index"] == 3
    assert record["message_ref"] == routes._assistant_anchor_scene_message_ref(raw["messages"][3])


def test_anchor_scene_persistence_rejects_unmatched_ref_without_index(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    Session(
        session_id="anchorpersist_no_index",
        messages=[
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "final"},
        ],
    ).save(skip_index=True)

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "session_id": "anchorpersist_no_index",
            "message_ref": "missing-ref",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [{"row_id": "tool-1", "role": "tool"}],
            },
        },
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: captured.update(error=msg, status=status) or True,
    )

    assert routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path="/api/session/anchor-scene")) is True
    assert captured["status"] == 404
    assert captured["error"] == "Assistant message not found"


def test_anchor_scene_persistence_rejects_ref_miss_stale_index_mismatch(tmp_path, monkeypatch):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    Session(
        session_id="anchorpersist_stale_index_mismatch",
        messages=[
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old final"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new final"},
        ],
    ).save(skip_index=True)

    captured = {}
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(
        routes,
        "read_body",
        lambda handler: {
            "session_id": "anchorpersist_stale_index_mismatch",
            "message_index": 1,
            "message_ref": "stale-ref-after-content-normalization",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "activity_rows": [{"row_id": "tool-1", "role": "tool"}],
                "final_answer": "new final",
            },
        },
    )
    monkeypatch.setattr(
        routes,
        "bad",
        lambda handler, msg, status=400: captured.update(error=msg, status=status) or True,
    )

    assert routes.handle_post(SimpleNamespace(command="POST"), SimpleNamespace(path="/api/session/anchor-scene")) is True
    assert captured["status"] == 404
    assert captured["error"] == "Assistant message not found"

    raw = json.loads((session_dir / "anchorpersist_stale_index_mismatch.json").read_text(encoding="utf-8"))
    assert not raw.get("anchor_activity_scenes")


def test_anchor_scene_hydration_repairs_tail_only_scene_from_full_turn():
    from api import routes

    final_answer = (
        "final answer with enough detail to be the answer and a shared verification paragraph "
        "about cron proxy fallback removal, 127.0.0.1:7890, direct git pulls, and durable Worklog ordering"
    )
    stale_final_draft = (
        "draft answer with enough detail to be the answer and a shared verification paragraph "
        "about cron proxy fallback removal, 127.0.0.1:7890, direct git pulls, and durable Worklog ordering"
    )
    short_stale_final_draft = "cron 127.0.0.1:7890 fallback " + ("x " * 45) + "Worklog"
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "first progress",
            "reasoning": "thinking near first progress",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        {"role": "assistant", "content": "second progress"},
        {"role": "assistant", "content": final_answer},
    ]
    old_scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "final_answer": "",
        "activity_rows": [
            {
                "row_id": "tail-prose",
                "role": "prose",
                "kind": "process_prose",
                "source_event_type": "token",
                "text": "second progress",
            },
            {
                "row_id": "bad-final-prefix",
                "role": "thinking",
                "kind": "reasoning",
                "source_event_type": "reasoning",
                "text": final_answer,
            },
            {
                "row_id": "tail-thinking",
                "role": "thinking",
                "kind": "reasoning",
                "source_event_type": "reasoning",
                "text": "thinking near first progress",
            },
            {
                "row_id": "tail-final-draft",
                "role": "prose",
                "kind": "process_prose",
                "source_event_type": "token",
                "text": stale_final_draft,
            },
            {
                "row_id": "tail-short-final-draft",
                "role": "prose",
                "kind": "process_prose",
                "source_event_type": "token",
                "text": short_stale_final_draft,
            },
            {"row_id": "done", "role": "terminal", "kind": "terminal_status", "source_event_type": "done"},
        ],
    }
    records = {
        "record": {
            "message_index": 4,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[4]),
            "stream_id": "stream-1",
            "scene": old_scene,
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        message_offset=0,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "call-1",
                "name": "terminal",
                "preview": "running",
                "snippet": "ok",
            }
        ],
    )

    scene = hydrated[4]["_anchor_activity_scene"]
    rows = scene["activity_rows"]
    prose_texts = [row.get("text") for row in rows if row.get("role") == "prose"]
    tool_ids = [row.get("tool_call_id") for row in rows if row.get("role") == "tool"]
    thinking_texts = [row.get("text") for row in rows if row.get("role") == "thinking"]
    chronological = [
        (row.get("role"), row.get("text") or row.get("tool_call_id"))
        for row in rows
        if row.get("role") != "terminal"
    ]

    assert scene["final_answer"] == final_answer
    assert prose_texts == ["first progress", "second progress"]
    assert tool_ids == ["call-1"]
    assert thinking_texts == ["thinking near first progress"]
    assert chronological[:4] == [
        ("prose", "first progress"),
        ("thinking", "thinking near first progress"),
        ("tool", "call-1"),
        ("prose", "second progress"),
    ]
    assert rows[-1]["role"] == "terminal"


def test_anchor_scene_hydration_backfills_turn_duration_from_final_message():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "final answer", "_turnDuration": 731.2},
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "final answer",
                "activity_rows": [
                    {"row_id": "done", "role": "terminal", "kind": "terminal_status"}
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)

    assert hydrated[1]["_anchor_activity_scene"]["turn_duration"] == 731.2


def test_anchor_scene_hydration_dedupes_compression_lifecycle_rows():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "thinking text", "reasoning": "thinking text"},
        {"role": "assistant", "content": "final answer"},
    ]
    records = {
        "record": {
            "message_index": 2,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[2]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "final answer",
                "activity_rows": [
                    {
                        "row_id": "compressing-1",
                        "role": "lifecycle",
                        "kind": "lifecycle_status",
                        "source_event_type": "compressing",
                        "status": "running",
                        "text": "Compressing context",
                    },
                    {
                        "row_id": "compressing-2",
                        "role": "lifecycle",
                        "kind": "lifecycle_status",
                        "source_event_type": "compressing",
                        "status": "running",
                        "text": "Compressing context",
                    },
                    {
                        "row_id": "compressed",
                        "role": "lifecycle",
                        "kind": "lifecycle_status",
                        "source_event_type": "compressed",
                        "status": "completed",
                        "text": "Context auto-compressed",
                    },
                    {"row_id": "done", "role": "terminal", "kind": "terminal_status", "source_event_type": "done"},
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)

    rows = hydrated[2]["_anchor_activity_scene"]["activity_rows"]
    compression_rows = [
        row
        for row in rows
        if row.get("role") == "lifecycle"
        and row.get("source_event_type") in {"compressing", "compressed"}
    ]
    assert len(compression_rows) == 1
    assert compression_rows[0]["source_event_type"] == "compressed"
    assert compression_rows[0]["order_index"] == compression_rows[0]["seq"]


def test_anchor_scene_hydration_drops_stale_live_running_thinking_when_settled_thinking_exists():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "process update",
            "reasoning": "settled thinking from transcript",
        },
        {"role": "assistant", "content": "final answer"},
    ]
    records = {
        "record": {
            "message_index": 2,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[2]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "final answer",
                "activity_rows": [
                    {
                        "row_id": "live-reasoning:stream-1:2",
                        "local_id": "live-reasoning:stream-1:2",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "running",
                        "text": "stale live reasoning text",
                    },
                    {"row_id": "done", "role": "terminal", "kind": "terminal_status", "source_event_type": "done"},
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)

    rows = hydrated[2]["_anchor_activity_scene"]["activity_rows"]
    thinking_rows = [row for row in rows if row.get("role") == "thinking"]
    assert [row.get("text") for row in thinking_rows] == ["settled thinking from transcript"]
    assert not any(str(row.get("row_id") or "").startswith("live-reasoning:") for row in rows)
    assert not any(
        row.get("role") in {"thinking", "prose", "tool"} and row.get("status") == "running"
        for row in rows
    )


def test_anchor_scene_hydration_seals_unmatched_live_running_activity_rows():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "final answer"},
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "final answer",
                "activity_rows": [
                    {
                        "row_id": "live-reasoning:stream-1:1",
                        "local_id": "live-reasoning:stream-1:1",
                        "role": "thinking",
                        "kind": "reasoning",
                        "source_event_type": "reasoning",
                        "status": "running",
                        "text": "only live reasoning",
                    },
                    {
                        "row_id": "live-prose:stream-1:1",
                        "local_id": "live-prose:stream-1:1",
                        "role": "prose",
                        "kind": "process_prose",
                        "source_event_type": "token",
                        "status": "running",
                        "text": "only live prose",
                    },
                    {
                        "row_id": "live-tool:stream-1:call-1",
                        "local_id": "live-tool:stream-1:call-1",
                        "role": "tool",
                        "kind": "tool_started",
                        "source_event_type": "tool",
                        "status": "running",
                        "tool_call_id": "call-1",
                        "tool": {"id": "call-1", "name": "terminal", "done": False},
                    },
                    {"row_id": "done", "role": "terminal", "kind": "terminal_status", "source_event_type": "done"},
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    activity_rows = [row for row in rows if row.get("role") in {"thinking", "prose", "tool"}]
    assert [row.get("status") for row in activity_rows] == ["completed", "completed", "completed"]
    assert [row.get("role") for row in activity_rows] == ["thinking", "prose", "tool"]


def test_runtime_journal_snapshot_includes_live_anchor_activity_scene(monkeypatch):
    from api import routes

    stream_id = "stream-live-scene"
    events = [
        {
            "event": "token",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": "first progress"},
        },
        {
            "event": "reasoning",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "created_at": 2.0,
            "payload": {"text": "thinking through plan"},
        },
        {
            "event": "interim_assistant",
            "seq": 3,
            "event_id": f"{stream_id}:3",
            "created_at": 3.0,
            "payload": {"text": "checkpoint"},
        },
        {
            "event": "tool",
            "seq": 4,
            "event_id": f"{stream_id}:4",
            "created_at": 4.0,
            "payload": {"name": "terminal", "tid": "call-1", "args": {"command": "pytest"}},
        },
        {
            "event": "tool_complete",
            "seq": 5,
            "event_id": f"{stream_id}:5",
            "created_at": 5.0,
            "payload": {"name": "terminal", "tid": "call-1", "preview": "ok"},
        },
        {
            "event": "token",
            "seq": 6,
            "event_id": f"{stream_id}:6",
            "created_at": 6.0,
            "payload": {"text": " tail"},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-live-scene",
            "last_seq": 6,
            "last_event_id": f"{stream_id}:6",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    scene = snapshot["anchor_activity_scene"]
    rows = scene["activity_rows"]

    assert scene["version"] == "activity_scene_v1"
    assert snapshot["last_assistant_text"] == "first progress\n\ncheckpoint tail"
    assert snapshot["last_reasoning_text"] == "thinking through plan"
    assert [row["role"] for row in rows] == ["prose", "thinking", "tool", "prose"]
    assert rows[0]["local_id"] == f"live-prose:{stream_id}:1"
    assert rows[1]["local_id"] == f"live-thinking:{stream_id}:1"
    assert rows[1]["thinking"]["text"] == "thinking through plan"
    assert rows[2]["tool_call_id"] == "call-1"
    assert rows[2]["tool"]["done"] is True
    assert rows[3]["status"] == "running"


def test_runtime_journal_snapshot_dedupes_reasoning_interim_progress_echo(monkeypatch):
    from api import routes

    stream_id = "stream-live-reasoning-interim-echo"
    progress = "我先检查当前仓库状态，然后定位重复渲染路径。"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": progress},
        },
        {
            "event": "interim_assistant",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "created_at": 2.0,
            "payload": {"text": progress},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-live-reasoning-interim-echo",
            "last_seq": 2,
            "last_event_id": f"{stream_id}:2",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    rows = snapshot["anchor_activity_scene"]["activity_rows"]

    assert snapshot["last_assistant_text"] == progress
    assert snapshot["last_reasoning_text"] == ""
    assert [row["role"] for row in rows] == ["prose"]
    assert rows[0]["text"] == progress


def test_runtime_journal_snapshot_has_running_anchor_row_before_first_token(monkeypatch):
    from api import routes

    stream_id = "stream-live-empty"
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-live-empty",
            "last_seq": 1,
            "last_event_id": f"{stream_id}:1",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {
            "events": [
                {
                    "event": "context_status",
                    "seq": 1,
                    "event_id": f"{stream_id}:1",
                    "created_at": 1.0,
                    "payload": {"session_id": "session-live-empty"},
                }
            ]
        },
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    rows = snapshot["anchor_activity_scene"]["activity_rows"]

    assert rows
    assert rows[0]["role"] == "lifecycle"
    assert rows[0]["status"] == "running"
