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


REQUIRED_FIELDS = {
    "owner",
    "deadline_at",
    "dispatch_mechanism",
    "source",
    "acceptance_criteria",
    "halt_policy",
    "evidence",
    "status",
}


def _patch_state_dir(monkeypatch, tmp_path):
    import api.config as config

    state_dir = tmp_path / "webui-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STATE_DIR", state_dir, raising=False)
    return state_dir


def _write_store(path: Path, commitments):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "updated_at": 123.0, "commitments": commitments}), encoding="utf-8")


def _valid_commitment(**overrides):
    card = {
        "id": "c_valid",
        "created_at": 100.0,
        "updated_at": 100.0,
        "profile": "default",
        "title": "Ship commitment cards",
        "summary": "Record source-backed promises as local commitment cards.",
        "owner": "Max",
        "deadline_at": "2026-05-30T17:00:00Z",
        "review_at": None,
        "dispatch_mechanism": {"kind": "manual", "label": "Manual WebUI follow-up", "would_execute": False},
        "source": {
            "kind": "operator_proposal",
            "proposal_id": "commitment_cards",
            "session_id": "abc123",
            "content_hash": "sha256:abc",
            "quote": "Commitment cards / promote to commitment",
        },
        "acceptance_criteria": ["Targeted tests pass", "No dispatch is wired"],
        "halt_policy": "Stop if evidence is missing or source cannot be verified.",
        "evidence": [{"kind": "source", "label": "Operator proposal", "state": "present"}],
        "status": "active",
        "would_execute": False,
    }
    card.update(overrides)
    return card


def _valid_promote_body(**overrides):
    body = {
        "title": "Commitment cards / promote to commitment",
        "summary": "Turn promises into durable local commitment cards.",
        "owner": "Max",
        "deadline_at": "2026-05-30T17:00:00Z",
        "dispatch_mechanism": {"kind": "manual", "label": "Manual WebUI follow-up", "would_execute": False},
        "source": {"kind": "operator_proposal", "proposal_id": "commitment_cards", "session_id": "abc123"},
        "acceptance_criteria": ["Commitment can be read back", "No Kanban mutation occurs"],
        "halt_policy": "Stop if source proof is missing or stale.",
        "status": "active",
    }
    body.update(overrides)
    return body


def _patch_truth(monkeypatch, *, status="live"):
    import api.operator_truth as operator_truth

    def fake_truth_payload(*, session_id=None, ui_board_hint=None, now=None):
        return {
            "version": 1,
            "verified_at": now,
            "status": status,
            "summary": f"Truth {status}",
            "chips": [],
            "sources": [],
            "issues": [] if status == "live" else [f"truth {status}"],
        }

    monkeypatch.setattr(operator_truth, "build_operator_truth_payload", fake_truth_payload, raising=False)


def _patch_proposals(monkeypatch):
    import api.operator_proposals as operator_proposals

    def fake_proposals(*, session_id=None, ui_board_hint=None, now=None):
        return {
            "version": 1,
            "generated_at": now,
            "status": "live",
            "mode": "manual-read-only",
            "would_execute": False,
            "proposals": [
                {
                    "id": "commitment_cards",
                    "rank": 5,
                    "title": "Commitment cards / promote to commitment",
                    "summary": "Convert promised work into durable objects with owner/deadline/dispatch/evidence.",
                    "owner": "future Hermes WebUI task",
                    "would_execute": False,
                    "evidence": [{"source_id": "action_summary", "status": "live"}],
                }
            ],
            "sources": [],
            "issues": [],
        }

    monkeypatch.setattr(operator_proposals, "build_operator_proposal_payload", fake_proposals, raising=False)


def test_operator_commitments_missing_store_returns_unknown_without_fake_cards(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_truth(monkeypatch, status="live")

    payload = commitments.build_operator_commitments_payload(now=123.0)

    assert payload["version"] == 1
    assert payload["generated_at"] == 123.0
    assert payload["mode"] == "local-commitment-cards"
    assert payload["would_execute"] is False
    assert payload["status"] == "unknown"
    assert payload["commitments"] == []
    assert not any("sample" in json.dumps(item).lower() or "demo" in json.dumps(item).lower() for item in payload["commitments"])
    assert any("missing" in issue.lower() or "unavailable" in issue.lower() for issue in payload["issues"])
    assert not commitments.commitment_store_path().exists()


def test_operator_commitments_store_path_is_local_state_not_repo_static_or_kanban(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    state_dir = _patch_state_dir(monkeypatch, tmp_path)

    store = commitments.commitment_store_path()

    assert store == state_dir / "operator_commitments.json"
    store_text = str(store)
    assert "/home/malac/hermes-webui" not in store_text
    assert "/mnt/c/Users/malac/.openclaw/workspace/main" not in store_text
    assert "/static/" not in store_text
    assert "/kanban/" not in store_text


def test_operator_commitments_existing_cards_require_required_fields_and_classify_notes(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_truth(monkeypatch, status="live")
    invalid_note = {"id": "note_1", "title": "Loose promise", "summary": "missing owner/deadline/dispatch"}
    _write_store(commitments.commitment_store_path(), [_valid_commitment(), invalid_note])

    payload = commitments.build_operator_commitments_payload(now=200.0)

    assert len(payload["commitments"]) == 1
    card = payload["commitments"][0]
    assert REQUIRED_FIELDS.issubset(card.keys())
    assert card["would_execute"] is False
    assert card["dispatch_mechanism"]["would_execute"] is False
    assert payload["notes"]
    assert payload["notes"][0]["classification"] == "note"
    assert any("missing" in issue.lower() for issue in payload["issues"])


def test_operator_commitments_existing_cards_reject_malformed_source_and_placeholder_evidence(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_truth(monkeypatch, status="live")
    malformed = _valid_commitment(source={"foo": "bar"}, evidence=[{}])
    _write_store(commitments.commitment_store_path(), [malformed])

    payload = commitments.build_operator_commitments_payload(now=225.0)

    assert payload["commitments"] == []
    assert payload["notes"]
    assert payload["notes"][0]["classification"] == "note"
    assert {"source", "evidence"}.issubset(set(payload["notes"][0]["missing"]))
    assert payload["status"] != "live"
    assert any("source" in issue.lower() or "evidence" in issue.lower() for issue in payload["issues"])


def test_operator_commitment_promote_rejects_incomplete_payload_as_note_without_writing(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)

    result = commitments.promote_operator_commitment({"title": "Loose note"}, now=300.0)

    assert result["ok"] is False
    assert result["classification"] == "note"
    assert {"owner", "deadline_or_review", "dispatch_mechanism", "source", "acceptance_criteria", "halt_policy"}.issubset(set(result["missing"]))
    assert not commitments.commitment_store_path().exists()


def test_operator_commitment_promote_requires_real_source_proposal_or_session_message(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_proposals(monkeypatch)

    bad = commitments.promote_operator_commitment(
        _valid_promote_body(source={"kind": "operator_proposal", "proposal_id": "missing", "session_id": "abc123"}),
        now=400.0,
    )
    assert bad["ok"] is False
    assert "source" in bad["missing"] or any("source" in issue.lower() for issue in bad.get("issues", []))
    assert not commitments.commitment_store_path().exists()

    good = commitments.promote_operator_commitment(
        _valid_promote_body(source={"kind": "operator_proposal", "proposal_id": "commitment_cards", "session_id": "abc123", "quote": "CLIENT FAKE QUOTE"}),
        now=401.0,
    )
    assert good["ok"] is True
    card = good["commitment"]
    assert card["source"]["proposal_id"] == "commitment_cards"
    assert "CLIENT FAKE QUOTE" not in json.dumps(card)
    assert "durable objects" in card["source"]["quote"]


def test_operator_commitment_promote_accepts_explicit_recall_session_message_without_session_load(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)

    import api.models as models

    calls = []

    def forbidden_load(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("session_message promotion must not call Session.load")

    monkeypatch.setattr(models.Session, "load", staticmethod(forbidden_load), raising=False)
    source = {
        "kind": "session_message",
        "session_id": "abc123",
        "message_index": 0,
        "content_hash": "sha256:" + "a" * 64,
        "quote": "Redacted source quote",
    }

    result = commitments.promote_operator_commitment(_valid_promote_body(source=source), now=450.0)

    assert result["ok"] is True
    assert calls == []
    card = result["commitment"]
    assert card["source"] == source
    assert card["evidence"] == [
        {
            "kind": "source",
            "label": "Session message",
            "state": "present",
            "session_id": "abc123",
            "message_index": 0,
            "content_hash": source["content_hash"],
        }
    ]

    stored = json.loads(commitments.commitment_store_path().read_text(encoding="utf-8"))
    stored_card = stored["commitments"][0]
    assert stored_card["source"] == source
    assert stored_card["source"]["quote"] == "Redacted source quote"
    assert "password=" not in json.dumps(stored_card).lower()


def test_operator_commitment_promote_rejects_session_message_without_explicit_hash_quote_and_strict_index(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)

    import api.models as models

    def fake_load(_sid):
        return types.SimpleNamespace(
            messages=[
                {"role": "user", "content": "Raw session message zero"},
                {"role": "assistant", "content": "Raw session message one"},
            ]
        )

    monkeypatch.setattr(models.Session, "load", staticmethod(fake_load), raising=False)
    valid_hash = "sha256:" + "b" * 64
    base_source = {
        "kind": "session_message",
        "session_id": "abc123",
        "message_index": 0,
        "content_hash": valid_hash,
        "quote": "Redacted source quote",
    }
    invalid_sources = [
        ("missing content_hash", {key: value for key, value in base_source.items() if key != "content_hash"}),
        ("missing quote", {key: value for key, value in base_source.items() if key != "quote"}),
        ("short hash", {**base_source, "content_hash": "sha256:abc"}),
        ("not a hash", {**base_source, "content_hash": "not-a-hash"}),
        ("float index", {**base_source, "message_index": 1.9}),
        ("negative-ish index", {**base_source, "message_index": -0.2}),
        ("non-decimal index", {**base_source, "message_index": "0x1"}),
    ]

    for label, source in invalid_sources:
        result = commitments.promote_operator_commitment(_valid_promote_body(source=source), now=460.0)
        assert result["ok"] is False, label
        assert "source" in result.get("missing", []) or any("source" in issue.lower() for issue in result.get("issues", [])), label
        assert not commitments.commitment_store_path().exists(), label


def test_operator_commitment_promote_rejects_raw_secret_session_message_quote_without_writing(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)

    import api.models as models

    calls = []

    def forbidden_load(_sid):
        calls.append(_sid)
        raise AssertionError("session_message raw-secret validation must not call Session.load")

    monkeypatch.setattr(models.Session, "load", staticmethod(forbidden_load), raising=False)

    def source_for(quote, hash_fill="c"):
        return {
            "kind": "session_message",
            "session_id": "abc123",
            "message_index": 0,
            "content_hash": "sha256:" + hash_fill * 64,
            "quote": quote,
        }

    raw_secret_quotes = [
        ("password key-value", "password=supersecret"),
        ("bearer token", "Bearer abcdefghijklmnop12345"),
        ("openai token", "sk-" + "a" * 48),
        ("slack bot token", "xox" + "b-123456789012-123456789012-abcdefghijklmnopqrstuvwx"),
    ]

    for label, quote in raw_secret_quotes:
        result = commitments.promote_operator_commitment(
            _valid_promote_body(source=source_for(quote)),
            now=470.0,
        )

        assert result["ok"] is False, label
        assert "source" in result.get("missing", []) or any("source" in issue.lower() for issue in result.get("issues", [])), label
        assert not commitments.commitment_store_path().exists(), label
        assert calls == [], label

    accepted = commitments.promote_operator_commitment(
        _valid_promote_body(source=source_for("password=[redacted]", hash_fill="d")),
        now=471.0,
    )

    assert accepted["ok"] is True
    assert calls == []
    stored = json.loads(commitments.commitment_store_path().read_text(encoding="utf-8"))
    stored_card = stored["commitments"][0]
    assert stored_card["source"]["quote"] == "password=[redacted]"
    assert "supersecret" not in json.dumps(stored_card).lower()


def test_operator_commitment_promote_rejects_github_token_session_message_quote_without_loading_or_writing(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)

    import api.models as models

    calls = []

    def forbidden_load(_sid):
        calls.append(_sid)
        raise AssertionError("session_message raw-secret validation must not call Session.load")

    monkeypatch.setattr(models.Session, "load", staticmethod(forbidden_load), raising=False)

    def source_for(quote, hash_fill="e"):
        return {
            "kind": "session_message",
            "session_id": "abc123",
            "message_index": 0,
            "content_hash": "sha256:" + hash_fill * 64,
            "quote": quote,
        }

    raw_classic_pat = "ghp_" + "A" * 36
    raw_fine_grained_pat = "github_pat_" + "B" * 22 + "_" + "C" * 59
    token_cases = [
        ("github classic token", raw_classic_pat),
        ("github fine-grained token", raw_fine_grained_pat),
    ]
    delimiter_cases = [
        ("end", ""),
        ("period", "."),
        ("close paren", ")"),
        ("close bracket", "]"),
        ("double quote", '"'),
        ("newline", "\nnext line"),
    ]
    for token_label, raw_token in token_cases:
        for delimiter_label, delimiter in delimiter_cases:
            quote = f"{raw_token}{delimiter}"
            label = f"{token_label} followed by {delimiter_label}"
            result = commitments.promote_operator_commitment(
                _valid_promote_body(source=source_for(quote)),
                now=472.0,
            )

            assert result["ok"] is False, label
            assert "source" in result.get("missing", []) or any("source" in issue.lower() for issue in result.get("issues", [])), label
            assert not commitments.commitment_store_path().exists(), label
            assert calls == [], label

    for label, quote in [
        ("github classic token embedded in longer token", raw_classic_pat + "A"),
        ("github fine-grained token embedded in longer token", raw_fine_grained_pat + "_"),
    ]:
        result = commitments.promote_operator_commitment(
            _valid_promote_body(source=source_for(quote)),
            now=472.0,
        )

        assert result["ok"] is False, label
        assert "source" in result.get("missing", []) or any("source" in issue.lower() for issue in result.get("issues", [])), label
        assert not commitments.commitment_store_path().exists(), label
        assert calls == [], label

    accepted = commitments.promote_operator_commitment(
        _valid_promote_body(source=source_for("github_pat_[redacted]", hash_fill="f")),
        now=473.0,
    )
    assert accepted["ok"] is True
    assert calls == []
    stored = json.loads(commitments.commitment_store_path().read_text(encoding="utf-8"))
    stored_card = stored["commitments"][0]
    assert stored_card["source"]["quote"] == "github_pat_[redacted]"
    assert raw_classic_pat not in json.dumps(stored_card)
    assert raw_fine_grained_pat not in json.dumps(stored_card)


def test_operator_commitment_promote_persists_local_card_with_would_execute_false(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_proposals(monkeypatch)

    result = commitments.promote_operator_commitment(_valid_promote_body(), now=500.0, client_context={"profile": "default"})

    assert result["ok"] is True
    card = result["commitment"]
    assert card["id"].startswith("c_")
    assert card["created_at"] == 500.0
    assert card["updated_at"] == 500.0
    assert card["profile"] == "default"
    assert card["would_execute"] is False
    assert card["dispatch_mechanism"] == {"kind": "manual", "label": "Manual WebUI follow-up", "would_execute": False}
    assert card["source"]["kind"] == "operator_proposal"
    assert card["evidence"]

    stored = json.loads(commitments.commitment_store_path().read_text(encoding="utf-8"))
    assert stored["version"] == 1
    assert stored["commitments"][0]["id"] == card["id"]


def test_operator_commitment_promote_rejects_auto_dispatch_cron_goal_aim_mechanisms(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_proposals(monkeypatch)

    forbidden = ["cron", "goal_loop", "auto_dispatch", "background_loop", "kanban_dispatcher", "aim_cron", "aim_runtime", "webhook"]
    for value in forbidden:
        result = commitments.promote_operator_commitment(
            _valid_promote_body(dispatch_mechanism={"kind": value, "label": value, "would_execute": False}),
            now=600.0,
        )
        assert result["ok"] is False, value
        assert "dispatch_mechanism" in result["missing"] or any(value in issue.lower() for issue in result.get("issues", []))
    assert not commitments.commitment_store_path().exists()


def test_operator_commitment_promote_never_calls_dispatch_cron_goal_shell_or_kanban(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_proposals(monkeypatch)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("commitment promotion must not dispatch, shell, cron, goal, or mutate Kanban")

    fake_kanban_bridge = types.ModuleType("api.kanban_bridge")
    for name in ("handle_kanban_post", "handle_kanban_patch", "handle_kanban_delete", "dispatch", "create_task", "claim", "complete"):
        setattr(fake_kanban_bridge, name, forbidden)
    fake_cron = types.ModuleType("cron.jobs")
    setattr(fake_cron, "create", forbidden)
    fake_goals = types.ModuleType("api.goals")
    setattr(fake_goals, "set_goal", forbidden)
    monkeypatch.setitem(sys.modules, "api.kanban_bridge", fake_kanban_bridge)
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_cron)
    monkeypatch.setitem(sys.modules, "api.goals", fake_goals)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(threading, "Thread", forbidden)
    monkeypatch.setattr(threading, "Timer", forbidden)

    result = commitments.promote_operator_commitment(_valid_promote_body(), now=700.0)

    assert result["ok"] is True
    assert calls == []


def test_operator_commitments_includes_truth_and_degrades_stale_truth(monkeypatch, tmp_path):
    commitments = importlib.import_module("api.operator_commitments")
    _patch_state_dir(monkeypatch, tmp_path)
    _patch_truth(monkeypatch, status="stale")
    _write_store(commitments.commitment_store_path(), [_valid_commitment()])

    payload = commitments.build_operator_commitments_payload(session_id="abc123", ui_board_hint="hermes-operator", now=800.0)

    assert payload["truth"]["status"] == "stale"
    assert payload["status"] != "live"
    assert any("operator_truth" in issue and "stale" in issue for issue in payload["issues"])


def test_operator_commitments_route_get_returns_json(monkeypatch):
    import api.routes as routes

    expected = {"version": 1, "status": "unknown", "commitments": [], "sources": []}
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        return True

    with patch("api.operator_commitments.build_operator_commitments_payload", return_value=expected) as build_payload, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_get(
            types.SimpleNamespace(wfile=io.BytesIO()),
            urlparse("/api/operator/commitments?session_id=abc123&ui_board=hermes-operator"),
        )

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"] == expected
    build_payload.assert_called_once_with(session_id="abc123", ui_board_hint="hermes-operator")


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


def test_operator_commitments_promote_route_accepts_loopback_localhost_only(monkeypatch):
    import api.routes as routes

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        return True

    def fake_promote(body, *, now=None, client_context=None):
        captured["body"] = body
        captured["client_context"] = client_context
        return {"ok": True, "commitment": {"id": "c_route"}}

    handler = _PostHandler(_valid_promote_body(), headers={"Origin": "http://127.0.0.1:8787"})
    with patch("api.operator_commitments.promote_operator_commitment", side_effect=fake_promote) as promote, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_post(handler, urlparse("/api/operator/commitments/promote"))

    assert handled is True
    assert promote.call_count == 1
    assert captured["status"] == 201
    assert captured["body"]["owner"] == "Max"
    assert captured["client_context"]["client_ip"] == "127.0.0.1"


def test_operator_commitments_promote_route_rejects_public_origin_even_when_allowed(monkeypatch):
    import api.routes as routes

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        return True

    def forbidden_promote(*args, **kwargs):
        raise AssertionError("public commitment writes must not call promote")

    monkeypatch.setenv("HERMES_WEBUI_ALLOWED_ORIGINS", "https://public.example")
    public_header_cases = [
        {"Origin": "https://public.example", "Host": "public.example"},
        {"Origin": "http://127.0.0.1:8787", "Host": "127.0.0.1:8787@public.example"},
        {"Origin": "http://127.0.0.1:8787@public.example", "Host": "127.0.0.1:8787"},
    ]
    with patch("api.operator_commitments.promote_operator_commitment", side_effect=forbidden_promote), patch(
        "api.routes.j", side_effect=fake_j
    ):
        for headers in public_header_cases:
            captured.clear()
            handler = _PostHandler(
                _valid_promote_body(),
                client_ip="127.0.0.1" if "@public.example" in json.dumps(headers) else "203.0.113.10",
                headers=headers,
            )
            handled = routes.handle_post(handler, urlparse("/api/operator/commitments/promote"))
            assert handled is True
            assert captured["status"] in {403, 404}


def test_operator_commitments_promote_route_rejects_public_x_forwarded_for_proxy(monkeypatch):
    import api.routes as routes

    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        return True

    def forbidden_promote(*args, **kwargs):
        raise AssertionError("proxied public commitment writes must not call promote")

    handler = _PostHandler(
        _valid_promote_body(),
        client_ip="127.0.0.1",
        headers={"Origin": "http://127.0.0.1:8787", "X-Forwarded-For": "203.0.113.11"},
    )
    with patch("api.operator_commitments.promote_operator_commitment", side_effect=forbidden_promote), patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_post(handler, urlparse("/api/operator/commitments/promote"))

    assert handled is True
    assert captured["status"] in {403, 404}
