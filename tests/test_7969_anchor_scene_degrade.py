"""#7969: an over-cap anchor scene degrades to fit instead of being rejected whole."""
from __future__ import annotations

import copy
from collections import OrderedDict

import pytest

import api.models as models
import api.routes as routes
from api.models import Session
from tests.test_state_saved_anchor_scene_budget import _encoded, _persist_scene

CAP = routes._ANCHOR_ACTIVITY_SCENE_MAX_BYTES
POLICY = "activity_scene_v1.degrade"


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


def _scene(rows, **extra):
    return {"version": "activity_scene_v1", "mode": "compact_worklog", "activity_rows": rows,
            "final_answer": "Final answer", **extra}


def _sanitize(scene):
    original = copy.deepcopy(scene)
    saved = routes._sanitize_anchor_activity_scene(scene)
    assert scene == original
    assert len(_encoded(saved)) <= CAP
    return saved


def _tool_card_snippet(row):
    # Mirrors static/ui.js _anchorSceneToolCallFromRow for a settled tool row.
    tool, payload = row.get("tool") or {}, row.get("payload") or {}
    return (tool.get("snippet") or payload.get("snippet") or payload.get("result")
            or payload.get("output") or row.get("text") or "")


def _session(sid, reasoning=None):
    final = {"role": "assistant", "content": "Final answer", "timestamp": 10}
    if reasoning is not None:
        final["reasoning"] = reasoning
    messages = [{"role": "user", "content": "Explain the work"}, final]
    session = Session(session_id=sid, messages=copy.deepcopy(messages), context_messages=copy.deepcopy(messages))
    session.save(skip_index=True)
    return session


def _hydrated_rows(sid):
    models.SESSIONS.clear()
    loaded = Session.load(sid)
    hydrated = routes._hydrate_anchor_activity_scenes(loaded.messages, loaded.anchor_activity_scenes)
    return hydrated[1]["_anchor_activity_scene"]["activity_rows"]


def test_dedupe_that_fits_only_without_marker_keeps_shrinking():
    text = "r" * 200_000
    scene = _scene([{"row_id": "p", "role": "prose", "text": text, "payload": {"text": text}}])
    unmarked = copy.deepcopy(scene)
    unmarked["activity_rows"][0].pop("payload")
    unmarked["activity_rows"][0]["payload"] = {}
    gap = CAP - len(_encoded(unmarked))
    unmarked["activity_rows"][0]["text"] = text = "r" * (200_000 + gap - 5)
    scene["activity_rows"][0].update(text=text, payload={"text": text})
    assert len(_encoded(unmarked)) == CAP - 5
    saved = _sanitize(scene)
    assert saved["truncated"]["deduped_fields"] == 1
    assert saved["truncated"]["clamped_fields"] == 1
    assert saved["activity_rows"][0]["clamped"]["text"]["bytes"] == len(text)


def test_row_trim_charges_retained_rows_and_final_marker_exactly():
    tool = {"row_id": "t", "role": "tool", "tool_call_id": "call-1", "status": "completed",
            "tool": {"id": "call-1", "name": "terminal", "args": {"command": ""}}}
    terminals = [{"row_id": f"z{i}", "role": "terminal", "text": "z" * 3_900} for i in range(8)]
    scene = _scene(terminals + [tool])
    marked_tool_only = _scene([tool], truncated={"policy": POLICY, "dropped_rows": 8})
    tool["tool"]["args"]["command"] = "c" * (CAP - 3 - len(_encoded(marked_tool_only)))
    marked_tool_only["activity_rows"] = [tool]
    assert len(_encoded(marked_tool_only)) == CAP - 3
    saved = _sanitize(scene)
    assert saved == marked_tool_only


@pytest.mark.parametrize("keep", [1, 3, 7])
def test_row_trim_drops_only_as_many_rows_as_needed(keep):
    rows = [{"row_id": f"z{i}", "role": "terminal", "text": "z" * 3_900} for i in range(8)]
    rows.append({"row_id": "t", "role": "tool", "tool": {"id": "t", "args": {"command": ""}}})
    probe = _scene(rows[8 - keep:], truncated={"policy": POLICY, "dropped_rows": 8 - keep})
    rows[-1]["tool"]["args"]["command"] = "c" * (CAP - len(_encoded(probe)))
    saved = _sanitize(_scene(rows))
    assert [row["row_id"] for row in saved["activity_rows"]] == [row["row_id"] for row in rows[8 - keep:]]
    assert saved["truncated"]["dropped_rows"] == 8 - keep


def test_dedupe_preserves_a_tool_rows_only_result_body():
    tool = {"row_id": "t", "role": "tool", "status": "completed", "text": "ONLY RESULT",
            "tool": {"id": "call-1", "name": "web_search", "preview": "SUMMARY", "snippet": ""},
            "payload": {"text": "ONLY RESULT"}}
    reasoning = routes._anchor_scene_thinking_row("x" * 300_000, 0, 1)
    assert _tool_card_snippet(tool) == "ONLY RESULT"
    saved = _sanitize(_scene([reasoning, tool]))
    assert saved["truncated"]["deduped_fields"] >= 1
    assert _tool_card_snippet(saved["activity_rows"][1]) == "ONLY RESULT"
    assert saved["activity_rows"][1]["tool"]["preview"] == "SUMMARY"


def test_dedupe_drops_only_proven_tool_mirrors():
    body = "b" * 100_000
    tool = {"row_id": "t", "role": "tool", "status": "completed", "text": body,
            "tool": {"id": "call-1", "tid": "call-1", "name": "terminal", "snippet": body, "args": {"a": 1}},
            "payload": {"text": body, "snippet": body, "args": {"a": 1}, "extra": "kept"}}
    row = _sanitize(_scene([tool]))["activity_rows"][0]
    assert row["tool"] == {"id": "call-1", "name": "terminal", "snippet": body, "args": {"a": 1}}
    assert row["payload"] == {"extra": "kept"}
    assert "text" not in row and _tool_card_snippet(row) == body


def test_clamp_never_rewrites_identity_or_invocation_fields():
    long_id = "id-" + "9" * 6_000
    args = {"path": "p" * 40_000, "edits": [{"old": "o" * 20_000, "new": "n" * 20_000}], "argv": ["a" * 9_000]}
    tool = {"row_id": long_id + "-a", "event_id": long_id + "-a", "role": "tool", "tool_call_id": long_id,
            "tool": {"id": long_id, "name": "edit", "args": args, "snippet": "s" * 120_000}}
    other = {"row_id": long_id + "-b", "event_id": long_id + "-b", "role": "thinking", "text": "t" * 120_000}
    final_answer = "f" * 30_000
    saved = _sanitize(_scene([tool, other], final_answer=final_answer, links={"call": long_id}))
    row, thinking = saved["activity_rows"]
    assert row["tool"]["args"] == args and row["tool"]["id"] == row["tool_call_id"] == long_id
    assert (row["row_id"], row["event_id"]) == (long_id + "-a", long_id + "-a")
    assert (thinking["row_id"], thinking["event_id"]) == (long_id + "-b", long_id + "-b")
    assert saved["final_answer"] == final_answer and saved["links"] == {"call": long_id}
    assert set(row["clamped"]) == {"tool.snippet"} and set(thinking["clamped"]) == {"text"}
    assert row["tool"]["snippet"].endswith(routes._ANCHOR_SCENE_CLAMP_SUFFIX)


def test_clamped_reasoning_rehydrates_as_one_full_row(scene_store, monkeypatch):
    reasoning = "思" + "r" * 300_013
    _session("degrade-reasoning", reasoning=reasoning)
    row = routes._anchor_scene_thinking_row(reasoning, 0, 1, "degrade-run")
    result = _persist_scene(monkeypatch, "degrade-reasoning", "degrade-run", _scene([row]))
    assert result["status"] == 200, result
    assert result["payload"]["truncated"]["clamped_fields"] == 1
    saved = next(iter(Session.load("degrade-reasoning").anchor_activity_scenes.values()))["scene"]
    assert len(_encoded(saved)) <= CAP and saved["activity_rows"][0]["text"] != reasoning
    rows = [r for r in _hydrated_rows("degrade-reasoning") if r.get("role") == "thinking"]
    assert [r["text"] for r in rows] == [reasoning]
    assert "clamped" not in rows[0]


def test_degraded_tool_result_survives_cold_hydration(scene_store, monkeypatch):
    _session("degrade-tool")
    tool = {"row_id": "t", "role": "tool", "status": "completed", "text": "ONLY RESULT", "tool_call_id": "call-1",
            "tool": {"id": "call-1", "name": "web_search", "preview": "SUMMARY", "snippet": ""},
            "payload": {"text": "ONLY RESULT"}}
    scene = _scene([routes._anchor_scene_thinking_row("x" * 300_000, 0, 1), tool])
    assert _persist_scene(monkeypatch, "degrade-tool", "degrade-run", scene)["status"] == 200
    tools = [r for r in _hydrated_rows("degrade-tool") if r.get("role") == "tool"]
    assert [_tool_card_snippet(r) for r in tools] == ["ONLY RESULT"]
