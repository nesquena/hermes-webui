"""Read-only report reader API for Hermes WebUI.

Reports are stored in the Hermes control-plane report tables and exposed here
as browser-safe reader payloads. This module does not write to the database,
send Telegram messages, mutate cron, or edit Obsidian authority.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from api.helpers import j

_FORBIDDEN_BROWSER_KEYS = {
    "target" + "_ref",
    "provider" + "_message_ref",
    "error" + "_summary",
    "provider" + "_error_body",
    "raw" + "_payload",
    "private" + "_target_payload",
    "raw" + "_source_packet",
    "raw" + "_source_dump",
}


def _strip_forbidden_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_forbidden_keys(item)
            for key, item in value.items()
            if str(key) not in _FORBIDDEN_BROWSER_KEYS
        }
    if isinstance(value, list):
        return [_strip_forbidden_keys(item) for item in value]
    return value


def _as_list(value: Any) -> List[Dict[str, Any]]:
    return value if isinstance(value, list) else []


def _section_kind(section: Dict[str, Any]) -> str:
    title = str(section.get("title") or "").lower()
    key = str(section.get("section_key") or "").lower()
    text = f"{key} {title}"
    if "summary" in text or "요약" in text or "판단" in text:
        return "summary"
    if "source" in text or "출처" in text or "보류" in text:
        return "source_quality"
    if "decision" in text or "queue" in text or "처리" in text:
        return "judgment"
    return "issue"


def _normalize_sections(sections: List[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = []
    summary = ""
    source_quality = []
    judgments = []
    for idx, section in enumerate(sections, start=1):
        item = {
            "section_key": section.get("section_key") or f"section_{idx}",
            "order": section.get("section_order") or idx,
            "title": section.get("title") or f"Issue {idx}",
            "body_md": section.get("body_md") or "",
            "judgment_label": section.get("judgment_label") or "review",
            "kind": _section_kind(section),
        }
        normalized.append(item)
        if item["kind"] == "summary" and not summary:
            summary = item["body_md"]
        if item["kind"] == "source_quality":
            source_quality.append(item)
        if item["kind"] == "judgment" or item.get("judgment_label"):
            judgments.append(
                {
                    "title": item["title"],
                    "judgment_label": item["judgment_label"],
                    "handling": "WebUI reader에 표시하고, 운영/채택 변경은 별도 승인 전까지 보류합니다.",
                }
            )

    if not summary and normalized:
        first = normalized[0]
        summary = first.get("body_md") or first.get("title") or "Latest Morning Brief report is available."

    return {
        "summary": summary,
        "sections": normalized,
        "issue_sections": [item for item in normalized if item.get("kind") == "issue"],
        "source_quality_sections": source_quality,
        "judgments": judgments[:8],
    }


def _telegram_contract(summary: str, sections: List[Dict[str, Any]]) -> Dict[str, Any]:
    top = [str(item.get("title") or "").strip() for item in sections[:3] if item.get("title")]
    lines = ["[Morning Brief] 최신 보고서", "요약: " + (summary or "WebUI에서 전체 보고서를 확인하세요.")[:140]]
    if top:
        lines.append("핵심: " + " / ".join(top))
    lines.append("전체: WebUI > Reports > Morning Brief")
    return {
        "mode": "short_notification_only",
        "send_enabled": False,
        "max_lines": 6,
        "preview_lines": lines[:6],
    }


def _artifact_dir() -> Path:
    configured = os.getenv("HERMES_MORNING_BRIEF_REPORT_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".hermes" / "webui-workspace" / "hermes-control-plane" / "artifacts" / "morning-brief-full"


def _latest_artifact_report_path() -> Path | None:
    root = _artifact_dir()
    try:
        candidates = [
            p for p in root.glob("*.md")
            if p.is_file() and "paperclip-packet" not in p.name
        ]
    except Exception:
        return None
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: (p.stat().st_mtime, p.name), reverse=True)[0]


def _split_markdown_sections(markdown: str) -> List[Dict[str, Any]]:
    text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", markdown, flags=re.S)
    blocks: List[Dict[str, Any]] = []
    current_title = "오늘의 요약"
    current_lines: List[str] = []
    for line in text.splitlines():
        match = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
        if match:
            body = "\n".join(current_lines).strip()
            if body:
                blocks.append({"title": current_title, "body_md": body})
            current_title = match.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    body = "\n".join(current_lines).strip()
    if body:
        blocks.append({"title": current_title, "body_md": body})
    return blocks


def _artifact_report_payload(source_reason: str = "supabase_unavailable") -> Dict[str, Any] | None:
    path = _latest_artifact_report_path()
    if not path:
        return None
    try:
        body = path.read_text(encoding="utf-8")
    except Exception:
        return None

    raw_sections = _split_markdown_sections(body)
    skip_title_fragments = (
        "run metadata",
        "source packet evidence",
        "delivery and storage",
        "patch candidates",
        "parent",
        "children",
    )
    reader_sections = [
        section for section in raw_sections
        if not any(fragment in str(section.get("title") or "").lower() for fragment in skip_title_fragments)
    ]
    normalized_input = []
    for idx, section in enumerate(reader_sections[:12], start=1):
        title = section.get("title") or f"Issue {idx}"
        label_match = re.search(r"`([^`]*(?:review|observe|hold|adopt|reject)[^`]*)`", title + "\n" + str(section.get("body_md") or ""), re.I)
        normalized_input.append({
            "section_key": f"artifact_section_{idx}",
            "section_order": idx,
            "title": title,
            "body_md": section.get("body_md") or "",
            "judgment_label": label_match.group(1) if label_match else "review",
        })
    normalized = _normalize_sections(normalized_input)
    report_date = path.stem
    payload = {
        "enabled": True,
        "status": "fallback_local_artifact",
        "mode": "reader_only",
        "report_type": "morning_brief",
        "storage": {
            "primary_store": "supabase",
            "browser_direct_db_access": False,
            "fallback_store": "local_artifact",
            "fallback_reason": source_reason,
        },
        "run": {
            "id": None,
            "run_ref": f"local_artifact:{path.name}",
            "report_date": report_date,
            "title": "Morning Brief",
            "status": "fallback_local_artifact",
            "paperclip_parent_ref": None,
            "obsidian_ref_present": False,
        },
        "title": "Morning Brief",
        "summary": normalized["summary"],
        "sections": normalized["issue_sections"],
        "source_quality_sections": normalized["source_quality_sections"],
        "judgments": normalized["judgments"],
        "sources": [],
        "telegram_contract": _telegram_contract(normalized["summary"], normalized["issue_sections"]),
        "counts": {"report_sections": len(normalized["issue_sections"]), "sources": 0},
        "boundaries": {
            "read_only": True,
            "writes_enabled": False,
            "telegram_send_enabled": False,
            "obsidian_authority_edit_enabled": False,
            "cron_change_enabled": False,
        },
    }
    return _strip_forbidden_keys(payload)


def _control_plane_root() -> Path:
    return Path.home() / ".hermes" / "webui-workspace" / "hermes-control-plane"


def _extract_json_rows(output: str) -> List[Dict[str, Any]]:
    start = output.find("[")
    if start < 0:
        return []
    try:
        rows = json.loads(output[start:])
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _run_report_store_query(sql: str) -> List[Dict[str, Any]]:
    root = _control_plane_root()
    if not root.exists():
        return []
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8") as handle:
        handle.write(sql)
        query_path = handle.name
    try:
        cp = subprocess.run(
            ["supabase", "db", "query", "--linked", "--output", "json", "-f", query_path],
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=8,
        )
        if cp.returncode != 0:
            return []
        return _extract_json_rows(cp.stdout)
    except Exception:
        return []
    finally:
        try:
            Path(query_path).unlink()
        except OSError:
            pass


def _report_store_payload() -> Dict[str, Any] | None:
    sql = """
with latest as (
  select
    report_id,
    report_kind,
    report_date::text,
    routine_id,
    title,
    status,
    telegram_summary,
    hermes_judgment_summary,
    source_mode,
    notebooklm_status,
    paperclip_parent_ref,
    obsidian_ledger_ref,
    webui_ref,
    source_refs,
    artifact_refs,
    metadata,
    retention_class,
    created_at::text,
    updated_at::text
  from hermes_reports.reports
  where report_kind = 'morning_brief'
  order by report_date desc, updated_at desc
  limit 1
)
select jsonb_build_object(
  'enabled', true,
  'status', 'ok',
  'mode', 'read_only',
  'project', 'hermes-control-plane-report-store',
  'updated_at', now()::text,
  'run', (
    select jsonb_build_object(
      'id', report_id,
      'run_ref', 'supabase://hermes_reports.reports/' || report_id,
      'report_date', report_date,
      'title', title,
      'status', status,
      'paperclip_parent_ref', paperclip_parent_ref,
      'obsidian_ref', obsidian_ledger_ref,
      'webui_ref', webui_ref,
      'metadata', metadata,
      'retention_class', retention_class,
      'created_at', created_at,
      'updated_at', updated_at
    ) from latest
  ),
  'sections', coalesce((
    select jsonb_agg(to_jsonb(s) order by s.section_order)
    from (
      select
        section_key,
        ordinal as section_order,
        title,
        body_markdown as body_md,
        coalesce(metadata->>'judgment_label', 'review') as judgment_label,
        created_at::text
      from hermes_reports.report_body_sections
      where report_id in (select report_id from latest)
      order by ordinal
    ) s
  ), '[]'::jsonb),
  'sources', coalesce((
    select jsonb_agg(to_jsonb(src) order by src.created_at)
    from (
      select source_key, source_url as source_ref, source_title, source_timing_basis,
             item_time_label, source_quality, created_at::text
      from hermes_reports.source_refs
      where report_id in (select report_id from latest)
      order by created_at
    ) src
  ), '[]'::jsonb),
  'counts', jsonb_build_object(
    'report_sections', (select count(*) from hermes_reports.report_body_sections where report_id in (select report_id from latest)),
    'sources', (select count(*) from hermes_reports.source_refs where report_id in (select report_id from latest))
  ),
  'boundaries', jsonb_build_object(
    'read_only', true,
    'writes_enabled', false,
    'telegram_send_enabled', false,
    'obsidian_authority_edit_enabled', false,
    'cron_change_enabled', false
  )
) as payload
from latest;
"""
    rows = _run_report_store_query(sql)
    if not rows:
        return None
    payload = rows[0].get("payload") if isinstance(rows[0], dict) else None
    return payload if isinstance(payload, dict) else None


def morning_brief_latest_report() -> Dict[str, Any]:
    from api.control_plane import control_plane_payload

    report_store = _report_store_payload()
    if report_store:
        source_payload = report_store
    else:
        source_payload = control_plane_payload()
    if not isinstance(source_payload, dict) or source_payload.get("enabled") is False:
        reason = source_payload.get("reason", "disabled") if isinstance(source_payload, dict) else "report_source_unavailable"
        fallback = _artifact_report_payload(reason)
        if fallback:
            return fallback
        return _strip_forbidden_keys(
            {
                "enabled": False,
                "status": source_payload.get("status", "disabled") if isinstance(source_payload, dict) else "disabled",
                "reason": reason,
                "message": source_payload.get("message", "Morning Brief report source is unavailable.") if isinstance(source_payload, dict) else "Morning Brief report source is unavailable.",
                "report_type": "morning_brief",
                "storage": {"primary_store": "supabase", "browser_direct_db_access": False},
                "boundaries": {
                    "read_only": True,
                    "writes_enabled": False,
                    "telegram_send_enabled": False,
                    "obsidian_authority_edit_enabled": False,
                    "cron_change_enabled": False,
                },
            }
        )

    run = source_payload.get("run") if isinstance(source_payload.get("run"), dict) else {}
    sections = _as_list(source_payload.get("sections"))
    metadata = run.get("metadata") if isinstance(run.get("metadata"), dict) else {}
    ref_integrity = {
        "webui_ref_status": metadata.get("webui_ref_status"),
        "webui_surface": metadata.get("webui_surface"),
        "webui_mutation_allowed": metadata.get("webui_mutation_allowed"),
        "supabase_ref_status": metadata.get("supabase_ref_status"),
        "supabase_verified_at": metadata.get("supabase_verified_at"),
        "summary_hash": metadata.get("summary_hash"),
        "content_hash": metadata.get("content_hash"),
        "fallback_artifact_ref": metadata.get("fallback_artifact_ref"),
    }
    normalized = _normalize_sections(sections)
    payload = {
        "enabled": True,
        "status": "ok",
        "mode": "reader_only",
        "report_type": "morning_brief",
        "storage": {"primary_store": "supabase", "browser_direct_db_access": False},
        "run": {
            "id": run.get("id"),
            "run_ref": run.get("run_ref"),
            "report_date": run.get("report_date"),
            "title": run.get("title") or "Morning Brief",
            "status": run.get("status"),
            "paperclip_parent_ref": run.get("paperclip_parent_ref"),
            "webui_ref": run.get("webui_ref"),
            "canonical_webui_ref": run.get("webui_ref"),
            "obsidian_ref_present": bool(run.get("obsidian_ref")),
            "ref_integrity": ref_integrity,
        },
        "title": run.get("title") or "Morning Brief",
        "summary": normalized["summary"],
        "sections": normalized["issue_sections"],
        "source_quality_sections": normalized["source_quality_sections"],
        "judgments": normalized["judgments"],
        "sources": _as_list(source_payload.get("sources"))[:24],
        "telegram_contract": _telegram_contract(normalized["summary"], normalized["issue_sections"]),
        "counts": source_payload.get("latest_run_counts") or source_payload.get("counts") or {},
        "boundaries": {
            "read_only": True,
            "writes_enabled": False,
            "telegram_send_enabled": False,
            "obsidian_authority_edit_enabled": False,
            "cron_change_enabled": False,
        },
    }
    return _strip_forbidden_keys(payload)


def handle_reports_get(handler, parsed) -> bool:
    if parsed.path == "/api/reports/morning-brief/latest":
        return j(handler, morning_brief_latest_report())
    return False
