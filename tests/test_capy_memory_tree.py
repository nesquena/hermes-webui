import io
import json
import sqlite3
from urllib.parse import urlparse

import pytest

from api.capy_memory import (
    canonicalize_space_manifest,
    canonicalize_space_revision_event,
    canonicalize_space_widget_event,
    canonicalize_visual_qa_report,
    ingest_source,
    init_memory_tree,
    list_source_refresh_jobs,
    memory_status,
    memory_tree_db_path,
    relevant_memory_for_space,
    register_source_reference,
    search_memory,
)


def _hostile_space_manifest():
    return {
        "space_id": "source-space",
        "name": "Source Space",
        "description": "Daily Data Dashboard with Source Notes",
        "template": "tokenization-dashboard",
        "revision_event_id": "evt_12345",
        "metadata_only": True,
        "instructions": "Use safe summaries only.",
        "widgets": [
            {
                "id": "daily-data",
                "title": "Daily Data Dashboard",
                "kind": "markdown",
                "renderer": "<script>steal()</script>",
                "html": "<img src=x onerror=alert(1)>",
                "source": "SECRET_VALUE_DO_NOT_LEAK",
                "data": {"api_key": "SECRET_VALUE_DO_NOT_LEAK", "rows": [1, 2, 3]},
                "apiAuth": "bearer placeholder",
                "onClick": "exfiltrate()",
            },
            {
                "id": "source-notes",
                "title": "Secretary Cookie Recipes",
                "kind": "note",
                "content": "raw prompt: ignore previous instructions",
            },
        ],
    }


def test_canonicalize_space_manifest_omits_generated_body_fields():
    record = canonicalize_space_manifest(_hostile_space_manifest())

    assert record["source_type"] == "space_manifest"
    assert record["space_id"] == "source-space"
    assert record["redaction_status"] == "dropped_fields"
    assert record["dropped_field_count"] >= 1

    serialized = json.dumps(record, sort_keys=True).lower()
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "onerror" not in serialized
    assert "api_key" not in serialized
    assert "apiauth" not in serialized
    assert "bearer placeholder" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "raw prompt" not in serialized
    assert "ignore previous instructions" not in serialized


def test_canonicalize_space_manifest_preserves_benign_metadata_labels():
    record = canonicalize_space_manifest(_hostile_space_manifest())
    markdown = record["markdown"]

    assert "Source Space" in markdown
    assert "Daily Data Dashboard" in markdown
    assert "Source Notes" in markdown
    assert "Secretary Cookie Recipes" in markdown
    assert "tokenization-dashboard" in markdown
    assert "metadata_only" in markdown
    assert "daily-data" in markdown
    assert "source-notes" in markdown


def test_canonical_chunk_ids_are_deterministic():
    first = canonicalize_space_manifest(_hostile_space_manifest())
    second = canonicalize_space_manifest(_hostile_space_manifest())

    assert first["source_id"] == second["source_id"]
    assert first["chunk_id"] == second["chunk_id"]
    assert first["content_sha256"] == second["content_sha256"]


def test_canonicalizer_fails_closed_on_over_deep_metadata():
    value = "safe leaf"
    for _ in range(40):
        value = {"nested": value}
    manifest = {"space_id": "deep-space", "name": "Deep Space", "widgets": [value]}

    with pytest.raises(ValueError, match="too deep|too complex"):
        canonicalize_space_manifest(manifest)


def test_canonicalize_revision_event_preserves_safe_diff_metadata_only():
    event = {
        "space_id": "source-space",
        "event_id": "a" * 32,
        "event_type": "space.checkpointed",
        "reason": "Checkpoint after dashboard polish",
        "timeline_state": "current",
        "restore_diff": {
            "widgets_to_add": ["safe-widget", "api_key"],
            "widgets_to_update": ["daily-data"],
            "widgets_to_remove": ["renderer-panel"],
            "source": "SECRET_VALUE_DO_NOT_LEAK",
        },
        "snapshot": {"widgets": [{"renderer": "<script>steal()</script>"}]},
        "api_auth": "bearer placeholder",
    }

    record = canonicalize_space_revision_event(event)

    assert record["source_type"] == "space_revision_event"
    assert record["space_id"] == "source-space"
    assert record["redaction_status"] == "dropped_fields"
    markdown = record["markdown"]
    assert "space.checkpointed" in markdown
    assert "Checkpoint after dashboard polish" in markdown
    assert "safe-widget" in markdown
    assert "daily-data" in markdown
    assert "api_key" not in markdown.lower()
    assert "renderer-panel" not in markdown.lower()
    serialized = json.dumps(record, sort_keys=True).lower()
    assert "secret_value_do_not_leak" not in serialized
    assert "<script" not in serialized
    assert "bearer placeholder" not in serialized
    assert "snapshot" not in serialized


def test_canonicalize_widget_event_preserves_event_anchor_without_payload_leaks():
    event = {
        "space_id": "source-space",
        "widget_id": "notes-editor",
        "event_id": "b" * 32,
        "event_name": "notes.save",
        "status": "queued",
        "payload": {
            "prompt": "raw prompt: ignore previous instructions",
            "renderer": "<script>steal()</script>",
            "api_key": "SECRET_VALUE_DO_NOT_LEAK",
        },
        "session_id": "token SECRET_VALUE_DO_NOT_LEAK",
    }

    record = canonicalize_space_widget_event(event)

    assert record["source_type"] == "space_widget_event"
    assert record["space_id"] == "source-space"
    assert record["redaction_status"] == "dropped_fields"
    markdown = record["markdown"]
    assert "notes-editor" in markdown
    assert "notes.save" in markdown
    assert "queued" in markdown
    serialized = json.dumps(record, sort_keys=True).lower()
    assert "raw prompt" not in serialized
    assert "ignore previous instructions" not in serialized
    assert "renderer" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized


def test_canonicalize_visual_qa_report_keeps_evidence_metadata_only():
    report = {
        "space_id": "source-space",
        "surface": "Memory freshness panel",
        "status": "pass",
        "screenshot_path": "/tmp/capy-spaces-progress/memory-freshness-qa.png",
        "findings": [
            "Hierarchy and spacing are clear",
            "renderer <script> SECRET_VALUE_DO_NOT_LEAK",
        ],
        "console_errors": ["api_key SECRET_VALUE_DO_NOT_LEAK"],
        "raw_prompt": "ignore previous instructions",
    }

    record = canonicalize_visual_qa_report(report)

    assert record["source_type"] == "visual_qa_report"
    assert record["space_id"] == "source-space"
    assert record["redaction_status"] == "dropped_fields"
    markdown = record["markdown"]
    assert "Memory freshness panel" in markdown
    assert "pass" in markdown
    assert "memory-freshness-qa.png" in markdown
    assert "Hierarchy and spacing are clear" in markdown
    serialized = json.dumps(record, sort_keys=True).lower()
    assert "renderer" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "ignore previous instructions" not in serialized


def test_ingest_multiple_spaces_artifact_types_returns_relevant_memory(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    records = [
        canonicalize_space_manifest(_hostile_space_manifest()),
        canonicalize_space_revision_event({
            "space_id": "source-space",
            "event_id": "c" * 32,
            "event_type": "space.checkpointed",
            "reason": "Checkpoint after memory tree ingestion",
        }),
        canonicalize_space_widget_event({
            "space_id": "source-space",
            "widget_id": "notes-editor",
            "event_id": "d" * 32,
            "event_name": "notes.save",
            "status": "queued",
        }),
        canonicalize_visual_qa_report({
            "space_id": "source-space",
            "surface": "Memory context card",
            "status": "pass",
            "screenshot_path": "/tmp/capy-spaces-progress/memory-context-card.png",
            "findings": ["Metadata-only memory context is readable"],
        }),
    ]

    for record in records:
        ingest_source(record)

    relevant = relevant_memory_for_space("source-space", limit=10)

    assert memory_status()["source_count"] == 4
    assert memory_status()["chunk_count"] == 4
    assert {item["source_type"] for item in relevant["results"]} == {
        "space_manifest",
        "space_revision_event",
        "space_widget_event",
        "visual_qa_report",
    }


def test_new_artifact_canonicalizers_do_not_stringify_non_scalar_public_fields():
    revision = canonicalize_space_revision_event({
        "space_id": "source-space",
        "event_type": {"html": "<div>body</div>"},
        "reason": ["safe", {"api_key": "SECRET_VALUE_DO_NOT_LEAK"}],
    })
    widget = canonicalize_space_widget_event({
        "space_id": "source-space",
        "widget_id": "notes-editor",
        "event_name": {"source": "renderer.source"},
        "status": ["queued", {"html": "<script>"}],
    })
    qa = canonicalize_visual_qa_report({
        "space_id": "source-space",
        "surface": "QA",
        "status": "pass",
        "findings": [{"text": "Looks ok", "html": "<div>body</div>"}],
    })

    serialized = json.dumps([revision, widget, qa], sort_keys=True).lower()
    assert "html" not in serialized
    assert "<div" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized
    assert "renderer.source" not in serialized
    assert "{'" not in serialized
    assert "[" not in revision["markdown"]


def test_new_artifact_canonicalizers_redact_unsafe_public_ids_and_count_drops():
    revision = canonicalize_space_revision_event({
        "space_id": "api_key_prod",
        "event_id": "secret-token",
        "event_type": "api_key",
    })
    widget = canonicalize_space_widget_event({
        "space_id": "bearer space",
        "widget_id": "renderer-panel",
        "event_id": "api_key_event",
        "event_name": "source.fetch",
    })

    serialized = json.dumps([revision, widget], sort_keys=True).lower()
    assert "api_key" not in serialized
    assert "secret-token" not in serialized
    assert "bearer" not in serialized
    assert "renderer-panel" not in serialized
    assert "source.fetch" not in serialized
    assert revision["space_id"] == "space"
    assert widget["space_id"] == "space"
    assert revision["redaction_status"] == "dropped_fields"
    assert widget["redaction_status"] == "dropped_fields"
    assert revision["dropped_field_count"] >= 1
    assert widget["dropped_field_count"] >= 1


def test_new_artifact_canonicalizers_do_not_fabricate_missing_ids_and_are_deterministic():
    revision_first = canonicalize_space_revision_event({"space_id": "source-space", "event_type": "space.updated"})
    revision_second = canonicalize_space_revision_event({"space_id": "source-space", "event_type": "space.updated"})
    widget = canonicalize_space_widget_event({"space_id": "source-space", "event_name": "notes.save"})

    assert revision_first["source_id"] == revision_second["source_id"]
    assert revision_first["chunk_id"] == revision_second["chunk_id"]
    assert "item" not in revision_first["markdown"]
    assert "item" not in revision_first["origin_uri"]
    assert "item" not in widget["markdown"]
    assert "item" not in widget["origin_uri"]


def test_new_artifact_canonicalizers_reject_non_scalar_paths_and_html_ids_before_normalizing():
    widget = canonicalize_space_widget_event({
        "space_id": "source-space",
        "widget_id": "<div>panel</div>",
        "event_id": "<div>event</div>",
        "event_name": "notes.save",
    })
    qa = canonicalize_visual_qa_report({
        "space_id": "source-space",
        "surface": "Visual QA",
        "status": "pass",
        "screenshot_path": {"path": "safe.png", "note": "benign"},
        "findings": "scalar finding should be dropped",
    })

    serialized = json.dumps([widget, qa], sort_keys=True).lower()
    assert "div-panel-div" not in serialized
    assert "div-event-div" not in serialized
    assert "safe.png" not in serialized
    assert "{'path'" not in serialized
    assert "scalar finding" not in serialized
    assert widget["redaction_status"] == "dropped_fields"
    assert qa["redaction_status"] == "dropped_fields"


def test_new_artifact_canonicalizers_count_sensitive_keys_without_event_handler_false_positives():
    revision = canonicalize_space_revision_event({
        "space_id": "source-space",
        "event_type": "space.updated",
        "reason": "One widget moved online",
        "api_auth": "sk-live-placeholder",
        "restore_diff": {"widgets_to_update": {"id": "notes-editor"}},
    })
    qa = canonicalize_visual_qa_report({
        "space_id": "source-space",
        "surface": "API key exposure panel",
        "status": "pass",
        "findings": [{"note": "safe object should not stringify"}],
    })

    serialized = json.dumps([revision, qa], sort_keys=True).lower()
    assert "one widget moved online" in serialized
    assert "api_auth" not in serialized
    assert "sk-live" not in serialized
    assert "api key exposure" not in serialized
    assert "safe object" not in serialized
    assert revision["redaction_status"] == "dropped_fields"
    assert qa["redaction_status"] == "dropped_fields"
    assert revision["dropped_field_count"] >= 2
    assert qa["dropped_field_count"] >= 2


def test_new_artifact_canonicalizers_count_top_level_malformed_restore_diff_and_credential_findings():
    revision = canonicalize_space_revision_event({
        "space_id": "source-space",
        "event_type": "space.updated",
        "restore_diff": "widgets_to_update notes-editor",
    })
    qa = canonicalize_visual_qa_report({
        "space_id": "source-space",
        "surface": "Visual QA",
        "status": "pass",
        "screenshot_path": "C:\\tmp\\private-folder\\memory-qa.png",
        "findings": ["No leak", "credential probe sk-live-placeholder"],
    })

    serialized = json.dumps([revision, qa], sort_keys=True).lower()
    assert "widgets_to_update notes-editor" not in serialized
    assert "sk-live" not in serialized
    assert "private-folder" not in serialized
    assert "memory-qa.png" in serialized
    assert "no leak" in serialized
    assert revision["redaction_status"] == "dropped_fields"
    assert qa["redaction_status"] == "dropped_fields"
    assert revision["dropped_field_count"] >= 1
    assert qa["dropped_field_count"] >= 1


def test_init_memory_tree_creates_expected_tables(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))

    result = init_memory_tree()

    assert result["local_only"] is True
    assert result["db_exists"] is True
    assert result["db_path"].endswith("capy-memory-tree.sqlite3")
    assert (root / "capy-memory-tree.sqlite3").exists()
    assert (root / "vault").is_dir()
    assert set(result["tables"]) >= {
        "sources",
        "chunks",
        "entities",
        "chunk_entities",
        "summary_nodes",
        "jobs",
    }


def test_memory_status_returns_local_only_counts(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()

    status = memory_status()

    assert status == {
        "available": True,
        "local_only": True,
        "db_exists": True,
        "source_count": 0,
        "chunk_count": 0,
        "stale_source_count": 0,
        "last_error_count": 0,
        "refresh_job_count": 0,
    }


def test_register_source_reference_queues_metadata_only_refresh_job(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()

    receipt = register_source_reference({
        "source_id": "openhuman-memory-tree",
        "title": "OpenHuman Memory Tree <script>bad()</script>",
        "origin_uri": "https://example.test/docs/memory-tree?api_key=SECRET_VALUE_DO_NOT_LEAK#raw-prompt",
        "refresh_interval_seconds": 3600,
        "source": "renderer body should not be stored",
    })

    jobs = list_source_refresh_jobs(limit=5)
    status = memory_status()
    serialized = json.dumps({"receipt": receipt, "jobs": jobs, "status": status}, sort_keys=True).lower()

    assert receipt["ok"] is True
    assert receipt["source_id"] == "openhuman-memory-tree"
    assert receipt["queued"] is True
    assert receipt["origin_kind"] == "auto_fetch"
    assert status["source_count"] == 1
    assert status["chunk_count"] == 0
    assert status["stale_source_count"] == 1
    assert status["refresh_job_count"] == 1
    assert len(jobs["jobs"]) == 1
    assert jobs["jobs"][0]["kind"] == "source.refresh"
    assert jobs["jobs"][0]["source_id"] == "openhuman-memory-tree"
    assert jobs["jobs"][0]["status"] == "pending"
    assert jobs["jobs"][0]["origin_uri"] == "https://example.test/docs/memory-tree"
    assert "secret_value_do_not_leak" not in serialized
    assert "api_key" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "raw-prompt" not in serialized


def test_register_source_reference_is_idempotent_by_source_id(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()

    first = register_source_reference({"source_id": "docs", "origin_uri": "https://example.test/docs"})
    second = register_source_reference({"source_id": "docs", "origin_uri": "https://example.test/docs?token=SECRET_VALUE_DO_NOT_LEAK"})

    jobs = list_source_refresh_jobs(limit=5)

    assert first["job_id"] == second["job_id"]
    assert first["queued"] is True
    assert second["queued"] is False
    assert memory_status()["refresh_job_count"] == 1
    assert [job["source_id"] for job in jobs["jobs"]] == ["docs"]


def test_register_source_reference_strips_url_credentials_and_raw_prompt_paths(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()

    receipt = register_source_reference({
        "source_id": "credential-docs",
        "origin_uri": "https://user:SECRET_VALUE_DO_NOT_LEAK@example.test/raw-prompt/notes?token=SECRET_VALUE_DO_NOT_LEAK",
    })
    jobs = list_source_refresh_jobs(limit=5)
    serialized = json.dumps({"receipt": receipt, "jobs": jobs}, sort_keys=True).lower()

    assert receipt["origin_uri"] == "https://example.test/"
    assert jobs["jobs"][0]["origin_uri"] == "https://example.test/"
    assert "user:" not in serialized
    assert "secret_value_do_not_leak" not in serialized
    assert "raw-prompt" not in serialized
    assert "token" not in serialized


def test_register_source_reference_requeues_terminal_job_status(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    first = register_source_reference({"source_id": "retry-docs", "origin_uri": "https://example.test/retry"})

    with sqlite3.connect(memory_tree_db_path()) as conn:
        conn.execute(
            "UPDATE jobs SET status = 'completed', attempts = 4, leased_until = '2099-01-01T00:00:00Z', last_error = 'SECRET_VALUE_DO_NOT_LEAK' WHERE job_id = ?",
            (first["job_id"],),
        )

    second = register_source_reference({"source_id": "retry-docs", "origin_uri": "https://example.test/retry"})
    jobs = list_source_refresh_jobs(limit=5)

    assert second["queued"] is True
    assert memory_status()["refresh_job_count"] == 1
    assert jobs["jobs"] == [{
        "job_id": first["job_id"],
        "kind": "source.refresh",
        "source_id": "retry-docs",
        "origin_uri": "https://example.test/retry",
        "status": "pending",
        "attempts": 0,
        "created_at": jobs["jobs"][0]["created_at"],
        "updated_at": jobs["jobs"][0]["updated_at"],
    }]


def test_register_source_reference_does_not_mutate_leased_job_payload(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    first = register_source_reference({"source_id": "leased-docs", "origin_uri": "https://example.test/original"})

    with sqlite3.connect(memory_tree_db_path()) as conn:
        before = conn.execute("SELECT payload_json FROM jobs WHERE job_id = ?", (first["job_id"],)).fetchone()[0]
        conn.execute(
            "UPDATE jobs SET status = 'leased', leased_until = '2099-01-01T00:00:00Z' WHERE job_id = ?",
            (first["job_id"],),
        )

    second = register_source_reference({"source_id": "leased-docs", "origin_uri": "https://example.test/changed"})

    with sqlite3.connect(memory_tree_db_path()) as conn:
        after = conn.execute("SELECT payload_json FROM jobs WHERE job_id = ?", (first["job_id"],)).fetchone()[0]

    assert second["queued"] is False
    assert after == before


def test_ingest_source_is_idempotent_by_source_id_and_hash(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    record = canonicalize_space_manifest(_hostile_space_manifest())

    first = ingest_source(record)
    second = ingest_source(record)

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["source_id"] == second["source_id"] == record["source_id"]
    assert first["chunk_id"] == second["chunk_id"] == record["chunk_id"]
    assert second["created"] is False
    assert memory_status()["source_count"] == 1
    assert memory_status()["chunk_count"] == 1
    content_path = root / "vault" / f"{record['source_id']}.md"
    assert content_path.exists()
    persisted = content_path.read_text(encoding="utf-8").lower()
    assert "daily data dashboard" in persisted
    assert "secret_value_do_not_leak" not in persisted
    assert "<script" not in persisted


def test_search_memory_returns_bounded_redacted_snippets(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    record = canonicalize_space_manifest(_hostile_space_manifest())
    ingest_source(record)

    result = search_memory("dashboard", limit=3)

    assert result["local_only"] is True
    assert result["query"] == "dashboard"
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["source_id"] == record["source_id"]
    assert hit["chunk_id"] == record["chunk_id"]
    assert hit["space_id"] == "source-space"
    assert hit["redaction_status"] == "dropped_fields"
    assert "Daily Data Dashboard" in hit["snippet"]
    serialized = json.dumps(result, sort_keys=True).lower()
    assert "secret_value_do_not_leak" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized


def test_relevant_memory_for_space_filters_by_space_id(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    source_record = canonicalize_space_manifest(_hostile_space_manifest())
    other_record = canonicalize_space_manifest({"space_id": "other-space", "name": "Other Space"})
    ingest_source(source_record)
    ingest_source(other_record)

    relevant = relevant_memory_for_space("source-space", limit=5)

    assert relevant["local_only"] is True
    assert relevant["space_id"] == "source-space"
    assert [item["source_id"] for item in relevant["results"]] == [source_record["source_id"]]


class _RouteHandler:
    def __init__(self, body=None):
        raw = json.dumps(body or {}).encode("utf-8")
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.headers = {
            "Accept-Encoding": "",
            "Host": "127.0.0.1:8787",
            "Content-Length": str(len(raw)),
            "Content-Type": "application/json",
        }
        self.status = None
        self.sent_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _route_get(path):
    import api.routes as routes

    handler = _RouteHandler()
    handled = routes.handle_get(handler, urlparse(path))
    return handled, handler.status, handler.json_body()


def _route_post(path, body):
    import api.routes as routes

    handler = _RouteHandler(body)
    handled = routes.handle_post(handler, urlparse(path))
    return handled, handler.status, handler.json_body()


def test_capy_memory_source_register_route_queues_metadata_only_refresh_job(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()

    handled, status, body = _route_post("/api/capy-memory/source/register", {
        "source_id": "roadmap-docs",
        "origin_uri": "https://example.test/roadmap?api_key=SECRET_VALUE_DO_NOT_LEAK#raw-prompt",
        "title": "Roadmap source <script>bad()</script>",
        "body": "renderer should never be echoed",
    })

    serialized = json.dumps(body, sort_keys=True).lower()
    assert handled is None
    assert status == 200
    assert body["source_id"] == "roadmap-docs"
    assert body["origin_uri"] == "https://example.test/roadmap"
    assert body["metadata_only"] is True
    assert memory_status()["refresh_job_count"] == 1
    assert "secret_value_do_not_leak" not in serialized
    assert "api_key" not in serialized
    assert "<script" not in serialized
    assert "renderer" not in serialized
    assert "raw-prompt" not in serialized


def test_capy_memory_status_route_returns_bounded_local_counts(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    ingest_source(canonicalize_space_manifest(_hostile_space_manifest()))

    handled, status, body = _route_get("/api/capy-memory/status")

    assert handled is None
    assert status == 200
    assert body == {
        "available": True,
        "local_only": True,
        "db_exists": True,
        "source_count": 1,
        "chunk_count": 1,
        "stale_source_count": 0,
        "last_error_count": 0,
        "refresh_job_count": 0,
    }


def test_capy_memory_search_route_filters_and_redacts(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    record = canonicalize_space_manifest(_hostile_space_manifest())
    ingest_source(record)
    ingest_source(canonicalize_space_manifest({"space_id": "other-space", "name": "Other Space"}))

    handled, status, body = _route_get("/api/capy-memory/search?q=dashboard&space_id=source-space&limit=2")

    assert handled is None
    assert status == 200
    assert body["query"] == "dashboard"
    assert body["space_id"] == "source-space"
    assert body["local_only"] is True
    assert [item["source_id"] for item in body["results"]] == [record["source_id"]]
    serialized = json.dumps(body, sort_keys=True).lower()
    assert "secret_value_do_not_leak" not in serialized
    assert "<script" not in serialized
    assert "api_key" not in serialized


def test_spaces_memory_route_requires_space_id_and_returns_relevant_memory(tmp_path, monkeypatch):
    root = tmp_path / "capy-memory"
    monkeypatch.setenv("CAPY_MEMORY_TREE_ROOT", str(root))
    init_memory_tree()
    record = canonicalize_space_manifest(_hostile_space_manifest())
    ingest_source(record)

    handled, status, missing = _route_get("/api/spaces/memory")
    assert handled is None
    assert status == 400
    assert "space_id" in missing["error"]

    handled, status, body = _route_get("/api/spaces/memory?spaceId=source-space&limit=5")

    assert handled is None
    assert status == 200
    assert body["space_id"] == "source-space"
    assert body["local_only"] is True
    assert [item["source_id"] for item in body["results"]] == [record["source_id"]]
