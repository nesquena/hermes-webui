"""Behavioral coverage for per-session skill provenance."""

import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

import api.models as models
import api.routes as routes
import api.streaming as streaming
from api.models import Session


class _Handler:
    def __init__(self, method="GET"):
        self.command = method
        self.headers = {}
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()

    def _safe_webui_print(self, *_args, **_kwargs):
        return None

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        return None


@pytest.fixture
def isolated_sessions(tmp_path, monkeypatch):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", session_dir / "_index.json")
    monkeypatch.setattr(routes, "SESSION_DIR", session_dir)
    models.SESSIONS.clear()
    return session_dir


def _route_get(monkeypatch, name, session_id=None):
    payload = {}
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "j", lambda _handler, body, **_kwargs: payload.update(body) or True)
    query = {"name": name}
    if session_id:
        query["session_id"] = session_id
    assert routes.handle_get(
        _Handler(),
        SimpleNamespace(path="/api/skills/content", query=urlencode(query)),
    ) is True
    return payload


def _call_writer(session_id, handler, names):
    writer = getattr(routes, "_record_session_skill_usage")
    return writer(session_id, handler, names)


def test_session_skill_usage_persists_canonical_names_without_invented_caps(isolated_sessions):
    session = Session(
        session_id="issue6593-persist",
        skill_provenance={"exact name": 2, "ignored": 0, "boolean": True},
    )
    assert callable(getattr(session, "record_skill_usage"))
    assert session.record_skill_usage(["exact name", "new name"]) is True
    many = [f"skill-{index}" for index in range(80)]
    assert session.record_skill_usage(many) is True
    session.save(touch_updated_at=False, skip_index=True)

    restored = Session.load(session.session_id)
    assert restored is not None
    assert restored.skill_provenance["exact name"] == 3
    assert restored.skill_provenance["new name"] == 1
    assert all(restored.skill_provenance[name] == 1 for name in many)
    assert restored.compact()["skill_provenance"] == restored.skill_provenance
    assert "skill_provenance" not in restored.compact(sidebar_metadata_only=True)


def test_skill_content_records_resolved_name_for_authorized_owner(monkeypatch):
    session = Session(session_id="issue6593-content", profile="default")
    saved = []
    session.save = lambda **kwargs: saved.append(kwargs)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(
        routes,
        "_skill_view_from_active_dir",
        lambda _name: {"success": True, "name": "server-resolved", "content": "body"},
    )

    response = _route_get(monkeypatch, "client-authored", session.session_id)

    assert session.skill_provenance == {"server-resolved": 1}
    assert "client-authored" not in session.skill_provenance
    assert saved == [{"touch_updated_at": False, "skip_index": True}]
    assert response["name"] == "server-resolved"


def test_skill_content_without_owner_and_linked_file_do_not_count(monkeypatch, tmp_path):
    session = Session(session_id="issue6593-browse", profile="default")
    monkeypatch.setattr(routes, "_skill_view_from_active_dir", lambda _name: {
        "success": True,
        "name": "browse-only",
        "content": "body",
        "linked_files": {},
    })
    monkeypatch.setattr(routes, "_record_session_skill_usage", lambda *_args: pytest.fail("browse was recorded"), raising=False)
    _route_get(monkeypatch, "browse-only")
    assert session.skill_provenance == {}

    skill_dir = tmp_path / "review"
    skill_dir.mkdir()
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text("# Review\n", encoding="utf-8")
    linked = skill_dir / "reference.md"
    linked.write_text("linked body", encoding="utf-8")
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda _directory: [tmp_path])
    monkeypatch.setattr(routes, "_find_skill_in_dirs", lambda *_args: (skill_dir, skill_md))
    payload = {}
    monkeypatch.setattr(routes, "j", lambda _handler, body, **_kwargs: payload.update(body) or True)
    assert routes.handle_get(
        _Handler(),
        SimpleNamespace(
            path="/api/skills/content",
            query=urlencode({"name": "review", "file": "reference.md", "session_id": session.session_id}),
        ),
    ) is True
    assert payload == {"content": "linked body", "path": "reference.md"}
    assert session.skill_provenance == {}


def test_bundle_records_loaded_skills_and_preserves_response(monkeypatch):
    session = Session(session_id="issue6593-bundle", profile="default")
    saved = []
    session.save = lambda **kwargs: saved.append(kwargs)
    body = {
        "command": "/bundle request",
        "session_id": session.session_id,
    }
    result = {
        "name": "bundle",
        "source": "bundle",
        "message": "resolved",
        "loaded_skills": ["one", "one", "two"],
        "missing_skills": ["missing"],
    }
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr("api.commands.resolve_bundle_command", lambda _command: result)

    response = routes.handle_post(
        _Handler("POST"),
        SimpleNamespace(path="/api/commands/bundles/resolve", query=""),
    )

    assert response is result
    assert session.skill_provenance == {"one": 2, "two": 1}
    assert saved == [{"touch_updated_at": False, "skip_index": True}]
    assert response["missing_skills"] == ["missing"]


def test_provenance_writer_enforces_profile_read_only_and_subagent_owners(monkeypatch):
    handler = _Handler()
    sessions = {
        "foreign": Session(session_id="foreign", profile="other"),
        "read-only": Session(session_id="read-only", profile="default", read_only=True),
        "subagent": Session(session_id="subagent", profile="default"),
        "background": Session(session_id="background", profile="default", source_tag="background"),
    }
    monkeypatch.setattr(routes, "get_session", lambda sid: sessions.get(sid))
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda _profile, _handler: _profile != "other")
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda sid: sid == "subagent")
    for sid in ("foreign", "read-only", "subagent"):
        assert _call_writer(sid, handler, "review") is False
        assert sessions[sid].skill_provenance == {}
    assert _call_writer("background", handler, "review") is True
    assert sessions["background"].skill_provenance == {"review": 1}


def test_duplicate_and_branch_copy_independent_skill_usage(isolated_sessions, monkeypatch):
    source = Session(
        session_id="issue6593-lineage",
        messages=[{"role": "user", "content": "hello"}],
        skill_provenance={"alpha": 2, "beta": 1},
    )
    source.save(touch_updated_at=False, skip_index=True)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_evict_sessions_over_cap", lambda: None)
    body = {"session_id": source.session_id}
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    duplicate_response = routes.handle_post(
        _Handler("POST"),
        SimpleNamespace(path="/api/session/duplicate", query=""),
    )
    duplicate = Session.load(duplicate_response["session"]["session_id"])
    assert duplicate.skill_provenance == source.skill_provenance
    duplicate.record_skill_usage("alpha")
    assert source.skill_provenance == {"alpha": 2, "beta": 1}

    monkeypatch.setattr(routes, "_load_branch_source_or_refuse", lambda _handler, _sid: source)
    monkeypatch.setattr(routes, "_session_requires_cli_metadata_lookup", lambda _session: False)
    monkeypatch.setattr(routes, "_is_messaging_session_record", lambda _session: False)
    monkeypatch.setattr(routes, "_webui_sidecar_lineage_messages_for_display", lambda _session: list(source.messages))
    monkeypatch.setattr(routes, "_merged_webui_lineage_messages_for_display", lambda _session, messages: messages)
    monkeypatch.setattr(routes, "get_state_db_session_messages", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(routes, "_state_db_backstop_limit_for_display", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "_reconcile_api_content_sidecars", lambda *_args, **_kwargs: None)
    body = {"session_id": source.session_id}
    branch_response = routes.handle_post(
        _Handler("POST"),
        SimpleNamespace(path="/api/session/branch", query=""),
    )
    branch = Session.load(branch_response["session_id"])
    assert branch.skill_provenance == {"alpha": 2, "beta": 1}
    branch.record_skill_usage("gamma")
    assert source.skill_provenance == {"alpha": 2, "beta": 1}


def test_compressed_and_compressed_fork_continuations_keep_skill_usage(isolated_sessions, monkeypatch):
    source = Session(
        session_id="issue6593-compress",
        skill_provenance={"review": 2},
    )
    monkeypatch.setattr(routes, "get_session", lambda _sid: source)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "find_compression_recovery_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        routes,
        "compression_recovery_payload_for_session",
        lambda _session: {"recommended_action": routes.COMPRESSION_RECOVERY_ACTION_START_FOCUSED},
    )
    monkeypatch.setattr(routes, "publish_session_list_changed", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    recovery = routes._handle_session_compression_recovery_start(
        _Handler("POST"), {"session_id": source.session_id}
    )
    focused = recovery["session"]
    assert focused["skill_provenance"] == {"review": 2}

    continuation = Session(
        session_id="issue6593-compress-child",
        parent_session_id=source.session_id,
        skill_provenance=copy.deepcopy(source.skill_provenance),
    )
    fork = Session(
        session_id="issue6593-compress-fork",
        parent_session_id=source.session_id,
        session_source="fork",
        skill_provenance=copy.deepcopy(source.skill_provenance),
    )
    fork_continuation = Session(
        session_id="issue6593-compress-fork-child",
        parent_session_id=fork.session_id,
        skill_provenance=copy.deepcopy(fork.skill_provenance),
    )
    assert continuation.skill_provenance == {"review": 2}
    assert fork_continuation.skill_provenance == {"review": 2}
    continuation.session_id = "issue6593-compress-rotated"
    assert continuation.skill_provenance == {"review": 2}


@pytest.mark.parametrize("is_fork", [False, True])
def test_automatic_compression_rotation_preserves_skill_usage_and_lineage(tmp_path, monkeypatch, is_fork):
    import api.profiles as profiles
    from tests.test_compression_snapshot_revision import _install_streaming_session

    old_sid = f"issue6593-auto-{'fork' if is_fork else 'direct'}"
    new_sid = f"{old_sid}-continued"
    stream_id = f"stream-{old_sid}"
    session, _events = _install_streaming_session(
        monkeypatch,
        tmp_path,
        sid=old_sid,
        stream_id=stream_id,
        messages=[{"role": "user", "content": "before compression", "timestamp": 1.0}],
        context_messages=[],
    )
    session.profile = "test-profile"
    session.session_source = "fork" if is_fork else "webui"
    session.parent_session_id = "fork-parent" if is_fork else None
    session.skill_provenance = {"review": 2}
    session.save(touch_updated_at=False)
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", lambda _profile: tmp_path)
    monkeypatch.setattr(profiles, "get_profile_runtime_env", lambda _home: {})

    class CompressingAgent:
        def __init__(self, session_id=None, **_kwargs):
            self.session_id = session_id
            self.context_compressor = None
            self.ephemeral_system_prompt = None
            self._last_error = None

        def run_conversation(self, **kwargs):
            self.session_id = new_sid
            return {
                "completed": True,
                "final_response": "continued",
                "messages": [
                    {"role": "user", "content": kwargs["persist_user_message"]},
                    {"role": "assistant", "content": "continued"},
                ],
            }

    monkeypatch.setattr(streaming, "_get_ai_agent", lambda: CompressingAgent)
    streaming._run_agent_streaming(
        session_id=old_sid,
        msg_text="continue after compression",
        model="test-model",
        workspace=str(tmp_path),
        stream_id=stream_id,
        attachments=[],
    )

    snapshot = Session.load(old_sid)
    continuation = Session.load(new_sid)
    assert snapshot is not None and snapshot.pre_compression_snapshot is True
    assert snapshot.skill_provenance == {"review": 2}
    assert continuation is not None and continuation.pre_compression_snapshot is False
    assert continuation.parent_session_id == old_sid
    assert continuation.skill_provenance == {"review": 2}
    assert streaming.SESSIONS.get(new_sid) is session
    assert streaming.SESSIONS.get(old_sid) is not session


def test_clear_resets_skill_usage_without_recovery_resurrection(isolated_sessions, monkeypatch):
    session = Session(
        session_id="issue6593-clear",
        messages=[{"role": "user", "content": "hello"}],
        skill_provenance={"review": 3},
    )
    session.save(touch_updated_at=False, skip_index=True)
    body = {"session_id": session.session_id}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(routes, "_session_is_subagent_view_only", lambda _sid: False)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    response = routes.handle_post(
        _Handler("POST"), SimpleNamespace(path="/api/session/clear", query="")
    )
    assert response["session"]["skill_provenance"] == {}
    assert Session.load(session.session_id).skill_provenance == {}


def test_skill_usage_projection_boundaries(monkeypatch):
    from api.shares import build_share_snapshot

    session = Session(
        session_id="issue6593-projection",
        profile="default",
        messages=[{"role": "user", "content": "hello"}],
        skill_provenance={"review": 2},
    )
    assert "skill_provenance" in session.compact()
    assert "skill_provenance" not in session.compact(sidebar_metadata_only=True)
    assert "skill_provenance" not in build_share_snapshot(session)

    payload = {}
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: Path("."))
    monkeypatch.setattr(routes, "_skills_list_from_dir", lambda _directory: {"skills": [{"name": "review"}]})
    monkeypatch.setattr(
        "api.skill_usage.read_skill_usage",
        lambda _directory: {"review": {"use_count": 1, "view_count": 0, "patch_count": 0}},
    )
    monkeypatch.setattr(routes, "j", lambda _handler, body, **_kwargs: payload.update(body) or True)
    assert routes.handle_get(_Handler(), SimpleNamespace(path="/api/skills/usage", query="")) is True
    assert payload["usage"]["review"]["use_count"] == 1
    assert "skill_provenance" not in payload
