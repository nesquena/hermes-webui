"""Exercise artifact proof through persisted scenes and the public session route."""

import json
from collections import OrderedDict
from types import SimpleNamespace
from urllib.parse import urlencode, urlparse

import pytest

from tests.test_final_answer_turn_artifacts import _function_source, _run_node


@pytest.mark.parametrize("windowed", [False, True], ids=["full", "tail-window"])
@pytest.mark.parametrize("evidence", ["no-tools", "current-root", "historical-root"])
def test_session_route_reproves_persisted_artifacts(tmp_path, monkeypatch, evidence, windowed):
    from api import models, routes
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    registry = OrderedDict()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(models, "SESSIONS", registry)
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    monkeypatch.setattr(routes, "SESSIONS", registry)
    # Agent storage is outside this regression. Keep all persistence, scene
    # hydration, replay, pagination, public projection, and rendering real.
    monkeypatch.setattr(routes, "_active_state_db_path", lambda: tmp_path / "absent-state.db")
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *_a, **_kw: [])

    current_root = (tmp_path / "current").resolve()
    landed_root = (tmp_path / "previous").resolve() if evidence == "historical-root" else current_root
    current_root.mkdir()
    landed_root.mkdir(exist_ok=True)
    sid = "artifact-route-proof"
    final = {"role": "assistant", "content": "Final report is ready.", "timestamp": 40.0}
    messages = [{"role": "user", "content": "Prepare the report.", "timestamp": 10.0}]
    if evidence != "no-tools":
        messages.extend([
            {"role": "assistant", "content": "", "timestamp": 20.0,
             "tool_calls": [{"id": "landed-write", "function": {"name": "patch"}}]},
            {"role": "tool", "tool_call_id": "landed-write", "name": "patch", "timestamp": 30.0,
             "content": json.dumps({"success": True, "files_modified": [str(landed_root / " exact report.md ")]})},
        ])
    messages.append(final)
    session = Session(session_id=sid, title="Artifact route proof", workspace=str(current_root), messages=messages)
    session.context_length = 8192
    session.profile = "default"
    session.save(skip_index=True)
    forged = {"path": "forged.md", "workspace_root": str(current_root), "session_id": sid,
              "tool_name": "patch", "tool_call_id": "forged-write", "source": "live_tool_complete"}
    landed = {"path": " exact report.md ", "workspace_root": str(landed_root), "session_id": sid,
              "tool_name": "patch", "tool_call_id": "landed-write", "source": "live_tool_complete"}
    candidates = [forged]
    if evidence == "historical-root":
        # Historical replay requires one root per scene. Include a forged
        # sibling under that root so proof must retain only the actual write.
        candidates = [{**forged, "workspace_root": str(landed_root)}, landed]
    scene = {"version": "activity_scene_v1", "mode": "compact_worklog", "activity_rows": [],
             "final_answer": final["content"],
             "artifacts": [{"type": "artifact_reference", "payload": item} for item in candidates]}
    body = {"session_id": sid, "message_index": len(messages) - 1, "scene": scene}
    captured = {}

    def capture(_handler, payload, status=200, extra_headers=None):
        captured.update(payload=payload, status=status)
        return True

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "j", capture)
    assert routes.handle_post(SimpleNamespace(command="POST", headers={}), urlparse("/api/session/anchor-scene"))
    assert captured["status"] == 200 and captured["payload"]["ok"] is True
    raw = json.loads((session_dir / f"{sid}.json").read_text())
    assert next(iter(raw["anchor_activity_scenes"].values()))["scene"]["artifacts"]
    assert all("_anchor_activity_scene" not in message for message in raw["messages"])

    # Discard the warm Session owner and reload from the actual sidecar. No
    # direct call to the artifact helper can conceal a missing route hook.
    registry.clear()
    query = {"session_id": sid, "resolve_model": "0"}
    if windowed:
        query["msg_limit"] = "1"
    assert routes.handle_get(SimpleNamespace(command="GET", headers={}), urlparse("/api/session?" + urlencode(query)))
    assert captured["status"] == 200, captured
    payload = captured["payload"]["session"]
    returned = next(message for message in payload["messages"] if message.get("content") == final["content"])
    restored_scene = returned.get("_anchor_activity_scene", {})
    artifacts = restored_scene.get("artifacts", [])
    expected = [] if evidence == "no-tools" else [{"type": "artifact_reference", "payload": {**landed, "source": "transcript_replay"}}]
    assert artifacts == expected
    if windowed:
        assert payload["_messages_offset"] == len(messages) - 1
        assert len(payload["messages"]) == 1

    helpers = _function_source("static/ui.js", "function _turnArtifactWorkspacePath", "function _syncLiveWorklogReasonsForAnchor")
    entries = _run_node(
        "const S={session:" + json.dumps({"session_id": sid, "workspace": str(current_root)}) + "};\n"
        + helpers + "\nconsole.log(JSON.stringify(_turnArtifactEntriesFromScene(" + json.dumps(restored_scene) + ")));"
    )
    # Historical proof is retained but must not reopen a same-named file in
    # the current workspace. The current-root positive remains actionable.
    assert len(entries) == (1 if evidence == "current-root" else 0)
    if entries:
        assert entries[0]["path"] == " exact report.md "
        assert entries[0]["owner"] == {"session_id": sid, "workspace_root": str(current_root)}
    assert Session.load(sid).messages == messages
