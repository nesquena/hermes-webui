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


def _anchor_scene_visible_semantics(scene, *, include_terminal=False):
    semantics = []
    for row in scene.get("activity_rows") or []:
        role = row.get("role")
        if role == "terminal" and not include_terminal:
            continue
        if role in {"prose", "thinking"}:
            semantics.append(
                {
                    "role": role,
                    "kind": row.get("kind"),
                    "text": " ".join(str(row.get("text") or "").split()),
                }
            )
            continue
        if role == "tool":
            tool = row.get("tool") if isinstance(row.get("tool"), dict) else {}
            payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
            args = tool.get("args") if "args" in tool else payload.get("args")
            if args is None:
                args = {}
            semantics.append(
                {
                    "role": role,
                    "kind": row.get("kind"),
                    "status": row.get("status"),
                    "tool_call_id": row.get("tool_call_id") or tool.get("id") or payload.get("tid"),
                    "name": tool.get("name") or payload.get("name"),
                    "args": args,
                    "done": tool.get("done"),
                }
            )
            continue
        if role == "terminal":
            semantics.append(
                {
                    "role": role,
                    "kind": row.get("kind"),
                    "source_event_type": row.get("source_event_type"),
                    "status": row.get("status"),
                }
            )
    return semantics


def test_anchor_scene_visible_semantics_preserves_empty_tool_args():
    scene = {
        "activity_rows": [
            {
                "role": "tool",
                "kind": "tool_completed",
                "status": "completed",
                "tool_call_id": "call-1",
                "tool": {"id": "call-1", "name": "terminal", "args": {}},
                "payload": {"tid": "call-1", "name": "terminal", "args": {"command": "stale"}},
            }
        ]
    }

    assert _anchor_scene_visible_semantics(scene)[0]["args"] == {}


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


def test_anchor_scene_hydration_promotes_final_content_array_tool_use_to_ordered_rows():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "Let me inspect the files first.",
                {"type": "tool_use", "tool_use_id": "toolu_content", "tool_name": "grep", "args": {"pattern": "TODO"}},
                "Found it,",
                {"type": "text", "text": "here's the fix."},
            ],
            "tool_calls": [
                {
                    "id": "toolu_message",
                    "name": "grep",
                    "input": {"pattern": "TODO"},
                    "snippet": "TODO in static/messages.js",
                    "started_at": 100,
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [
                    {"row_id": "done", "role": "terminal", "kind": "terminal_status", "source_event_type": "done"}
                ],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "toolu_durable",
                "name": "grep",
                "input": {"pattern": "TODO"},
                "snippet": "TODO in static/messages.js",
                "started_at": 100,
            }
        ],
    )

    scene = hydrated[1]["_anchor_activity_scene"]
    rows = scene["activity_rows"]
    activity = [
        (row.get("role"), row.get("text") or row.get("tool_call_id"))
        for row in rows
        if row.get("role") != "terminal"
    ]

    assert scene["final_answer"] == "Found it,\nhere's the fix."
    assert activity == [
        ("prose", "Let me inspect the files first."),
        ("tool", "toolu_content"),
    ]
    assert rows[1]["tool"]["name"] == "grep"
    assert rows[1]["tool"]["args"] == {"pattern": "TODO"}
    assert rows[1]["tool"]["snippet"] == "TODO in static/messages.js"
    assert len([row for row in rows if row.get("role") == "tool"]) == 1
    assert rows[-1]["role"] == "terminal"


def test_anchor_scene_hydration_preserves_non_final_post_tool_text():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "I will inspect first.",
                {"type": "tool_use", "tool_use_id": "toolu_content", "tool_name": "grep"},
                {"type": "text", "text": "I found the relevant file."},
                {"type": "thinking", "text": "Need one more check."},
            ],
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
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records, tool_calls=[])

    scene = hydrated[2]["_anchor_activity_scene"]
    rows = scene["activity_rows"]
    activity = [
        (row.get("role"), row.get("text") or row.get("tool_call_id"))
        for row in rows
    ]

    assert scene["final_answer"] == "final answer"
    assert activity == [
        ("prose", "I will inspect first."),
        ("tool", "toolu_content"),
        ("prose", "I found the relevant file."),
        ("thinking", "Need one more check."),
    ]


def test_anchor_scene_hydration_keeps_final_tail_thinking_as_activity_only():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check first."},
                {
                    "type": "tool_use",
                    "tool_use_id": "toolu_content",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                {"type": "thinking", "text": "Tail thinking must stay activity."},
                {"type": "reasoning", "reasoning": "Tail reasoning key must stay activity."},
                {"type": "text", "text": "Final visible answer."},
            ],
            "tool_calls": [
                {
                    "id": "toolu_message",
                    "name": "terminal",
                    "args": {"cmd": "ls"},
                    "snippet": "OUTPUT",
                    "started_at": 100,
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records, tool_calls=[])

    scene = hydrated[1]["_anchor_activity_scene"]
    rows = scene["activity_rows"]
    activity = [
        (row.get("role"), row.get("text") or row.get("tool_call_id"))
        for row in rows
    ]

    assert scene["final_answer"] == "Final visible answer."
    assert activity == [
        ("prose", "Let me check first."),
        ("tool", "toolu_content"),
        ("thinking", "Tail thinking must stay activity."),
        ("thinking", "Tail reasoning key must stay activity."),
    ]
    assert rows[1]["tool"]["snippet"] == "OUTPUT"
    assert len([row for row in rows if row.get("role") == "tool"]) == 1


def test_anchor_scene_hydration_promotes_output_text_content_tail_to_final_answer():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check first."},
                {
                    "type": "tool_use",
                    "tool_use_id": "toolu_content",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                {"type": "output_text", "content": "Final answer from content field."},
            ],
            "tool_calls": [
                {
                    "id": "toolu_message",
                    "name": "terminal",
                    "args": {"cmd": "ls"},
                    "snippet": "OUTPUT",
                    "started_at": 100,
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records, tool_calls=[])

    scene = hydrated[1]["_anchor_activity_scene"]
    rows = scene["activity_rows"]
    activity = [
        (row.get("role"), row.get("text") or row.get("tool_call_id"))
        for row in rows
    ]

    assert scene["final_answer"] == "Final answer from content field."
    assert activity == [
        ("prose", "Let me check first."),
        ("tool", "toolu_content"),
    ]
    assert rows[1]["tool"]["snippet"] == "OUTPUT"
    assert len([row for row in rows if row.get("role") == "tool"]) == 1


def test_anchor_scene_hydration_restores_durable_body_after_message_tool_merge():
    from api import routes

    full_output = "X" * 9000
    capped_preview = full_output[:4000]
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "I will inspect first.",
                {
                    "type": "tool_use",
                    "tool_use_id": "toolu_content",
                    "tool_name": "terminal",
                    "args": {"cmd": "long-output"},
                },
                "Done.",
            ],
            "tool_calls": [
                {
                    "id": "toolu_message",
                    "name": "terminal",
                    "input": {"cmd": "long-output"},
                    "snippet": capped_preview,
                    "started_at": 100,
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "toolu_durable",
                "name": "terminal",
                "input": {"cmd": "long-output"},
                "snippet": full_output,
                "started_at": 100,
            }
        ],
    )

    tools = [row for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"] if row.get("role") == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool_call_id"] == "toolu_content"
    assert tools[0]["tool"]["snippet"] == full_output
    assert tools[0]["payload"]["snippet"] == full_output


def test_anchor_scene_hydration_keeps_third_same_command_id_distinct_after_alt_id_match():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "message-a",
                    "name": "terminal",
                    "input": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                    "started_at": 100,
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "durable-b",
                "name": "terminal",
                "input": {"cmd": "ls"},
                "snippet": "OUTPUT B",
                "started_at": 200,
            }
        ],
    )

    tools = [row for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"] if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "OUTPUT A"
    assert "message-a" not in by_id
    assert by_id["durable-b"]["tool"]["snippet"] == "OUTPUT B"
    assert len(tools) == 2


def test_anchor_scene_hydration_keeps_identical_output_repeat_distinct_after_alt_id_match():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "message-a",
                    "name": "terminal",
                    "input": {"cmd": "ls"},
                    "snippet": "SAME OUTPUT",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "durable-b",
                "name": "terminal",
                "input": {"cmd": "ls"},
                "snippet": "SAME OUTPUT",
            }
        ],
    )

    tools = [row for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"] if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "SAME OUTPUT"
    assert "message-a" not in by_id
    assert by_id["durable-b"]["tool"]["snippet"] == "SAME OUTPUT"
    assert len(tools) == 2


def test_anchor_scene_hydration_keeps_same_started_at_repeat_distinct_after_alt_id_match():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "message-a",
                    "name": "terminal",
                    "input": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                    "started_at": 100,
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "durable-b",
                "name": "terminal",
                "input": {"cmd": "ls"},
                "snippet": "OUTPUT B",
                "started_at": 100,
            }
        ],
    )

    tools = [row for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"] if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "OUTPUT A"
    assert "message-a" not in by_id
    assert by_id["durable-b"]["tool"]["snippet"] == "OUTPUT B"
    assert len(tools) == 2


def test_anchor_scene_hydration_keeps_short_persisted_body_after_durable_merge():
    from api import routes

    full_output = "short output line\nwith more detail that came later"
    short_body = "short output line"
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "I will inspect first.",
                {
                    "type": "tool_use",
                    "tool_use_id": "toolu_content",
                    "tool_name": "terminal",
                    "args": {"cmd": "short-output"},
                },
                "Done.",
            ],
            "tool_calls": [
                {
                    "id": "toolu_message",
                    "name": "terminal",
                    "input": {"cmd": "short-output"},
                    "snippet": short_body,
                    "started_at": 100,
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "toolu_durable",
                "name": "terminal",
                "input": {"cmd": "short-output"},
                "snippet": full_output,
                "started_at": 100,
            }
        ],
    )

    tools = [row for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"] if row.get("role") == "tool"]
    assert len(tools) == 1
    assert tools[0]["tool"]["snippet"] == short_body
    assert tools[0]["payload"]["snippet"] == short_body


def test_anchor_scene_hydration_merges_missing_args_after_content_tool_match():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "I will patch the file.",
                {
                    "type": "tool_use",
                    "tool_use_id": "patch-content",
                    "tool_name": "edit_file",
                    "args": {"path": "x.py"},
                },
                "Done.",
            ],
            "tool_calls": [
                {
                    "id": "patch-message",
                    "name": "edit_file",
                    "input": {
                        "path": "x.py",
                        "old_string": "old",
                        "new_string": "new",
                    },
                    "snippet": "@@ -1 +1 @@\n-old\n+new",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records, tool_calls=[])

    tools = [
        row
        for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"]
        if row.get("role") == "tool"
    ]
    assert len(tools) == 1
    assert tools[0]["tool"]["args"] == {
        "path": "x.py",
        "old_string": "old",
        "new_string": "new",
    }
    assert tools[0]["payload"]["args"] == {
        "path": "x.py",
        "old_string": "old",
        "new_string": "new",
    }


def test_anchor_scene_hydration_keeps_consumed_different_name_tool_distinct():
    from api import routes

    messages = [
        {"role": "user", "content": "edit then inspect"},
        {
            "role": "assistant",
            "content": [
                "Edit the file.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "edit_file",
                    "args": {"path": "x.py"},
                },
                "Inspect it.",
            ],
            "tool_calls": [
                {
                    "id": "content-a",
                    "name": "edit_file",
                    "input": {"path": "x.py"},
                    "snippet": "EDITED",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "live-b",
                "name": "terminal",
                "input": {"path": "x.py"},
                "snippet": "TERMINAL OUTPUT",
            }
        ],
    )

    tools = [
        row
        for row in hydrated[1]["_anchor_activity_scene"]["activity_rows"]
        if row.get("role") == "tool"
    ]
    by_id = {row["tool"]["id"]: row for row in tools}

    assert by_id["content-a"]["tool"]["name"] == "edit_file"
    assert by_id["content-a"]["tool"]["snippet"] == "EDITED"
    assert by_id["live-b"]["tool"]["name"] == "terminal"
    assert by_id["live-b"]["tool"]["snippet"] == "TERMINAL OUTPUT"


def test_anchor_scene_hydration_does_not_position_merge_ambiguous_different_id_tools():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {"type": "tool_use", "tool_use_id": "content-a", "tool_name": "terminal"},
                "Second check.",
                {"type": "tool_use", "tool_use_id": "content-b", "tool_name": "terminal"},
                "Done.",
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {"assistant_msg_idx": 1, "tid": "message-b", "name": "terminal", "snippet": "OUTPUT B"},
            {"assistant_msg_idx": 1, "tid": "message-a", "name": "terminal", "snippet": "OUTPUT A"},
        ],
    )

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == ""
    assert by_id["content-b"]["tool"]["snippet"] == ""
    assert by_id["message-a"]["tool"]["snippet"] == "OUTPUT A"
    assert by_id["message-b"]["tool"]["snippet"] == "OUTPUT B"


def test_anchor_scene_hydration_does_not_name_merge_remaining_same_name_tool_after_exact_match():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {"type": "tool_use", "tool_use_id": "content-a", "tool_name": "terminal"},
                "Second check.",
                {"type": "tool_use", "tool_use_id": "content-b", "tool_name": "terminal"},
                "Done.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "snippet": "OUTPUT A"},
                {"id": "message-b", "name": "terminal", "snippet": "OUTPUT B"},
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records, tool_calls=[])

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "OUTPUT A"
    assert by_id["content-b"]["tool"]["snippet"] == ""
    assert by_id["message-b"]["tool"]["snippet"] == "OUTPUT B"


def test_anchor_scene_hydration_merges_remaining_matching_tool_after_exact_match():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-b",
                    "tool_name": "terminal",
                    "args": {"cmd": "pwd"},
                },
                "Done.",
            ],
            "tool_calls": [
                {"id": "content-a", "name": "terminal", "input": {"cmd": "ls"}, "snippet": "OUTPUT A"},
                {"id": "message-b", "name": "terminal", "input": {"cmd": "pwd"}, "snippet": "OUTPUT B"},
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records, tool_calls=[])

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "OUTPUT A"
    assert by_id["content-b"]["tool"]["snippet"] == "OUTPUT B"
    assert "message-b" not in by_id
    assert len(tools) == 2


def test_anchor_scene_hydration_keeps_distinct_used_singleton_tool_call():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "content-a",
                    "name": "terminal",
                    "input": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "durable-b",
                "name": "terminal",
                "input": {"cmd": "pwd"},
                "snippet": "OUTPUT B",
            }
        ],
    )

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "OUTPUT A"
    assert by_id["durable-b"]["tool"]["snippet"] == "OUTPUT B"
    assert len(tools) == 2


def test_anchor_scene_hydration_keeps_same_command_used_singleton_tool_distinct():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "content-a",
                    "name": "terminal",
                    "input": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "durable-b",
                "name": "terminal",
                "input": {"cmd": "ls"},
                "snippet": "OUTPUT B",
            }
        ],
    )

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "OUTPUT A"
    assert by_id["durable-b"]["tool"]["snippet"] == "OUTPUT B"
    assert len(tools) == 2


def test_anchor_scene_hydration_keeps_anonymous_used_singleton_tool_distinct():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "content-a",
                    "name": "terminal",
                    "input": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "name": "terminal",
                "input": {"cmd": "ls"},
                "snippet": "OUTPUT B",
            }
        ],
    )

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    snippets = sorted(row["tool"]["snippet"] for row in tools)

    assert len(tools) == 2
    assert snippets == ["OUTPUT A", "OUTPUT B"]


def test_anchor_scene_hydration_keeps_body_only_distinct_used_singleton_tool_call():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "First check.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "terminal",
                    "args": {"cmd": "ls"},
                },
                "Second check.",
            ],
            "tool_calls": [
                {
                    "id": "content-a",
                    "name": "terminal",
                    "input": {"cmd": "ls"},
                    "snippet": "OUTPUT A",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(
        messages,
        records,
        tool_calls=[
            {
                "assistant_msg_idx": 1,
                "tid": "durable-b",
                "name": "terminal",
                "snippet": "OUTPUT B",
            }
        ],
    )

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["snippet"] == "OUTPUT A"
    assert by_id["durable-b"]["tool"]["snippet"] == "OUTPUT B"
    assert len(tools) == 2


def test_anchor_scene_hydration_does_not_name_merge_singleton_with_conflicting_args():
    from api import routes

    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": [
                "Patch a.py.",
                {
                    "type": "tool_use",
                    "tool_use_id": "content-a",
                    "tool_name": "edit_file",
                    "args": {"path": "a.py"},
                },
            ],
            "tool_calls": [
                {
                    "id": "message-b",
                    "name": "edit_file",
                    "input": {"path": "b.py"},
                    "snippet": "PATCH B",
                }
            ],
        },
    ]
    records = {
        "record": {
            "message_index": 1,
            "message_ref": routes._assistant_anchor_scene_message_ref(messages[1]),
            "stream_id": "stream-1",
            "scene": {
                "version": "activity_scene_v1",
                "mode": "compact_worklog",
                "final_answer": "",
                "activity_rows": [],
            },
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records, tool_calls=[])

    rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    tools = [row for row in rows if row.get("role") == "tool"]
    by_id = {row.get("tool_call_id"): row for row in tools}

    assert by_id["content-a"]["tool"]["args"] == {"path": "a.py"}
    assert by_id["content-a"]["tool"]["snippet"] == ""
    assert by_id["message-b"]["tool"]["args"] == {"path": "b.py"}
    assert by_id["message-b"]["tool"]["snippet"] == "PATCH B"
    assert len(tools) == 2


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


def test_runtime_journal_anchor_scene_matches_settled_hydrated_visible_semantics(tmp_path, monkeypatch):
    """Runtime journal replay and settled read hydration must preserve the same
    visible anchor activity semantics for one turn.

    Compared path:
    _run_journal_live_snapshot(...).anchor_activity_scene
    -> persisted anchor_activity_scenes record
    -> _hydrate_anchor_activity_scenes(...)._anchor_activity_scene.
    """
    from api import models, routes
    from api.run_journal import RunJournalWriter

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    session_id = "anchorparity1"
    stream_id = "stream-parity-1"
    process_before_tool = "first progress"
    thinking = "thinking through plan"
    process_after_tool = "checkpoint tail"
    final_answer = "Final answer: keep the activity above this answer."

    writer = RunJournalWriter(session_id, stream_id, session_dir=session_dir)
    writer.append_sse_event("token", {"text": process_before_tool})
    writer.append_sse_event("reasoning", {"text": thinking})
    writer.append_sse_event(
        "tool",
        {"name": "terminal", "tid": "call-1", "args": {"command": "pytest"}},
    )
    writer.append_sse_event(
        "tool_complete",
        {"name": "terminal", "tid": "call-1", "preview": "ok"},
    )
    writer.append_sse_event("token", {"text": f" {process_after_tool}"})

    runtime_snapshot = routes._run_journal_live_snapshot(stream_id)
    assert runtime_snapshot is not None
    runtime_scene = runtime_snapshot.get("anchor_activity_scene")
    assert isinstance(runtime_scene, dict)
    assert _anchor_scene_visible_semantics(runtime_scene) == [
        {"role": "prose", "kind": "process_prose", "text": process_before_tool},
        {"role": "thinking", "kind": "reasoning", "text": thinking},
        {
            "role": "tool",
            "kind": "tool_completed",
            "status": "completed",
            "tool_call_id": "call-1",
            "name": "terminal",
            "args": {"command": "pytest"},
            "done": True,
        },
        {"role": "prose", "kind": "process_prose", "text": process_after_tool},
    ]

    persisted_scene = json.loads(json.dumps(runtime_scene))
    persisted_scene["activity_rows"].append(
        {
            "row_id": "terminal-done",
            "role": "terminal",
            "kind": "terminal_status",
            "source_event_type": "done",
            "status": "completed",
            "text": "Response complete",
        }
    )
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": process_before_tool,
            "reasoning": thinking,
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "terminal",
                    "args": {"command": "pytest"},
                    "preview": "pytest",
                    "snippet": "ok",
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        {"role": "assistant", "content": process_after_tool},
        {"role": "assistant", "content": final_answer},
    ]
    message_ref = routes._assistant_anchor_scene_message_ref(messages[4])
    records = {
        message_ref: {
            "version": "anchor_activity_scene_record_v1",
            "message_index": 4,
            "message_ref": message_ref,
            "stream_id": stream_id,
            "scene": persisted_scene,
        }
    }

    hydrated = routes._hydrate_anchor_activity_scenes(messages, records)
    settled_scene = hydrated[4]["_anchor_activity_scene"]

    assert settled_scene["final_answer"] == final_answer
    assert final_answer not in [
        row.get("text") for row in settled_scene["activity_rows"] if row.get("role") != "terminal"
    ]
    assert _anchor_scene_visible_semantics(settled_scene) == _anchor_scene_visible_semantics(runtime_scene)
    assert _anchor_scene_visible_semantics(settled_scene, include_terminal=True)[-1] == {
        "role": "terminal",
        "kind": "terminal_status",
        "source_event_type": "done",
        "status": "completed",
    }
    assert not any(
        row.get("role") in {"prose", "thinking", "tool"} and row.get("status") == "running"
        for row in settled_scene["activity_rows"]
    )


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
