from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

from api import external_memory
from api import routes


class _FakeHandler:
    def __init__(self, body: bytes = b""):
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.headers = {"Content-Length": str(len(body))}
        self.status = None
        self.sent_headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers[key] = value

    def end_headers(self):
        pass

    def payload(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _register_provider(home: Path, *, provider_id: str = "custom_store", label: str = "Custom Store", db_name: str = "items.sqlite", config: dict | None = None) -> Path:
    db = home / provider_id / db_name
    cfg_path = home / provider_id / "config.json"
    providers = {
        "providers": [
            {
                "id": provider_id,
                "label": label,
                "db_path": str(db),
                "config_path": str(cfg_path),
            }
        ]
    }
    (home / "external_memory_providers.json").write_text(json.dumps(providers), encoding="utf-8")
    if config is not None:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config), encoding="utf-8")
    external_memory.ensure_db(db)
    return db


def _seed_candidate(
    home: Path,
    *,
    provider_id: str = "custom_store",
    candidate_id: str = "cand_test123",
    text: str = "External Memory candidates require review before approval.",
    state: str = "candidate",
    created_at: float = 1000,
) -> str:
    db = _register_provider(home, provider_id=provider_id)
    with sqlite3.connect(db) as con:
        con.execute(
            """
            insert into candidates(id, text, source, metadata_json, state, content_sha256, created_at, updated_at)
            values (?, ?, 'auto_capture', ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                text,
                json.dumps({"type": "decision", "confidence": 0.91}),
                state,
                f"sha-{candidate_id}",
                created_at,
                created_at,
            ),
        )
    return candidate_id


def test_list_providers_is_empty_until_custom_provider_is_registered(tmp_path):
    data = external_memory.list_providers(tmp_path)

    assert data == {"ok": True, "active": "", "providers": []}


def test_custom_provider_can_be_registered_and_queried(tmp_path):
    custom_db = _register_provider(tmp_path, provider_id="custom_store", label="Custom Store")
    with sqlite3.connect(custom_db) as con:
        con.execute(
            """
            insert into candidates(id, text, source, metadata_json, state, content_sha256, created_at, updated_at)
            values ('custom_1', 'Custom external memory providers can expose reviewable candidates.', 'custom', '{}', 'candidate', 'sha', 1, 1)
            """
        )

    providers = external_memory.list_providers(tmp_path)
    data = external_memory.list_candidates(tmp_path, provider="custom_store", state="candidate")

    assert providers["active"] == "custom_store"
    assert any(p["id"] == "custom_store" for p in providers["providers"])
    assert data["provider"] == "custom_store"
    assert data["candidates"][0]["id"] == "custom_1"


def test_missing_provider_raises_not_found(tmp_path):
    try:
        external_memory.list_candidates(tmp_path, state="all")
        assert False, "unconfigured external memory should not pick an implicit provider"
    except external_memory.ExternalMemoryNotFound as exc:
        assert "no external memory providers configured" in str(exc)


def test_list_candidates_reads_profile_scoped_db(tmp_path):
    candidate_id = _seed_candidate(tmp_path)

    data = external_memory.list_candidates(tmp_path, state="all", limit=10, offset=0)

    assert data["ok"] is True
    assert data["state"] == "all"
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert data["total"] == 1
    assert data["count"] == 1
    assert data["provider"] == "custom_store"
    assert data["candidates"][0]["id"] == candidate_id
    assert data["candidates"][0]["metadata"]["type"] == "decision"


def test_list_candidates_filters_state_and_paginates(tmp_path):
    _seed_candidate(tmp_path, candidate_id="cand_old", state="candidate", created_at=1000)
    _seed_candidate(tmp_path, candidate_id="cand_new", state="candidate", created_at=2000)
    _seed_candidate(tmp_path, candidate_id="approved", state="approved", created_at=3000)

    data = external_memory.list_candidates(tmp_path, state="candidate", limit=1, offset=1)

    assert data["ok"] is True
    assert data["state"] == "candidate"
    assert data["total"] == 2
    assert data["count"] == 1
    assert data["candidates"][0]["id"] == "cand_old"


def test_reject_candidate_updates_state(tmp_path):
    candidate_id = _seed_candidate(tmp_path)

    data = external_memory.reject_candidate(tmp_path, candidate_id, reason="not durable")

    assert data["ok"] is True
    row = external_memory.get_candidate(tmp_path, candidate_id)
    assert row["candidate"]["state"] == "rejected"
    assert row["candidate"]["metadata"]["review_reason"] == "not durable"


def test_delete_candidate_removes_row(tmp_path):
    candidate_id = _seed_candidate(tmp_path)

    data = external_memory.delete_candidate(tmp_path, candidate_id)

    assert data["ok"] is True
    assert data["provider"] == "custom_store"
    assert external_memory.get_candidate(tmp_path, candidate_id)["candidate"] is None


def test_update_candidate_text_edits_unapproved_candidate(tmp_path):
    candidate_id = _seed_candidate(tmp_path)

    data = external_memory.update_candidate_text(tmp_path, candidate_id, "External Memory candidates should be edited into canonical semantic statements before approval.")

    assert data["ok"] is True
    row = external_memory.get_candidate(tmp_path, candidate_id)["candidate"]
    assert row["text"] == "External Memory candidates should be edited into canonical semantic statements before approval."
    assert row["state"] == "candidate"
    assert "edited_at" in row["metadata"]


def test_update_candidate_text_rejects_approved_candidate(tmp_path):
    candidate_id = _seed_candidate(tmp_path, state="approved")

    try:
        external_memory.update_candidate_text(tmp_path, candidate_id, "New text")
        assert False, "approved external_memory should not be editable"
    except ValueError as exc:
        assert "approved external memory cannot be edited" in str(exc)


def test_approve_candidate_indexes_then_marks_approved(tmp_path):
    candidate_id = _seed_candidate(tmp_path)

    with patch("api.external_memory.check_write_policy", return_value={"ok": True, "stage": "policy"}), patch(
        "api.external_memory.index_candidate", return_value={"point_id": "pt-1"}
    ) as index:
        data = external_memory.approve_candidate(tmp_path, candidate_id)

    assert data["ok"] is True
    index.assert_called_once()
    row = external_memory.get_candidate(tmp_path, candidate_id)["candidate"]
    assert row["state"] == "approved"
    assert row["metadata"]["qdrant_point_id"] == "pt-1"
    assert "approved_at" in row["metadata"]


def test_approve_candidate_keeps_candidate_when_index_fails(tmp_path):
    candidate_id = _seed_candidate(tmp_path)

    with patch("api.external_memory.index_candidate", side_effect=external_memory.ExternalMemoryError("index down")):
        try:
            external_memory.approve_candidate(tmp_path, candidate_id)
            assert False, "approval should fail when indexing fails"
        except external_memory.ExternalMemoryError:
            pass

    row = external_memory.get_candidate(tmp_path, candidate_id)["candidate"]
    assert row["state"] == "candidate"


def test_approve_candidate_requires_explicit_indexing_config(tmp_path):
    candidate_id = _seed_candidate(tmp_path)

    try:
        external_memory.approve_candidate(tmp_path, candidate_id)
        assert False, "approval should fail when indexing is not configured"
    except external_memory.ExternalMemoryNotConfigured as exc:
        assert "external memory indexing is not configured" in str(exc)

    row = external_memory.get_candidate(tmp_path, candidate_id)["candidate"]
    assert row["state"] == "candidate"


def test_policy_check_reports_missing_config_before_provider_io(tmp_path):
    _seed_candidate(tmp_path)

    try:
        external_memory.check_write_policy(tmp_path)
        assert False, "write policy should fail before provider I/O when indexing is not configured"
    except external_memory.ExternalMemoryNotConfigured as exc:
        error = exc.to_response()

    assert error == {
        "ok": False,
        "stage": "policy",
        "code": "indexing_not_configured",
        "message": "external memory indexing is not configured; missing ollama_url, embed_model, qdrant_url, qdrant_collection",
        "retryable": True,
    }


def test_index_candidate_requires_verified_active_qdrant_payload(tmp_path):
    _register_provider(
        tmp_path,
        config={
            "ollama_url": "http://127.0.0.1:11434",
            "embed_model": "fixture-embed-model",
            "qdrant_url": "http://127.0.0.1:6333",
            "qdrant_collection": "fixture_collection",
        },
    )
    candidate = {
        "provider": "custom_store",
        "id": "cand_verify",
        "text": "External Memory approval requires verified active indexing.",
        "source": "auto_capture",
        "metadata": {},
    }

    with patch("api.external_memory.embed_text", return_value=[0.1]), patch("api.external_memory.qdrant_upsert"), patch(
        "api.external_memory.qdrant_verify_active", return_value=False
    ):
        try:
            external_memory.index_candidate(tmp_path, candidate)
            assert False, "indexing should fail when active Qdrant payload cannot be verified"
        except external_memory.ExternalMemoryError as exc:
            assert exc.stage == "persistence"
            assert exc.code == "indexing_failed"
            assert exc.retryable is True


def test_load_config_reads_provider_config_and_env_without_defaults(tmp_path, monkeypatch):
    _register_provider(tmp_path, config={"qdrant_collection": "fixture_collection"})
    monkeypatch.setenv("HERMES_EXTERNAL_MEMORY_OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setenv("HERMES_EXTERNAL_MEMORY_QDRANT_URL", "http://127.0.0.1:6333")
    monkeypatch.setenv("HERMES_EXTERNAL_MEMORY_EMBED_MODEL", "fixture-embed-model")

    cfg = external_memory.load_config(tmp_path)

    assert cfg["ollama_url"] == "http://127.0.0.1:11434"
    assert cfg["qdrant_url"] == "http://127.0.0.1:6333"
    assert cfg["embed_model"] == "fixture-embed-model"
    assert cfg["qdrant_collection"] == "fixture_collection"


def test_qdrant_upsert_uses_put_method():
    cfg = {"qdrant_url": "http://127.0.0.1:6333", "qdrant_collection": "fixture_collection", "ollama_url": "http://127.0.0.1:11434", "embed_model": "fixture-embed-model", "timeout": 1}
    with patch("api.external_memory._request_json", return_value={}) as req:
        external_memory.qdrant_upsert(cfg, "00000000-0000-0000-0000-000000000001", [0.1], {"text": "x"})
    assert req.call_args.kwargs["method"] == "PUT"


def test_search_external_memory_uses_local_sqlite_like(tmp_path):
    _seed_candidate(tmp_path, candidate_id="hit", text="Approved external memory fact about backend APIs", state="approved", created_at=1000)
    _seed_candidate(tmp_path, candidate_id="miss", text="Unrelated local note", state="approved", created_at=2000)
    data = external_memory.search_external_memory(tmp_path, "external memory fact", limit=3)

    assert data["ok"] is True
    assert data["q"] == "external memory fact"
    assert data["count"] == 1
    assert data["results"][0]["id"] == "hit"
    assert data["results"][0]["text"] == "Approved external memory fact about backend APIs"


def test_get_candidates_endpoint_uses_active_home_and_offset(tmp_path, monkeypatch):
    _seed_candidate(tmp_path, candidate_id="old", state="candidate", created_at=1000)
    _seed_candidate(tmp_path, candidate_id="new", state="candidate", created_at=2000)
    monkeypatch.setattr(routes, "_active_hermes_home_for_external_memory", lambda: tmp_path)
    handler = _FakeHandler()

    routes.handle_get(handler, urlparse("/api/external-memory/candidates?state=candidate&limit=1&offset=1"))

    payload = handler.payload()
    assert handler.status == 200
    assert payload["ok"] is True
    assert payload["provider"] == "custom_store"
    assert payload["candidates"][0]["id"] == "old"


def test_delete_candidate_endpoint_removes_candidate(tmp_path, monkeypatch):
    candidate_id = _seed_candidate(tmp_path)
    monkeypatch.setattr(routes, "_active_hermes_home_for_external_memory", lambda: tmp_path)
    handler = _FakeHandler()

    handled = routes.handle_delete(handler, urlparse(f"/api/external-memory/candidates/{candidate_id}"))

    assert handled is True
    assert handler.status == 200
    assert handler.payload() == {"ok": True, "provider": "custom_store", "deleted": candidate_id}
    assert external_memory.get_candidate(tmp_path, candidate_id)["candidate"] is None


def test_approve_endpoint_returns_400_when_indexing_is_not_configured(tmp_path, monkeypatch):
    candidate_id = _seed_candidate(tmp_path)
    monkeypatch.setattr(routes, "_active_hermes_home_for_external_memory", lambda: tmp_path)
    handler = _FakeHandler(b"{}")

    routes.handle_post(handler, urlparse(f"/api/external-memory/candidates/{candidate_id}/approve"))

    assert handler.status == 400
    payload = handler.payload()
    assert payload["ok"] is False
    assert payload["stage"] == "policy"
    assert payload["code"] == "indexing_not_configured"
    assert "external memory indexing is not configured" in payload["message"]
    assert payload["retryable"] is True
    assert external_memory.get_candidate(tmp_path, candidate_id)["candidate"]["state"] == "candidate"


def test_approve_endpoint_returns_structured_persistence_error(tmp_path, monkeypatch):
    candidate_id = _seed_candidate(tmp_path)
    monkeypatch.setattr(routes, "_active_hermes_home_for_external_memory", lambda: tmp_path)
    handler = _FakeHandler(b"{}")

    with patch("api.external_memory.check_write_policy", return_value={"ok": True, "stage": "policy"}), patch(
        "api.external_memory.index_candidate",
        side_effect=external_memory.ExternalMemoryError(
            "external memory indexing did not verify an active persisted record",
            stage="persistence",
            code="indexing_failed",
            retryable=True,
        ),
    ):
        routes.handle_post(handler, urlparse(f"/api/external-memory/candidates/{candidate_id}/approve"))

    assert handler.status == 502
    payload = handler.payload()
    assert payload["ok"] is False
    assert payload["stage"] == "persistence"
    assert payload["code"] == "indexing_failed"
    assert payload["retryable"] is True
    assert external_memory.get_candidate(tmp_path, candidate_id)["candidate"]["state"] == "candidate"


def test_approve_and_reject_candidate_endpoints_update_state(tmp_path, monkeypatch):
    approve_id = _seed_candidate(tmp_path, candidate_id="cand_approve")
    reject_id = _seed_candidate(tmp_path, candidate_id="cand_reject")
    monkeypatch.setattr(routes, "_active_hermes_home_for_external_memory", lambda: tmp_path)

    approve_handler = _FakeHandler(b"{}")
    with patch("api.external_memory.check_write_policy", return_value={"ok": True, "stage": "policy"}), patch(
        "api.external_memory.index_candidate", return_value={"point_id": "pt-route"}
    ):
        routes.handle_post(approve_handler, urlparse(f"/api/external-memory/candidates/{approve_id}/approve"))
    assert approve_handler.status == 200
    assert approve_handler.payload()["candidate"]["state"] == "approved"

    reject_handler = _FakeHandler(json.dumps({"reason": "not stable"}).encode("utf-8"))
    routes.handle_post(reject_handler, urlparse(f"/api/external-memory/candidates/{reject_id}/reject"))
    assert reject_handler.status == 200
    payload = reject_handler.payload()
    assert payload["candidate"]["state"] == "rejected"
    assert payload["candidate"]["metadata"]["review_reason"] == "not stable"


def test_edit_candidate_endpoint_updates_text(tmp_path, monkeypatch):
    candidate_id = _seed_candidate(tmp_path)
    monkeypatch.setattr(routes, "_active_hermes_home_for_external_memory", lambda: tmp_path)
    handler = _FakeHandler(json.dumps({"text": "External Memory candidates should use semantic statements before approval."}).encode("utf-8"))

    routes.handle_post(handler, urlparse(f"/api/external-memory/candidates/{candidate_id}/edit"))

    assert handler.status == 200
    payload = handler.payload()
    assert payload["candidate"]["text"] == "External Memory candidates should use semantic statements before approval."
    assert payload["candidate"]["metadata"]["edited_at"]
