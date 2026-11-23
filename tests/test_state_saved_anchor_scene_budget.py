"""Keep near-cap Worklog rows durable after state_saved-before-done recovery."""
from __future__ import annotations

import copy
import json
from collections import OrderedDict
from types import SimpleNamespace

import pytest

import api.models as models
import api.routes as routes
from api.models import Session
from api.run_journal import RunJournalWriter, _run_path, read_run_events


def _encoded(scene):
    return json.dumps(scene, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")


@pytest.fixture
def scene_store(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", directory)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", directory / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", OrderedDict())
    monkeypatch.setattr(routes, "SESSION_DIR", directory)
    monkeypatch.setattr(routes, "SESSIONS", models.SESSIONS)
    return directory


def _persist_scene(monkeypatch, session_id, stream_id, scene):
    captured = {}
    monkeypatch.setattr(routes, "j", lambda handler, payload, status=200, **kwargs:
                        captured.update(status=status, payload=payload) or True)
    monkeypatch.setattr(routes, "bad", lambda handler, error, status=400, **kwargs:
                        captured.update(status=status, error=error) or True)
    assert routes._handle_session_anchor_scene(SimpleNamespace(), {
        "session_id": session_id, "stream_id": stream_id,
        "message_index": 1, "scene": scene,
    }) is True
    return captured


def _session(session_id):
    messages = [{"role": "user", "content": "Explain the work"},
                {"role": "assistant", "content": "Final answer", "timestamp": 10}]
    session = Session(session_id=session_id, messages=copy.deepcopy(messages),
                      context_messages=copy.deepcopy(messages))
    session.save(skip_index=True)
    return session


@pytest.mark.parametrize("character", ["x", "雪"], ids=["ascii", "utf8"])
def test_recovered_state_saved_near_cap_persists_original_worklog_rows(scene_store, monkeypatch, character):
    """Real journal recovery in the reload-after-save/before-done window."""
    sid, stream = "near-cap-state-saved", "near-cap-state-saved-run"
    session = _session(sid)
    writer = RunJournalWriter(sid, stream)
    writer.append_sse_event("token", {"text": "Initial progress"})
    writer.append_sse_event("tool", {"name": "read_file", "tid": "near-cap-tool", "args": {"path": "example.txt"}})
    writer.append_sse_event("tool_complete", {"name": "read_file", "tid": "near-cap-tool", "preview": "Read result"})
    writer.append_sse_event("token", {"text": "P"})
    initial = routes._run_journal_live_snapshot(stream)["anchor_activity_scene"]
    initial.pop("side_effects")
    # The prose text appears in both its row and payload. Fill using actual
    # journal tokens, not a handmade oversized scene or a changed cap.
    remaining = 252_572 - len(_encoded(initial))
    writer.append_sse_event("token", {"text": character * (remaining // (2 * len(character.encode("utf-8"))))})
    for number in range(11):
        writer.append_sse_event("state_saved", {
            "session_id": sid, "kind": "memory", "action": "saved",
            "name": f"saved-outcome-{number}-" + "n" * 60,
        })
    journal_before = _run_path(sid, stream).read_bytes()
    assert not any(event.get("terminal") for event in read_run_events(sid, stream)["events"])
    snapshot = routes._run_journal_live_snapshot(stream)
    scene = snapshot["anchor_activity_scene"]
    assert scene["lifecycle"]["status"] == "running"
    assert len(scene["side_effects"]) == 11
    original_scene = copy.deepcopy(scene)
    rows_only = copy.deepcopy(scene)
    rows_only.pop("side_effects")
    assert 252_500 <= len(_encoded(rows_only)) <= 252_650
    assert len(_encoded(rows_only)) < 256_000 < len(_encoded(scene))
    if character == "雪":
        assert len(_encoded(scene).decode("utf-8")) < 256_000

    result = _persist_scene(monkeypatch, sid, stream, scene)
    assert result["status"] == 200, result
    models.SESSIONS.clear()
    loaded = Session.load(sid)
    record = next(iter(loaded.anchor_activity_scenes.values()))
    assert record["scene"] == rows_only
    assert len(_encoded(record["scene"])) <= 256_000
    assert loaded.messages == session.messages
    assert loaded.context_messages == session.context_messages
    assert loaded.updated_at == session.updated_at
    hydrated = routes._hydrate_anchor_activity_scenes(loaded.messages, loaded.anchor_activity_scenes)
    saved_rows = hydrated[1]["_anchor_activity_scene"]["activity_rows"]
    # Hydration normalizes compatibility metadata (e.g. seq None -> 0).
    # Verify exact durable rows above, and stable IDs/order/visible content here.
    assert [row["row_id"] for row in saved_rows] == [row["row_id"] for row in scene["activity_rows"]]
    assert [(row.get("role"), row.get("text"), row.get("tool_call_id")) for row in saved_rows] == [
        (row.get("role"), row.get("text"), row.get("tool_call_id")) for row in scene["activity_rows"]
    ]
    assert any(row.get("tool_call_id") == "near-cap-tool" for row in saved_rows)
    assert scene == original_scene
    assert _run_path(sid, stream).read_bytes() == journal_before


def _scene_at_size(target, *, side_effects=True, multibyte=False):
    scene = {
        "version": "activity_scene_v1", "mode": "compact_worklog",
        "identity": {"session_id": "scene-budget", "stream_id": "budget-run"},
        "activity_rows": [{"role": "prose", "text": "雪" if multibyte else ""}],
        "final_answer": "Final answer", "side_effects_truncated": False,
    }
    if side_effects:
        scene["side_effects"] = [{"source_event_type": "state_saved", "payload": {"kind": "memory", "action": "saved"}}]
    padding = target - len(_encoded(scene))
    assert padding >= 0
    scene["activity_rows"][0]["text"] += "x" * padding
    assert len(_encoded(scene)) == target
    return scene


@pytest.mark.parametrize("size", [255_999, 256_000, 256_001])
@pytest.mark.parametrize("multibyte", [False, True])
def test_full_scene_byte_boundary_preserves_outcomes_until_over_cap(scene_store, monkeypatch, size, multibyte):
    _session("scene-budget")
    scene = _scene_at_size(size, multibyte=multibyte)
    original = copy.deepcopy(scene)
    expected = copy.deepcopy(scene)
    if size > 256_000:
        expected.pop("side_effects")
    result = _persist_scene(monkeypatch, "scene-budget", "budget-run", scene)
    assert result["status"] == 200, result
    loaded = Session.load("scene-budget")
    assert next(iter(loaded.anchor_activity_scenes.values()))["scene"] == expected
    assert scene == original


@pytest.mark.parametrize("size", [255_999, 256_000, 256_001])
@pytest.mark.parametrize("outcomes", ["missing", "empty", "recovered"])
def test_rows_only_budget_is_still_enforced(scene_store, monkeypatch, size, outcomes):
    session = _session("scene-budget")
    scene = _scene_at_size(size, side_effects=False)
    if outcomes == "empty":
        scene["side_effects"] = []
    elif outcomes == "recovered":
        scene["side_effects"] = [{"source_event_type": "state_saved", "payload": {"kind": "memory", "action": "saved"}}]
    original = copy.deepcopy(scene)
    session_file = scene_store / "scene-budget.json"
    before = session_file.read_bytes()
    result = _persist_scene(monkeypatch, "scene-budget", "budget-run", scene)
    loaded = Session.load("scene-budget")
    if size > 256_000:
        assert result["status"] == 400
        assert result["error"] == "scene payload is too large"
        assert session_file.read_bytes() == before
        assert loaded.anchor_activity_scenes == {}
    else:
        assert result["status"] == 200, result
        saved = next(iter(loaded.anchor_activity_scenes.values()))["scene"]
        expected = copy.deepcopy(scene)
        if len(_encoded(scene)) > 256_000:
            expected.pop("side_effects")
        assert saved == expected
    assert loaded.messages == session.messages
    assert loaded.context_messages == session.context_messages
    assert scene == original


def test_outcome_fallback_does_not_bypass_existing_row_count_limit():
    scene = {"version": "activity_scene_v1", "activity_rows": [{}] * 1_001,
             "side_effects": [{"payload": {"name": "x" * 256_000}}]}
    with pytest.raises(ValueError, match="scene.activity_rows is too large"):
        routes._sanitize_anchor_activity_scene(scene)
