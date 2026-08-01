import inspect
import json
import sys
from contextlib import nullcontext
from types import ModuleType

import api.commands as commands
import api.models as models
import api.routes as routes
from api.models import Session
from api.session_skill_usage import (
    MAX_SKILL_IDENTIFIERS,
    compact_skill_provenance,
    increment_skill_provenance,
    normalize_skill_provenance,
    successful_skill_names,
)


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
    assert successful_skill_names('{"success": true, "name": "review", "args": {"secret": "x"}}') == ("review",)


def test_use_bundle_and_skill_view_route_through_owner(monkeypatch):
    agent_pkg = ModuleType("agent")
    skill_bundles = ModuleType("agent.skill_bundles")
    skill_bundles.resolve_bundle_command_key = lambda name: "/" + name
    skill_bundles.build_bundle_invocation_message = lambda key, args: (
        "resolved", ["canonical-skill", "../raw", {"content": "secret"}], []
    )
    monkeypatch.setitem(sys.modules, "agent", agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.skill_bundles", skill_bundles)
    monkeypatch.setattr(commands, "_bundle_profile_context", lambda purpose: nullcontext())

    result = commands.resolve_bundle_command("/bundle request")

    assert result["loaded_skills"] == ["canonical-skill"]
    assert "content" not in json.dumps(result)
    assert "normalize_skill_provenance" in inspect.getsource(commands.resolve_bundle_command)
    assert "record_server_skill_names" in inspect.getsource(routes)


def test_session_lineage_variants_preserve_or_reject_provenance():
    parent = Session(
        session_id="issue6593-lineage",
        profile="default",
        skill_provenance={"alpha": 2, "beta": 1},
    )
    restored = Session(**parent.compact())
    duplicate = Session(skill_provenance=compact_skill_provenance(parent.skill_provenance))
    compressed = Session(skill_provenance=compact_skill_provenance(parent.skill_provenance))
    cron = Session(source_tag="cron", skill_provenance=parent.skill_provenance)

    assert restored.skill_provenance == parent.skill_provenance
    assert duplicate.skill_provenance == parent.skill_provenance
    assert compressed.skill_provenance == parent.skill_provenance
    assert cron.record_server_skill_names(["gamma"]) is False
    assert cron.skill_provenance == parent.skill_provenance
    parent.clear_server_skill_provenance()
    assert parent.skill_provenance == {}


def test_foreign_profile_unknown_and_read_only_writes_are_rejected():
    read_only = Session(read_only=True)
    cron = Session(source_tag="cron")
    assert read_only.record_server_skill_result('{"success": true, "name": "client"}') is False
    assert cron.record_server_skill_result('{"success": true, "name": "client"}') is False
    assert successful_skill_names('{"success": true, "name": "unknown", "error": "not found"}') == ()
    assert successful_skill_names('{"success": false, "name": "unknown"}') == ()
    assert increment_skill_provenance({}, {"client": 1}) == {}


def test_aggregate_skill_usage_contract_is_unchanged():
    source = inspect.getsource(routes)
    start = source.index('if parsed.path == "/api/skills/usage":')
    end = source.index('if parsed.path == "/api/skills/content":', start)
    block = source[start:end]
    assert "read_skill_usage" in block
    assert "skill_provenance" not in block
    assert "session_id" not in block


def test_session_skill_service_normalizes_and_projects_without_io():
    source = inspect.getsource(sys.modules["api.session_skill_usage"])
    assert "open(" not in source
    assert "requests" not in source
    assert normalize_skill_provenance({"alpha": 2, "bad name": 3}) == {"alpha": 2}
    assert compact_skill_provenance({"alpha": 2, "bad name": 3}) == {"alpha": 2}


def test_producers_use_session_skill_service():
    assert "record_server_skill_result(function_result)" in inspect.getsource(sys.modules["api.streaming"])
    assert "record_server_skill_names(result.get(\"loaded_skills\"))" in inspect.getsource(routes)
    assert "self.skill_provenance[" not in inspect.getsource(sys.modules["api.streaming"])
    assert "self.skill_provenance[" not in inspect.getsource(commands)


def test_identifier_cap_is_enforced():
    names = [f"skill-{index}" for index in range(MAX_SKILL_IDENTIFIERS + 10)]
    result = increment_skill_provenance({}, names)
    assert len(result) == MAX_SKILL_IDENTIFIERS
