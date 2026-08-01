import io
import json
import shutil
import subprocess
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import urlencode

import pytest
import api.commands as commands
import api.routes as routes
from api.models import Session
from api.session_skill_usage import (
    MAX_SKILL_IDENTIFIERS,
    compact_skill_provenance,
    increment_skill_provenance,
    normalize_skill_provenance,
    successful_skill_names,
)


class _RouteHandler:
    def __init__(self, method):
        self.command = method
        self.headers = {}
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def test_successful_server_load_persists_only_canonical_skill():
    session = Session(session_id="issue6593", profile="default")

    assert session.record_server_skill_result(
        '{"success": true, "name": "review"}'
    ) is True
    assert session.compact()["skill_provenance"] == {"review": 1}

    session.save(touch_updated_at=False)
    try:
        restored = Session.load("issue6593")
        metadata = Session.load_metadata_only("issue6593")
        assert restored.skill_provenance == {"review": 1}
        assert metadata.skill_provenance == {"review": 1}
        assert restored.compact()["skill_provenance"] == {"review": 1}
        assert "content" not in restored.compact()["skill_provenance"]
    finally:
        session.path.unlink(missing_ok=True)


def test_malformed_oversized_and_raw_values_are_dropped():
    raw = {
        "review": 2,
        "args": {"path": "C:/private"},
        "result body": 8,
        "../escape": 4,
    }
    normalized = normalize_skill_provenance(raw)
    assert normalized == {"review": 2}
    assert successful_skill_names('{"success": false, "name": "failed"}') == ()
    assert successful_skill_names('{"success": true, "name": "../raw"}') == ()
    assert successful_skill_names(
        '{"success": true, "name": "review", "args": {"secret": "x"}}'
    ) == ("review",)


def test_path_shaped_skill_identifiers_match_agent_lookup_guards():
    for value in ("C:/private", r"C:\private", "foo/../bar", "../outside", "./foo", "foo/./bar"):
        assert normalize_skill_provenance({value: 1}) == {}


def test_opaque_skill_identifiers_preserve_agent_names_and_reject_qualified_paths():
    valid = {
        "中文 技能": 1,
        "category/analysis skill": 2,
        "plugin:分析助手": 1,
        "p:one-character-namespace": 1,
    }
    assert normalize_skill_provenance(valid) == valid

    rejected = {
        "plugin:C:/private": 1,
        "plugin:/absolute": 1,
        "plugin:../outside": 1,
        "tool_calls": 1,
        "function_result": 1,
    }
    assert normalize_skill_provenance(rejected) == {}


def test_linked_file_fetch_survives_skill_metadata_failure(monkeypatch, tmp_path):
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    linked_file = skill_dir / "references" / "api.md"
    linked_file.parent.mkdir()
    linked_file.write_text("linked file", encoding="utf-8")
    response = {}

    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda _directory: [tmp_path])
    monkeypatch.setattr(routes, "_find_skill_in_dirs", lambda *_args: (skill_dir, skill_dir / "SKILL.md"))
    monkeypatch.setattr(routes, "_skill_view_from_file", lambda *_args: (_ for _ in ()).throw(UnicodeError("bad metadata")))
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: response.update(payload) or True)

    assert routes.handle_get(
        _RouteHandler("GET"),
        SimpleNamespace(
            path="/api/skills/content",
            query=urlencode({"name": "skill", "file": "references/api.md"}),
        ),
    ) is True
    assert response == {"content": "linked file", "path": "references/api.md"}


def test_bundle_resolver_preserves_its_existing_loaded_skills_response(monkeypatch):
    agent_pkg = ModuleType("agent")
    skill_bundles = ModuleType("agent.skill_bundles")
    skill_bundles.resolve_bundle_command_key = lambda name: "/" + name
    loaded_skills = ["重复", "重复", {"content": "secret"}]
    skill_bundles.build_bundle_invocation_message = lambda key, args: (
        "resolved",
        loaded_skills,
        [],
    )
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_bundles", skill_bundles)
    monkeypatch.setattr(commands, "_bundle_profile_context", lambda purpose: nullcontext())

    result = commands.resolve_bundle_command("/bundle request")

    assert result["loaded_skills"] == loaded_skills


def test_skill_content_route_records_server_resolved_name(monkeypatch):
    session = Session(session_id="issue6593-content", profile="default")
    saved = []
    session.save = lambda **kwargs: saved.append(kwargs)
    response = {}
    handler = _RouteHandler("GET")

    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        routes,
        "_skill_view_from_active_dir",
        lambda client_name: {
            "success": True,
            "name": "server-resolved",
            "content": "private skill instructions",
        },
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: response.update(payload) or True)

    assert routes.handle_get(
        handler,
        SimpleNamespace(
            path="/api/skills/content",
            query=urlencode({"name": "client-authored", "session_id": session.session_id}),
        ),
    ) is True

    assert session.skill_provenance == {"server-resolved": 1}
    assert "client-authored" not in session.skill_provenance
    assert saved == [{"touch_updated_at": False, "skip_index": True}]
    assert response["name"] == "server-resolved"


def test_provenance_save_failure_does_not_break_content_or_bundle_resolution(monkeypatch):
    session = Session(session_id="issue6593-save-failure", profile="default")
    session.save = lambda **kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "_skill_view_from_active_dir", lambda name: {
        "success": True, "name": "resolved", "content": "body", "linked_files": {},
    })
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: payload)
    content = routes.handle_get(
        _RouteHandler("GET"),
        SimpleNamespace(path="/api/skills/content", query=urlencode({"name": "typed", "session_id": session.session_id})),
    )
    assert content["name"] == "resolved"

    body = {"command": "/bundle request", "session_id": session.session_id}
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(commands, "resolve_bundle_command", lambda command: {
        "name": "bundle", "source": "bundle", "message": "resolved",
        "loaded_skills": ["bundle-skill"], "missing_skills": [],
    })
    bundle = routes.handle_post(
        _RouteHandler("POST"),
        SimpleNamespace(path="/api/commands/bundles/resolve", query=""),
    )
    assert bundle["message"] == "resolved"
    assert bundle["loaded_skills"] == ["bundle-skill"]


def test_linked_file_route_survives_provenance_save_failure(monkeypatch, tmp_path):
    pytest.importorskip(
        "tools.skills_tool",
        reason="hermes-agent tools package is unavailable in this environment",
    )
    session = Session(session_id="issue6593-linked-save-failure", profile="default")
    session.save = lambda **kwargs: (_ for _ in ()).throw(OSError("disk unavailable"))
    skill_dir = tmp_path / "review"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Review\n", encoding="utf-8")
    (skill_dir / "references.md").write_text("linked body", encoding="utf-8")
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(routes, "_active_skill_search_dirs", lambda skills_dir: [skills_dir])
    monkeypatch.setattr(routes, "_skill_view_from_file", lambda *_args: {
        "success": True, "name": "review", "content": "# Review", "linked_files": {},
    })
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: payload)
    response = routes.handle_get(
        _RouteHandler("GET"),
        SimpleNamespace(
            path="/api/skills/content",
            query=urlencode({"name": "review", "file": "references.md", "session_id": session.session_id}),
        ),
    )
    assert response == {"content": "linked body", "path": "references.md"}


def test_bundle_route_records_session_and_separates_session_errors(monkeypatch):
    handler = _RouteHandler("POST")
    parsed = SimpleNamespace(path="/api/commands/bundles/resolve", query="")
    body = {
        "command": "/bundle request",
        "session_id": "issue6593-bundle",
    }
    response = {}
    session = Session(session_id=body["session_id"], profile="default")
    saved = []
    session.save = lambda **kwargs: saved.append(kwargs)

    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        commands,
        "resolve_bundle_command",
        lambda command: {
            "name": "bundle",
            "source": "bundle",
            "message": "resolved",
            "loaded_skills": ["bundle-skill", "bundle-skill", "重复"],
            "missing_skills": [],
        },
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: response.update(payload) or True)

    assert routes.handle_post(handler, parsed) is True
    assert session.skill_provenance == {"bundle-skill": 2, "重复": 1}
    assert saved == [{"touch_updated_at": False, "skip_index": True}]
    assert response["loaded_skills"] == ["bundle-skill", "bundle-skill", "重复"]

    monkeypatch.setattr(routes, "get_session", lambda sid: (_ for _ in ()).throw(KeyError(sid)))
    errors = {}
    monkeypatch.setattr(
        routes,
        "bad",
        lambda _handler, message, status=400: errors.update(message=message, status=status) or True,
    )
    assert routes.handle_post(handler, parsed) is True
    assert errors == {}


def test_provenance_writer_saves_only_after_locked_session_recheck(monkeypatch):
    session = Session(session_id="issue6593-lock-recheck", profile="default")
    saved = []
    session.save = lambda **kwargs: saved.append(kwargs)
    monkeypatch.setattr(routes, "get_session", lambda _sid: session)

    assert routes._record_session_skill_provenance(
        session.session_id, None, "review"
    ) is True
    assert saved == [{"touch_updated_at": False, "skip_index": True}]


def test_provenance_writer_returns_404_state_after_locked_recheck(monkeypatch):
    monkeypatch.setattr(routes, "get_session", lambda _sid: (_ for _ in ()).throw(KeyError("deleted")))

    assert routes._record_session_skill_provenance(
        "issue6593-deleted-race", None, "review"
    ) is None


def test_session_lineage_variants_preserve_or_reject_provenance():
    parent = Session(
        session_id="issue6593-lineage",
        profile="default",
        skill_provenance={"alpha": 2, "beta": 1},
    )
    restored = Session(**parent.compact())
    duplicate = Session(skill_provenance=compact_skill_provenance(parent.skill_provenance))
    compressed = Session(skill_provenance=compact_skill_provenance(parent.skill_provenance))

    assert restored.skill_provenance == parent.skill_provenance
    assert duplicate.skill_provenance == parent.skill_provenance
    assert compressed.skill_provenance == parent.skill_provenance
    parent.clear_server_skill_provenance()
    assert parent.skill_provenance == {}


def test_cron_webhook_and_read_only_variants_reject_writes():
    ordinary = Session(session_id="cron_issue6593")
    assert ordinary.record_server_skill_names(["client"]) is False

    variants = [
        {"source_tag": "cron"},
        {"raw_source": "cron"},
        {"session_source": "cron"},
        {"source_tag": "background"},
        {"raw_source": "background"},
        {"session_source": "background"},
        {"source_tag": "webhook"},
        {"raw_source": "webhook"},
        {"session_source": "webhook"},
        {"source_tag": "subagent"},
        {"raw_source": "subagent"},
        {"session_source": "subagent"},
        {"read_only": True},
    ]
    for index, kwargs in enumerate(variants):
        kwargs = dict(kwargs)
        session_id = kwargs.pop("session_id", f"issue6593-source-{index}")
        session = Session(session_id=session_id, **kwargs)
        assert session.record_server_skill_names(["client"]) is False
        assert session.skill_provenance == {}


def test_foreign_profile_unknown_and_failed_writes_are_rejected(monkeypatch):
    session = Session(session_id="issue6593-foreign", profile="foreign")
    monkeypatch.setattr(routes, "get_session", lambda sid: session)
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *args, **kwargs: False)

    assert routes._record_session_skill_provenance("issue6593-foreign", object(), "client") is False
    assert session.skill_provenance == {}
    assert successful_skill_names('{"success": true, "name": "unknown", "error": "not found"}') == ()
    assert successful_skill_names('{"success": false, "name": "unknown"}') == ()
    assert increment_skill_provenance({}, {"client": 1}) == {}


def test_aggregate_skill_usage_contract_is_unchanged(monkeypatch):
    response = {}
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *args, **kwargs: True)
    monkeypatch.setattr(routes, "_active_skills_dir", lambda: Path("."))
    monkeypatch.setattr(
        routes,
        "_skills_list_from_dir",
        lambda skills_dir: {"skills": [{"name": "review"}]},
    )
    monkeypatch.setattr(
        "api.skill_usage.read_skill_usage",
        lambda skills_dir: {"review": {"use_count": 1, "view_count": 2, "patch_count": 3}},
    )
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **kwargs: response.update(payload) or True)

    assert routes.handle_get(
        _RouteHandler("GET"),
        SimpleNamespace(path="/api/skills/usage", query=""),
    ) is True

    assert response["usage"]["review"]["use_count"] == 1
    assert response["total_invocations"] == 6
    assert "skill_provenance" not in response


def test_session_skill_service_normalizes_and_projects_without_io():
    expected = {"alpha": 2, "valid name": 3}
    assert normalize_skill_provenance(expected) == expected
    assert compact_skill_provenance(expected) == expected


def test_identifier_cap_is_enforced():
    names = [f"skill-{index}" for index in range(MAX_SKILL_IDENTIFIERS + 10)]
    result = increment_skill_provenance({}, names)
    assert len(result) == MAX_SKILL_IDENTIFIERS


def test_bundle_client_sends_current_session_id():
    node = shutil.which("node")
    if node is None:
        return
    source = Path("static/commands.js").read_text(encoding="utf-8")
    start = source.index("async function resolveBundleCommand(")
    brace = source.index("{", start)
    depth = 0
    end = None
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    assert end is not None
    function = source[start:end]
    script = "\n".join(
        [
            "const calls=[];",
            "const S={session:{session_id:'current-session'}};",
            "const api=async (url, options) => { calls.push({url, body:JSON.parse(options.body)}); return {}; };",
            function,
            "resolveBundleCommand('/bundle request').then(() => console.log(JSON.stringify(calls[0].body)));",
        ]
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
        script_path = handle.name
    try:
        completed = subprocess.run([node, script_path], check=True, capture_output=True, text=True)
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert json.loads(completed.stdout) == {
        "command": "/bundle request",
        "session_id": "current-session",
    }
