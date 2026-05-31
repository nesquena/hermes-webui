import importlib
import json
from pathlib import Path

import pytest


def _routes_source():
    return Path("api/routes.py").read_text(encoding="utf-8")


def test_docs_artifacts_payload_has_version_mode_timestamp_sources_items_and_no_execution(monkeypatch):
    docs_artifacts = importlib.import_module("api.operator_docs_artifacts")
    monkeypatch.setattr(docs_artifacts, "SOURCE_SPECS", {}, raising=False)

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=123.0)

    assert payload["version"] == 1
    assert payload["mode"] == "docs-artifacts-read-only"
    assert payload["generated_at"] == 123.0
    assert payload["would_execute"] is False
    assert payload["status"] in {"live", "stale", "unknown"}
    assert payload["summary"]
    assert payload["query"] == {"text": "", "kind": "all", "root": "all", "limit": 50}
    assert payload["sources"] == []
    assert payload["items"] == []
    assert payload["count"] == 0
    assert isinstance(payload["issues"], list)


def test_docs_artifacts_preview_payload_rejects_unknown_item_without_fake_preview(monkeypatch):
    docs_artifacts = importlib.import_module("api.operator_docs_artifacts")
    monkeypatch.setattr(docs_artifacts, "SOURCE_SPECS", {}, raising=False)

    payload = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id="missing", now=123.0)

    assert payload["version"] == 1
    assert payload["mode"] == "docs-artifacts-preview-read-only"
    assert payload["generated_at"] == 123.0
    assert payload["would_execute"] is False
    assert payload["status"] == "unknown"
    assert payload["item"]["id"] == "missing"
    assert payload["item"]["preview_available"] is False
    assert payload["preview"] == {
        "format": "metadata-only",
        "text": "",
        "truncated": False,
        "bytes_read": 0,
        "max_bytes": docs_artifacts.MAX_PREVIEW_BYTES,
    }
    assert any("missing" in issue.lower() or "unknown" in issue.lower() for issue in payload["issues"])


def test_routes_expose_docs_artifacts_get_routes_near_operator_routes():
    routes = _routes_source()

    assert '"/api/operator/docs-artifacts"' in routes
    assert '"/api/operator/docs-artifacts/open"' in routes
    assert "build_operator_docs_artifacts_payload" in routes
    assert "build_operator_docs_artifact_preview_payload" in routes

    session_recall_index = routes.index('parsed.path == "/api/operator/session-recall"')
    docs_index = routes.index('parsed.path == "/api/operator/docs-artifacts"')
    docs_open_index = routes.index('parsed.path == "/api/operator/docs-artifacts/open"')
    models_index = routes.index('parsed.path == "/api/models"')
    post_index = routes.index('def handle_post(')

    assert session_recall_index < docs_index < docs_open_index < models_index
    assert docs_open_index < post_index
    post_section = routes[post_index:]
    assert '"/api/operator/docs-artifacts"' not in post_section
    assert '"/api/operator/docs-artifacts/open"' not in post_section


def _docs_artifacts_module(monkeypatch, source_specs):
    docs_artifacts = importlib.import_module("api.operator_docs_artifacts")
    monkeypatch.setattr(docs_artifacts, "SOURCE_SPECS", source_specs, raising=False)
    return docs_artifacts


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(path, data):
    return _write(path, json.dumps(data))


def _catalog_source_specs(tmp_path):
    root = tmp_path / "workspace"
    plans = root / ".hermes" / "plans"
    research = root / "obsidian-vault" / "Agent-Kimi" / "Deep Research Briefs"
    active_plan = root / "obsidian-vault" / "Agent-Shared" / "ACTIVE PLAN.md"
    artifacts = root / "artifacts"
    state = root / "state"
    changelog = tmp_path / "webui" / "CHANGELOG.md"

    _write(plans / "2026-05-26_204427-hermes-mission-control-execution-strategy.md", "# Hermes Mission Control Execution Strategy\nreal plan body should stay out of list cards\n")
    _write(plans / "2026-05-27_103306-slice-7-docs-artifact-browser-handoff.md", "# Slice 7 Docs/Artifact Browser TDD Handoff\nfull handoff body should stay out of list cards\n")
    _write(research / "agentic-browser-brief.md", "# Agentic Browser Brief\nbrief body should stay out of list cards\n")
    _write_json(research / "manifest.json", {"generated_at": "2026-05-27T10:00:00Z", "briefs": ["agentic-browser-brief.md"]})
    _write(research / "raw" / "transcript.txt", "raw transcript must not be cataloged\n")
    _write(active_plan, "# ACTIVE PLAN\nsource-backed current plan\n")
    _write_json(artifacts / "hermes-video-RoBD7Lc-0MI" / "action-summary.json", {"ranked_actions": [1, 2, 3], "avoid": ["fake data"]})
    _write_json(artifacts / "hermes-video-RoBD7Lc-0MI" / "manifest.json", {"artifact_dir": "real-artifacts"})
    _write(artifacts / "hermes-video-RoBD7Lc-0MI" / "raw" / "transcript.txt", "raw transcript must not be cataloged\n")
    _write_json(state / "hermes_reverse_prompt_latest.json", {"ok": True})
    _write_json(state / "hermes_youtube_recommendations_2026-05-20.json", {"ok": True})
    _write_json(state / "hermes_operator_kanban_hardening_2026-05-20.json", {"ok": True})
    _write_json(state / "local_model_probe_2026-05-20.json", {"excluded": True})
    _write(changelog, "## [Unreleased]\n\n### Added\n\n- Existing entry.\n")

    return {
        "plans": {
            "type": "directory",
            "label": "Plans / handoffs",
            "path": plans,
            "display_path": ".hermes/plans",
            "includes": [{"glob": "*.md", "kind": "auto"}],
        },
        "deep_research_briefs": {
            "type": "directory",
            "label": "Deep research briefs",
            "path": research,
            "display_path": "obsidian-vault/Agent-Kimi/Deep Research Briefs",
            "includes": [{"glob": "*.md", "kind": "brief"}, {"glob": "manifest.json", "kind": "artifact_manifest"}],
        },
        "agent_shared_active_plan": {
            "type": "file",
            "label": "Shared active plan",
            "path": active_plan,
            "display_path": "obsidian-vault/Agent-Shared/ACTIVE PLAN.md",
            "kind": "plan",
        },
        "generated_artifacts": {
            "type": "directory",
            "label": "Generated artifacts",
            "path": artifacts,
            "display_path": "artifacts",
            "includes": [{"glob": "*/action-summary.json", "kind": "action_summary"}, {"glob": "*/manifest.json", "kind": "artifact_manifest"}],
        },
        "state_summaries": {
            "type": "directory",
            "label": "Selected state summaries",
            "path": state,
            "display_path": "state",
            "includes": [
                {"path": "hermes_reverse_prompt_latest.json", "kind": "state_summary"},
                {"path": "hermes_youtube_recommendations_2026-05-20.json", "kind": "state_summary"},
                {"path": "hermes_operator_kanban_hardening_2026-05-20.json", "kind": "state_summary"},
            ],
        },
        "webui_changelog": {
            "type": "file",
            "label": "WebUI changelog",
            "path": changelog,
            "display_path": "CHANGELOG.md",
            "kind": "changelog",
        },
    }


def test_docs_artifacts_catalogs_allowlisted_plans_briefs_artifact_json_state_and_changelog_metadata(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(monkeypatch, _catalog_source_specs(tmp_path))

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1234567890.0)

    assert payload["would_execute"] is False
    assert payload["count"] == len(payload["items"])
    assert payload["items"]
    assert {source["id"] for source in payload["sources"]} == {
        "plans",
        "deep_research_briefs",
        "agent_shared_active_plan",
        "generated_artifacts",
        "state_summaries",
        "webui_changelog",
    }

    by_key = {(item["root_id"], item["relative_path"]): item for item in payload["items"]}
    expected = {
        ("plans", "2026-05-26_204427-hermes-mission-control-execution-strategy.md"),
        ("plans", "2026-05-27_103306-slice-7-docs-artifact-browser-handoff.md"),
        ("deep_research_briefs", "agentic-browser-brief.md"),
        ("deep_research_briefs", "manifest.json"),
        ("agent_shared_active_plan", "ACTIVE PLAN.md"),
        ("generated_artifacts", "hermes-video-RoBD7Lc-0MI/action-summary.json"),
        ("generated_artifacts", "hermes-video-RoBD7Lc-0MI/manifest.json"),
        ("state_summaries", "hermes_reverse_prompt_latest.json"),
        ("state_summaries", "hermes_youtube_recommendations_2026-05-20.json"),
        ("state_summaries", "hermes_operator_kanban_hardening_2026-05-20.json"),
        ("webui_changelog", "CHANGELOG.md"),
    }
    assert expected <= set(by_key)
    assert ("state_summaries", "local_model_probe_2026-05-20.json") not in by_key

    kinds = {item["kind"] for item in payload["items"]}
    assert {"meta_plan", "handoff", "brief", "artifact_manifest", "action_summary", "state_summary", "changelog"} <= kinds

    for item in payload["items"]:
        for key in ["id", "kind", "root_id", "title", "relative_path", "display_path", "extension", "size_bytes", "mtime", "freshness", "preview_available", "metadata", "issues"]:
            assert key in item
        assert item["id"].startswith("da_")
        assert isinstance(item["size_bytes"], int)
        assert isinstance(item["mtime"], float)
        assert item["freshness"]["label"] in {"current", "historical", "stale", "unknown"}
        assert "body should stay out of list cards" not in json.dumps(item)
        assert "raw transcript" not in json.dumps(item)


def test_docs_artifacts_missing_root_is_unknown_not_fake_card(monkeypatch, tmp_path):
    specs = {
        "workspace_assets": {
            "type": "directory",
            "label": "Workspace assets",
            "path": tmp_path / "missing-assets",
            "display_path": "assets",
            "includes": [{"glob": "*.md", "kind": "artifact_manifest"}],
        }
    }
    docs_artifacts = _docs_artifacts_module(monkeypatch, specs)

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)

    assert payload["status"] == "unknown"
    assert payload["items"] == []
    assert payload["count"] == 0
    assert payload["sources"][0]["id"] == "workspace_assets"
    assert payload["sources"][0]["state"] == "unknown"
    assert "missing" in payload["sources"][0]["issue"].lower()
    assert any("workspace_assets" in issue and "missing" in issue.lower() for issue in payload["issues"])


def test_docs_artifacts_excludes_raw_memory_sessions_attachments_and_config_dirs(monkeypatch, tmp_path):
    source_root = tmp_path / "source"
    allowed = _write(source_root / "visible.md", "# Visible\n")
    for rel in [
        "raw/transcript.md",
        ".obsidian/workspace.json",
        "memory/2026-05-27.md",
        "memory-ledger/claims.json",
        "sessions/session.json",
        "attachments/image.txt",
        "node_modules/pkg/index.js",
        ".git/config",
        "__pycache__/x.pyc",
    ]:
        _write(source_root / rel, "excluded\n")
    specs = {
        "broad_test_root": {
            "type": "directory",
            "label": "Broad test root",
            "path": source_root,
            "display_path": "source",
            "includes": [{"glob": "**/*", "kind": "plan"}],
        }
    }
    docs_artifacts = _docs_artifacts_module(monkeypatch, specs)

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)

    assert [item["relative_path"] for item in payload["items"]] == [allowed.name]
    serialized = json.dumps(payload)
    for forbidden in ["raw/", ".obsidian", "memory/", "memory-ledger", "sessions/", "attachments/", "node_modules", ".git", "__pycache__"]:
        assert forbidden not in serialized


def test_docs_artifacts_filters_query_kind_root_and_clamps_limit(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(monkeypatch, _catalog_source_specs(tmp_path))

    payload = docs_artifacts.build_operator_docs_artifacts_payload(
        query_text="slice-7",
        kind="handoff",
        root="plans",
        limit="500",
        now=1.0,
    )

    assert payload["query"] == {"text": "slice-7", "kind": "handoff", "root": "plans", "limit": 100}
    assert payload["count"] == 1
    assert payload["items"][0]["root_id"] == "plans"
    assert payload["items"][0]["kind"] == "handoff"
    assert "slice-7" in payload["items"][0]["relative_path"]

    low_limit = docs_artifacts.build_operator_docs_artifacts_payload(limit="0", now=1.0)
    assert low_limit["query"]["limit"] == 1
    assert low_limit["count"] <= 1


def test_docs_artifacts_redacts_secretish_titles_paths_issues(monkeypatch, tmp_path):
    source_root = tmp_path / "secret-source"
    secrets = [
        "password=supersecretvalue",
        "sk-abcdefghijklmnop",
        "xoxb-abcdefghijklmnop",
        "ghp_abcdefghijklmnop",
        "github_pat_abcdefghijklmnop",
    ]
    for secret in secrets:
        _write(source_root / f"{secret}.md", f"# {secret}\n")
    specs = {
        "secret_root": {
            "type": "directory",
            "label": "Bearer abcdefghijklmnop",
            "path": source_root,
            "display_path": "secret-root",
            "includes": [{"glob": "*.md", "kind": "plan"}],
        }
    }
    docs_artifacts = _docs_artifacts_module(monkeypatch, specs)

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)
    serialized = json.dumps(payload)

    for secret in [*secrets, "Bearer abcdefghijklmnop"]:
        assert secret not in serialized
    assert "[redacted]" in serialized


def test_docs_artifacts_redacts_normalized_titles_json_keys_and_omitted_display_path(monkeypatch, tmp_path):
    source_root = tmp_path / "secret-source"
    _write(source_root / "sk-abc...wxyz.md", "# secret\n")
    _write(source_root / "xoxb-a...wxyz.md", "# secret\n")
    _write(source_root / "github...wxyz.md", "# secret\n")
    _write(source_root / "api_key=abcdefghijklmnopqrstuvwxyz.md", "# secret\n")
    _write_json(source_root / "manifest.json", {"api_key=abcdefghijklmnopqrstuvwxyz": 1, "safe": 2})
    specs = {
        "no_display": {
            "type": "directory",
            "label": "No display root",
            "path": source_root,
            "includes": [{"glob": "*", "kind": "auto"}],
        }
    }
    docs_artifacts = _docs_artifacts_module(monkeypatch, specs)

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)
    serialized = json.dumps(payload)

    assert str(source_root) not in serialized
    for leaked in [
        "Sk Abcdefghijklmnopqrstuvwxyz",
        "Xoxb Abcdefghijklmnopqrstuvwxyz",
        "Github Pat Abcdefghijklmnopqrstuvwxyz",
        "Api Key=abcdefghijklmnopqrstuvwxyz",
        "api_key=abcdefghijklmnopqrstuvwxyz",
    ]:
        assert leaked not in serialized
    assert "[redacted]" in serialized


def test_docs_artifacts_redacts_json_metadata_path_keys_and_prefixed_secret_keys(monkeypatch, tmp_path):
    source_root = tmp_path / "json-source"
    _write_json(
        source_root / "manifest.json",
        {
            "/tmp/Secret Folder/manifest.md": 1,
            r"C:\Users\malac\Secret Folder\manifest.md": 2,
            "OPENAI_API_KEY": 3,
            "safe": 4,
        },
    )
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "json_source": {
                "type": "directory",
                "label": "JSON source",
                "path": source_root,
                "display_path": "json-source",
                "includes": [{"glob": "manifest.json", "kind": "artifact_manifest"}],
            }
        },
    )

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)
    item = payload["items"][0]
    serialized_keys = json.dumps(item["metadata"]["json_keys"])

    assert "safe" in item["metadata"]["json_keys"]
    for leaked in ["/tmp/Secret Folder", r"C:\Users", "Secret Folder", "OPENAI_API_KEY"]:
        assert leaked not in serialized_keys
    assert "[path]" in serialized_keys
    assert "[redacted]" in serialized_keys


def test_docs_artifacts_rejects_source_root_symlink_escape(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "secret.md", "# outside\n")
    root_link = tmp_path / "root-link"
    try:
        root_link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unsupported on this platform")
    specs = {
        "symlink_root": {
            "type": "directory",
            "label": "Symlink root",
            "path": root_link,
            "display_path": "symlink-root",
            "includes": [{"glob": "*.md", "kind": "plan"}],
        }
    }
    docs_artifacts = _docs_artifacts_module(monkeypatch, specs)

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)
    serialized = json.dumps(payload)

    assert payload["items"] == []
    assert payload["sources"][0]["state"] == "unknown"
    assert "symlink" in payload["sources"][0]["issue"].lower()
    assert "secret.md" not in serialized
    assert str(outside) not in serialized


def test_docs_artifacts_unreadable_file_issue_does_not_leak_absolute_path(monkeypatch, tmp_path):
    source_root = tmp_path / "source"
    denied = _write(source_root / "denied.md", "# denied\n")
    try:
        denied.chmod(0)
    except OSError:
        pytest.skip("chmod unsupported on this platform")
    specs = {
        "permission_root": {
            "type": "directory",
            "label": "Permission root",
            "path": source_root,
            "display_path": "permission-root",
            "includes": [{"glob": "*.md", "kind": "plan"}],
        }
    }
    docs_artifacts = _docs_artifacts_module(monkeypatch, specs)

    try:
        payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)
    finally:
        denied.chmod(0o600)
    serialized = json.dumps(payload)
    if "unreadable" not in serialized and "Permission" not in serialized:
        pytest.skip("platform allowed reading chmod-000 file")

    assert str(source_root) not in serialized
    assert str(denied) not in serialized
    assert "denied.md" in serialized


def test_docs_artifacts_short_error_redacts_windows_absolute_paths(monkeypatch):
    docs_artifacts = _docs_artifacts_module(monkeypatch, {})
    raw = "Permission denied: 'C:\\Users\\malac\\Secret Folder\\denied.md'"

    message = docs_artifacts._short_error(PermissionError(raw))

    assert "C:\\" not in message
    assert "Users" not in message
    assert "Secret Folder" not in message
    assert "denied.md" not in message
    assert "[path]" in message


def test_docs_artifacts_short_error_redacts_posix_paths_with_spaces_and_unc_paths(monkeypatch):
    docs_artifacts = _docs_artifacts_module(monkeypatch, {})
    raw = "Failed '/tmp/Secret Folder/denied.md' and '\\\\server\\share\\Secret Folder\\denied.md'"

    message = docs_artifacts._short_error(PermissionError(raw))

    assert "/tmp" not in message
    assert "\\\\server" not in message
    assert "Secret Folder" not in message
    assert "denied.md" not in message
    assert message.count("[path]") >= 2


def test_docs_artifacts_preview_reads_bounded_redacted_text_for_catalog_item(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "text_root": {
                "type": "directory",
                "label": "Text root",
                "path": tmp_path / "docs",
                "display_path": "docs",
                "includes": [{"glob": "*.md", "kind": "plan"}],
            }
        },
    )
    monkeypatch.setattr(docs_artifacts, "MAX_PREVIEW_BYTES", 72, raising=False)
    _write(tmp_path / "docs" / "note.md", "needle Bearer abcdefghijklmnop " + "x" * 200)
    item = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)["items"][0]

    payload = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id=item["id"], now=1.0)

    assert payload["mode"] == "docs-artifacts-preview-read-only"
    assert payload["would_execute"] is False
    assert payload["status"] == "live"
    assert payload["item"]["id"] == item["id"]
    assert payload["preview"]["format"] == "text"
    assert "needle" in payload["preview"]["text"]
    assert "Bearer abcdefghijklmnop" not in payload["preview"]["text"]
    assert "[redacted]" in payload["preview"]["text"]
    assert payload["preview"]["truncated"] is True
    assert payload["preview"]["bytes_read"] == 72
    assert payload["preview"]["max_bytes"] == 72


def test_docs_artifacts_preview_redacts_local_paths_and_prefixed_env_secrets(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "text_root": {
                "type": "directory",
                "label": "Text root",
                "path": tmp_path / "docs",
                "display_path": "docs",
                "includes": [{"glob": "*.md", "kind": "plan"}],
            }
        },
    )
    body = "\n".join(
        [
            "keep needle",
            "unix path /tmp/Secret Folder/brief.md",
            r"windows path C:\Users\malac\Secret Folder\brief.md",
            "OPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz",
            "HERMES_TOKEN=supersecrettokenvalue",
        ]
    )
    _write(tmp_path / "docs" / "paths-and-secrets.md", body)
    item = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)["items"][0]

    payload = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id=item["id"], now=1.0)
    text = payload["preview"]["text"]

    assert payload["status"] == "live"
    assert payload["preview"]["format"] == "text"
    assert "keep needle" in text
    for leaked in ["/tmp/Secret Folder", r"C:\Users", "Secret Folder", "abcdefghijklmnopqrstuvwxyz", "supersecrettokenvalue"]:
        assert leaked not in text
    assert "[path]" in text
    assert "[redacted]" in text


def test_docs_artifacts_preview_json_summarizes_known_action_summary_without_dumping_raw_values(monkeypatch, tmp_path):
    long_raw = "raw-value-" * 80
    action_summary = {
        "ranked_actions": [{"id": "a"}, {"id": "b"}],
        "avoid": ["fake data"],
        "brief_path": "/tmp/Secret Folder/brief.md",
        "artifact_dir": "/tmp/Secret Folder/artifacts",
        "raw_transcript": long_raw,
    }
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "generated_artifacts": {
                "type": "directory",
                "label": "Generated artifacts",
                "path": tmp_path / "artifacts",
                "display_path": "artifacts",
                "includes": [{"glob": "*/action-summary.json", "kind": "action_summary"}],
            }
        },
    )
    _write_json(tmp_path / "artifacts" / "video" / "action-summary.json", action_summary)
    item = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)["items"][0]

    payload = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id=item["id"], now=1.0)

    assert payload["status"] == "live"
    assert payload["preview"]["format"] == "json-summary"
    text = payload["preview"]["text"]
    assert "ranked_actions: 2" in text
    assert "avoid: 1" in text
    assert "brief_path:" in text
    assert "artifact_dir:" in text
    assert long_raw not in text
    assert "/tmp/Secret Folder" not in text


def test_docs_artifacts_preview_malformed_json_is_unknown_metadata_only_and_marks_item_unavailable(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "generated_artifacts": {
                "type": "directory",
                "label": "Generated artifacts",
                "path": tmp_path / "artifacts",
                "display_path": "artifacts",
                "includes": [{"glob": "*/action-summary.json", "kind": "action_summary"}],
            }
        },
    )
    _write(tmp_path / "artifacts" / "video" / "action-summary.json", "{not-json raw body must not leak")
    item = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)["items"][0]

    payload = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id=item["id"], now=1.0)

    assert payload["status"] == "unknown"
    assert payload["item"]["preview_available"] is False
    assert payload["preview"] == {
        "format": "metadata-only",
        "text": "",
        "truncated": False,
        "bytes_read": 0,
        "max_bytes": docs_artifacts.MAX_PREVIEW_BYTES,
    }
    assert "raw body must not leak" not in json.dumps(payload)
    assert any("malformed json" in issue.lower() or "preview unavailable" in issue.lower() for issue in payload["issues"])


def test_docs_artifacts_preview_rejects_path_traversal_absolute_url_tilde_windows_drive_and_nul(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(monkeypatch, _catalog_source_specs(tmp_path))

    for item_id in ["../secret", "/etc/passwd", "~/.ssh/id_rsa", "C:\\Users\\malac\\secret.txt", "file:///tmp/secret", "da_bad\x00id"]:
        payload = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id=item_id, now=1.0)
        serialized = json.dumps(payload)
        assert payload["status"] == "unknown"
        assert payload["preview"]["format"] == "metadata-only"
        assert payload["preview"]["text"] == ""
        assert payload["would_execute"] is False
        assert "etc/passwd" not in serialized
        assert "Users" not in serialized
        assert any("rejected" in issue.lower() or "unknown" in issue.lower() for issue in payload["issues"])


def test_docs_artifacts_preview_rejects_symlink_escape(monkeypatch, tmp_path):
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    _write(outside / "escape.md", "# outside\n")
    root.mkdir(parents=True, exist_ok=True)
    link = root / "escape.md"
    try:
        link.symlink_to(outside / "escape.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unsupported on this platform")
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "allowed": {
                "type": "directory",
                "label": "Allowed",
                "path": root,
                "display_path": "allowed",
                "includes": [{"glob": "*.md", "kind": "plan"}],
            }
        },
    )

    catalog = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)
    guessed_id = docs_artifacts._stable_item_id("allowed", "escape.md")
    preview = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id=guessed_id, now=1.0)

    assert catalog["items"] == []
    assert preview["status"] == "unknown"
    assert preview["preview"]["format"] == "metadata-only"
    assert str(outside) not in json.dumps(preview)


def test_docs_artifacts_preview_oversize_file_is_metadata_only(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "docs": {
                "type": "directory",
                "label": "Docs",
                "path": tmp_path / "docs",
                "display_path": "docs",
                "includes": [{"glob": "*.md", "kind": "plan"}],
            }
        },
    )
    monkeypatch.setattr(docs_artifacts, "MAX_SOURCE_BYTES", 20, raising=False)
    _write(tmp_path / "docs" / "large.md", "x" * 80)
    item = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)["items"][0]

    payload = docs_artifacts.build_operator_docs_artifact_preview_payload(item_id=item["id"], now=1.0)

    assert item["preview_available"] is False
    assert payload["status"] == "unknown"
    assert payload["preview"] == {
        "format": "metadata-only",
        "text": "",
        "truncated": False,
        "bytes_read": 0,
        "max_bytes": docs_artifacts.MAX_PREVIEW_BYTES,
    }
    assert any("preview unavailable" in issue.lower() or "exceeds" in issue.lower() for issue in payload["issues"])


def test_docs_artifacts_catalog_malformed_json_is_unknown_not_green_or_previewable(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "generated_artifacts": {
                "type": "directory",
                "label": "Generated artifacts",
                "path": tmp_path / "artifacts",
                "display_path": "artifacts",
                "includes": [{"glob": "*/action-summary.json", "kind": "action_summary"}],
            }
        },
    )
    _write(tmp_path / "artifacts" / "video" / "action-summary.json", "{not-json raw body must not leak")

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)

    serialized = json.dumps(payload)
    assert payload["status"] == "unknown"
    assert payload["sources"][0]["state"] == "unknown"
    assert payload["items"]
    item = payload["items"][0]
    assert item["preview_available"] is False
    assert item["freshness"]["label"] in {"unknown", "stale"}
    assert any("malformed json" in issue.lower() for issue in item["issues"])
    assert any("malformed json" in issue.lower() for issue in payload["issues"])
    assert "raw body must not leak" not in serialized


def test_docs_artifacts_exact_include_missing_file_reports_issue_even_when_other_files_exist(monkeypatch, tmp_path):
    docs_artifacts = _docs_artifacts_module(
        monkeypatch,
        {
            "state_summaries": {
                "type": "directory",
                "label": "Selected state summaries",
                "path": tmp_path / "state",
                "display_path": "state",
                "includes": [
                    {"path": "present.json", "kind": "state_summary"},
                    {"path": "missing.json", "kind": "state_summary"},
                ],
            }
        },
    )
    _write_json(tmp_path / "state" / "present.json", {"ok": True})

    payload = docs_artifacts.build_operator_docs_artifacts_payload(now=1.0)

    serialized = json.dumps(payload)
    assert payload["status"] == "unknown"
    assert payload["sources"][0]["state"] == "unknown"
    assert payload["count"] == 1
    assert payload["items"][0]["relative_path"] == "present.json"
    assert "missing.json" in serialized
    assert str(tmp_path) not in serialized
    assert any("missing" in issue.lower() and "missing.json" in issue for issue in payload["issues"])
