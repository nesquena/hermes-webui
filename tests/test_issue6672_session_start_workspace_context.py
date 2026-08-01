"""Regression coverage for the session-start workspace prompt authority."""

import json
from pathlib import Path


def _session_start_workspace(session):
    return getattr(session, "session_start_workspace", session.workspace)


def test_reported_mid_session_switch_keeps_system_prefix(tmp_path):
    from api import streaming
    from api.models import Session

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(session_id="issue6672prompt", workspace=initial)

    first = streaming._webui_ephemeral_system_prompt(
        None,
        surface_context={"source": "webui", "workspace": _session_start_workspace(session)},
    )
    session.workspace = str(changed.resolve())
    second = streaming._webui_ephemeral_system_prompt(
        None,
        surface_context={"source": "webui", "workspace": _session_start_workspace(session)},
    )

    assert f"Workspace: {initial.resolve()}" in first
    assert first == second
    assert f"Workspace: {changed.resolve()}" not in second


def test_session_start_workspace_round_trips_and_legacy_sidecar_freezes_once(tmp_path, monkeypatch):
    from api import models
    from api.models import Session

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(
        session_id="issue6672persist",
        workspace=initial,
        messages=[{"role": "user", "content": "hello"}],
    )
    session.save(skip_index=True)
    session.workspace = str(changed.resolve())
    session.save(skip_index=True)

    payload = json.loads(session.path.read_text(encoding="utf-8"))
    assert payload["session_start_workspace"] == str(initial.resolve())
    assert Session.load(session.session_id).session_start_workspace == str(initial.resolve())
    assert Session.load_metadata_only(session.session_id).session_start_workspace == str(initial.resolve())

    legacy_id = "issue6672legacy"
    legacy_payload = dict(payload)
    legacy_payload["session_id"] = legacy_id
    legacy_payload["workspace"] = str(initial.resolve())
    legacy_payload.pop("session_start_workspace")
    legacy_path = session_dir / f"{legacy_id}.json"
    legacy_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    legacy = Session.load(legacy_id)
    assert legacy.session_start_workspace == str(initial.resolve())
    assert json.loads(legacy_path.read_text(encoding="utf-8"))["session_start_workspace"] == str(initial.resolve())
    legacy_meta = Session.load_metadata_only(legacy_id)
    assert legacy_meta.session_start_workspace == str(initial.resolve())
    legacy.workspace = str(changed.resolve())
    legacy.save(skip_index=True)
    assert Session.load(legacy_id).session_start_workspace == str(initial.resolve())


def test_current_turn_tag_and_workspace_io_remain_dynamic(tmp_path):
    from api import streaming
    from api.models import Session

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    changed.mkdir()
    session = Session(session_id="issue6672dynamic", workspace=initial)
    session.workspace = str(changed.resolve())

    turn_tag = streaming._workspace_context_prefix(session.workspace)
    assert str(changed.resolve()).replace("\\", "\\\\") in turn_tag
    assert str(initial.resolve()).replace("\\", "\\\\") not in turn_tag

    marker = Path(session.workspace) / "authorized-write.txt"
    marker.write_text("changed workspace", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "changed workspace"


def test_stream_and_sync_context_use_one_session_authority(tmp_path):
    from api import routes, streaming
    from api.models import Session

    initial = tmp_path / "initial-workspace"
    changed = tmp_path / "changed-workspace"
    session = Session(session_id="issue6672consumers", workspace=initial)
    session.workspace = str(changed.resolve())

    streaming_context = streaming._webui_workspace_system_prompt(session.session_start_workspace)
    route_source = Path(routes.__file__).read_text(encoding="utf-8")
    assert f"Active workspace at session start: {initial.resolve()}" in streaming_context
    assert str(changed.resolve()) not in streaming_context
    assert "_webui_workspace_system_prompt(s.session_start_workspace)" in route_source
