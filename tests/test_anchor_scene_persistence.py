import json
from collections import OrderedDict
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest


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


def _authoritative_journal_events(events, session_id, run_id):
    stamped = []
    for event in events:
        stamped_event = dict(event)
        stamped_event.setdefault("version", 1)
        stamped_event.setdefault("run_id", run_id)
        stamped_event.setdefault("session_id", session_id)
        stamped.append(stamped_event)
    return stamped


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


@pytest.mark.parametrize("stale_status", ["running", "completed"])
def test_anchor_scene_hydration_drops_stale_live_thinking_when_settled_thinking_exists(
    stale_status,
):
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
                        "status": stale_status,
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
                        "row_id": "tool:call-1:0",
                        "local_id": "call-1",
                        "role": "tool",
                        "kind": "tool_started",
                        "source_event_type": "tool",
                        "status": "running",
                        "run_id": "run-1",
                        "stream_id": "stream-1",
                        "identity": {
                            "local_id": "call-1",
                            "run_id": "run-1",
                            "stream_id": "stream-1",
                        },
                        "group": {"group_key": "segment:1", "activity_segment_seq": 1},
                        "tool_call_id": "call-1",
                        "tool": {"id": "call-1", "name": "terminal", "done": False},
                        "payload": {"tid": "call-1", "status": "running", "done": False},
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
    tool_row = activity_rows[-1]
    assert tool_row["tool"]["done"] is True
    assert tool_row["payload"]["status"] == "completed"
    assert tool_row["payload"]["done"] is True


def test_anchor_scene_settlement_does_not_reclassify_transcript_owned_tool_row():
    from api import routes

    row = {
        "row_id": "hydrated:stream-1:tool:call-1",
        "local_id": "call-1",
        "role": "tool",
        "status": "running",
        "stream_id": "stream-1",
        "group": {"assistant_msg_idx": 1},
        "tool": {"id": "call-1", "done": False},
        "payload": {"tid": "call-1", "status": "running", "done": False},
    }

    assert routes._anchor_scene_settle_live_running_row(row, drop_live_thinking=False) is row
    assert row["tool"]["done"] is False
    assert row["payload"]["done"] is False


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


def test_run_journal_live_snapshot_pages_terminal_after_default_row_cap(tmp_path, monkeypatch):
    from api import models, routes
    from api.run_journal import RunJournalWriter

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)

    session_id = "session-long-terminal"
    stream_id = "stream-long-terminal"
    assistant = {"role": "assistant", "content": "suffix final", "_ts": 9.0}
    writer = RunJournalWriter(session_id, stream_id, session_dir=session_dir)
    for seq in range(1, 2049):
        writer.append_sse_event("reasoning", {"text": f"step {seq}"})
    writer.append_sse_event("token", {"text": "suffix final"})
    writer.append_sse_event(
        "done",
        {
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": session_id,
                "run_id": stream_id,
                "stream_id": stream_id,
                "message_index": 1,
                "message_ref": _client_anchor_scene_message_ref(assistant),
            }
        },
    )

    snapshot = routes._run_journal_live_snapshot(stream_id, settled=True)

    assert snapshot is not None
    assert snapshot["terminal_state"] == "completed"
    assert snapshot["terminal_message_index"] == 1
    assert snapshot["anchor_activity_scene"]["final_answer"] == "suffix final"


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
    assert rows[1]["local_id"] == f"live-reasoning:{stream_id}:1"
    assert rows[1]["thinking"]["text"] == "thinking through plan"
    assert rows[2]["tool_call_id"] == "call-1"
    assert rows[2]["tool"]["done"] is True
    assert rows[3]["status"] == "running"


def test_terminal_journal_snapshot_can_settle_anchor_activity_scene(monkeypatch):
    from api import routes

    stream_id = "stream-terminal-scene"
    session_id = "session-terminal-scene"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": "plan"},
        },
        {
            "event": "token",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "created_at": 2.0,
            "payload": {"text": "progress"},
        },
        {
            "version": 1,
            "event": "done",
            "seq": 3,
            "event_id": f"{stream_id}:3",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 3.0,
            "terminal": True,
            "payload": {
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": session_id,
                    "run_id": stream_id,
                    "stream_id": stream_id,
                    "message_index": 1,
                    "message_ref": "0" * 64,
                }
            },
        },
    ]
    events = _authoritative_journal_events(events, session_id, stream_id)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 3,
            "last_event_id": f"{stream_id}:3",
            "terminal": True,
            "terminal_state": "completed",
        },
    )
    monkeypatch.setattr(routes, "read_run_events", lambda session_id, run_id: {"events": events})

    snapshot = routes._run_journal_live_snapshot(stream_id, settled=True)
    scene = snapshot["anchor_activity_scene"]

    assert snapshot["terminal_state"] == "completed"
    assert snapshot["terminal_message_index"] == 1
    assert scene["lifecycle"]["status"] == "completed"
    assert scene["terminal_state"] == "completed"
    assert scene["final_answer"] == "progress"
    assert [row["status"] for row in scene["activity_rows"]] == ["completed", "completed"]


def test_terminal_journal_materializes_missing_settled_anchor_scene(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1
            self.save_kwargs = kwargs

    stream_id = "stream-detached-terminal"
    session_id = "session-detached-terminal"
    messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "working final answer", "_ts": 6.0},
        {"role": "user", "content": "newer prompt"},
        {"role": "assistant", "content": "newer answer", "_ts": 6.0},
    ]
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": "checking identity"},
        },
        {
            "event": "token",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "created_at": 2.0,
            "payload": {"text": "working"},
        },
        {
            "event": "tool",
            "seq": 3,
            "event_id": f"{stream_id}:3",
            "created_at": 3.0,
            "payload": {"name": "terminal", "tid": "call-detached", "args": {"command": "pytest"}},
        },
        {
            "event": "tool_complete",
            "seq": 4,
            "event_id": f"{stream_id}:4",
            "created_at": 4.0,
            "payload": {"name": "terminal", "tid": "call-detached", "preview": "ok"},
        },
        {
            "event": "token",
            "seq": 5,
            "event_id": f"{stream_id}:5",
            "created_at": 5.0,
            "payload": {"text": " final answer"},
        },
        {
            "version": 1,
            "event": "done",
            "seq": 6,
            "event_id": f"{stream_id}:6",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 6.0,
            "terminal": True,
            "payload": {
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": session_id,
                    "run_id": stream_id,
                    "stream_id": stream_id,
                    "message_index": 1,
                    "message_ref": _client_anchor_scene_message_ref(messages[1]),
                },
                "session": {
                    "session_id": session_id,
                    "message_count": 2,
                    "messages": [
                        {"role": "user", "content": "do work"},
                        {"role": "assistant", "content": "working final answer", "_ts": 6.0},
                    ],
                },
            },
        },
    ]
    events = _authoritative_journal_events(events, session_id, stream_id)
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 6,
        "last_event_id": f"{stream_id}:6",
        "terminal": True,
        "terminal_state": "completed",
    }
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: [summary])
    monkeypatch.setattr(routes, "find_run_summary", lambda sid: summary)
    monkeypatch.setattr(routes, "read_run_events", lambda session_id, run_id: {"events": events})
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    assert session.save_calls == 1
    assert session.save_kwargs == {"touch_updated_at": False, "skip_index": True}
    records = session.anchor_activity_scenes
    assert len(records) == 1
    record = next(iter(records.values()))
    assert record["message_index"] == 1
    assert record["stream_id"] == stream_id
    scene = record["scene"]
    assert scene["final_answer"] == "final answer"
    assert _anchor_scene_visible_semantics(scene) == [
        {"role": "thinking", "kind": "reasoning", "text": "checking identity"},
        {"role": "prose", "kind": "process_prose", "text": "working"},
        {
            "role": "tool",
            "kind": "tool_completed",
            "status": "completed",
            "tool_call_id": "call-detached",
            "name": "terminal",
            "args": {"command": "pytest"},
            "done": True,
        },
    ]


def test_terminal_journal_materialization_fails_closed_without_terminal_target(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    stream_id = "stream-detached-cancel-no-target"
    session_id = "session-detached-cancel-no-target"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": "checking"},
        },
        {
            "event": "token",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "created_at": 2.0,
            "payload": {"text": "partial work"},
        },
        {
            "version": 1,
            "event": "cancel",
            "seq": 3,
            "event_id": f"{stream_id}:3",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 3.0,
            "terminal": True,
            "payload": {"message": "Cancelled by user"},
        },
    ]
    events = _authoritative_journal_events(events, session_id, stream_id)
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 3,
        "last_event_id": f"{stream_id}:3",
        "terminal": True,
        "terminal_state": "interrupted-by-user",
    }
    messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "partial work", "_ts": 3.0},
        {"role": "user", "content": "newer prompt"},
        {"role": "assistant", "content": "newer answer", "_ts": 4.0},
    ]
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: [summary])
    monkeypatch.setattr(routes, "find_run_summary", lambda sid: summary)
    monkeypatch.setattr(routes, "read_run_events", lambda session_id, run_id: {"events": events})
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert session.save_calls == 1
    assert session.anchor_activity_scenes == {}
    progress = session.terminal_anchor_reconciliation
    assert stream_id in progress["recent_stream_ids"]
    assert progress["last_non_materializable"]["stream_id"] == stream_id
    assert progress["last_non_materializable"]["reason"] == "missing_terminal_target"


def test_terminal_journal_materialization_keeps_cursor_pending_for_incomplete_page(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    session_id = "session-incomplete-terminal"
    stream_id = "stream-incomplete-terminal"
    generation = {"dev": 1, "ino": 1, "size": 64, "mtime_ns": 1, "ctime_ns": 1}
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 4096,
        "last_event_id": f"{stream_id}:4096",
        "terminal": True,
        "terminal_state": "completed",
    }
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "partial"},
    ]
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    monkeypatch.setattr(
        routes,
        "terminal_run_summary_page_for_session",
        lambda *args, **kwargs: {
            "summaries": [summary],
            "index_size": 64,
            "index_generation": generation,
            "next_index_end_offset": 0,
            "exhausted": True,
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {
            "events": [],
            "malformed": [{"line": None, "reason": "replay_limit_rows"}],
            "complete": False,
            "limit_reason": "replay_limit_rows",
            "next_after_seq": 2048,
        },
    )

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is False
    assert session.save_calls == 0
    assert session.anchor_activity_scenes == {}
    progress = session.terminal_anchor_reconciliation
    assert "index_cursor" not in progress
    assert stream_id not in progress["recent_stream_ids"]


def test_terminal_journal_materialization_fails_closed_without_message_ref(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    stream_id = "stream-detached-index-only"
    session_id = "session-detached-index-only"
    events = [
        {
            "event": "token",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": "partial work"},
        },
        {
            "version": 1,
            "event": "cancel",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 2.0,
            "terminal": True,
            "payload": {
                "message": "Cancelled by user",
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": "session-detached-index-only",
                    "run_id": stream_id,
                    "stream_id": stream_id,
                    "message_index": 1,
                },
            },
        },
    ]
    events = _authoritative_journal_events(events, session_id, stream_id)
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 2,
        "last_event_id": f"{stream_id}:2",
        "terminal": True,
        "terminal_state": "interrupted-by-user",
    }
    messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "partial work", "_ts": 2.0},
    ]
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: [summary])
    monkeypatch.setattr(routes, "find_run_summary", lambda sid: summary)
    monkeypatch.setattr(routes, "read_run_events", lambda session_id, run_id: {"events": events})
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert session.save_calls == 1
    assert session.anchor_activity_scenes == {}
    progress = session.terminal_anchor_reconciliation
    assert stream_id in progress["recent_stream_ids"]
    assert progress["last_non_materializable"]["stream_id"] == stream_id
    assert progress["last_non_materializable"]["reason"] == "missing_terminal_target"


def test_terminal_journal_materializes_detached_cancel_with_owned_target(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1
            self.save_kwargs = kwargs

    stream_id = "stream-detached-cancel-target"
    session_id = "session-detached-cancel-target"
    messages = [
        {"role": "user", "content": "do work"},
        {
            "role": "assistant",
            "content": "**Task cancelled:** Cancelled by user",
            "_ts": 3.0,
        },
        {"role": "user", "content": "newer prompt"},
        {"role": "assistant", "content": "newer answer", "_ts": 4.0},
    ]
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": "checking"},
        },
        {
            "event": "token",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "created_at": 2.0,
            "payload": {"text": "partial work"},
        },
        {
            "version": 1,
            "event": "cancel",
            "seq": 3,
            "event_id": f"{stream_id}:3",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 3.0,
            "terminal": True,
            "payload": {
                "message": "Cancelled by user",
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": session_id,
                    "run_id": stream_id,
                    "stream_id": stream_id,
                    "message_index": 1,
                    "message_ref": _client_anchor_scene_message_ref(messages[1]),
                },
                "session": {
                    "session_id": session_id,
                    "message_count": 2,
                    "messages": messages[:2],
                },
            },
        },
    ]
    events = _authoritative_journal_events(events, session_id, stream_id)
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 3,
        "last_event_id": f"{stream_id}:3",
        "terminal": True,
        "terminal_state": "interrupted-by-user",
    }
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: [summary])
    monkeypatch.setattr(routes, "find_run_summary", lambda sid: summary)
    monkeypatch.setattr(routes, "read_run_events", lambda session_id, run_id: {"events": events})
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert session.save_calls == 1
    assert session.save_kwargs == {"touch_updated_at": False, "skip_index": True}
    record = next(iter(session.anchor_activity_scenes.values()))
    assert record["message_index"] == 1
    assert record["stream_id"] == stream_id
    assert record["scene"]["terminal_state"] == "interrupted-by-user"
    assert _anchor_scene_visible_semantics(record["scene"]) == [
        {"role": "thinking", "kind": "reasoning", "text": "checking"},
        {"role": "prose", "kind": "process_prose", "text": "partial work"},
    ]


def test_terminal_journal_rejects_mismatched_terminal_target(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    stream_id = "stream-detached-mismatch"
    session_id = "session-detached-mismatch"
    messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "old assistant", "_ts": 2.0},
    ]
    events = [
        {
            "event": "token",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "created_at": 1.0,
            "payload": {"text": "old assistant"},
        },
        {
            "version": 1,
            "event": "apperror",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 2.0,
            "terminal": True,
            "payload": {
                "type": "gateway_error",
                "message": "Gateway failed",
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": "session-detached-mismatch",
                    "run_id": stream_id,
                    "stream_id": stream_id,
                    "message_index": 1,
                    "message_ref": _client_anchor_scene_message_ref(
                        {"role": "assistant", "content": "different", "_ts": 2.0}
                    ),
                },
            },
        },
    ]
    events = _authoritative_journal_events(events, session_id, stream_id)
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 2,
        "last_event_id": f"{stream_id}:2",
        "terminal": True,
        "terminal_state": "errored",
    }
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: [summary])
    monkeypatch.setattr(routes, "find_run_summary", lambda sid: summary)
    monkeypatch.setattr(routes, "read_run_events", lambda session_id, run_id: {"events": events})
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert session.save_calls == 1
    assert session.anchor_activity_scenes == {}
    progress = session.terminal_anchor_reconciliation
    assert stream_id in progress["recent_stream_ids"]
    assert progress["last_non_materializable"]["stream_id"] == stream_id
    assert progress["last_non_materializable"]["reason"] == "terminal_target_not_found"


def test_terminal_target_rejects_missing_version_and_malformed_index():
    from api import routes

    stream_id = "stream-strict-terminal-target"
    session_id = "session-strict-terminal-target"
    messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "owned assistant", "_ts": 2.0},
    ]
    message_ref = _client_anchor_scene_message_ref(messages[1])
    base_target = {
        "version": "terminal_message_target_v1",
        "session_id": session_id,
        "run_id": stream_id,
        "stream_id": stream_id,
        "message_index": 1,
        "message_ref": message_ref,
    }
    base_snapshot = {
        "session_id": session_id,
        "stream_id": stream_id,
        "anchor_activity_scene": {
            "identity": {
                "session_id": session_id,
                "run_id": stream_id,
                "stream_id": stream_id,
            }
        },
    }

    invalid_targets = []
    missing_version = dict(base_target)
    missing_version.pop("version")
    invalid_targets.append(missing_version)
    invalid_targets.append({**base_target, "message_index": "1"})
    invalid_targets.append({**base_target, "message_index": True})
    invalid_targets.append({**base_target, "message_index": -1})
    invalid_targets.append({**base_target, "message_index": 2})

    for target in invalid_targets:
        snapshot = {**base_snapshot, "terminal_message_target": target}
        assert routes._terminal_anchor_scene_message_index(messages, snapshot) is None
        if target.get("message_index") != 2:
            assert routes._terminal_anchor_scene_target_from_payload(
                session_id,
                stream_id,
                stream_id,
                {"terminal_message_target": target},
            ) is None


def test_run_journal_live_snapshot_rejects_conflicting_run_envelope(monkeypatch):
    from api import routes

    stream_id = "stream-conflicting-envelope"
    events = [
        {
            "event": "token",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "run_id": stream_id,
            "created_at": 1.0,
            "payload": {"text": "partial"},
        },
        {
            "event": "done",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "run_id": "different-run",
            "created_at": 2.0,
            "terminal": True,
            "payload": {
                "session": {
                    "session_id": "session-conflicting-envelope",
                    "message_count": 2,
                    "messages": [
                        {"role": "user", "content": "do work"},
                        {"role": "assistant", "content": "partial", "_ts": 2.0},
                    ],
                },
            },
        },
    ]
    summary = {
        "session_id": "session-conflicting-envelope",
        "run_id": stream_id,
        "last_seq": 2,
        "last_event_id": f"{stream_id}:2",
        "terminal": True,
        "terminal_state": "completed",
    }
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: summary)
    monkeypatch.setattr(routes, "read_run_events", lambda _session_id, _run_id: {"events": events})

    assert routes._run_journal_live_snapshot(stream_id, settled=True) is None


@pytest.mark.parametrize(
    ("terminal_patch", "malformed_rows"),
    [
        ({"run_id": None}, []),
        ({"session_id": "foreign-session"}, []),
        ({"version": 2}, []),
        ({"seq": "2"}, []),
        ({"seq": 0}, []),
        ({"event_id": "stream-terminal-authority:7"}, []),
        ({}, [{"line": 2, "raw": "{bad json"}]),
    ],
)
def test_run_journal_live_snapshot_rejects_malformed_terminal_authority(
    monkeypatch,
    terminal_patch,
    malformed_rows,
):
    from api import routes

    stream_id = "stream-terminal-authority"
    session_id = "session-terminal-authority"
    message = {"role": "assistant", "content": "owned answer", "_ts": 2.0}
    terminal_event = {
        "version": 1,
        "event": "done",
        "seq": 2,
        "event_id": f"{stream_id}:2",
        "run_id": stream_id,
        "session_id": session_id,
        "created_at": 2.0,
        "terminal": True,
        "payload": {
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": session_id,
                "run_id": stream_id,
                "stream_id": stream_id,
                "message_index": 1,
                "message_ref": _client_anchor_scene_message_ref(message),
            },
        },
    }
    for key, value in terminal_patch.items():
        if value is None:
            terminal_event.pop(key, None)
        else:
            terminal_event[key] = value
    events = [
        {
            "version": 1,
            "event": "token",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 1.0,
            "payload": {"text": "owned answer"},
        },
        terminal_event,
    ]
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 2,
        "last_event_id": f"{stream_id}:2",
        "terminal": True,
        "terminal_state": "completed",
    }
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: summary)
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events, "malformed": malformed_rows},
    )

    assert routes._run_journal_live_snapshot(stream_id, settled=True) is None


def test_run_journal_live_snapshot_rejects_malformed_nonterminal_sibling_authority(
    monkeypatch,
):
    from api import routes

    stream_id = "stream-sibling-authority"
    session_id = "session-sibling-authority"
    message = {"role": "assistant", "content": "owned answer", "_ts": 2.0}
    events = [
        {
            "version": 999,
            "event": "token",
            "seq": 42,
            "event_id": f"{stream_id}:1",
            "run_id": stream_id,
            "session_id": "foreign-session",
            "created_at": 1.0,
            "payload": {"text": "must not project"},
        },
        {
            "version": 1,
            "event": "done",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "run_id": stream_id,
            "session_id": session_id,
            "created_at": 2.0,
            "terminal": True,
            "payload": {
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": session_id,
                    "run_id": stream_id,
                    "stream_id": stream_id,
                    "message_index": 1,
                    "message_ref": _client_anchor_scene_message_ref(message),
                },
            },
        },
    ]
    events = _authoritative_journal_events(events, session_id, stream_id)
    summary = {
        "session_id": session_id,
        "run_id": stream_id,
        "last_seq": 2,
        "last_event_id": f"{stream_id}:2",
        "terminal": True,
        "terminal_state": "completed",
    }
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: summary)
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, _run_id: {"events": events, "malformed": []},
    )

    assert routes._run_journal_live_snapshot(stream_id, settled=True) is None


def test_terminal_journal_records_non_materializable_disposition_once(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1
            self.save_kwargs = kwargs

    stream_id = "stream-non-materializable"
    events = [
        {
            "event": "token",
            "seq": 1,
            "event_id": f"{stream_id}:1",
            "run_id": stream_id,
            "created_at": 1.0,
            "payload": {"text": "partial"},
        },
        {
            "event": "cancel",
            "seq": 2,
            "event_id": f"{stream_id}:2",
            "run_id": stream_id,
            "created_at": 2.0,
            "terminal": True,
            "payload": {
                "message": "Cancelled by user",
                "terminal_disposition": {
                    "version": "terminal_disposition_v1",
                    "kind": "consumed_non_materializable",
                    "reason": "process_wakeup_success_cancel_rollback",
                    "session_id": "session-non-materializable",
                    "run_id": stream_id,
                    "stream_id": stream_id,
                },
            },
        },
    ]
    events = _authoritative_journal_events(events, "session-non-materializable", stream_id)
    summary = {
        "session_id": "session-non-materializable",
        "run_id": stream_id,
        "last_seq": 2,
        "last_event_id": f"{stream_id}:2",
        "terminal": True,
        "terminal_state": "interrupted-by-user",
    }
    session = SessionStub(
        session_id="session-non-materializable",
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    messages = [
        {"role": "user", "content": "do work"},
        {"role": "assistant", "content": "prior answer", "_ts": 1.0},
    ]
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: [summary])
    monkeypatch.setattr(routes, "find_run_summary", lambda _stream_id: summary)
    monkeypatch.setattr(routes, "read_run_events", lambda _session_id, _run_id: {"events": events})
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert session.save_calls == 1
    assert session.save_kwargs == {"touch_updated_at": False, "skip_index": True}
    assert session.anchor_activity_scenes == {}
    progress = session.terminal_anchor_reconciliation
    assert progress["last_non_materializable"]["stream_id"] == stream_id
    assert progress["last_non_materializable"]["kind"] == "consumed_non_materializable"
    assert stream_id in progress["recent_stream_ids"]

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is False
    assert session.save_calls == 1


def test_terminal_journal_materializer_durably_skips_invalid_batch_and_reaches_older_valid(
    monkeypatch,
):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1
            self.save_kwargs = kwargs

    session_id = "session-starvation-progress"
    old_stream = "stream-valid-after-invalid"
    messages = [
        {"role": "user", "content": "old work"},
        {"role": "assistant", "content": "old answer", "_ts": 2.0},
    ]
    invalid_streams = [f"stream-invalid-{idx:02d}" for idx in range(16)]
    summaries = [
        {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 2,
            "last_event_id": f"{stream_id}:2",
            "terminal": True,
            "terminal_state": "completed",
        }
        for stream_id in invalid_streams
    ]
    summaries.append(
        {
            "session_id": session_id,
            "run_id": old_stream,
            "last_seq": 5,
            "last_event_id": f"{old_stream}:5",
            "terminal": True,
            "terminal_state": "completed",
        }
    )
    old_events = [
        {
            "version": 1,
            "event": "reasoning",
            "seq": 1,
            "event_id": f"{old_stream}:1",
            "run_id": old_stream,
            "session_id": session_id,
            "created_at": 1.0,
            "payload": {"text": "checking older work"},
        },
        {
            "version": 1,
            "event": "tool",
            "seq": 2,
            "event_id": f"{old_stream}:2",
            "run_id": old_stream,
            "session_id": session_id,
            "created_at": 2.0,
            "payload": {"name": "terminal", "tid": "call-old", "args": {"command": "pytest"}},
        },
        {
            "version": 1,
            "event": "tool_complete",
            "seq": 3,
            "event_id": f"{old_stream}:3",
            "run_id": old_stream,
            "session_id": session_id,
            "created_at": 3.0,
            "payload": {"name": "terminal", "tid": "call-old", "preview": "ok"},
        },
        {
            "version": 1,
            "event": "token",
            "seq": 4,
            "event_id": f"{old_stream}:4",
            "run_id": old_stream,
            "session_id": session_id,
            "created_at": 4.0,
            "payload": {"text": "old answer"},
        },
        {
            "version": 1,
            "event": "done",
            "seq": 5,
            "event_id": f"{old_stream}:5",
            "run_id": old_stream,
            "session_id": session_id,
            "created_at": 5.0,
            "terminal": True,
            "payload": {
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": session_id,
                    "run_id": old_stream,
                    "stream_id": old_stream,
                    "message_index": 1,
                    "message_ref": _client_anchor_scene_message_ref(messages[1]),
                },
            },
        },
    ]

    def events_for(run_id):
        if run_id == old_stream:
            return old_events
        return [
            {
                "version": 1,
                "event": "done",
                "seq": 1,
                "event_id": f"{run_id}:1",
                "run_id": run_id,
                "session_id": session_id,
                "created_at": 1.0,
                "terminal": True,
                "payload": {"message": "missing target"},
            }
        ]

    seen_kwargs = {}

    def terminal_summaries(_sid, **kwargs):
        seen_kwargs.update(kwargs)
        return summaries

    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", terminal_summaries)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda run_id: next(summary for summary in summaries if summary["run_id"] == run_id),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, run_id: {"events": events_for(run_id), "malformed": []},
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    assert seen_kwargs["limit"] == 64
    assert seen_kwargs["max_candidates"] == 64
    assert "scan_all_candidates" not in seen_kwargs
    assert session.save_calls == 1
    records_by_stream = {
        record["stream_id"]: record
        for record in session.anchor_activity_scenes.values()
        if isinstance(record, dict)
    }
    progress = session.terminal_anchor_reconciliation
    recent_streams = progress["recent_stream_ids"]
    assert len(recent_streams) <= routes._TERMINAL_ANCHOR_RECONCILIATION_RECENT_LIMIT
    assert set(recent_streams).issubset(set(invalid_streams))
    assert progress["last_non_materializable"]["reason"] == "missing_terminal_target"
    assert records_by_stream[old_stream]["scene"]["activity_rows"]
    assert records_by_stream[old_stream]["message_index"] == 1


def test_terminal_reconciliation_progress_does_not_evict_real_anchor_scenes(
    monkeypatch,
):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1
            self.save_kwargs = kwargs

    session_id = "session-progress-map"
    messages = [
        {"role": "user", "content": "older work"},
        {"role": "assistant", "content": "older answer", "_ts": 2.0},
    ]
    real_records = {
        f"real:{idx:03d}": {
            "version": "anchor_activity_scene_record_v1",
            "message_index": idx + 10,
            "message_ref": f"real:{idx:03d}",
            "stream_id": f"stream-real-{idx:03d}",
            "scene": {"version": "activity_scene_v1", "activity_rows": [{"role": "prose"}]},
            "updated_at": float(idx),
        }
        for idx in range(256)
    }
    invalid_streams = [f"stream-invalid-progress-{idx:03d}" for idx in range(320)]
    summaries = [
        {
            "session_id": session_id,
            "run_id": stream_id,
            "last_seq": 1,
            "last_event_id": f"{stream_id}:1",
            "terminal": True,
            "terminal_state": "completed",
        }
        for stream_id in invalid_streams
    ]

    def events_for(run_id):
        return [
            {
                "version": 1,
                "event": "done",
                "seq": 1,
                "event_id": f"{run_id}:1",
                "run_id": run_id,
                "session_id": session_id,
                "created_at": 1.0,
                "terminal": True,
                "payload": {"message": "missing target"},
            }
        ]

    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes=dict(real_records),
        terminal_anchor_reconciliation={},
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: summaries)
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda run_id: next(summary for summary in summaries if summary["run_id"] == run_id),
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda _session_id, run_id: {"events": events_for(run_id), "malformed": []},
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    assert set(real_records).issubset(session.anchor_activity_scenes)
    streams = {
        record["stream_id"]
        for record in session.anchor_activity_scenes.values()
        if isinstance(record, dict)
    }
    assert streams == {record["stream_id"] for record in real_records.values()}
    progress = getattr(session, "terminal_anchor_reconciliation", {})
    recent_streams = progress.get("recent_stream_ids") if isinstance(progress, dict) else None
    assert isinstance(recent_streams, list)
    assert len(recent_streams) <= routes._TERMINAL_ANCHOR_RECONCILIATION_RECENT_LIMIT
    assert set(recent_streams).issubset(set(invalid_streams))
    assert "streams" not in progress


def test_terminal_reconciliation_cursor_advances_past_invalid_index_pages(
    tmp_path,
    monkeypatch,
):
    from api import models, routes
    from api.run_journal import append_run_event

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1
            self.save_kwargs = kwargs

    session_id = "session-terminal-cursor"
    valid_stream = "stream-terminal-cursor-valid"
    messages = [
        {"role": "user", "content": "finish work"},
        {"role": "assistant", "content": "working final answer", "_ts": 2.0},
    ]
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    append_run_event(
        session_id,
        valid_stream,
        "reasoning",
        {"text": "checking"},
        session_dir=tmp_path,
        created_at=0.5,
    )
    append_run_event(
        session_id,
        valid_stream,
        "token",
        {"text": "working"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    append_run_event(
        session_id,
        valid_stream,
        "done",
        {
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": session_id,
                "run_id": valid_stream,
                "stream_id": valid_stream,
                "message_index": 1,
                "message_ref": _client_anchor_scene_message_ref(messages[1]),
            },
            "session": {
                "session_id": session_id,
                "message_count": len(messages),
                "messages": messages,
            },
        },
        session_dir=tmp_path,
        created_at=2.0,
    )
    invalid_streams = [f"stream-terminal-cursor-invalid-{idx:03d}" for idx in range(130)]
    for idx, stream_id in enumerate(invalid_streams):
        append_run_event(
            session_id,
            stream_id,
            "done",
            {"message": "missing target"},
            session_dir=tmp_path,
            created_at=10.0 + idx,
        )

    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        terminal_anchor_reconciliation={},
        save_calls=0,
    )

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    assert session.save_calls == 1
    records_by_stream = {
        record["stream_id"]: record
        for record in session.anchor_activity_scenes.values()
        if isinstance(record, dict)
    }
    assert records_by_stream[valid_stream]["message_index"] == 1
    progress = session.terminal_anchor_reconciliation
    assert "streams" not in progress
    assert len(progress["recent_stream_ids"]) <= routes._TERMINAL_ANCHOR_RECONCILIATION_RECENT_LIMIT
    assert set(progress["recent_stream_ids"]).issubset(set(invalid_streams))
    cursor = progress["index_cursor"]
    assert cursor["index_size"] > 0
    assert 0 <= cursor["end_offset"] < cursor["index_size"]


def test_terminal_reconciliation_append_barrier_does_not_skip_new_terminal(
    tmp_path,
    monkeypatch,
):
    from api import models, routes
    from api.run_journal import append_run_event

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    session_id = "session-terminal-append-race"
    invalid_stream = "stream-terminal-invalid"
    valid_stream = "stream-terminal-valid"
    messages = [
        {"role": "user", "content": "finish work"},
        {"role": "assistant", "content": "finished answer", "_ts": 2.0},
    ]
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    append_run_event(
        session_id,
        invalid_stream,
        "done",
        {"message": "missing target"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        terminal_anchor_reconciliation={},
        save_calls=0,
    )

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert not session.anchor_activity_scenes
    assert session.terminal_anchor_reconciliation["index_cursor"]["end_offset"] == 0

    original_page = routes.terminal_run_summary_page_for_session
    state = {"appended": False}

    def append_before_page(session_id_arg, **kwargs):
        if not state["appended"]:
            state["appended"] = True
            append_run_event(
                session_id,
                valid_stream,
                "reasoning",
                {"text": "checking"},
                session_dir=tmp_path,
                created_at=2.0,
            )
            append_run_event(
                session_id,
                valid_stream,
                "token",
                {"text": "finished answer"},
                session_dir=tmp_path,
                created_at=3.0,
            )
            append_run_event(
                session_id,
                valid_stream,
                "done",
                {
                    "terminal_message_target": {
                        "version": "terminal_message_target_v1",
                        "session_id": session_id,
                        "run_id": valid_stream,
                        "stream_id": valid_stream,
                        "message_index": 1,
                        "message_ref": _client_anchor_scene_message_ref(messages[1]),
                    },
                    "session": {
                        "session_id": session_id,
                        "message_count": len(messages),
                        "messages": messages,
                    },
                },
                session_dir=tmp_path,
                created_at=4.0,
            )
        return original_page(session_id_arg, **kwargs)

    monkeypatch.setattr(routes, "terminal_run_summary_page_for_session", append_before_page)

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    records_by_stream = {
        record["stream_id"]: record
        for record in session.anchor_activity_scenes.values()
        if isinstance(record, dict)
    }
    assert records_by_stream[valid_stream]["message_index"] == 1


def test_terminal_reconciliation_active_terminal_blocks_ack_and_compaction(
    tmp_path,
    monkeypatch,
):
    from api import models, routes
    import api.run_journal as run_journal
    from api.run_journal import append_run_event

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    session_id = "session-terminal-active-barrier"
    stream_id = "stream-terminal-active"
    messages = [
        {"role": "user", "content": "finish work"},
        {"role": "assistant", "content": "finished answer", "_ts": 2.0},
    ]
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(run_journal, "_TERMINAL_INDEX_COMPACT_TRIGGER_BYTES", 1)
    active_streams = {stream_id}
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set(active_streams))

    append_run_event(
        session_id,
        stream_id,
        "reasoning",
        {"text": "checking"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    append_run_event(
        session_id,
        stream_id,
        "token",
        {"text": "finished answer"},
        session_dir=tmp_path,
        created_at=2.0,
    )
    append_run_event(
        session_id,
        stream_id,
        "done",
        {
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": session_id,
                "run_id": stream_id,
                "stream_id": stream_id,
                "message_index": 1,
                "message_ref": _client_anchor_scene_message_ref(messages[1]),
            },
            "session": {
                "session_id": session_id,
                "message_count": len(messages),
                "messages": messages,
            },
        },
        session_dir=tmp_path,
        created_at=3.0,
    )
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        terminal_anchor_reconciliation={},
        save_calls=0,
    )

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    assert not session.anchor_activity_scenes
    progress = session.terminal_anchor_reconciliation
    assert progress["recent_stream_ids"] == []
    cursor = progress["index_cursor"]
    assert cursor["end_offset"] == cursor["index_size"]
    index_path = tmp_path / "_run_journal" / session_id / "_terminal_runs.jsonl"
    assert stream_id in index_path.read_text(encoding="utf-8")

    active_streams.clear()

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    records_by_stream = {
        record["stream_id"]: record
        for record in session.anchor_activity_scenes.values()
        if isinstance(record, dict)
    }
    assert records_by_stream[stream_id]["message_index"] == 1


def test_terminal_reconciliation_cursor_publish_rejects_same_generation_rewind():
    from api import routes

    generation = {"dev": 1, "ino": 2, "size": 100, "mtime_ns": 3, "ctime_ns": 4}
    progress = {
        "version": routes._TERMINAL_ANCHOR_RECONCILIATION_VERSION,
        "recent_stream_ids": [],
        "index_cursor": {
            "index_size": 100,
            "generation": generation,
            "end_offset": 10,
        },
    }

    assert not routes._terminal_anchor_reconciliation_set_cursor(
        progress,
        index_size=100,
        index_generation=generation,
        end_offset=50,
    )
    assert progress["index_cursor"]["end_offset"] == 10
    assert routes._terminal_anchor_reconciliation_set_cursor(
        progress,
        index_size=100,
        index_generation=generation,
        end_offset=5,
    )
    assert progress["index_cursor"]["end_offset"] == 5


def test_terminal_reconciliation_cursor_publish_requires_expected_cursor_or_monotonic_progress():
    from api import routes

    generation = {"dev": 1, "ino": 2, "size": 100, "mtime_ns": 3, "ctime_ns": 4}
    expected = {"index_size": 100, "generation": generation, "end_offset": 80}
    current = {"index_size": 100, "generation": generation, "end_offset": 10}
    rewind = {"index_size": 100, "generation": generation, "end_offset": 50}
    advanced = {"index_size": 100, "generation": generation, "end_offset": 5}
    replacement = {
        "index_size": 100,
        "generation": {"dev": 1, "ino": 9, "size": 100, "mtime_ns": 3, "ctime_ns": 4},
        "end_offset": 5,
    }

    assert not routes._terminal_anchor_reconciliation_cursor_publish_allowed(current, expected, rewind)
    assert routes._terminal_anchor_reconciliation_cursor_publish_allowed(current, expected, advanced)
    assert not routes._terminal_anchor_reconciliation_cursor_publish_allowed(current, expected, replacement)


def test_terminal_reconciliation_real_two_reconciler_cursor_publication(
    tmp_path,
    monkeypatch,
):
    from api import models, routes
    from api.run_journal import append_run_event

    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    session_id = "session-terminal-two-reconcilers"
    for idx in range(3):
        stream_id = f"stream-terminal-two-reconcilers-{idx}"
        append_run_event(
            session_id,
            stream_id,
            "done",
            {"message": "missing target"},
            session_dir=tmp_path,
            created_at=float(idx),
        )

    first_page = routes.terminal_run_summary_page_for_session(
        session_id,
        limit=1,
        max_candidates=1,
    )
    first_cursor = routes._terminal_anchor_reconciliation_page_cursor(first_page)
    assert first_cursor is not None
    assert "digest" in first_cursor["generation"]
    assert "authority" in first_cursor["generation"]
    second_page = routes.terminal_run_summary_page_for_session(
        session_id,
        limit=1,
        max_candidates=1,
        index_cursor=first_cursor,
    )
    second_cursor = routes._terminal_anchor_reconciliation_page_cursor(second_page)
    assert second_cursor is not None
    assert second_cursor["end_offset"] < first_cursor["end_offset"]

    progress = {
        "version": routes._TERMINAL_ANCHOR_RECONCILIATION_VERSION,
        "recent_stream_ids": [],
        "index_cursor": first_cursor,
    }
    assert routes._terminal_anchor_reconciliation_cursor_publish_allowed(
        routes._terminal_anchor_reconciliation_cursor(progress),
        first_cursor,
        second_cursor,
    )
    assert routes._terminal_anchor_reconciliation_set_cursor(
        progress,
        index_size=second_cursor["index_size"],
        index_generation=second_cursor["generation"],
        end_offset=second_cursor["end_offset"],
    )

    assert not routes._terminal_anchor_reconciliation_cursor_publish_allowed(
        routes._terminal_anchor_reconciliation_cursor(progress),
        None,
        first_cursor,
    )
    assert not routes._terminal_anchor_reconciliation_set_cursor(
        progress,
        index_size=first_cursor["index_size"],
        index_generation=first_cursor["generation"],
        end_offset=first_cursor["end_offset"],
    )
    assert progress["index_cursor"]["end_offset"] == second_cursor["end_offset"]


def test_terminal_reconciliation_retries_compaction_without_scene_mutation(
    tmp_path,
    monkeypatch,
):
    from api import models, routes
    import api.run_journal as run_journal
    from api.run_journal import append_run_event

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    session_id = "session-terminal-compact-retry"
    stream_id = "stream-terminal-invalid"
    messages = [
        {"role": "user", "content": "finish work"},
        {"role": "assistant", "content": "finished answer", "_ts": 2.0},
    ]
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(run_journal, "_TERMINAL_INDEX_COMPACT_TRIGGER_BYTES", 1)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())
    append_run_event(
        session_id,
        stream_id,
        "done",
        {"message": "missing target"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    calls = []

    def compact_once_then_succeed(sid, *, index_cursor=None):
        calls.append({"sid": sid, "index_cursor": index_cursor})
        return len(calls) == 2

    monkeypatch.setattr(routes, "compact_terminal_run_index_for_session", compact_once_then_succeed)
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        terminal_anchor_reconciliation={},
        save_calls=0,
    )

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert session.save_calls == 1
    assert len(calls) == 1

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    assert session.save_calls == 1
    assert len(calls) == 2
    assert calls[0]["index_cursor"] == calls[1]["index_cursor"]


def test_terminal_reconciliation_advances_past_over_cap_index_tail(
    tmp_path,
    monkeypatch,
):
    from api import models, routes
    from api.run_journal import append_run_event

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    session_id = "session-terminal-overcap-tail"
    valid_stream = "stream-terminal-valid"
    messages = [
        {"role": "user", "content": "finish work"},
        {"role": "assistant", "content": "finished answer", "_ts": 2.0},
    ]
    monkeypatch.setattr(models, "SESSION_DIR", tmp_path)
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: set())

    append_run_event(
        session_id,
        valid_stream,
        "reasoning",
        {"text": "checking"},
        session_dir=tmp_path,
        created_at=1.0,
    )
    append_run_event(
        session_id,
        valid_stream,
        "token",
        {"text": "finished answer"},
        session_dir=tmp_path,
        created_at=2.0,
    )
    append_run_event(
        session_id,
        valid_stream,
        "done",
        {
            "terminal_message_target": {
                "version": "terminal_message_target_v1",
                "session_id": session_id,
                "run_id": valid_stream,
                "stream_id": valid_stream,
                "message_index": 1,
                "message_ref": _client_anchor_scene_message_ref(messages[1]),
            },
            "session": {
                "session_id": session_id,
                "message_count": len(messages),
                "messages": messages,
            },
        },
        session_dir=tmp_path,
        created_at=3.0,
    )
    index_path = tmp_path / "_run_journal" / session_id / "_terminal_runs.jsonl"
    with index_path.open("ab") as fh:
        fh.write(b'{"version":1,"oversized":"' + (b"x" * (600 * 1024)) + b'"}')

    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={},
        terminal_anchor_reconciliation={},
        save_calls=0,
    )

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True

    records_by_stream = {
        record["stream_id"]: record
        for record in session.anchor_activity_scenes.values()
        if isinstance(record, dict)
    }
    assert records_by_stream[valid_stream]["message_index"] == 1


@pytest.mark.parametrize("terminal_event", ["cancel", "apperror"])
def test_public_session_get_materializes_writer_terminal_target_for_detached_cancel_and_error(
    tmp_path,
    monkeypatch,
    terminal_event,
):
    from api import models, routes
    from api.models import Session
    from api.run_journal import RunJournalWriter

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)

    session_id = f"session-detached-{terminal_event}"
    stream_id = f"stream-detached-{terminal_event}"
    assistant = {
        "role": "assistant",
        "content": "partial terminal work",
        "_ts": 2.0,
    }
    Session(
        session_id=session_id,
        title="Detached terminal",
        messages=[
            {"role": "user", "content": "do work", "timestamp": 1.0},
            assistant,
        ],
    ).save(skip_index=True)

    writer = RunJournalWriter(session_id, stream_id, session_dir=session_dir)
    writer.append_sse_event("token", {"text": "partial terminal work"})
    writer.append_sse_event("tool", {"name": "terminal", "tid": "call-detached", "args": {"command": "pytest"}})
    writer.append_sse_event("tool_complete", {"name": "terminal", "tid": "call-detached", "preview": "ok"})
    payload = {
        "message": "Cancelled by user" if terminal_event == "cancel" else "Gateway failed",
        "terminal_message_target": {
            "version": "terminal_message_target_v1",
            "session_id": session_id,
            "run_id": stream_id,
            "stream_id": stream_id,
            "message_index": 1,
            "message_ref": _client_anchor_scene_message_ref(assistant),
        },
    }
    writer.append_sse_event(terminal_event, payload)

    captured = {}
    monkeypatch.setattr(
        routes,
        "j",
        lambda handler, payload, status=200, extra_headers=None: captured.update(
            payload=payload,
            status=status,
        ) or True,
    )
    handler = SimpleNamespace(headers={}, _safe_webui_print=lambda *args, **kwargs: None)

    assert routes.handle_get(
        handler,
        urlparse(f"/api/session?session_id={session_id}&messages=1&resolve_model=0"),
    ) is True

    assert captured["status"] == 200
    loaded = captured["payload"]["session"]
    hydrated = loaded["messages"][1]
    assert hydrated["_anchor_stream_id"] == stream_id
    scene = hydrated["_anchor_activity_scene"]
    assert scene["terminal_state"] in {"interrupted-by-user", "errored"}
    assert _anchor_scene_visible_semantics(scene) == [
        {
            "role": "tool",
            "kind": "tool_completed",
            "status": "completed",
            "tool_call_id": "call-detached",
            "name": "terminal",
            "args": {"command": "pytest"},
            "done": True,
        }
    ]


def test_terminal_journal_materializes_older_unresolved_terminal_once(monkeypatch):
    from api import routes

    class SessionStub(SimpleNamespace):
        def save(self, **kwargs):
            self.save_calls += 1

    session_id = "session-older-unresolved"
    old_stream = "stream-old-terminal"
    new_stream = "stream-new-terminal"
    messages = [
        {"role": "user", "content": "old work"},
        {"role": "assistant", "content": "old answer", "_ts": 2.0},
        {"role": "user", "content": "new work"},
        {"role": "assistant", "content": "new answer", "_ts": 4.0},
    ]
    summaries = [
        {
            "session_id": session_id,
            "run_id": new_stream,
            "last_seq": 2,
            "last_event_id": f"{new_stream}:2",
            "terminal": True,
            "terminal_state": "completed",
        },
        {
            "session_id": session_id,
            "run_id": old_stream,
            "last_seq": 3,
            "last_event_id": f"{old_stream}:3",
            "terminal": True,
            "terminal_state": "completed",
        },
    ]
    old_events = [
        {
            "event": "reasoning",
            "seq": 1,
            "event_id": f"{old_stream}:1",
            "created_at": 1.0,
            "payload": {"text": "old thinking"},
        },
        {
            "event": "token",
            "seq": 2,
            "event_id": f"{old_stream}:2",
            "created_at": 2.0,
            "payload": {"text": "old answer"},
        },
        {
            "version": 1,
            "event": "done",
            "seq": 3,
            "event_id": f"{old_stream}:3",
            "run_id": old_stream,
            "session_id": session_id,
            "created_at": 3.0,
            "terminal": True,
            "payload": {
                "terminal_message_target": {
                    "version": "terminal_message_target_v1",
                    "session_id": session_id,
                    "run_id": old_stream,
                    "stream_id": old_stream,
                    "message_index": 1,
                    "message_ref": _client_anchor_scene_message_ref(messages[1]),
                },
                "session": {
                    "session_id": session_id,
                    "message_count": 2,
                    "messages": messages[:2],
                },
            },
        },
    ]
    old_events = _authoritative_journal_events(old_events, session_id, old_stream)
    new_events = [
        {
            "event": "token",
            "seq": 1,
            "event_id": f"{new_stream}:1",
            "created_at": 3.0,
            "payload": {"text": "new answer"},
        },
        {
            "event": "done",
            "seq": 2,
            "event_id": f"{new_stream}:2",
            "created_at": 4.0,
            "terminal": True,
            "payload": {
                "session": {
                    "session_id": "session-older-unresolved",
                    "message_count": 4,
                    "messages": messages,
                },
            },
        },
    ]
    new_events = _authoritative_journal_events(new_events, session_id, new_stream)
    new_ref = _client_anchor_scene_message_ref(messages[3])
    session = SessionStub(
        session_id=session_id,
        tool_calls=[],
        anchor_activity_scenes={
            new_ref: {
                "version": "anchor_activity_scene_record_v1",
                "message_index": 3,
                "message_ref": new_ref,
                "stream_id": new_stream,
                "scene": {"version": "activity_scene_v1", "activity_rows": [{"role": "prose"}]},
                "updated_at": 1.0,
            }
        },
        save_calls=0,
    )
    monkeypatch.setattr(routes, "terminal_run_summaries_for_session", lambda sid, **kwargs: summaries)
    monkeypatch.setattr(routes, "find_run_summary", lambda rid: summaries[0] if rid == new_stream else summaries[1])
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {"events": new_events if run_id == new_stream else old_events},
    )
    monkeypatch.setattr(routes, "_active_stream_ids", lambda: {new_stream})

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is True
    assert session.save_calls == 1
    assert {record["stream_id"] for record in session.anchor_activity_scenes.values()} == {
        new_stream,
        old_stream,
    }
    old_record = [
        record for record in session.anchor_activity_scenes.values() if record["stream_id"] == old_stream
    ][0]
    assert old_record["message_index"] == 1

    assert routes._materialize_terminal_anchor_scene_from_run_journal(session, messages) is False
    assert session.save_calls == 1


def test_anchor_scene_hydration_splits_final_suffix_from_owned_process_prefix():
    from api import routes

    stream_id = "stream-prefix-suffix"
    messages = [
        {"role": "user", "content": "do work"},
        {
            "role": "assistant",
            "content": "Inspecting the fixture before the tool call. Lifecycle gate final answer.",
            "_ts": 10.0,
        },
    ]
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "final_answer": "",
        "activity_rows": [
            {
                "role": "thinking",
                "kind": "reasoning",
                "source_event_type": "reasoning",
                "local_id": f"live-reasoning:{stream_id}:1",
                "text": "Checking the persistent assistant turn.",
                "status": "completed",
            },
            {
                "role": "prose",
                "kind": "process_prose",
                "source_event_type": "token",
                "local_id": f"live-prose:{stream_id}:1",
                "text": "Inspecting the fixture before the tool call.",
                "status": "completed",
            },
            {
                "role": "tool",
                "kind": "tool_completed",
                "source_event_type": "tool_complete",
                "local_id": "lifecycle-tool-1",
                "tool_call_id": "lifecycle-tool-1",
                "text": "README fixture read",
                "status": "completed",
                "tool": {"id": "lifecycle-tool-1", "name": "read_file", "args": {}, "done": True},
                "payload": {"tid": "lifecycle-tool-1", "name": "read_file"},
            },
        ],
    }

    hydrated = routes._complete_hydrated_anchor_scene(messages, scene, 1, stream_id=stream_id)

    assert hydrated["final_answer"] == "Lifecycle gate final answer."
    assert all(row.get("source_event_type") != "settled_message" for row in hydrated["activity_rows"])
    assert _anchor_scene_visible_semantics(hydrated) == [
        {
            "role": "thinking",
            "kind": "reasoning",
            "text": "Checking the persistent assistant turn.",
        },
        {
            "role": "prose",
            "kind": "process_prose",
            "text": "Inspecting the fixture before the tool call.",
        },
        {
            "role": "tool",
            "kind": "tool_completed",
            "status": "completed",
            "tool_call_id": "lifecycle-tool-1",
            "name": "read_file",
            "args": {},
            "done": True,
        },
    ]


def test_runtime_journal_snapshot_preserves_reasoning_segment_identities(monkeypatch):
    from api import routes

    stream_id = "stream-segmented-reasoning"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "created_at": 1.0,
            "payload": {"text": "plan first"},
        },
        {
            "event": "token",
            "seq": 2,
            "created_at": 2.0,
            "payload": {"text": "first progress"},
        },
        {
            "event": "tool",
            "seq": 3,
            "created_at": 3.0,
            "payload": {"name": "read_file", "tid": "call-1"},
        },
        {
            "event": "tool_complete",
            "seq": 4,
            "created_at": 4.0,
            "payload": {"name": "read_file", "tid": "call-1", "preview": "done"},
        },
        {
            "event": "reasoning",
            "seq": 5,
            "created_at": 5.0,
            "payload": {"text": "check result"},
        },
        {
            "event": "token",
            "seq": 6,
            "created_at": 6.0,
            "payload": {"text": " second progress"},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-segmented-reasoning",
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
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    thinking_rows = [row for row in rows if row["role"] == "thinking"]

    assert [row["role"] for row in rows] == ["thinking", "prose", "tool", "thinking", "prose"]
    assert [row["local_id"] for row in thinking_rows] == [
        f"live-reasoning:{stream_id}:1",
        f"live-reasoning:{stream_id}:2",
    ]
    assert [row["text"] for row in thinking_rows] == ["plan first", "check result"]
    assert [row["group"]["activity_segment_seq"] for row in thinking_rows] == [1, 2]


def test_runtime_journal_completion_only_tool_starts_a_new_reasoning_segment(monkeypatch):
    from api import routes

    stream_id = "stream-completion-only-tool"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "created_at": 1.0,
            "payload": {"text": "plan first"},
        },
        {
            "event": "tool_complete",
            "seq": 2,
            "created_at": 2.0,
            "payload": {"name": "read_file", "tid": "call-1", "preview": "done"},
        },
        {
            "event": "reasoning",
            "seq": 3,
            "created_at": 3.0,
            "payload": {"text": "check result"},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-completion-only-tool",
            "last_seq": 3,
            "last_event_id": f"{stream_id}:3",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    rows = snapshot["anchor_activity_scene"]["activity_rows"]
    thinking_rows = [row for row in rows if row["role"] == "thinking"]

    assert [row["role"] for row in rows] == ["thinking", "tool", "thinking"]
    assert [row["local_id"] for row in thinking_rows] == [
        f"live-reasoning:{stream_id}:1",
        f"live-reasoning:{stream_id}:2",
    ]
    assert [row["text"] for row in thinking_rows] == ["plan first", "check result"]
    assert rows[1]["tool"]["done"] is True
    assert snapshot["current_live_segment_seq"] == 2


def test_runtime_journal_no_prose_tool_preserves_global_segment_order(monkeypatch):
    from api import routes

    stream_id = "stream-no-prose-tool-global-order"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "created_at": 1.0,
            "payload": {"text": "plan first"},
        },
        {
            "event": "tool",
            "seq": 2,
            "created_at": 2.0,
            "payload": {"name": "read_file", "tid": "call-1"},
        },
        {
            "event": "tool_complete",
            "seq": 3,
            "created_at": 3.0,
            "payload": {"name": "read_file", "tid": "call-1", "preview": "done"},
        },
        {
            "event": "interim_assistant",
            "seq": 4,
            "created_at": 4.0,
            "payload": {"text": "progress after tool"},
        },
        {
            "event": "tool",
            "seq": 5,
            "created_at": 5.0,
            "payload": {"name": "search", "tid": "call-2"},
        },
        {
            "event": "tool_complete",
            "seq": 6,
            "created_at": 6.0,
            "payload": {"name": "search", "tid": "call-2", "preview": "done"},
        },
        {
            "event": "reasoning",
            "seq": 7,
            "created_at": 7.0,
            "payload": {"text": "check result"},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-no-prose-tool-global-order",
            "last_seq": 7,
            "last_event_id": f"{stream_id}:7",
        },
    )
    monkeypatch.setattr(
        routes,
        "read_run_events",
        lambda session_id, run_id: {"events": events},
    )

    snapshot = routes._run_journal_live_snapshot(stream_id)
    rows = snapshot["anchor_activity_scene"]["activity_rows"]

    assert [row["role"] for row in rows] == [
        "thinking",
        "tool",
        "prose",
        "tool",
        "thinking",
    ]
    assert [row["local_id"] for row in rows] == [
        f"live-reasoning:{stream_id}:1",
        "call-1",
        f"live-prose:{stream_id}:2",
        "call-2",
        f"live-reasoning:{stream_id}:3",
    ]
    assert [row["group"].get("activity_segment_seq") for row in rows] == [1, 1, 2, 2, 3]
    assert snapshot["current_live_segment_seq"] == 3


def test_runtime_journal_reasoning_segment_keeps_its_first_timestamp(monkeypatch):
    from api import routes

    stream_id = "stream-reasoning-timestamp"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "created_at": 10.0,
            "payload": {"text": "first "},
        },
        {
            "event": "reasoning",
            "seq": 2,
            "created_at": 20.0,
            "payload": {"text": "second"},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-reasoning-timestamp",
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
    thinking_rows = [
        row
        for row in snapshot["anchor_activity_scene"]["activity_rows"]
        if row["role"] == "thinking"
    ]

    assert len(thinking_rows) == 1
    assert thinking_rows[0]["text"] == "first second"
    assert thinking_rows[0]["created_at"] == 10.0


def test_hydration_prefers_persisted_reasoning_segments_over_transcript_aggregate():
    from api import routes

    stream_id = "stream-settled-segments"
    messages = [
        {"role": "user", "content": "prompt"},
        {
            "role": "assistant",
            "content": "final answer",
            "reasoning": "first thoughtsecond thought",
        },
    ]
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "activity_rows": [
            {
                "role": "thinking",
                "kind": "reasoning",
                "source_event_type": "reasoning",
                "status": "running",
                "row_id": f"live-reasoning:{stream_id}:1",
                "local_id": f"live-reasoning:{stream_id}:1",
                "text": "first thought",
            },
            {
                "role": "tool",
                "kind": "tool_completed",
                "source_event_type": "tool_complete",
                "status": "completed",
                "row_id": "tool-1",
                "local_id": "tool-1",
                "tool_call_id": "tool-1",
                "tool": {"id": "tool-1", "name": "read_file", "done": True},
            },
            {
                "role": "thinking",
                "kind": "reasoning",
                "source_event_type": "reasoning",
                "status": "running",
                "row_id": f"live-reasoning:{stream_id}:2",
                "local_id": f"live-reasoning:{stream_id}:2",
                "text": "second thought",
            },
        ],
    }

    hydrated = routes._complete_hydrated_anchor_scene(
        messages,
        scene,
        1,
        stream_id=stream_id,
    )
    rows = hydrated["activity_rows"]
    thinking_rows = [row for row in rows if row["role"] == "thinking"]

    assert [row["role"] for row in rows] == ["thinking", "tool", "thinking"]
    assert [row["local_id"] for row in thinking_rows] == [
        f"live-reasoning:{stream_id}:1",
        f"live-reasoning:{stream_id}:2",
    ]
    assert [row["text"] for row in thinking_rows] == ["first thought", "second thought"]
    assert all(row["status"] == "completed" for row in thinking_rows)


def test_hydration_prefers_persisted_segments_over_structured_content_reasoning():
    from api import routes

    stream_id = "stream-structured-settled-segments"
    messages = [
        {"role": "user", "content": "prompt"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "text": "first thoughtsecond thought"},
                {"type": "tool_use", "id": "tool-1", "name": "read_file"},
                {"type": "text", "text": "final answer"},
            ],
        },
    ]
    scene = {
        "version": "activity_scene_v1",
        "mode": "compact_worklog",
        "activity_rows": [
            {
                "role": "thinking",
                "kind": "reasoning",
                "source_event_type": "reasoning",
                "status": "running",
                "row_id": f"live-reasoning:{stream_id}:1",
                "local_id": f"live-reasoning:{stream_id}:1",
                "text": "first thought",
            },
            {
                "role": "tool",
                "kind": "tool_completed",
                "source_event_type": "tool_complete",
                "status": "completed",
                "row_id": "tool-1",
                "local_id": "tool-1",
                "tool_call_id": "tool-1",
                "tool": {"id": "tool-1", "name": "read_file", "done": True},
            },
            {
                "role": "thinking",
                "kind": "reasoning",
                "source_event_type": "reasoning",
                "status": "running",
                "row_id": f"live-reasoning:{stream_id}:2",
                "local_id": f"live-reasoning:{stream_id}:2",
                "text": "second thought",
            },
        ],
    }

    hydrated = routes._complete_hydrated_anchor_scene(
        messages,
        scene,
        1,
        stream_id=stream_id,
    )
    rows = hydrated["activity_rows"]
    thinking_rows = [row for row in rows if row["role"] == "thinking"]

    assert [row["role"] for row in rows] == ["thinking", "tool", "thinking"]
    assert [row["local_id"] for row in thinking_rows] == [
        f"live-reasoning:{stream_id}:1",
        f"live-reasoning:{stream_id}:2",
    ]
    assert [row["text"] for row in thinking_rows] == ["first thought", "second thought"]


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


def test_runtime_journal_snapshot_dedupes_echo_spanning_reasoning_segments(monkeypatch):
    from api import routes

    stream_id = "stream-cross-segment-reasoning-echo"
    first = "先检查仓库。"
    second = "再确认测试。"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "created_at": 1.0,
            "payload": {"text": first},
        },
        {
            "event": "token",
            "seq": 2,
            "created_at": 2.0,
            "payload": {"text": "正在处理。"},
        },
        {
            "event": "tool",
            "seq": 3,
            "created_at": 3.0,
            "payload": {"name": "read_file", "tid": "call-1"},
        },
        {
            "event": "tool_complete",
            "seq": 4,
            "created_at": 4.0,
            "payload": {"name": "read_file", "tid": "call-1", "preview": "done"},
        },
        {
            "event": "reasoning",
            "seq": 5,
            "created_at": 5.0,
            "payload": {"text": second},
        },
        {
            "event": "interim_assistant",
            "seq": 6,
            "created_at": 6.0,
            "payload": {"text": first + second, "reasoning_echo": True},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-cross-segment-reasoning-echo",
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
    rows = snapshot["anchor_activity_scene"]["activity_rows"]

    assert snapshot["last_reasoning_text"] == ""
    assert not any(row["role"] == "thinking" for row in rows)
    assert [row["role"] for row in rows] == ["prose", "tool", "prose"]


def test_runtime_journal_reasoning_echo_preserves_consumed_segment_high_water(monkeypatch):
    from api import routes

    stream_id = "stream-reasoning-echo-high-water"
    events = [
        {
            "event": "reasoning",
            "seq": 1,
            "created_at": 1.0,
            "payload": {"text": "first"},
        },
        {
            "event": "tool",
            "seq": 2,
            "created_at": 2.0,
            "payload": {"name": "read_file", "tid": "call-1"},
        },
        {
            "event": "tool_complete",
            "seq": 3,
            "created_at": 3.0,
            "payload": {"name": "read_file", "tid": "call-1", "preview": "done"},
        },
        {
            "event": "reasoning",
            "seq": 4,
            "created_at": 4.0,
            "payload": {"text": "second"},
        },
        {
            "event": "interim_assistant",
            "seq": 5,
            "created_at": 5.0,
            "payload": {"text": "second", "reasoning_echo": True},
        },
        {
            "event": "reasoning",
            "seq": 6,
            "created_at": 6.0,
            "payload": {"text": "third"},
        },
    ]
    monkeypatch.setattr(
        routes,
        "find_run_summary",
        lambda sid: {
            "session_id": "session-reasoning-echo-high-water",
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
    thinking_rows = [
        row
        for row in snapshot["anchor_activity_scene"]["activity_rows"]
        if row["role"] == "thinking"
    ]

    assert [row["local_id"] for row in thinking_rows] == [
        f"live-reasoning:{stream_id}:1",
        f"live-reasoning:{stream_id}:3",
    ]
    assert [row["text"] for row in thinking_rows] == ["first", "third"]
    assert snapshot["current_live_segment_seq"] == 3


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
