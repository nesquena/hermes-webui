from pathlib import Path

import api.reports as reports

REPO = Path(__file__).resolve().parent.parent
INDEX_HTML = (REPO / "static" / "index.html").read_text(encoding="utf-8")
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO / "api" / "routes.py").read_text(encoding="utf-8")
REPORTS_PY = (REPO / "api" / "reports.py").read_text(encoding="utf-8")
STORE_SCRIPT = (
    Path.home()
    / ".hermes"
    / "webui-workspace"
    / "hermes-control-plane"
    / "scripts"
    / "store_morning_brief_report.py"
).read_text(encoding="utf-8")


def test_reports_menu_and_reader_surface_are_present():
    assert 'data-panel="reports"' in INDEX_HTML
    assert 'id="panelReports"' in INDEX_HTML
    assert 'id="mainReports"' in INDEX_HTML
    assert 'Reports' in INDEX_HTML
    assert 'Morning Brief' in INDEX_HTML
    assert 'WebUI 보고서 리더' in INDEX_HTML


def test_reports_routes_are_spa_and_api_protected_by_default():
    assert 'parsed.path in ("/reports", "/reports/morning-brief")' in ROUTES_PY
    assert 'parsed.path.startswith("/api/reports/")' in ROUTES_PY
    assert 'handle_reports_get' in ROUTES_PY
    assert '"/api/reports/morning-brief/latest"' in REPORTS_PY


def test_morning_brief_reader_uses_supabase_backed_payload_not_browser_supabase():
    assert "control_plane_payload()" in REPORTS_PY
    assert '"primary_store": "supabase"' in REPORTS_PY
    assert '"fallback_store": "local_artifact"' in REPORTS_PY
    assert "HERMES_MORNING_BRIEF_REPORT_DIR" in REPORTS_PY
    assert "supabase" not in PANELS_JS.lower()
    assert "SUPABASE" not in INDEX_HTML


def test_morning_brief_reader_contract_sections_and_telegram_short_contract():
    expected_text = [
        '오늘의 요약',
        '핵심 이슈',
        'Hermes 처리 판단',
        '보류 / 출처 약함',
        'Telegram은 짧은 알림만',
    ]
    for text in expected_text:
        assert text in PANELS_JS or text in INDEX_HTML
    assert "api('/api/reports/morning-brief/latest')" in PANELS_JS
    assert 'report-article' in PANELS_JS
    assert 'report-jgment' not in PANELS_JS


def test_report_reader_browser_payload_excludes_forbidden_raw_fields():
    forbidden = [
        'target_ref',
        'provider_message_ref',
        'provider_error_body',
        'raw_payload',
        'private_target_payload',
        'raw_source_packet',
        'raw_source_dump',
    ]
    for key in forbidden:
        assert key not in REPORTS_PY


def test_report_store_script_writes_ref_integrity_into_metadata_not_schema_columns():
    expected_metadata_keys = [
        'webui_ref_status',
        'supabase_ref_status',
        'supabase_verified_at',
        'summary_hash',
        'content_hash',
        'fallback_artifact_ref',
    ]
    for key in expected_metadata_keys:
        assert key in STORE_SCRIPT
    assert "hashlib.sha256" in STORE_SCRIPT
    assert "metadata.update(ref_integrity_metadata" in STORE_SCRIPT


def test_morning_brief_reader_prefers_report_store_before_legacy_canary():
    assert "def _report_store_payload" in REPORTS_PY
    assert "hermes_reports.reports" in REPORTS_PY
    assert "hermes_reports.report_body_sections" in REPORTS_PY
    assert "report_store = _report_store_payload()" in REPORTS_PY
    assert REPORTS_PY.index("report_store = _report_store_payload()") < REPORTS_PY.index("control_plane_payload()")


def test_reader_exposes_ref_integrity_metadata_without_raw_fields():
    allowed_keys = [
        'webui_ref_status',
        'webui_surface',
        'webui_mutation_allowed',
        'supabase_ref_status',
        'supabase_verified_at',
        'summary_hash',
        'content_hash',
        'fallback_artifact_ref',
    ]
    for key in allowed_keys:
        assert f'"{key}"' in REPORTS_PY
    assert 'ref_integrity' in REPORTS_PY


def test_reader_runtime_payload_exposes_only_selected_ref_integrity(monkeypatch):
    monkeypatch.setattr(
        reports,
        "_report_store_payload",
        lambda: {
            "enabled": True,
            "run": {
                "id": "morning-brief-test",
                "run_ref": "supabase://hermes_reports.reports/morning-brief-test",
                "report_date": "2026-05-10",
                "title": "Morning Brief Test",
                "status": "complete",
                "metadata": {
                    "webui_ref_status": "live",
                    "webui_surface": "report",
                    "webui_mutation_allowed": False,
                    "supabase_ref_status": "available",
                    "supabase_verified_at": "2026-05-10T15:15:52Z",
                    "summary_hash": "sha256:summary",
                    "content_hash": "sha256:content",
                    "fallback_artifact_ref": "obsidian://ledger",
                    "private_target_payload": "do-not-leak",
                },
                "obsidian_ref": "obsidian://ledger",
            },
            "sections": [
                {
                    "section_key": "summary",
                    "section_order": 1,
                    "title": "요약",
                    "body_md": "clean summary",
                    "judgment_label": "review",
                }
            ],
            "sources": [],
            "counts": {"report_sections": 1},
        },
    )
    payload = reports.morning_brief_latest_report()
    run = payload["run"]
    assert "metadata" not in run
    assert run["ref_integrity"] == {
        "webui_ref_status": "live",
        "webui_surface": "report",
        "webui_mutation_allowed": False,
        "supabase_ref_status": "available",
        "supabase_verified_at": "2026-05-10T15:15:52Z",
        "summary_hash": "sha256:summary",
        "content_hash": "sha256:content",
        "fallback_artifact_ref": "obsidian://ledger",
    }
    assert "private_target_payload" not in str(payload)


def test_report_store_script_emits_canonical_morning_brief_webui_ref():
    canonical = "hermes://webui/reports/morning-brief/"
    legacy = "hermes://webui/morning-brief/"
    assert f"WEBUI_MORNING_BRIEF_REF_PREFIX = '{canonical}'" in STORE_SCRIPT
    assert "def webui_morning_brief_ref(report_id: str)" in STORE_SCRIPT
    assert "webui_morning_brief_ref(report_id)" in STORE_SCRIPT
    assert f"{legacy}' + report_id" not in STORE_SCRIPT
    assert f"{legacy}{{report_id}}" not in STORE_SCRIPT


def test_reader_runtime_payload_exposes_canonical_webui_ref_for_readback(monkeypatch):
    canonical_ref = "hermes://webui/reports/morning-brief/morning-brief-test"
    monkeypatch.setattr(
        reports,
        "_report_store_payload",
        lambda: {
            "enabled": True,
            "run": {
                "id": "morning-brief-test",
                "run_ref": "supabase://hermes_reports.reports/morning-brief-test",
                "report_date": "2026-05-10",
                "title": "Morning Brief Test",
                "status": "complete",
                "webui_ref": canonical_ref,
                "metadata": {
                    "webui_ref_status": "live",
                    "webui_surface": "report",
                    "webui_mutation_allowed": False,
                    "supabase_ref_status": "available",
                    "fallback_artifact_ref": "obsidian://ledger",
                },
                "obsidian_ref": "obsidian://ledger",
            },
            "sections": [{"section_key": "summary", "section_order": 1, "title": "요약", "body_md": "clean summary"}],
            "sources": [],
            "counts": {"report_sections": 1},
        },
    )
    payload = reports.morning_brief_latest_report()
    assert payload["run"]["webui_ref"] == canonical_ref
    assert payload["run"].get("canonical_webui_ref") == canonical_ref
