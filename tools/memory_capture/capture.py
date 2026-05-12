from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .privacy_filter import sanitize_text

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUARANTINE = ROOT / ".memory-quarantine"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_quarantine(root: Path) -> None:
    for name in ["tool-errors", "sessions", "worker-receipts", "research-signals", "promote-candidates"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# Yuto Memory Quarantine\n\n"
            "Temporary sanitized capture area. Do not treat these files as curated memory.\n"
            "Promote to knowledge/ only after Yuto review.\n",
            encoding="utf-8",
        )


def append_audit(root: Path, event: str, item_id: str, reason: str, path: str | None = None) -> None:
    ensure_quarantine(root)
    row = {
        "event": event,
        "timestamp": utc_now(),
        "item_id": item_id,
        "actor": "yuto",
        "reason": reason,
        "path": path,
    }
    with (root / "audit-log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _redaction_dicts(results: list[Any]) -> list[dict[str, str]]:
    return [{"type": r.type, "replacement": r.replacement} for r in results]


def capture_tool_error(
    *,
    root: Path = DEFAULT_QUARANTINE,
    session_id: str,
    project: str,
    agent: str,
    tool: str,
    command: str,
    exit_code: int,
    stderr: str = "",
    stdout: str = "",
) -> dict[str, Any]:
    ensure_quarantine(root)
    day = datetime.now().strftime("%Y-%m-%d")
    out = root / "tool-errors" / day
    out.mkdir(parents=True, exist_ok=True)
    item_id = f"toolerr-{day}-{session_id}"
    path = out / f"{session_id}.jsonl"

    sanitized_stderr = sanitize_text(stderr[:8000])
    sanitized_stdout = sanitize_text(stdout[:4000])
    redactions = [*sanitized_stderr.redactions, *sanitized_stdout.redactions]
    row = {
        "kind": "tool_error",
        "item_id": item_id,
        "timestamp": utc_now(),
        "status": "sanitized",
        "session_id": session_id,
        "project": project,
        "agent": agent,
        "tool": tool,
        "command": command,
        "exit_code": exit_code,
        "stderr": sanitized_stderr.text,
        "stdout": sanitized_stdout.text,
        "redactions": _redaction_dicts(redactions),
        "review_required": bool(redactions),
        "promotion_status": "quarantined",
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    append_audit(root, "capture_tool_error", item_id, "sanitized tool error captured", str(path))
    return {"item_id": item_id, "path": str(path), "redactions": len(redactions)}


def _sanitize_list(values: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    sanitized: list[str] = []
    redactions: list[dict[str, str]] = []
    for value in values:
        result = sanitize_text(value)
        sanitized.append(result.text)
        redactions.extend(_redaction_dicts(result.redactions))
    return sanitized, redactions


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def capture_session_summary(
    *,
    root: Path = DEFAULT_QUARANTINE,
    session_id: str,
    project: str,
    agent: str,
    decisions: list[str] | None = None,
    verified_outputs: list[str] | None = None,
    open_risks: list[str] | None = None,
    changed_files: list[str] | None = None,
) -> dict[str, Any]:
    ensure_quarantine(root)
    day = datetime.now().strftime("%Y-%m-%d")
    out = root / "sessions" / day
    out.mkdir(parents=True, exist_ok=True)
    item_id = f"session-{day}-{session_id}"
    path = out / f"{session_id}.json"

    decisions_s, red1 = _sanitize_list(decisions or [])
    outputs_s, red2 = _sanitize_list(verified_outputs or [])
    risks_s, red3 = _sanitize_list(open_risks or [])
    files_s, red4 = _sanitize_list(changed_files or [])
    redactions = [*red1, *red2, *red3, *red4]
    data = {
        "kind": "session_summary",
        "item_id": item_id,
        "timestamp": utc_now(),
        "status": "sanitized",
        "session_id": session_id,
        "project": project,
        "agent": agent,
        "decisions": decisions_s,
        "verified_outputs": outputs_s,
        "open_risks": risks_s,
        "changed_files": files_s,
        "redactions": redactions,
        "review_required": bool(redactions),
        "promotion_status": "quarantined",
    }
    _write_json(path, data)
    append_audit(root, "capture_session_summary", item_id, "sanitized session summary captured", str(path))
    return {"item_id": item_id, "path": str(path), "redactions": len(redactions)}


def capture_worker_receipt(
    *,
    root: Path = DEFAULT_QUARANTINE,
    session_id: str,
    project: str,
    agent: str,
    lane: str,
    task_id: str,
    summary: str,
    findings: list[str] | None = None,
    artifact_paths: list[str] | None = None,
    verification_status: Literal["pass", "partial", "fail"] = "partial",
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    ensure_quarantine(root)
    if verification_status not in {"pass", "partial", "fail"}:
        raise ValueError("verification_status must be pass|partial|fail")
    day = datetime.now().strftime("%Y-%m-%d")
    out = root / "worker-receipts" / day
    out.mkdir(parents=True, exist_ok=True)
    item_id = f"worker-{day}-{session_id}-{task_id}"
    path = out / f"{session_id}-{task_id}.json"

    summary_s = sanitize_text(summary)
    findings_s, red1 = _sanitize_list(findings or [])
    artifact_paths_s, red2 = _sanitize_list(artifact_paths or [])
    next_actions_s, red3 = _sanitize_list(next_actions or [])
    redactions = [*_redaction_dicts(summary_s.redactions), *red1, *red2, *red3]
    data = {
        "kind": "worker_receipt",
        "item_id": item_id,
        "timestamp": utc_now(),
        "status": "sanitized",
        "session_id": session_id,
        "project": project,
        "agent": agent,
        "lane": lane,
        "task_id": task_id,
        "summary": summary_s.text,
        "findings": findings_s,
        "artifact_paths": artifact_paths_s,
        "verification_status": verification_status,
        "next_actions": next_actions_s,
        "redactions": redactions,
        "review_required": bool(redactions) or verification_status != "pass",
        "promotion_status": "quarantined",
    }
    _write_json(path, data)
    append_audit(root, "capture_worker_receipt", item_id, "sanitized worker receipt captured", str(path))
    return {"item_id": item_id, "path": str(path), "redactions": len(redactions)}


def iter_quarantine_records(root: Path = DEFAULT_QUARANTINE) -> list[dict[str, Any]]:
    ensure_quarantine(root)
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "item_id" in data:
            data = dict(data)
            data["path"] = str(path)
            records.append(data)
    for path in sorted(root.rglob("*.jsonl")):
        if path.name == "audit-log.jsonl":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and "item_id" in data:
                data = dict(data)
                data["path"] = str(path)
                records.append(data)
    return sorted(records, key=lambda item: str(item.get("timestamp", "")))


def list_quarantine_items(root: Path = DEFAULT_QUARANTINE, kind: str | None = None) -> list[dict[str, Any]]:
    records = iter_quarantine_records(root)
    if kind is not None:
        records = [record for record in records if record.get("kind") == kind]
    return [
        {
            "item_id": str(record.get("item_id", "")),
            "kind": str(record.get("kind", "")),
            "timestamp": str(record.get("timestamp", "")),
            "project": str(record.get("project", "")),
            "agent": str(record.get("agent", "")),
            "promotion_status": str(record.get("promotion_status", "")),
            "review_required": bool(record.get("review_required", False)),
            "path": str(record.get("path", "")),
        }
        for record in records
    ]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9ก-๙]+", "-", value.strip().lower()).strip("-")
    return slug[:120] or "capture-item"


def find_quarantine_record(root: Path, item_id: str) -> dict[str, Any]:
    matches = [record for record in iter_quarantine_records(root) if record.get("item_id") == item_id]
    if not matches:
        raise FileNotFoundError(f"quarantine item not found: {item_id}")
    return matches[-1]


def _markdown_list(title: str, values: list[Any]) -> str:
    if not values:
        return f"## {title}\n\n- None\n"
    lines = [f"## {title}", ""]
    lines.extend(f"- {value}" for value in values)
    return "\n".join(lines) + "\n"


def promotion_markdown(record: dict[str, Any], *, reviewer: str, rationale: str, destination: str) -> str:
    title = f"Promoted Capture - {record.get('item_id', 'unknown')}"
    lines = [
        f"# {title}",
        "",
        f"Created: {datetime.now().date().isoformat()}",
        "Type: capture-promotion",
        "Status: reviewed-draft",
        "",
        "## Promotion Review",
        "",
        f"- reviewer: {reviewer}",
        f"- rationale: {rationale}",
        f"- destination: {destination}",
        f"- source_item_id: {record.get('item_id', '')}",
        f"- source_kind: {record.get('kind', '')}",
        f"- source_path: {record.get('path', '')}",
        "",
        "## Core Summary",
        "",
        f"- project: {record.get('project', '')}",
        f"- agent: {record.get('agent', '')}",
        f"- lane: {record.get('lane', '')}",
        f"- verification_status: {record.get('verification_status', '')}",
        f"- review_required_at_capture: {record.get('review_required', False)}",
        "",
    ]
    if record.get("summary"):
        lines.extend(["## Summary", "", str(record.get("summary", "")), ""])
    for section, key in [
        ("Findings", "findings"),
        ("Decisions", "decisions"),
        ("Verified Outputs", "verified_outputs"),
        ("Open Risks", "open_risks"),
        ("Next Actions", "next_actions"),
        ("Changed Files", "changed_files"),
        ("Artifact Paths", "artifact_paths"),
    ]:
        values = record.get(key)
        if isinstance(values, list) and values:
            lines.append(_markdown_list(section, values))
    if record.get("kind") == "tool_error":
        lines.extend([
            "## Tool Error",
            "",
            f"- tool: {record.get('tool', '')}",
            f"- command: `{record.get('command', '')}`",
            f"- exit_code: {record.get('exit_code', '')}",
            "",
            "### Stderr",
            "",
            "```text",
            str(record.get("stderr", ""))[:2000],
            "```",
            "",
        ])
    lines.extend([
        "## Safety Note",
        "",
        "This note is a reviewed promotion draft from sanitized quarantine. It is not raw evidence and must not be treated as legal, forensic, or production truth without the relevant human/expert gate.",
        "",
    ])
    return "\n".join(lines)


def promote_quarantine_item(
    *,
    root: Path = DEFAULT_QUARANTINE,
    knowledge_root: Path = ROOT / "knowledge",
    item_id: str,
    destination: Literal["kg-draft"] = "kg-draft",
    reviewer: str,
    rationale: str,
    force_reviewed: bool = False,
) -> dict[str, Any]:
    ensure_quarantine(root)
    if destination != "kg-draft":
        raise ValueError("destination must be kg-draft")
    if not rationale.strip():
        raise ValueError("rationale is required")
    record = find_quarantine_record(root, item_id)
    if record.get("review_required") and not force_reviewed:
        raise ValueError(f"item has review_required=true; pass force_reviewed only after human review: {item_id}")
    out_dir = knowledge_root / "capture-promotions"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{_slug(item_id)}.md"
    if path.exists():
        raise FileExistsError(f"promotion already exists: {path}")
    content = promotion_markdown(record, reviewer=reviewer, rationale=rationale, destination=destination)
    path.write_text(content, encoding="utf-8")
    append_audit(root, "promote_quarantine_item", item_id, f"promoted to {destination}: {rationale}", str(path))
    return {
        "item_id": item_id,
        "path": str(path),
        "promotion_status": "promoted_to_kg_draft",
        "destination": destination,
    }


def quarantine_doctor(root: Path = DEFAULT_QUARANTINE) -> dict[str, Any]:
    ensure_quarantine(root)
    counts: Counter[str] = Counter()
    invalid: list[str] = []
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            counts[str(data.get("kind", "unknown"))] += 1
        except json.JSONDecodeError:
            invalid.append(str(path))
    for path in root.rglob("*.jsonl"):
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                data = json.loads(line)
                counts[str(data.get("kind", "audit" if path.name == "audit-log.jsonl" else "unknown"))] += 1
        except json.JSONDecodeError:
            invalid.append(str(path))
    result_counts = {
        "tool_error": counts.get("tool_error", 0),
        "session_summary": counts.get("session_summary", 0),
        "worker_receipt": counts.get("worker_receipt", 0),
        "audit": counts.get("audit", 0),
    }
    for key, value in counts.items():
        result_counts.setdefault(key, value)
    return {"root": str(root), "ok": not invalid, "counts": result_counts, "invalid": invalid}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yuto-native memory quarantine capture")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor_p = sub.add_parser("doctor", help="validate quarantine JSON/JSONL files")
    doctor_p.add_argument("--root", type=Path, default=DEFAULT_QUARANTINE)

    list_p = sub.add_parser("list", help="list quarantined capture items")
    list_p.add_argument("--root", type=Path, default=DEFAULT_QUARANTINE)
    list_p.add_argument("--kind", choices=["tool_error", "session_summary", "worker_receipt"])

    tool_p = sub.add_parser("tool-error", help="capture a sanitized tool failure")
    tool_p.add_argument("--root", type=Path, default=DEFAULT_QUARANTINE)
    tool_p.add_argument("--session-id", required=True)
    tool_p.add_argument("--project", required=True)
    tool_p.add_argument("--agent", required=True)
    tool_p.add_argument("--tool", required=True)
    tool_p.add_argument("--command", dest="tool_command", required=True)
    tool_p.add_argument("--exit-code", type=int, required=True)
    tool_p.add_argument("--stderr", default="")
    tool_p.add_argument("--stdout", default="")

    session_p = sub.add_parser("session-summary", help="capture a sanitized session summary")
    session_p.add_argument("--root", type=Path, default=DEFAULT_QUARANTINE)
    session_p.add_argument("--session-id", required=True)
    session_p.add_argument("--project", required=True)
    session_p.add_argument("--agent", required=True)
    session_p.add_argument("--decision", action="append", default=[])
    session_p.add_argument("--verified-output", action="append", default=[])
    session_p.add_argument("--open-risk", action="append", default=[])
    session_p.add_argument("--changed-file", action="append", default=[])

    worker_p = sub.add_parser("worker-receipt", help="capture a sanitized team worker receipt")
    worker_p.add_argument("--root", type=Path, default=DEFAULT_QUARANTINE)
    worker_p.add_argument("--session-id", required=True)
    worker_p.add_argument("--project", required=True)
    worker_p.add_argument("--agent", required=True)
    worker_p.add_argument("--lane", required=True)
    worker_p.add_argument("--task-id", required=True)
    worker_p.add_argument("--summary", required=True)
    worker_p.add_argument("--finding", action="append", default=[])
    worker_p.add_argument("--artifact-path", action="append", default=[])
    worker_p.add_argument("--verification-status", choices=["pass", "partial", "fail"], default="partial")
    worker_p.add_argument("--next-action", action="append", default=[])

    promote_p = sub.add_parser("promote", help="promote one reviewed quarantine item to a KG draft note")
    promote_p.add_argument("--root", type=Path, default=DEFAULT_QUARANTINE)
    promote_p.add_argument("--knowledge-root", type=Path, default=ROOT / "knowledge")
    promote_p.add_argument("item_id")
    promote_p.add_argument("--destination", choices=["kg-draft"], default="kg-draft")
    promote_p.add_argument("--reviewer", required=True)
    promote_p.add_argument("--rationale", required=True)
    promote_p.add_argument("--force-reviewed", action="store_true", help="allow promotion of review_required items after explicit human review")

    args = parser.parse_args(argv)
    if args.command == "doctor":
        result = quarantine_doctor(args.root)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    if args.command == "list":
        print(json.dumps(list_quarantine_items(args.root, args.kind), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "tool-error":
        result = capture_tool_error(
            root=args.root,
            session_id=args.session_id,
            project=args.project,
            agent=args.agent,
            tool=args.tool,
            command=args.tool_command,
            exit_code=args.exit_code,
            stderr=args.stderr,
            stdout=args.stdout,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "session-summary":
        result = capture_session_summary(
            root=args.root,
            session_id=args.session_id,
            project=args.project,
            agent=args.agent,
            decisions=args.decision,
            verified_outputs=args.verified_output,
            open_risks=args.open_risk,
            changed_files=args.changed_file,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "worker-receipt":
        result = capture_worker_receipt(
            root=args.root,
            session_id=args.session_id,
            project=args.project,
            agent=args.agent,
            lane=args.lane,
            task_id=args.task_id,
            summary=args.summary,
            findings=args.finding,
            artifact_paths=args.artifact_path,
            verification_status=args.verification_status,
            next_actions=args.next_action,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "promote":
        result = promote_quarantine_item(
            root=args.root,
            knowledge_root=args.knowledge_root,
            item_id=args.item_id,
            destination=args.destination,
            reviewer=args.reviewer,
            rationale=args.rationale,
            force_reviewed=args.force_reviewed,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
