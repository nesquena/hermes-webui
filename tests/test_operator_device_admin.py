import importlib
import json
from pathlib import Path


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _device_admin_module(monkeypatch, tmp_path, allowlist=None, receipts=None, allowlist_text=None, receipts_text=None):
    device_admin = importlib.import_module("api.operator_device_admin")
    allowlist_path = tmp_path / "state" / "operator_device_admin_allowlist.json"
    receipt_log_path = tmp_path / "state" / "operator_device_admin_receipts.jsonl"
    if allowlist is not None:
        _write_json(allowlist_path, allowlist)
    elif allowlist_text is not None:
        _write_text(allowlist_path, allowlist_text)
    if receipts is not None:
        _write_text(receipt_log_path, "\n".join(json.dumps(receipt) for receipt in receipts))
    elif receipts_text is not None:
        _write_text(receipt_log_path, receipts_text)
    monkeypatch.setattr(device_admin, "ALLOWLIST_PATH", allowlist_path, raising=False)
    monkeypatch.setattr(device_admin, "RECEIPT_LOG_PATH", receipt_log_path, raising=False)
    return device_admin


def test_device_admin_payload_missing_sources_is_unknown_blocked_and_no_execution(monkeypatch, tmp_path):
    device_admin = _device_admin_module(monkeypatch, tmp_path)

    payload = device_admin.build_operator_device_admin_payload(now=123.0)

    assert payload["version"] == 1
    assert payload["mode"] == "device-admin-foundations-read-only"
    assert payload["generated_at"] == 123.0
    assert payload["status"] == "unknown"
    assert payload["execution_state"] == "blocked"
    assert payload["summary"]
    assert payload["query"] == {"text": "", "host": "all", "action": "all", "limit": 50}
    assert payload["would_execute"] is False
    assert payload["hosts"] == []
    assert payload["paths"] == []
    assert payload["dry_runs"] == []
    assert payload["receipts"] == []
    assert payload["approval_model"]["required"] is True
    assert payload["approval_model"]["per_action"] is True
    assert payload["approval_model"]["execution_enabled"] is False
    assert "action_id" in payload["approval_model"]["required_fields"]
    assert "approved_by" in payload["approval_model"]["required_fields"]
    assert any(source["id"] == "allowlist" and source["state"] == "unknown" for source in payload["sources"])
    assert any(source["id"] == "receipts" and source["state"] == "unknown" for source in payload["sources"])
    assert any("allowlist" in issue.lower() and "missing" in issue.lower() for issue in payload["issues"])
    assert any("receipt" in issue.lower() and "missing" in issue.lower() for issue in payload["issues"])


def test_device_admin_preview_empty_id_is_unknown_blocked_and_no_execution(monkeypatch, tmp_path):
    device_admin = _device_admin_module(monkeypatch, tmp_path)

    payload = device_admin.build_operator_device_admin_preview_payload(action_id="", now=123.0)

    assert payload["version"] == 1
    assert payload["mode"] == "device-admin-dry-run-preview-read-only"
    assert payload["generated_at"] == 123.0
    assert payload["status"] == "unknown"
    assert payload["execution_state"] == "blocked"
    assert payload["would_execute"] is False
    assert payload["action"] == {"id": "", "action": "unknown", "summary": ""}
    assert payload["preview"] == {
        "format": "dry-run-summary",
        "text": "No device action was executed. Approval and execution are disabled in Slice 8.",
        "truncated": False,
        "bytes_read": 0,
        "max_bytes": device_admin.MAX_PREVIEW_BYTES,
    }
    assert any("missing" in issue.lower() or "unknown" in issue.lower() for issue in payload["issues"])


def test_routes_expose_device_admin_get_routes_after_docs_artifacts_and_no_post():
    routes = Path("api/routes.py").read_text(encoding="utf-8")

    assert '"/api/operator/device-admin"' in routes
    assert '"/api/operator/device-admin/preview"' in routes
    assert routes.index('"/api/operator/docs-artifacts/open"') < routes.index('"/api/operator/device-admin"') < routes.index('"/api/models"')
    assert routes.index('"/api/operator/device-admin"') < routes.index('"/api/operator/device-admin/preview"') < routes.index('"/api/models"')
    post_index = routes.index("def handle_post")
    post_section = routes[post_index:]
    assert '"/api/operator/device-admin"' not in post_section
    assert '"/api/operator/device-admin/preview"' not in post_section
    for forbidden in ["device-admin/apply", "device-admin/approve", "device-admin/execute", "device-admin/run"]:
        assert forbidden not in routes


def test_device_admin_catalogs_source_backed_hosts_paths_and_dry_runs(monkeypatch, tmp_path):
    raw_path = "/mnt/storage/inbox"
    allowlist = {
        "version": 1,
        "generated_at": "2026-05-27T00:00:00Z",
        "hosts": [
            {
                "id": "h1",
                "label": "Host One",
                "kind": "linux",
                "state": "allowed",
                "paths": [
                    {
                        "id": "p1",
                        "label": "Inbox",
                        "path": raw_path,
                        "capabilities": ["read", "copy_preview"],
                        "state": "allowed",
                    }
                ],
            }
        ],
        "proposed_actions": [
            {
                "id": "a1",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p1",
                "summary": "copy preview only",
                "reason": "operator requested a dry-run only",
                "risk": "low",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=1.0)
    payload_text = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "live"
    assert payload["execution_state"] == "blocked"
    assert payload["would_execute"] is False
    assert payload["hosts"][0]["id"] == "h1"
    assert payload["hosts"][0]["label"] == "Host One"
    assert payload["paths"][0]["id"] == "p1"
    assert payload["paths"][0]["host_id"] == "h1"
    assert payload["paths"][0]["label"] == "Inbox"
    assert raw_path not in payload_text
    assert "[redacted" in payload_text
    dry_run = payload["dry_runs"][0]
    assert dry_run["id"].startswith("dda_")
    assert dry_run["source_action_id"] == "a1"
    assert dry_run["action"] == "copy"
    assert dry_run["host_id"] == "h1"
    assert dry_run["approval_required"] is True
    assert dry_run["state"] == "blocked"
    assert dry_run["would_execute"] is False


def test_device_admin_malformed_allowlist_is_unknown_without_raw_body_leak(monkeypatch, tmp_path):
    device_admin = _device_admin_module(
        monkeypatch,
        tmp_path,
        allowlist_text='{not json /mnt/secret OPENAI_API_KEY=sk-mal...cret}',
        receipts=[],
    )

    payload = device_admin.build_operator_device_admin_payload(now=2.0)
    payload_text = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "unknown"
    assert payload["execution_state"] == "blocked"
    assert payload["hosts"] == []
    assert payload["paths"] == []
    assert payload["dry_runs"] == []
    assert any("malformed" in issue.lower() for issue in payload["issues"])
    assert "{not json" not in payload_text
    assert "/mnt/secret" not in payload_text
    assert "OPENAI_API_KEY" not in payload_text
    assert "sk-mal...cret" not in payload_text


def test_device_admin_stale_allowlist_and_receipts_are_stale_blocked_not_live(monkeypatch, tmp_path):
    allowlist = {
        "version": 1,
        "generated_at": "1970-01-01T00:00:00Z",
        "hosts": [
            {
                "id": "h1",
                "label": "Host One",
                "kind": "linux",
                "state": "allowed",
                "paths": [{"id": "p1", "label": "One", "path": "/safe", "capabilities": ["read"], "state": "allowed"}],
            }
        ],
        "proposed_actions": [
            {
                "id": "a1",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p1",
                "summary": "stale source should not look live",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }
    receipts = [
        {"id": "r1", "action_id": "a1", "status": "dry_run", "created_at": "1970-01-01T00:00:00Z", "summary": "old dry-run", "would_execute": False}
    ]
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=receipts)

    payload = device_admin.build_operator_device_admin_payload(now=2_000_000_000.0)
    preview = device_admin.build_operator_device_admin_preview_payload(action_id=payload["dry_runs"][0]["id"], now=2_000_000_000.0)

    assert payload["status"] == "stale"
    assert payload["execution_state"] == "blocked"
    assert payload["would_execute"] is False
    assert payload["sources"][0]["id"] == "allowlist"
    assert payload["sources"][0]["state"] == "stale"
    assert payload["sources"][1]["id"] == "receipts"
    assert payload["sources"][1]["state"] == "stale"
    assert any("stale" in issue.lower() for issue in payload["issues"])
    assert preview["status"] == "stale"
    assert preview["execution_state"] == "blocked"
    assert preview["would_execute"] is False



def test_device_admin_redacts_paths_and_secretish_values_from_catalog(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "id": "h-secret",
                "label": "Bearer abcdefghijklmnop",
                "kind": "windows",
                "state": "allowed",
                "paths": [
                    {
                        "id": "p-secret",
                        "label": "OPENAI_API_KEY=sk-abcdefghijklmnop",
                        "path": "C:\\Users\\malac\\Secrets\\file.txt",
                        "capabilities": ["read"],
                        "state": "allowed",
                    }
                ],
            }
        ],
        "proposed_actions": [
            {
                "id": "a-secret",
                "action": "copy",
                "host_id": "h-secret",
                "source_path_id": "p-secret",
                "destination_path_id": "p-secret",
                "summary": "copy ghp_abcdefghijklmnop from /etc/passwd",
                "reason": "token=xoxb-abcdefghijklmnop and password=hunter2",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.0)
    payload_text = json.dumps(payload, sort_keys=True)

    for leaked in [
        "Bearer abcdefghijklmnop",
        "OPENAI_API_KEY",
        "sk-abcdefghijklmnop",
        "C:\\Users\\malac\\Secrets",
        "ghp_abcdefghijklmnop",
        "/etc/passwd",
        "xoxb-abcdefghijklmnop",
        "hunter2",
    ]:
        assert leaked not in payload_text
    assert "[redacted" in payload_text
    assert payload["would_execute"] is False


def test_device_admin_redacts_path_and_secret_variants_from_catalog(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "id": "h1",
                "label": "Host One",
                "kind": "windows",
                "state": "allowed",
                "paths": [
                    {"id": "p1", "label": "Drive path", "path": "C:/Users/malac/Secrets/file.txt", "capabilities": ["read"], "state": "allowed"},
                    {"id": "p2", "label": "Spaced drive path", "path": "C:\\Users\\malac\\Secret Folder\\file.txt", "capabilities": ["read"], "state": "allowed"},
                    {"id": "p3", "label": "Spaced POSIX path", "path": "/mnt/storage/My Drive/file.txt", "capabilities": ["read"], "state": "allowed"},
                    {"id": "p4", "label": "UNC path", "path": "\\\\nas\\Secret Share\\file.txt", "capabilities": ["read"], "state": "allowed"},
                ],
            }
        ],
        "proposed_actions": [
            {
                "id": "a1",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p2",
                "summary": "copy with password: hunter2 and token abcdefghijklmnop",
                "reason": "quoted password=\"hunter2\" should be hidden",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.5)
    payload_text = json.dumps(payload, sort_keys=True)

    for leaked in [
        "C:/Users/malac/Secrets",
        "C:\\Users\\malac\\Secret Folder",
        "/mnt/storage/My Drive",
        "\\\\nas\\Secret Share",
        "hunter2",
        "token abcdefghijklmnop",
    ]:
        assert leaked not in payload_text
    assert payload_text.count("[redacted") >= 4


def test_device_admin_redacts_delimiter_path_suffixes_and_generic_env_secrets(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "id": "h1",
                "label": "AWS_ACCESS_KEY_ID=AKIAULTRALEAKVALUE",
                "kind": "linux",
                "state": "allowed",
                "paths": [
                    {"id": "p1", "label": "PRIVATE_KEY=abcdef1234567890", "path": "/tmp/project,ultraleaksecret.txt", "capabilities": ["read"], "state": "allowed"},
                    {"id": "p2", "label": "DB_PASS=letmeinvalue", "path": "C:\\Users\\malac\\Secrets;winleaksecret.txt", "capabilities": ["read"], "state": "allowed"},
                    {"id": "p3", "label": "SESSION_COOKIE=sessioncookievalue", "path": "\\\\nas\\Share\\folder|uncleaksecret.txt", "capabilities": ["read"], "state": "allowed"},
                ],
            }
        ],
        "proposed_actions": [
            {
                "id": "a1",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p2",
                "summary": "copy /var/tmp/source,summaryleaksecret.txt with AWS_SECRET_ACCESS_KEY=secretaccessvalue",
                "reason": "use PRIVATE_KEY=abcdef1234567890 and SESSION_COOKIE=sessioncookievalue only in redacted metadata",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }
    receipts = [
        {"id": "r1", "action_id": "a1", "status": "dry_run", "created_at": "2026-05-27T00:00:00Z", "summary": "receipt C:/tmp/path,receiptleaksecret.txt DB_PASS=letmeinvalue", "would_execute": False}
    ]
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=receipts)

    payload = device_admin.build_operator_device_admin_payload(now=3.6)
    action_id = payload["dry_runs"][0]["id"]
    preview = device_admin.build_operator_device_admin_preview_payload(action_id=action_id, now=3.6)
    payload_text = json.dumps({"payload": payload, "preview": preview}, sort_keys=True)

    for leaked in [
        "ultraleaksecret",
        "winleaksecret",
        "uncleaksecret",
        "summaryleaksecret",
        "receiptleaksecret",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "PRIVATE_KEY",
        "SESSION_COOKIE",
        "DB_PASS",
        "AKIAULTRALEAKVALUE",
        "secretaccessvalue",
        "abcdef1234567890",
        "sessioncookievalue",
        "letmeinvalue",
    ]:
        assert leaked not in payload_text
    assert payload_text.count("[redacted") >= 6


def test_device_admin_malformed_action_type_degrades_to_blocked_unknown(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "id": "h1",
                "label": "Host One",
                "kind": "linux",
                "state": "allowed",
                "paths": [{"id": "p1", "label": "One", "path": "/safe", "capabilities": ["read"], "state": "allowed"}],
            }
        ],
        "proposed_actions": [
            {
                "id": "a-ssh",
                "action": "ssh",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p1",
                "summary": "invalid action must not look live",
                "approval_required": True,
                "state": "draft",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.75)

    dry_run = payload["dry_runs"][0]
    assert payload["status"] == "unknown"
    assert dry_run["action"] == "unknown"
    assert dry_run["state"] == "blocked"
    assert dry_run["would_execute"] is False
    assert any("malformed action" in issue.lower() or "unknown action" in issue.lower() for issue in dry_run["issues"])


def test_device_admin_missing_source_ids_are_unknown_not_fabricated(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "label": "Host Missing ID",
                "kind": "linux",
                "state": "allowed",
                "paths": [{"label": "Path Missing ID", "path": "/missing-id", "capabilities": ["read"], "state": "allowed"}],
            }
        ],
        "proposed_actions": [
            {
                "action": "copy",
                "host_id": "",
                "source_path_id": "",
                "destination_path_id": "",
                "summary": "missing ids must not be fabricated",
                "approval_required": True,
                "state": "draft",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.9)
    payload_text = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "unknown"
    assert "host-0" not in payload_text
    assert "path-0" not in payload_text
    assert "action-0" not in payload_text
    assert any("missing host id" in issue.lower() for issue in payload["issues"])
    assert any("missing path id" in issue.lower() for issue in payload["issues"])
    dry_run = payload["dry_runs"][0]
    assert payload["hosts"][0]["state"] == "unknown"
    assert payload["paths"][0]["state"] == "unknown"
    assert dry_run["id"] == ""
    assert dry_run["source_action_id"] == "unknown"
    assert dry_run["state"] == "blocked"
    assert any("missing action id" in issue.lower() for issue in dry_run["issues"])


def test_device_admin_mixed_path_and_secret_fields_are_fully_redacted(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "id": "h1",
                "label": "Host One",
                "kind": "linux",
                "state": "allowed",
                "paths": [{"id": "p1", "label": "One", "path": "/safe", "capabilities": ["read"], "state": "allowed"}],
            }
        ],
        "proposed_actions": [
            {
                "id": "a1",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p1",
                "summary": "copy /tmp/foo with api_key: 'top secret value'",
                "reason": "move /tmp/bar HERMES_TOKEN='tokensecretvalue' and password=\"top secret\"",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.91)
    payload_text = json.dumps(payload, sort_keys=True)

    for leaked in ["/tmp/foo", "/tmp/bar", "api_key", "top secret", "HERMES_TOKEN", "tokensecretvalue", "password"]:
        assert leaked not in payload_text
    assert "[redacted" in payload_text


def test_device_admin_malformed_nested_allowlist_fields_are_unknown_not_live(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {"id": "h1", "label": "Bad Paths", "kind": "linux", "state": "allowed", "paths": ""},
            {
                "id": "h2",
                "label": "Bad Capabilities",
                "kind": "linux",
                "state": "allowed",
                "paths": [{"id": "p2", "label": "Two", "path": "/safe", "capabilities": "read", "state": "allowed"}],
            },
        ],
        "proposed_actions": [
            {"id": "a2", "action": "copy", "host_id": "h2", "source_path_id": "p2", "destination_path_id": "p2", "summary": "bad nested fields", "approval_required": True, "state": "blocked"},
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.97)

    assert payload["status"] == "unknown"
    assert payload["sources"][0]["state"] == "unknown"
    by_host = {host["id"]: host for host in payload["hosts"]}
    by_path = {path["id"]: path for path in payload["paths"]}
    assert by_host["h1"]["state"] == "unknown"
    assert any("paths field" in issue.lower() for issue in by_host["h1"]["issues"])
    assert by_path["p2"]["state"] == "unknown"
    assert any("capabilities" in issue.lower() for issue in by_path["p2"]["issues"])
    assert any("paths field" in issue.lower() for issue in payload["issues"])
    assert any("capabilities" in issue.lower() for issue in payload["issues"])


def test_device_admin_missing_destination_reference_degrades_to_blocked_unknown(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "id": "h1",
                "label": "Host One",
                "kind": "linux",
                "state": "allowed",
                "paths": [{"id": "p1", "label": "One", "path": "/safe", "capabilities": ["read"], "state": "allowed"}],
            }
        ],
        "proposed_actions": [
            {
                "id": "a-no-dest",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "summary": "destination is required for approval model",
                "approval_required": True,
                "state": "draft",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.95)

    dry_run = payload["dry_runs"][0]
    assert payload["status"] == "unknown"
    assert dry_run["state"] == "blocked"
    assert dry_run["would_execute"] is False
    assert any("missing destination" in issue.lower() for issue in dry_run["issues"])


def test_device_admin_redacts_secretish_labels_keys_and_single_quoted_values(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {
                "id": "h1",
                "label": "Secret NAS",
                "kind": "nas",
                "state": "allowed",
                "paths": [
                    {
                        "id": "p1",
                        "label": {"password": "hunter2"},
                        "path": "/safe",
                        "capabilities": ["read"],
                        "state": "allowed",
                    }
                ],
            }
        ],
        "proposed_actions": [
            {
                "id": "a1",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p1",
                "summary": "api_key: 'top secret value'",
                "reason": "HERMES_TOKEN='tokensecretvalue' and password='hunter2'",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=3.99)
    payload_text = json.dumps(payload, sort_keys=True)

    for leaked in ["Secret NAS", "password", "hunter2", "api_key", "top secret value", "HERMES_TOKEN", "tokensecretvalue"]:
        assert leaked not in payload_text
    assert "[redacted" in payload_text


def test_device_admin_missing_action_references_degrade_to_blocked_unknown(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [{"id": "h1", "label": "Host One", "kind": "linux", "state": "allowed", "paths": []}],
        "proposed_actions": [
            {
                "id": "a-bad",
                "action": "move",
                "host_id": "missing-host",
                "source_path_id": "missing-source",
                "destination_path_id": "missing-destination",
                "summary": "bad refs must not execute",
                "approval_required": True,
                "state": "draft",
            }
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(now=4.0)

    dry_run = payload["dry_runs"][0]
    assert payload["status"] == "unknown"
    assert dry_run["source_action_id"] == "a-bad"
    assert dry_run["state"] == "blocked"
    assert dry_run["would_execute"] is False
    assert any("missing host" in issue.lower() for issue in dry_run["issues"])
    assert any("missing source" in issue.lower() for issue in dry_run["issues"])
    assert any("missing destination" in issue.lower() for issue in dry_run["issues"])


def _preview_allowlist():
    return {
        "hosts": [
            {
                "id": "h1",
                "label": "Host One",
                "kind": "linux",
                "state": "allowed",
                "paths": [
                    {"id": "p1", "label": "Source", "path": "/mnt/source", "capabilities": ["read"], "state": "allowed"},
                    {"id": "p2", "label": "Destination", "path": "/mnt/destination", "capabilities": ["copy_preview"], "state": "allowed"},
                ],
            }
        ],
        "proposed_actions": [
            {
                "id": "a1",
                "action": "copy",
                "host_id": "h1",
                "source_path_id": "p1",
                "destination_path_id": "p2",
                "summary": "copy /mnt/source to /mnt/destination preview only",
                "reason": "operator requested dry run",
                "risk": "low",
                "approval_required": True,
                "state": "blocked",
            }
        ],
    }


def test_device_admin_preview_known_action_is_blocked_dry_run(monkeypatch, tmp_path):
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=_preview_allowlist(), receipts=[])
    action = device_admin.build_operator_device_admin_payload(now=6.0)["dry_runs"][0]

    payload = device_admin.build_operator_device_admin_preview_payload(action_id=action["id"], now=6.0)
    payload_text = json.dumps(payload, sort_keys=True)

    assert payload["version"] == 1
    assert payload["mode"] == "device-admin-dry-run-preview-read-only"
    assert payload["status"] == "live"
    assert payload["execution_state"] == "blocked"
    assert payload["action"]["id"] == action["id"]
    assert payload["action"]["source_action_id"] == "a1"
    assert payload["action"]["action"] == "copy"
    assert payload["would_execute"] is False
    assert payload["preview"]["format"] == "dry-run-summary"
    assert "No device action was executed" in payload["preview"]["text"]
    assert "copy" in payload["preview"]["text"]
    assert payload["preview"]["bytes_read"] <= payload["preview"]["max_bytes"] == device_admin.MAX_PREVIEW_BYTES
    assert "/mnt/source" not in payload_text
    assert "/mnt/destination" not in payload_text


def test_device_admin_preview_rejects_suspicious_ids_without_path_leak(monkeypatch, tmp_path):
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=_preview_allowlist(), receipts=[])

    for suspicious in [
        "../x",
        "/etc/passwd",
        " /etc/passwd",
        "~/.ssh/id_rsa",
        "C:\\Users\\malac\\secret",
        " C:/Users/malac/secret",
        "file:///tmp/x",
        "ftp://host/path",
        "smb://server/share",
        "data:text/plain,secret",
        "javascript:alert(1)",
        "abc\x00def",
    ]:
        payload = device_admin.build_operator_device_admin_preview_payload(action_id=suspicious, now=7.0)
        payload_text = json.dumps(payload, sort_keys=True)

        assert payload["status"] == "unknown"
        assert payload["execution_state"] == "blocked"
        assert payload["would_execute"] is False
        assert payload["action"]["id"] == ""
        assert any("rejected" in issue.lower() or "malformed" in issue.lower() for issue in payload["issues"])
        assert suspicious.replace("\x00", "") not in payload_text


def test_device_admin_receipt_log_parses_valid_dry_run_receipts_and_reports_malformed_lines(monkeypatch, tmp_path):
    valid = {
        "id": "r1",
        "action_id": "a1",
        "status": "dry_run",
        "created_at": "2026-05-27T00:00:00Z",
        "summary": "dry run for /mnt/source with HERMES_TOKEN='tokensecretvalue'",
        "would_execute": False,
    }
    model_only = {
        "id": "r2",
        "action_id": "a1",
        "status": "approved_model_only",
        "created_at": "2026-05-27T00:01:00Z",
        "summary": "approval model only",
        "would_execute": True,
    }
    invalid_status = {"id": "r3", "action_id": "a1", "status": "executed", "summary": "must not count"}
    receipts_text = "\n".join([
        json.dumps(valid),
        "{bad json /mnt/source HERMES_TOKEN='tokensecretvalue'",
        json.dumps(invalid_status),
        json.dumps(model_only),
    ])
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=_preview_allowlist(), receipts_text=receipts_text)

    payload = device_admin.build_operator_device_admin_payload(now=8.0)
    payload_text = json.dumps(payload, sort_keys=True)

    assert payload["status"] == "unknown"
    assert payload["sources"][1]["id"] == "receipts"
    assert payload["sources"][1]["state"] == "unknown"
    assert payload["sources"][1]["count"] == 2
    assert [receipt["id"] for receipt in payload["receipts"]] == ["r2", "r1"]
    assert {receipt["status"] for receipt in payload["receipts"]} == {"dry_run", "approved_model_only"}
    assert all(receipt["would_execute"] is False for receipt in payload["receipts"])
    assert any("malformed receipt" in issue.lower() for issue in payload["issues"])
    assert any("unsupported receipt status" in issue.lower() for issue in payload["issues"])
    assert "executed" not in json.dumps(payload["receipts"], sort_keys=True)
    assert "/mnt/source" not in payload_text
    assert "HERMES_TOKEN" not in payload_text
    assert "tokensecretvalue" not in payload_text


def test_device_admin_query_filters_host_action_text_and_limit_without_browsing(monkeypatch, tmp_path):
    allowlist = {
        "hosts": [
            {"id": "h1", "label": "Alpha", "kind": "linux", "state": "allowed", "paths": [{"id": "p1", "label": "One", "path": "/alpha", "capabilities": ["read"], "state": "allowed"}]},
            {"id": "h2", "label": "Beta", "kind": "linux", "state": "allowed", "paths": [{"id": "p2", "label": "Two", "path": "/beta", "capabilities": ["read"], "state": "allowed"}]},
        ],
        "proposed_actions": [
            {"id": "a1", "action": "copy", "host_id": "h1", "source_path_id": "p1", "destination_path_id": "p1", "summary": "Alpha copy", "approval_required": True, "state": "blocked"},
            {"id": "a2", "action": "delete", "host_id": "h2", "source_path_id": "p2", "destination_path_id": "p2", "summary": "Keep Beta delete dry-run", "approval_required": True, "state": "blocked"},
            {"id": "a3", "action": "delete", "host_id": "h1", "source_path_id": "p1", "destination_path_id": "p1", "summary": "Other delete", "approval_required": True, "state": "blocked"},
        ],
    }
    device_admin = _device_admin_module(monkeypatch, tmp_path, allowlist=allowlist, receipts=[])

    payload = device_admin.build_operator_device_admin_payload(query_text="Keep", host="h2", action="delete", limit="1", now=5.0)
    payload_text = json.dumps(payload, sort_keys=True)

    assert payload["query"] == {"text": "Keep", "host": "h2", "action": "delete", "limit": 1}
    assert [item["id"] for item in payload["hosts"]] == ["h2"]
    assert [item["id"] for item in payload["paths"]] == ["p2"]
    assert len(payload["dry_runs"]) == 1
    assert payload["dry_runs"][0]["source_action_id"] == "a2"
    assert "/alpha" not in payload_text
    assert "/beta" not in payload_text
