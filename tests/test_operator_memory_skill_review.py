import importlib
import io
import json
import subprocess
import sys
import threading
import types
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch


REQUIRED_REVIEW_FIELDS = {
    "target",
    "proposed_change",
    "source_evidence",
    "classification",
    "stale_risk",
    "decision",
    "rollback",
    "would_execute",
}


VALID_EVIDENCE_HASH = "sha256:" + "a" * 64
VALID_PREVIOUS_HASH = "sha256:" + "b" * 64


def _patch_state_dir(monkeypatch, tmp_path):
    import api.config as config

    state_dir = tmp_path / "webui-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    return state_dir


def _write_store(path: Path, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "updated_at": 123.0, "items": items}), encoding="utf-8")


def _valid_review_item(**overrides):
    item = {
        "id": "msr_valid",
        "created_at": 100.0,
        "updated_at": 100.0,
        "profile": "default",
        "target": {
            "kind": "memory",
            "section": "memory",
            "file_path": "MEMORY.md",
            "path": "/tmp/hermes-profile/default/MEMORY.md",
        },
        "proposed_change": {
            "operation": "edit",
            "summary": "Remember a durable operator preference.",
            "diff": "--- previous\n+++ proposed\n@@\n-Old preference\n+Updated durable preference\n",
            "proposed_content": "Updated durable preference",
        },
        "previous_content": "Old preference",
        "source_evidence": [
            {
                "kind": "session_message",
                "session_id": "abc123",
                "message_index": 4,
                "content_hash": VALID_EVIDENCE_HASH,
                "quote": "Please remember this durable preference.",
            }
        ],
        "classification": {
            "durability": "durable",
            "reason": "The user stated a stable preference that should survive sessions.",
            "transient_risk": "low",
        },
        "stale_risk": {
            "state": "current",
            "expires_at": "2026-06-10T00:00:00Z",
            "reason": "Preference is stable unless the user supersedes it.",
        },
        "decision": {
            "state": "pending",
            "decided_at": None,
            "decided_by": None,
            "reason": "",
        },
        "rollback": {
            "previous_hash": VALID_PREVIOUS_HASH,
            "previous_excerpt": "Old preference",
        },
        "would_execute": False,
    }
    item.update(overrides)
    return item


def test_memory_skill_review_missing_store_returns_unknown_without_fake_items(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)

    payload = review.build_operator_memory_skill_review_payload(now=123.0)

    assert payload["version"] == 1
    assert payload["generated_at"] == 123.0
    assert payload["mode"] == "local-memory-skill-review-queue"
    assert payload["would_execute"] is False
    assert payload["status"] == "unknown"
    assert payload["items"] == []
    assert not any("sample" in json.dumps(item).lower() or "demo" in json.dumps(item).lower() for item in payload["items"])
    assert any("missing" in issue.lower() or "unavailable" in issue.lower() for issue in payload["issues"])
    assert not review.review_store_path().exists()


def test_memory_skill_review_store_path_is_local_state_not_repo_static_memory_skills_or_kanban(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)

    store = review.review_store_path()

    assert store == state_dir / "operator_memory_skill_review.json"
    assert store.parent == state_dir
    store_text = str(store)
    assert "/home/malac/hermes-webui" not in store_text
    assert "/mnt/c/Users/malac/.openclaw/workspace/main" not in store_text
    assert "/static/" not in store_text
    assert "/memory/" not in store_text
    assert "/memories/" not in store_text
    assert "/skills/" not in store_text
    assert "/kanban/" not in store_text
    assert "MEMORY.md" not in store_text
    assert "SKILL.md" not in store_text


def test_memory_skill_review_valid_items_require_all_review_fields(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    invalid_note = {"id": "msr_note", "summary": "Loose unsourced memory note"}
    _write_store(review.review_store_path(), [_valid_review_item(), invalid_note])

    payload = review.build_operator_memory_skill_review_payload(now=200.0)

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert REQUIRED_REVIEW_FIELDS.issubset(item.keys())
    assert item["would_execute"] is False
    assert item["target"]["kind"] in {"memory", "skill"}
    assert item["target"].get("file_path")
    assert item["proposed_change"]["operation"] in {"append", "edit", "delete"}
    assert item["proposed_change"].get("summary")
    assert item["proposed_change"].get("diff")
    assert item["proposed_change"].get("proposed_content")
    assert item.get("previous_content")
    assert item["source_evidence"]
    assert item["classification"].get("durability") in {"durable", "transient", "unknown"}
    assert item["classification"].get("reason")
    assert item["classification"].get("transient_risk") in {"low", "medium", "high"}
    assert item["stale_risk"].get("state") in {"current", "review_required", "expired"}
    assert item["stale_risk"].get("expires_at")
    assert item["decision"].get("state") == "pending"
    assert item["rollback"].get("previous_hash", "").startswith("sha256:")
    assert payload["notes"]
    assert payload["notes"][0].get("classification") in {"invalid", "note"}
    assert {"target", "proposed_change", "source_evidence", "classification", "stale_risk", "decision", "rollback"}.issubset(
        set(payload["notes"][0].get("missing", []))
    )
    assert any("missing" in issue.lower() for issue in payload["issues"])


def test_memory_skill_review_existing_items_reject_malformed_proof_and_placeholder_evidence(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    malformed_items = [
        _valid_review_item(id="msr_bad_evidence", source_evidence=[{}]),
        _valid_review_item(id="msr_bad_target", target={"foo": "bar"}),
        _valid_review_item(id="msr_bad_classification", classification={"durability": "unknown"}),
        _valid_review_item(id="msr_bad_stale_risk", stale_risk={"state": "current"}),
        _valid_review_item(
            id="msr_bad_proposed_change",
            proposed_change={"summary": "present but no diff/proposed content"},
        ),
        _valid_review_item(id="msr_bad_rollback", rollback={}),
    ]
    _write_store(review.review_store_path(), malformed_items)

    payload = review.build_operator_memory_skill_review_payload(now=225.0)

    assert payload["items"] == []
    assert payload["notes"]
    assert payload["status"] != "live"
    serialized_notes = json.dumps(payload["notes"], sort_keys=True).lower()
    for expected in ["source_evidence", "target", "classification", "stale_risk", "proposed_change", "rollback"]:
        assert expected in serialized_notes
    serialized_issues = json.dumps(payload["issues"], sort_keys=True).lower()
    assert "evidence" in serialized_issues or "source" in serialized_issues
    assert "target" in serialized_issues
    assert "classification" in serialized_issues
    assert "stale" in serialized_issues
    assert "proposed" in serialized_issues
    assert "rollback" in serialized_issues


def test_memory_skill_review_existing_items_reject_placeholder_sha256_hashes(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    placeholder_evidence = _valid_review_item(
        id="msr_placeholder_evidence",
        source_evidence=[
            {
                "kind": "session_message",
                "session_id": "abc123",
                "message_index": 4,
                "content_hash": "sha256:",
                "quote": "Placeholder hashes are not source proof.",
            }
        ],
    )
    placeholder_rollback = _valid_review_item(
        id="msr_placeholder_rollback",
        rollback={"previous_hash": "sha256:", "previous_excerpt": "Old preference"},
    )
    _write_store(review.review_store_path(), [placeholder_evidence, placeholder_rollback])

    payload = review.build_operator_memory_skill_review_payload(now=225.0)

    assert payload["items"] == []
    assert payload["notes"]
    assert payload["status"] != "live"
    serialized_notes = json.dumps(payload["notes"], sort_keys=True).lower()
    assert "source_evidence" in serialized_notes
    assert "rollback" in serialized_notes
    serialized_issues = json.dumps(payload["issues"], sort_keys=True).lower()
    assert "sha256" in serialized_issues or "hash" in serialized_issues


def test_memory_skill_review_edit_delete_require_previous_content_and_rollback(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    delete_change = {
        "operation": "delete",
        "summary": "Remove obsolete skill guidance.",
        "diff": "--- previous\n+++ proposed\n@@\n-Obsolete guidance\n",
    }
    malformed_items = [
        _valid_review_item(id="msr_edit_missing_previous", previous_content=None),
        _valid_review_item(id="msr_edit_missing_rollback", rollback={}),
        _valid_review_item(id="msr_delete_missing_previous", proposed_change=delete_change, previous_content=None),
        _valid_review_item(id="msr_delete_missing_rollback", proposed_change=delete_change, rollback={}),
    ]
    _write_store(review.review_store_path(), malformed_items)

    payload = review.build_operator_memory_skill_review_payload(now=250.0)

    assert payload["items"] == []
    assert payload["notes"]
    assert payload["status"] != "live"
    serialized_notes = json.dumps(payload["notes"], sort_keys=True).lower()
    assert "previous_content" in serialized_notes or "previous content" in serialized_notes
    assert "rollback" in serialized_notes
    assert "edit" in serialized_notes
    assert "delete" in serialized_notes
    assert any("previous" in issue.lower() or "rollback" in issue.lower() for issue in payload["issues"])


def test_memory_skill_review_expired_items_degrade_to_stale_with_issues(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    expired = _valid_review_item(
        stale_risk={
            "state": "current",
            "expires_at": "1970-01-01T00:00:01Z",
            "reason": "This proposal needed prompt review.",
        }
    )
    _write_store(review.review_store_path(), [expired])

    payload = review.build_operator_memory_skill_review_payload(now=123.0)

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert payload["status"] == "stale"
    assert item["stale_risk"]["state"] == "expired"
    assert item["stale_risk"].get("expires_at") == "1970-01-01T00:00:01Z"
    assert any("expired" in issue.lower() or "stale" in issue.lower() for issue in payload["issues"])


def test_memory_skill_review_payload_redacts_previous_and_proposed_sensitive_text(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    raw_previous = "old database password is hunter2-password"
    raw_proposed = "new api_token=sk-live-abcdef123456 and secret=topsecret-value"
    sensitive = _valid_review_item(
        previous_content=raw_previous,
        proposed_change={
            "operation": "edit",
            "summary": "Rotate stored credential guidance without exposing credentials.",
            "diff": f"--- previous\n+++ proposed\n@@\n-{raw_previous}\n+{raw_proposed}\n",
            "proposed_content": raw_proposed,
        },
        rollback={
            "previous_hash": VALID_PREVIOUS_HASH,
            "previous_excerpt": raw_previous,
        },
    )
    _write_store(review.review_store_path(), [sensitive])

    payload = review.build_operator_memory_skill_review_payload(now=275.0)

    assert len(payload["items"]) == 1
    item = payload["items"][0]
    serialized = json.dumps(payload, sort_keys=True)
    for raw in [raw_previous, raw_proposed, "hunter2-password", "sk-live-abcdef123456", "topsecret-value"]:
        assert raw not in serialized
    assert item.get("previous_content") != raw_previous
    assert item["proposed_change"].get("proposed_content") != raw_proposed
    assert "sk-live-abcdef123456" not in item["proposed_change"].get("diff", "")
    assert "redact" in serialized.lower()
    assert payload["would_execute"] is False


class _PostHandler:
    def __init__(self, body=None, *, client_ip="127.0.0.1", headers=None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.client_address = (client_ip, 54321)
        base_headers = {"Content-Length": str(len(raw)), "Host": "127.0.0.1:8787"}
        if headers:
            base_headers.update(headers)
        self.headers = base_headers


def _capture_json_response():
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        captured["extra_headers"] = extra_headers
        return True

    return captured, fake_j


def _valid_propose_body(**overrides):
    body = {
        "target": {"kind": "memory", "section": "memory"},
        "proposed_change": {
            "operation": "append",
            "summary": "Add a durable local memory review item.",
            "diff": "--- previous\n+++ proposed\n@@\n+Proposed durable operator memory.\n",
            "proposed_content": "Proposed durable operator memory.",
        },
        "source_evidence": [
            {
                "kind": "session_message",
                "session_id": "route-session",
                "message_index": 2,
                "content_hash": VALID_EVIDENCE_HASH,
                "quote": "Please remember this durable preference.",
            }
        ],
        "classification": {
            "durability": "durable",
            "reason": "The user explicitly asked for a stable memory update.",
            "transient_risk": "low",
        },
        "stale_risk": {
            "state": "current",
            "expires_at": "2026-06-10T00:00:00Z",
            "reason": "The proposal is current unless superseded by the user.",
        },
    }
    body.update(overrides)
    return body


def _valid_session_message_evidence(**overrides):
    evidence = {
        "kind": "session_message",
        "session_id": "route-session",
        "message_index": 2,
        "content_hash": VALID_EVIDENCE_HASH,
        "quote": "Please remember this durable preference.",
    }
    evidence.update(overrides)
    return evidence


def test_memory_skill_review_propose_rejects_malformed_session_message_evidence_without_writing(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)

    import api.models as models

    session_load_calls = []

    def forbidden_session_load(*args, **kwargs):
        session_load_calls.append((args, kwargs))
        raise AssertionError("memory/skill review proposal validation must not call Session.load")

    monkeypatch.setattr(models.Session, "load", forbidden_session_load, raising=False)

    invalid_evidence_cases = [
        ("missing_session_id", _valid_session_message_evidence(session_id="")),
        ("object_session_id", _valid_session_message_evidence(session_id={"value": "route-session"})),
        ("missing_message_index", {k: v for k, v in _valid_session_message_evidence().items() if k != "message_index"}),
        ("negative_message_index", _valid_session_message_evidence(message_index=-1)),
        ("non_integer_message_index", _valid_session_message_evidence(message_index="not-an-index")),
        ("bool_message_index", _valid_session_message_evidence(message_index=True)),
        ("missing_content_hash", _valid_session_message_evidence(content_hash="")),
        ("invalid_content_hash", _valid_session_message_evidence(content_hash="sha256:not-a-real-hash")),
        ("missing_quote", _valid_session_message_evidence(quote="")),
        ("object_quote", _valid_session_message_evidence(quote={"text": "Please remember this durable preference."})),
    ]

    for label, evidence in invalid_evidence_cases:
        result = review.propose_operator_memory_skill_review(
            _valid_propose_body(source_evidence=[evidence]),
            now=300.0,
            client_context={"profile": "default", "client_ip": "127.0.0.1"},
        )
        assert result.get("ok") is False, label
        assert result.get("would_execute") is False, label
        assert "source_evidence" in result.get("missing", []), label
        assert not review.review_store_path().exists(), label
        assert not (state_dir / "operator_memory_skill_review.json").exists(), label
        _assert_profile_files_unchanged(files)

    assert session_load_calls == []


def test_memory_skill_review_propose_rejects_raw_secret_session_message_quotes_without_writing(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)

    import api.models as models

    session_load_calls = []

    def forbidden_session_load(*args, **kwargs):
        session_load_calls.append((args, kwargs))
        raise AssertionError("memory/skill review proposal validation must not call Session.load")

    monkeypatch.setattr(models.Session, "load", forbidden_session_load, raising=False)

    raw_secret_quotes = [
        "password=hunter2.",
        "Authorization: Bearer abcdefghijklmnop1234567890.",
        "sk-" + "A" * 32 + ".",
        "xoxb-" + "B" * 20 + ".",
        "ghp_" + "C" * 36 + ".",
        "github_pat_" + "D" * 22 + "_" + "E" * 59 + ".",
    ]

    for quote in raw_secret_quotes:
        result = review.propose_operator_memory_skill_review(
            _valid_propose_body(source_evidence=[_valid_session_message_evidence(quote=quote)]),
            now=325.0,
            client_context={"profile": "default", "client_ip": "127.0.0.1"},
        )
        assert result.get("ok") is False, quote
        assert result.get("would_execute") is False, quote
        assert "source_evidence" in result.get("missing", []), quote
        assert not review.review_store_path().exists(), quote
        assert not (state_dir / "operator_memory_skill_review.json").exists(), quote
        _assert_profile_files_unchanged(files)

    assert session_load_calls == []


def test_memory_skill_review_propose_accepts_redacted_session_message_quote(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)

    redacted_quote = "User said password=[redacted], Bearer [redacted], ghp_[redacted], and github_pat_[redacted]."
    body = _valid_propose_body(source_evidence=[_valid_session_message_evidence(quote=redacted_quote)])

    result = review.propose_operator_memory_skill_review(
        body,
        now=350.0,
        client_context={"profile": "default", "client_ip": "127.0.0.1"},
    )

    assert result.get("ok") is True
    assert result.get("would_execute") is False
    stored = json.loads(review.review_store_path().read_text(encoding="utf-8"))
    assert len(stored["items"]) == 1
    assert stored["items"][0]["source_evidence"] == body["source_evidence"]
    _assert_profile_files_unchanged(files)


def test_memory_skill_review_propose_rejects_raw_secret_user_fields_without_writing(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)

    raw_field_cases = [
        (
            "proposed_change summary",
            _valid_propose_body(
                proposed_change={
                    "operation": "append",
                    "summary": "Store password=hunter2 in memory.",
                    "diff": "--- previous\n+++ proposed\n@@\n+Safe proposed text.\n",
                    "proposed_content": "Safe proposed text.",
                }
            ),
        ),
        (
            "proposed_change content",
            _valid_propose_body(
                proposed_change={
                    "operation": "append",
                    "summary": "Safe summary.",
                    "diff": "--- previous\n+++ proposed\n@@\n+Bearer abcdefghijklmnop.\n",
                    "proposed_content": "Bearer abcdefghijklmnop.",
                }
            ),
        ),
        (
            "classification reason",
            _valid_propose_body(
                classification={"durability": "durable", "reason": "Contains sk-" + "A" * 32 + ".", "transient_risk": "low"}
            ),
        ),
        (
            "stale risk reason",
            _valid_propose_body(
                stale_risk={"state": "current", "expires_at": "2026-06-10T00:00:00Z", "reason": "Contains ghp_" + "B" * 36 + "."}
            ),
        ),
        (
            "proposed_change raw key",
            _valid_propose_body(
                proposed_change={
                    "operation": "append",
                    "summary": "Safe summary.",
                    "diff": "--- previous\n+++ proposed\n@@\n+Safe proposed text.\n",
                    "proposed_content": "Safe proposed text.",
                    "password=hunter2": "present as a key",
                }
            ),
        ),
        (
            "classification raw key",
            _valid_propose_body(
                classification={"durability": "durable", "reason": "Safe reason.", "transient_risk": "low", "token=sk-secret": "present as a key"}
            ),
        ),
        (
            "stale_risk raw key",
            _valid_propose_body(
                stale_risk={"state": "current", "expires_at": "2026-06-10T00:00:00Z", "reason": "Safe reason.", "secret=topsecret": "present as a key"}
            ),
        ),
    ]

    for label, body in raw_field_cases:
        result = review.propose_operator_memory_skill_review(
            body,
            now=360.0,
            client_context={"profile": "default", "client_ip": "127.0.0.1"},
        )
        assert result.get("ok") is False, label
        assert result.get("would_execute") is False, label
        assert not review.review_store_path().exists(), label
        assert not (state_dir / "operator_memory_skill_review.json").exists(), label
        _assert_profile_files_unchanged(files)


def test_memory_skill_review_propose_redacts_existing_target_secrets_before_store(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    raw_previous = "# Memory\n\nExisting password=hunter2 and token=sk-" + "A" * 32 + ".\n"
    files["memory_file"].write_text(raw_previous, encoding="utf-8")

    result = review.propose_operator_memory_skill_review(
        _valid_propose_body(),
        now=370.0,
        client_context={"profile": "default", "client_ip": "127.0.0.1"},
    )

    assert result.get("ok") is True
    stored_text = review.review_store_path().read_text(encoding="utf-8")
    assert "hunter2" not in stored_text
    assert "sk-" + "A" * 32 not in stored_text
    assert "redact" in stored_text.lower()


def test_memory_skill_review_decision_redacts_raw_secret_reason_before_writing(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    item = _route_review_item("msr_secret_store_decision", files["memory_file"])
    _write_store(review.review_store_path(), [item])
    captured, fake_j = _capture_json_response()
    secret_reason = "denied because password=hunter2 and token=sk-secret should not persist"
    handler = _PostHandler(
        {"id": "msr_secret_store_decision", "decision": "denied", "reason": secret_reason},
        headers={"Host": "localhost:8787", "Origin": "http://localhost:8787"},
    )

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/decision"))

    assert handled is True
    assert captured["status"] == 200
    stored_text = review.review_store_path().read_text(encoding="utf-8")
    assert secret_reason not in stored_text
    assert "hunter2" not in stored_text
    assert "redact" in stored_text.lower()


def _patch_active_profile_files(monkeypatch, tmp_path):
    import api.profiles as profiles

    hermes_home = tmp_path / "hermes-home"
    memories_dir = hermes_home / "memories"
    skill_dir = hermes_home / "skills" / "review-skill"
    memories_dir.mkdir(parents=True, exist_ok=True)
    skill_dir.mkdir(parents=True, exist_ok=True)

    memory_file = memories_dir / "MEMORY.md"
    user_file = memories_dir / "USER.md"
    soul_file = hermes_home / "SOUL.md"
    skill_file = skill_dir / "SKILL.md"
    kanban_file = tmp_path / "kanban" / "hermes-operator.json"

    original_memory = "# Memory\n\nExisting durable memory.\n"
    original_user = "# User\n\nExisting user memory.\n"
    original_soul = "# Soul\n\nExisting soul memory.\n"
    original_skill = "# Review Skill\n\nExisting skill guidance.\n"
    original_kanban = '{"board":"hermes-operator","items":[]}'

    memory_file.write_text(original_memory, encoding="utf-8")
    user_file.write_text(original_user, encoding="utf-8")
    soul_file.write_text(original_soul, encoding="utf-8")
    skill_file.write_text(original_skill, encoding="utf-8")
    kanban_file.parent.mkdir(parents=True, exist_ok=True)
    kanban_file.write_text(original_kanban, encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_BASE_HOME", str(hermes_home))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: hermes_home, raising=False)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default", raising=False)

    review_module = sys.modules.get("api.operator_memory_skill_review")
    if review_module is not None:
        monkeypatch.setattr(review_module, "get_active_hermes_home", lambda: hermes_home, raising=False)
        monkeypatch.setattr(review_module, "get_active_profile_name", lambda: "default", raising=False)
        profile_module = getattr(review_module, "profiles", None)
        if profile_module is not None:
            monkeypatch.setattr(profile_module, "get_active_hermes_home", lambda: hermes_home, raising=False)
            monkeypatch.setattr(profile_module, "get_active_profile_name", lambda: "default", raising=False)

    return {
        "home": hermes_home,
        "memory_file": memory_file,
        "user_file": user_file,
        "soul_file": soul_file,
        "skill_file": skill_file,
        "kanban_file": kanban_file,
        "original_memory": original_memory,
        "original_user": original_user,
        "original_soul": original_soul,
        "original_skill": original_skill,
        "original_kanban": original_kanban,
    }


def _assert_profile_files_unchanged(files):
    assert files["memory_file"].read_text(encoding="utf-8") == files["original_memory"]
    assert files["user_file"].read_text(encoding="utf-8") == files["original_user"]
    assert files["soul_file"].read_text(encoding="utf-8") == files["original_soul"]
    assert files["skill_file"].read_text(encoding="utf-8") == files["original_skill"]
    assert files["kanban_file"].read_text(encoding="utf-8") == files["original_kanban"]


def _assert_valid_stored_propose_item(
    item,
    *,
    target_path: Path,
    previous_content: str,
    expected_target,
    expected_proposed_change,
    expected_source_evidence,
    expected_classification,
    expected_stale_risk,
):
    assert REQUIRED_REVIEW_FIELDS.issubset(item.keys())
    assert item.get("id", "").startswith("msr_")
    assert isinstance(item.get("created_at"), (int, float))
    assert item["created_at"] > 0
    assert isinstance(item.get("updated_at"), (int, float))
    assert item["updated_at"] >= item["created_at"]
    assert item.get("profile") == "default"
    assert item["would_execute"] is False
    for key, value in expected_target.items():
        assert item["target"].get(key) == value
    assert "file_path" in item["target"]
    assert item["target"]["file_path"]
    assert item["target"].get("path")
    assert Path(item["target"]["path"]).is_absolute()
    assert Path(item["target"]["path"]).resolve() == target_path.resolve()
    assert item.get("previous_content") == previous_content
    assert item["proposed_change"] == expected_proposed_change
    assert item["source_evidence"] == expected_source_evidence
    assert item["classification"] == expected_classification
    assert item["stale_risk"] == expected_stale_risk
    assert item["decision"]["state"] == "pending"
    assert item["decision"].get("decided_at") is None
    assert item["decision"].get("decided_by") is None
    assert item["decision"].get("reason") == ""
    previous_hash = item["rollback"].get("previous_hash", "")
    assert previous_hash.startswith("sha256:")
    assert len(previous_hash) == len("sha256:") + 64
    previous_excerpt = item["rollback"].get("previous_excerpt", "")
    assert previous_excerpt
    assert "Existing" in previous_excerpt


def _poison_forbidden_memory_skill_review_side_effects(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("memory/skill review routes must not shell, thread, cron, goal, mutate Kanban, or write targets directly")

    fake_kanban_bridge = types.ModuleType("api.kanban_bridge")
    for name in ("handle_kanban_post", "handle_kanban_patch", "handle_kanban_delete", "dispatch", "create_task", "claim", "complete"):
        setattr(fake_kanban_bridge, name, forbidden)
    fake_cron_pkg = types.ModuleType("cron")
    fake_cron_jobs = types.ModuleType("cron.jobs")
    for name in ("create", "update", "delete", "run", "schedule"):
        setattr(fake_cron_jobs, name, forbidden)
    setattr(fake_cron_pkg, "jobs", fake_cron_jobs)
    fake_goals = types.ModuleType("api.goals")
    for name in ("set_goal", "create_goal", "update_goal", "delete_goal", "start_goal_loop"):
        setattr(fake_goals, name, forbidden)

    monkeypatch.setitem(sys.modules, "api.kanban_bridge", fake_kanban_bridge)
    monkeypatch.setitem(sys.modules, "cron", fake_cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_cron_jobs)
    monkeypatch.setitem(sys.modules, "api.goals", fake_goals)

    import api as api_pkg
    import api.routes as routes

    monkeypatch.setattr(api_pkg, "kanban_bridge", fake_kanban_bridge, raising=False)
    monkeypatch.setattr(api_pkg, "goals", fake_goals, raising=False)
    for name in ("_handle_memory_write", "_handle_skill_save", "_handle_skill_delete", "_handle_skill_toggle"):
        monkeypatch.setattr(routes, name, forbidden, raising=False)

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(threading, "Thread", forbidden)
    monkeypatch.setattr(threading, "Timer", forbidden)
    return calls


def _route_review_item(item_id, target_path, **overrides):
    item = _valid_review_item(
        id=item_id,
        target={
            "kind": "memory",
            "section": "memory",
            "file_path": "MEMORY.md",
            "path": str(target_path),
        },
        proposed_change={
            "operation": "append",
            "summary": "Append durable memory through review only.",
            "diff": "--- previous\n+++ proposed\n@@\n+Approved text must still not be applied.\n",
            "proposed_content": "Approved text must still not be applied.",
        },
        previous_content="Existing durable memory.",
    )
    item.update(overrides)
    return item


def test_memory_skill_review_route_get_returns_json(monkeypatch):
    import api.routes as routes

    expected = {
        "version": 1,
        "mode": "local-memory-skill-review-queue",
        "status": "unknown",
        "would_execute": False,
        "items": [],
        "sources": [],
        "issues": [],
    }
    captured, fake_j = _capture_json_response()

    with patch("api.operator_memory_skill_review.build_operator_memory_skill_review_payload", return_value=expected) as build_payload, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_get(
            types.SimpleNamespace(wfile=io.BytesIO()),
            urlparse("/api/operator/memory-skill-review?session_id=abc123&ui_board=hermes-operator"),
        )

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"] == expected
    assert captured["payload"]["mode"] == "local-memory-skill-review-queue"
    assert captured["payload"]["would_execute"] is False
    build_payload.assert_called_once_with(session_id="abc123", ui_board_hint="hermes-operator")


def test_memory_skill_review_propose_route_accepts_loopback_localhost_only(monkeypatch):
    import api.routes as routes

    captured, fake_j = _capture_json_response()

    def fake_propose(body, *, now=None, client_context=None):
        captured["body"] = body
        captured["client_context"] = client_context
        return {"ok": True, "id": "msr_route", "would_execute": False}

    handler = _PostHandler(
        _valid_propose_body(),
        headers={"Host": "localhost:8787", "Origin": "http://localhost:8787", "Referer": "http://localhost:8787/operator"},
    )
    with patch("api.operator_memory_skill_review.propose_operator_memory_skill_review", side_effect=fake_propose) as propose, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))

    assert handled is True
    assert propose.call_count == 1
    assert captured["status"] == 201
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["would_execute"] is False
    assert captured["body"]["target"] == {"kind": "memory", "section": "memory"}
    assert captured["client_context"]["client_ip"] == "127.0.0.1"


def test_memory_skill_review_propose_route_rejects_public_origin_even_when_allowed(monkeypatch):
    import api.routes as routes

    captured, fake_j = _capture_json_response()

    def forbidden_propose(*args, **kwargs):
        raise AssertionError("public memory/skill review writes must not call propose")

    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://public.example")
    handler = _PostHandler(
        _valid_propose_body(),
        client_ip="203.0.113.10",
        headers={"Origin": "https://public.example", "Referer": "https://public.example/operator", "Host": "public.example"},
    )
    with patch("api.operator_memory_skill_review.propose_operator_memory_skill_review", side_effect=forbidden_propose) as propose, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))

    assert handled is True
    assert propose.call_count == 0
    assert captured["status"] == 403
    assert captured["payload"]["would_execute"] is False


def test_memory_skill_review_propose_route_rejects_public_client_even_with_loopback_headers(monkeypatch):
    import api.routes as routes

    captured, fake_j = _capture_json_response()

    def forbidden_propose(*args, **kwargs):
        raise AssertionError("public memory/skill review clients must not call propose even with loopback headers")

    handler = _PostHandler(
        _valid_propose_body(),
        client_ip="203.0.113.10",
        headers={"Host": "localhost:8787", "Origin": "http://localhost:8787", "Referer": "http://localhost:8787/operator"},
    )
    with patch("api.operator_memory_skill_review.propose_operator_memory_skill_review", side_effect=forbidden_propose) as propose, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))

    assert handled is True
    assert propose.call_count == 0
    assert captured["status"] == 403
    assert captured["payload"]["would_execute"] is False


def test_memory_skill_review_propose_route_rejects_public_host_origin_or_referer_from_loopback(monkeypatch):
    import api.routes as routes

    captured, fake_j = _capture_json_response()

    def forbidden_propose(*args, **kwargs):
        raise AssertionError("loopback memory/skill review writes with public headers must not call propose")

    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://public.example")
    public_header_cases = [
        (
            "host_userinfo_loopback_prefix",
            {"Host": "127.0.0.1:8787@public.example", "Origin": "http://127.0.0.1:8787", "Referer": "http://127.0.0.1:8787/operator"},
        ),
        (
            "origin_userinfo_loopback_prefix",
            {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787@public.example", "Referer": "http://127.0.0.1:8787/operator"},
        ),
        (
            "host",
            {"Host": "public.example", "Origin": "http://127.0.0.1:8787", "Referer": "http://127.0.0.1:8787/operator"},
        ),
        (
            "origin",
            {"Host": "127.0.0.1:8787", "Origin": "https://public.example", "Referer": "http://127.0.0.1:8787/operator"},
        ),
        (
            "referer",
            {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787", "Referer": "https://public.example/operator"},
        ),
    ]
    with patch("api.operator_memory_skill_review.propose_operator_memory_skill_review", side_effect=forbidden_propose) as propose, patch(
        "api.routes.j", side_effect=fake_j
    ):
        for label, headers in public_header_cases:
            captured.clear()
            handler = _PostHandler(_valid_propose_body(), client_ip="127.0.0.1", headers=headers)
            handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))
            assert handled is True, label
            assert captured["status"] == 403, label
            assert captured["payload"]["would_execute"] is False, label

    assert propose.call_count == 0


def test_memory_skill_review_propose_route_rejects_public_x_forwarded_for_proxy(monkeypatch):
    import api.routes as routes

    captured, fake_j = _capture_json_response()

    def forbidden_propose(*args, **kwargs):
        raise AssertionError("proxied public memory/skill review writes must not call propose")

    public_proxy_headers = [
        {"X-Forwarded-For": "203.0.113.11"},
        {"X-Real-IP": "203.0.113.12"},
        {"X-Forwarded-Host": "public.example"},
        {"X-Real-Host": "public.example"},
    ]
    with patch("api.operator_memory_skill_review.propose_operator_memory_skill_review", side_effect=forbidden_propose) as propose, patch(
        "api.routes.j", side_effect=fake_j
    ):
        for proxy_headers in public_proxy_headers:
            captured.clear()
            headers = {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"}
            headers.update(proxy_headers)
            handler = _PostHandler(_valid_propose_body(), client_ip="127.0.0.1", headers=headers)
            handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))
            assert handled is True
            assert captured["status"] == 403
            assert captured["payload"]["would_execute"] is False

    assert propose.call_count == 0


def test_memory_skill_review_propose_writes_only_review_queue_not_memory_skill_or_kanban(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    calls = _poison_forbidden_memory_skill_review_side_effects(monkeypatch)
    captured, fake_j = _capture_json_response()
    proposed_text = "Proposed durable operator memory from the route."
    body = _valid_propose_body(
        proposed_change={
            "operation": "append",
            "summary": "Append durable memory through review only.",
            "diff": f"--- previous\n+++ proposed\n@@\n+{proposed_text}\n",
            "proposed_content": proposed_text,
        }
    )
    handler = _PostHandler(
        body,
        headers={"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"},
    )

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))

    assert handled is True
    assert captured["status"] == 201
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["would_execute"] is False
    store_path = state_dir / "operator_memory_skill_review.json"
    assert review.review_store_path() == store_path
    assert store_path.exists()
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert len(stored["items"]) == 1
    item = stored["items"][0]
    _assert_valid_stored_propose_item(
        item,
        target_path=files["memory_file"],
        previous_content=files["original_memory"],
        expected_target=body["target"],
        expected_proposed_change=body["proposed_change"],
        expected_source_evidence=body["source_evidence"],
        expected_classification=body["classification"],
        expected_stale_risk=body["stale_risk"],
    )
    assert proposed_text in json.dumps(item)
    _assert_profile_files_unchanged(files)
    assert not files["memory_file"].read_text(encoding="utf-8").endswith(proposed_text)
    assert calls == []


def test_memory_skill_review_propose_valid_skill_target_writes_only_queue_not_skill_file(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    calls = _poison_forbidden_memory_skill_review_side_effects(monkeypatch)
    captured, fake_j = _capture_json_response()
    proposed_skill = "# Review Skill\n\nExisting skill guidance.\n\nProposed review-only skill guidance.\n"
    body = _valid_propose_body(
        target={"kind": "skill", "name": "review-skill"},
        proposed_change={
            "operation": "edit",
            "summary": "Revise review-skill guidance through the local queue only.",
            "diff": "--- previous\n+++ proposed\n@@\n Existing skill guidance.\n+Proposed review-only skill guidance.\n",
            "proposed_content": proposed_skill,
        },
    )
    handler = _PostHandler(
        body,
        headers={"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"},
    )

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))

    assert handled is True
    assert captured["status"] == 201
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["would_execute"] is False
    store_path = state_dir / "operator_memory_skill_review.json"
    assert review.review_store_path() == store_path
    assert store_path.exists()
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert len(stored["items"]) == 1
    item = stored["items"][0]
    _assert_valid_stored_propose_item(
        item,
        target_path=files["skill_file"],
        previous_content=files["original_skill"],
        expected_target=body["target"],
        expected_proposed_change=body["proposed_change"],
        expected_source_evidence=body["source_evidence"],
        expected_classification=body["classification"],
        expected_stale_risk=body["stale_risk"],
    )
    assert item["proposed_change"]["proposed_content"] == proposed_skill
    _assert_profile_files_unchanged(files)
    assert files["skill_file"].read_text(encoding="utf-8") == files["original_skill"]
    assert calls == []


def test_memory_skill_review_propose_rejects_unresolved_or_path_traversal_targets_without_writing(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    calls = _poison_forbidden_memory_skill_review_side_effects(monkeypatch)
    captured, fake_j = _capture_json_response()
    invalid_bodies = [
        _valid_propose_body(target={"kind": "memory", "section": "unknown"}),
        _valid_propose_body(target={"kind": "skill", "name": "../escape"}),
        _valid_propose_body(target={"kind": "skill", "category": "../../escape", "name": "review-skill"}),
        _valid_propose_body(target={"kind": "skill", "name": "missing-skill"}),
    ]

    with patch("api.routes.j", side_effect=fake_j):
        for body in invalid_bodies:
            captured.clear()
            handler = _PostHandler(body, headers={"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"})
            handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/propose"))
            assert handled is True
            assert captured["status"] == 400
            assert captured["payload"].get("ok") is False
            assert captured["payload"]["would_execute"] is False
            assert not review.review_store_path().exists()
            assert not (state_dir / "operator_memory_skill_review.json").exists()
            _assert_profile_files_unchanged(files)

    assert calls == []


def test_memory_skill_review_decision_route_rejects_public_client_or_headers_without_calling_handler(monkeypatch):
    import api.routes as routes

    captured, fake_j = _capture_json_response()

    def forbidden_decision(*args, **kwargs):
        raise AssertionError("public memory/skill review decision writes must not call decision handler")

    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://public.example")
    public_contexts = [
        (
            "client_ip",
            "203.0.113.10",
            {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787", "Referer": "http://127.0.0.1:8787/operator"},
        ),
        (
            "host",
            "127.0.0.1",
            {"Host": "public.example", "Origin": "http://127.0.0.1:8787", "Referer": "http://127.0.0.1:8787/operator"},
        ),
        (
            "origin",
            "127.0.0.1",
            {"Host": "127.0.0.1:8787", "Origin": "https://public.example", "Referer": "http://127.0.0.1:8787/operator"},
        ),
        (
            "referer",
            "127.0.0.1",
            {"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787", "Referer": "https://public.example/operator"},
        ),
    ]
    with patch("api.operator_memory_skill_review.decide_operator_memory_skill_review", side_effect=forbidden_decision) as decide, patch(
        "api.routes.j", side_effect=fake_j
    ):
        for label, client_ip, headers in public_contexts:
            captured.clear()
            handler = _PostHandler(
                {"id": "msr_public_decision", "decision": "denied", "reason": "No public writes."},
                client_ip=client_ip,
                headers=headers,
            )
            handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/decision"))
            assert handled is True, label
            assert captured["status"] == 403, label
            assert captured["payload"]["would_execute"] is False, label

    assert decide.call_count == 0


def test_memory_skill_review_decision_route_rejects_public_reverse_proxy_headers_without_calling_handler(monkeypatch):
    import api.routes as routes

    captured, fake_j = _capture_json_response()

    def forbidden_decision(*args, **kwargs):
        raise AssertionError("proxied public memory/skill review decisions must not call decision handler")

    public_proxy_header_cases = [
        ("x_forwarded_for", {"X-Forwarded-For": "203.0.113.11"}),
        ("x_real_ip", {"X-Real-IP": "203.0.113.12"}),
        ("x_forwarded_host", {"X-Forwarded-Host": "public.example"}),
        ("x_real_host", {"X-Real-Host": "public.example"}),
    ]
    with patch("api.operator_memory_skill_review.decide_operator_memory_skill_review", side_effect=forbidden_decision) as decide, patch(
        "api.routes.j", side_effect=fake_j
    ):
        for label, proxy_headers in public_proxy_header_cases:
            captured.clear()
            headers = {
                "Host": "127.0.0.1:8787",
                "Origin": "http://127.0.0.1:8787",
                "Referer": "http://127.0.0.1:8787/operator",
            }
            headers.update(proxy_headers)
            handler = _PostHandler(
                {"id": "msr_proxy_decision", "decision": "denied", "reason": "No proxied public writes."},
                client_ip="127.0.0.1",
                headers=headers,
            )
            handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/decision"))
            assert handled is True, label
            assert captured["status"] == 403, label
            assert captured["payload"]["would_execute"] is False, label

    assert decide.call_count == 0


def test_memory_skill_review_decision_updates_only_local_decision_state(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    calls = _poison_forbidden_memory_skill_review_side_effects(monkeypatch)
    item = _route_review_item("msr_decision", files["memory_file"])
    _write_store(review.review_store_path(), [item])
    before = json.loads(json.dumps(item, sort_keys=True))
    captured, fake_j = _capture_json_response()
    handler = _PostHandler(
        {"id": "msr_decision", "decision": "approved", "reason": "Looks durable and source-backed."},
        headers={"Host": "localhost:8787", "Origin": "http://localhost:8787"},
    )

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/decision"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["would_execute"] is False
    stored = json.loads(review.review_store_path().read_text(encoding="utf-8"))
    assert len(stored["items"]) == 1
    after = stored["items"][0]
    assert after["decision"]["state"] == "approved"
    assert after["decision"]["reason"] == "Looks durable and source-backed."
    assert after["decision"].get("decided_at")
    assert after["decision"].get("decided_by")
    assert {k: v for k, v in after.items() if k != "decision"} == {k: v for k, v in before.items() if k != "decision"}
    _assert_profile_files_unchanged(files)
    assert calls == []


def test_memory_skill_review_decision_response_redacts_sensitive_reason(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    item = _route_review_item("msr_secret_decision", files["memory_file"])
    _write_store(review.review_store_path(), [item])
    captured, fake_j = _capture_json_response()
    secret_reason = "denied because password=hunter2 and token=sk-secret should not echo"
    handler = _PostHandler(
        {"id": "msr_secret_decision", "decision": "denied", "reason": secret_reason},
        headers={"Host": "localhost:8787", "Origin": "http://localhost:8787"},
    )

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/decision"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["would_execute"] is False
    assert captured["payload"]["decision"]["state"] == "denied"
    assert captured["payload"]["decision"]["reason"] == "[redacted sensitive text]"
    assert secret_reason not in json.dumps(captured["payload"])


def test_memory_skill_review_denied_decision_updates_only_local_decision_state(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    calls = _poison_forbidden_memory_skill_review_side_effects(monkeypatch)
    item = _route_review_item("msr_denied", files["memory_file"])
    _write_store(review.review_store_path(), [item])
    before = json.loads(json.dumps(item, sort_keys=True))
    captured, fake_j = _capture_json_response()
    handler = _PostHandler(
        {"id": "msr_denied", "decision": "denied", "reason": "Not durable enough for memory."},
        headers={"Host": "localhost:8787", "Origin": "http://localhost:8787"},
    )

    with patch("api.routes.j", side_effect=fake_j):
        handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/decision"))

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"]["ok"] is True
    assert captured["payload"]["would_execute"] is False
    stored = json.loads(review.review_store_path().read_text(encoding="utf-8"))
    assert len(stored["items"]) == 1
    after = stored["items"][0]
    assert after["decision"]["state"] == "denied"
    assert after["decision"]["reason"] == "Not durable enough for memory."
    assert after["decision"].get("decided_at")
    assert after["decision"].get("decided_by")
    assert {k: v for k, v in after.items() if k != "decision"} == {k: v for k, v in before.items() if k != "decision"}
    _assert_profile_files_unchanged(files)
    assert calls == []


def test_memory_skill_review_store_mutations_hold_lock_across_read_modify_write(monkeypatch, tmp_path):
    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    _write_store(review.review_store_path(), [])

    class ProbeLock:
        def __init__(self):
            self.depth = 0

        def __enter__(self):
            self.depth += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            self.depth -= 1

        @property
        def locked(self):
            return self.depth > 0

    probe_lock = ProbeLock()
    original_read_store = review._read_store
    read_lock_states = []

    def guarded_read_store(path):
        read_lock_states.append(probe_lock.locked)
        return original_read_store(path)

    monkeypatch.setattr(review, "_STORE_LOCK", probe_lock, raising=False)
    monkeypatch.setattr(review, "_read_store", guarded_read_store, raising=False)

    propose_result = review.propose_operator_memory_skill_review(_valid_propose_body(), now=1779866500.0)
    assert propose_result["ok"] is True
    assert read_lock_states and all(read_lock_states)

    read_lock_states.clear()
    decision_result = review.decide_operator_memory_skill_review(
        {"id": propose_result["id"], "decision": "denied", "reason": "Do not keep this."},
        now=1779866501.0,
    )
    assert decision_result["ok"] is True
    assert read_lock_states and all(read_lock_states)
    _assert_profile_files_unchanged(files)


def test_memory_skill_review_approve_rejects_invalid_or_stale_items_without_apply(monkeypatch, tmp_path):
    import api.routes as routes

    review = importlib.import_module("api.operator_memory_skill_review")
    _patch_state_dir(monkeypatch, tmp_path)
    files = _patch_active_profile_files(monkeypatch, tmp_path)
    calls = _poison_forbidden_memory_skill_review_side_effects(monkeypatch)
    invalid = _route_review_item("msr_invalid", files["memory_file"], source_evidence=[{}])
    stale = _route_review_item(
        "msr_stale",
        files["memory_file"],
        stale_risk={
            "state": "current",
            "expires_at": "1970-01-01T00:00:01Z",
            "reason": "This proposal is stale and needs fresh proof before approval.",
        },
    )
    _write_store(review.review_store_path(), [invalid, stale])
    captured, fake_j = _capture_json_response()

    with patch("api.routes.j", side_effect=fake_j):
        for item_id in ("msr_invalid", "msr_stale"):
            captured.clear()
            handler = _PostHandler(
                {"id": item_id, "decision": "approved", "reason": "Approve despite invalid state should fail."},
                headers={"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"},
            )
            handled = routes.handle_post(handler, urlparse("/api/operator/memory-skill-review/decision"))
            assert handled is True
            assert captured["status"] == 400
            assert captured["payload"].get("ok") is False
            assert captured["payload"]["would_execute"] is False
            _assert_profile_files_unchanged(files)

        apply_handler = _PostHandler({"id": "msr_stale"}, headers={"Host": "127.0.0.1:8787", "Origin": "http://127.0.0.1:8787"})
        assert routes.handle_post(apply_handler, urlparse("/api/operator/memory-skill-review/apply")) is False

    stored = json.loads(review.review_store_path().read_text(encoding="utf-8"))
    decisions = {item["id"]: item["decision"]["state"] for item in stored["items"]}
    assert decisions["msr_invalid"] != "approved"
    assert decisions["msr_stale"] != "approved"
    _assert_profile_files_unchanged(files)
    assert calls == []
