"""Reversible WebUI session cleanup helpers.

The cleanup mode deliberately starts conservative:
- scan is read-only;
- only explicit cleanup candidates can be quarantined;
- quarantine moves JSON files to a restoreable trash directory with a manifest;
- state.db is never modified here.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MESSAGING_SOURCES = {"telegram", "discord", "slack", "signal", "matrix", "yuanbao"}
KNOWN_SOURCES = {"webui", "cron", "cli", "gateway", "browser"} | MESSAGING_SOURCES
DAY = 86400
_SECRET_RE = re.compile(r"(?i)\b(api[_-]?key|token|password|passwd|secret|credential|cookie|bearer)\b\s*[:=]\s*[^\s,;]+")


def _redact_text(value: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", value)


def _safe_session_id(session_id: str) -> bool:
    return bool(session_id) and all(c in "0123456789abcdefghijklmnopqrstuvwxyz_" for c in session_id)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _messages_count(payload: dict[str, Any]) -> int:
    msgs = payload.get("messages")
    return len(msgs) if isinstance(msgs, list) else 0


def _message_text(payload: dict[str, Any], *, limit: int = 12000) -> str:
    """Return bounded message text for local retention heuristics.

    This deliberately does not call an LLM or persist raw transcript text. It is
    used only to decide whether a chat needs structured review before deletion.
    """
    parts: list[str] = []
    msgs = payload.get("messages")
    if isinstance(msgs, list):
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            content = msg.get("content") or msg.get("text") or ""
            if isinstance(content, list):
                content = " ".join(str(x.get("text") if isinstance(x, dict) else x) for x in content)
            parts.append(str(content))
            if sum(len(p) for p in parts) >= limit:
                break
    for key in ("summary", "handoff_summary", "title"):
        value = payload.get(key)
        if value:
            parts.append(str(value))
    return "\n".join(parts)[:limit]


def _structured_retention_review(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify whether a chat contains durable value before raw deletion."""
    lower = _message_text(payload).lower()
    targets: list[str] = []
    reasons: list[str] = []
    keyword_sets = [
        ("secret_risk", ["api_key", "apikey", "secret", "token", "password", "passwd", "credential", "cookie", "bearer", "비밀", "토큰", "패스워드", "비밀번호", "자격증명"]),
        ("memory_candidate", ["remember", "기억", "기억해", "선호", "prefer", "preference", "앞으로", "항상", "don't", "하지마", "하지 말", "사용자는"]),
        ("skill_candidate", ["절차", "workflow", "워크플로", "반복", "재사용", "해결", "debug", "검증", "runbook", "sop", "방법"]),
        ("obsidian_candidate", ["obsidian", "rule map", "soul", "contract", "계약", "규칙", "권한", "authority", "승인", "merge review", " mr", "canonical", "정책"]),
        ("handoff_candidate", ["handoff", "인수인계", "safe_next_action", "next step", "다음", "진행", "blocked", "blocker", "todo", "남은", "pending"]),
    ]
    for target, keywords in keyword_sets:
        if any(k in lower for k in keywords):
            targets.append(target)
    if targets:
        reasons.append("raw_chat_not_long_term_memory")
    if "secret_risk" in targets:
        reasons.append("secret_or_credential_risk")
    decision = "discard_raw_ok"
    if targets:
        decision = "structure_before_delete"
    if "secret_risk" in targets:
        decision = "do_not_preserve_secret_review"
    return {
        "retention_decision": decision,
        "structured_targets": targets,
        "retention_reasons": reasons,
        "raw_chat_preservation": "not_required" if not targets else "review_structured_summary_only",
    }


def _source(payload: dict[str, Any], path: Path) -> str:
    for key in ("source", "session_source", "raw_source", "source_tag", "platform"):
        value = str(payload.get(key) or "").strip().lower()
        if value:
            return value
    sid = str(payload.get("session_id") or path.stem).lower()
    title = str(payload.get("title") or "").lower()
    if sid.startswith("cron") or "cron" in title:
        return "cron"
    return "webui"


def _coerce_timestamp(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


def _age_days(payload: dict[str, Any], path: Path, now: float) -> float:
    for key in ("updated_at", "last_message_at", "last_updated", "session_start", "created_at"):
        ts = _coerce_timestamp(payload.get(key))
        if ts is not None:
            return max(0.0, (now - ts) / DAY)
    return max(0.0, (now - path.stat().st_mtime) / DAY)


def _age_bucket(age_days: float) -> str:
    if age_days <= 7:
        return "0-7d"
    if age_days <= 14:
        return "8-14d"
    if age_days <= 30:
        return "15-30d"
    if age_days <= 90:
        return "31-90d"
    return "90d+"


def _session_summary(path: Path, payload: dict[str, Any], *, now: float) -> dict[str, Any]:
    sid = str(payload.get("session_id") or path.stem)
    src = _source(payload, path)
    msg_count = _messages_count(payload)
    age = _age_days(payload, path, now)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "session_id": sid,
        "title": _redact_text(str(payload.get("title") or sid)[:120]),
        "source": src,
        "age_days": round(age, 2),
        "age_bucket": _age_bucket(age),
        "message_count": msg_count,
        "bytes": size,
        "reasons": [],
    }


def _classify(path: Path, payload: dict[str, Any], *, now: float, current_session_id: str | None = None) -> tuple[str, dict[str, Any]]:
    item = _session_summary(path, payload, now=now)
    review = _structured_retention_review(payload)
    item.update({
        "retention_decision": review["retention_decision"],
        "structured_targets": review["structured_targets"],
        "raw_chat_preservation": review["raw_chat_preservation"],
    })
    sid = item["session_id"]
    src = item["source"]
    age = item["age_days"]
    msg_count = item["message_count"]
    last_status = str(payload.get("last_status") or payload.get("status") or "").lower()

    protected_reasons = []
    if current_session_id and sid == current_session_id:
        protected_reasons.append("current_session")
    if payload.get("pinned") or payload.get("starred"):
        protected_reasons.append("pinned")
    if payload.get("active_stream_id") or payload.get("streaming") or payload.get("pending"):
        protected_reasons.append("active_or_streaming")
    if src in MESSAGING_SOURCES:
        protected_reasons.append("messaging_source")
    if src == "cli":
        protected_reasons.append("cli_state_db_linked")
    if protected_reasons:
        item["reasons"] = protected_reasons
        return "protected", item

    if msg_count == 0:
        item["reasons"] = ["zero_messages"]
        return "cleanup_candidates", item
    if src == "cron" and last_status in {"error", "failed", "failure"}:
        item["reasons"] = ["cron_failure_retained_30d"]
        return "protected" if age <= 30 else "needs_review", item
    if src == "cron" and age >= 14:
        if review["structured_targets"]:
            item["reasons"] = review["retention_reasons"] + ["cron_success_structured_review_required"]
            return "needs_review", item
        item["reasons"] = ["cron_success_older_than_14d"]
        return "cleanup_candidates", item
    if src == "webui" and age >= 30:
        archived = bool(payload.get("archived") or payload.get("is_archived") or payload.get("deleted"))
        if archived:
            if review["structured_targets"]:
                item["reasons"] = review["retention_reasons"] + ["webui_archived_structured_review_required"]
                return "needs_review", item
            item["reasons"] = ["webui_archived_older_than_30d"]
            return "cleanup_candidates", item
        item["reasons"] = ["webui_non_archived_retained"]
        return "protected", item
    if src not in KNOWN_SOURCES:
        item["reasons"] = ["unknown_source_review_required"]
        return "needs_review", item

    item["reasons"] = ["retention_window"]
    return "protected", item


def build_session_cleanup_report(
    *,
    session_dir: str | Path,
    state_db_path: str | Path | None = None,
    now: float | None = None,
    current_session_id: str | None = None,
) -> dict[str, Any]:
    """Return a read-only cleanup report for WebUI JSON session files."""
    session_dir = Path(session_dir)
    now = time.time() if now is None else float(now)
    buckets = {"cleanup_candidates": [], "protected": [], "needs_review": [], "invalid": []}
    source_counts: dict[str, int] = {}
    age_buckets: dict[str, int] = {}
    total_bytes = 0

    for path in sorted(session_dir.glob("*.json")) if session_dir.exists() else []:
        if path.name.startswith("_") or ".tmp." in path.name:
            continue
        if path.is_symlink():
            try:
                size = path.lstat().st_size
            except OSError:
                size = 0
            buckets["invalid"].append({"path": path.name, "reasons": ["symlink_not_allowed"], "bytes": size})
            continue
        payload = _read_json(path)
        if payload is None:
            buckets["invalid"].append({"path": path.name, "reasons": ["invalid_json"], "bytes": path.stat().st_size})
            continue
        payload_sid = str(payload.get("session_id") or "")
        expected_stems = {payload_sid, f"session_{payload_sid}"} if payload_sid else {path.stem}
        if payload_sid and path.stem not in expected_stems:
            buckets["invalid"].append({
                "path": path.name,
                "session_id": payload_sid,
                "reasons": ["session_id_filename_mismatch"],
                "bytes": path.stat().st_size,
            })
            continue
        if payload_sid and not _safe_session_id(payload_sid):
            buckets["invalid"].append({
                "path": path.name,
                "session_id": payload_sid,
                "reasons": ["invalid_session_id"],
                "bytes": path.stat().st_size,
            })
            continue
        if not payload_sid and not _safe_session_id(path.stem):
            buckets["invalid"].append({
                "path": path.name,
                "session_id": path.stem,
                "reasons": ["invalid_session_id"],
                "bytes": path.stat().st_size,
            })
            continue
        kind, item = _classify(path, payload, now=now, current_session_id=current_session_id)
        item["path"] = path.name
        buckets[kind].append(item)
        source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1
        age_buckets[item["age_bucket"]] = age_buckets.get(item["age_bucket"], 0) + 1
        total_bytes += item["bytes"]

    estimated = sum(i.get("bytes", 0) for i in buckets["cleanup_candidates"])
    return {
        "ok": True,
        "mode": "read_only",
        "generated_at": now,
        "session_dir": str(session_dir),
        "state_db_path": str(state_db_path) if state_db_path else None,
        "policy": {
            "cron_success_days": 14,
            "cron_failure_days": 30,
            "webui_days": 30,
            "zero_message": "candidate",
            "messaging": "protected",
            "cli_state_db": "protected",
            "state_db": "not_modified",
        },
        "summary": {
            "total_sessions": sum(len(v) for k, v in buckets.items() if k != "invalid"),
            "total_bytes": total_bytes,
            "estimated_reclaim_bytes": estimated,
            "cleanup_candidate_count": len(buckets["cleanup_candidates"]),
            "protected_count": len(buckets["protected"]),
            "needs_review_count": len(buckets["needs_review"]),
            "invalid_count": len(buckets["invalid"]),
            "source_counts": source_counts,
            "age_buckets": age_buckets,
            "state_db_touched": False,
            "structured_review_count": sum(
                1 for items in (buckets["cleanup_candidates"], buckets["needs_review"])
                for item in items
                if item.get("retention_decision") in {"structure_before_delete", "do_not_preserve_secret_review"}
            ),
        },
        **buckets,
    }


def build_session_retention_plan(report: dict[str, Any]) -> dict[str, Any]:
    """Build a read-only plan for structured retention before raw deletion."""
    cleanup = list(report.get("cleanup_candidates", []) or [])
    needs_review = list(report.get("needs_review", []) or [])
    protected = list(report.get("protected", []) or [])
    structured = [
        item for item in needs_review + cleanup
        if item.get("retention_decision") in {"structure_before_delete", "do_not_preserve_secret_review"}
    ]
    quarantine_ready = [
        item for item in cleanup
        if item.get("retention_decision") == "discard_raw_ok" and not item.get("structured_targets")
    ]
    structured_ids = {item.get("session_id") for item in structured}
    keep = [item for item in protected if item.get("session_id") not in structured_ids]
    return {
        "ok": True,
        "mode": "read_only_retention_plan",
        "generated_at": report.get("generated_at"),
        "policy": {
            "raw_chat": "quarantine/delete only after structured value is promoted or judged disposable",
            "memory": "compact durable preferences/environment facts only",
            "skills": "repeatable procedures and workflows",
            "obsidian": "rules/contracts/canonical decisions through MR path",
            "secrets": "do not preserve raw credentials",
            "state_db": "not_modified",
        },
        "summary": {
            "quarantine_ready_count": len(quarantine_ready),
            "structure_before_delete_count": len(structured),
            "keep_count": len(keep),
            "needs_review_count": len(needs_review),
            "state_db_touched": False,
        },
        "quarantine_ready": quarantine_ready,
        "structured_candidates": structured,
        "keep": keep,
    }


def _invalidate_index(session_dir: Path) -> None:
    try:
        (session_dir / "_index.json").unlink(missing_ok=True)
    except Exception:
        pass


def quarantine_sessions(
    session_ids: list[str],
    *,
    report: dict[str, Any],
    session_dir: str | Path,
    trash_root: str | Path,
    actor: str = "webui",
) -> dict[str, Any]:
    """Move selected cleanup candidates into a manifest-backed quarantine."""
    session_dir = Path(session_dir).resolve()
    trash_root = Path(trash_root).resolve()
    candidate_ids = {str(i.get("session_id")): i for i in report.get("cleanup_candidates", [])}
    requested = [str(sid) for sid in session_ids if isinstance(sid, str)]
    batch = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    batch_dir = trash_root / batch
    trash_sessions = batch_dir / "sessions"
    trash_sessions.mkdir(parents=True, exist_ok=False)

    manifest = {
        "operation": "quarantine",
        "created_at": time.time(),
        "actor": actor,
        "session_dir": str(session_dir),
        "state_db_touched": False,
        "items": [],
        "skipped": [],
    }
    moved: list[str] = []
    skipped: list[dict[str, str]] = []

    for sid in requested:
        if not _safe_session_id(sid):
            skipped.append({"session_id": sid, "reason": "invalid_session_id"})
            continue
        candidate = candidate_ids.get(sid)
        if not candidate:
            skipped.append({"session_id": sid, "reason": "not_cleanup_candidate"})
            continue
        candidate_path_name = str(candidate.get("path") or "")
        if candidate_path_name not in {f"{sid}.json", f"session_{sid}.json"}:
            skipped.append({"session_id": sid, "reason": "candidate_path_mismatch"})
            continue
        src = session_dir / candidate_path_name
        if src.is_symlink():
            skipped.append({"session_id": sid, "reason": "symlink_not_allowed"})
            continue
        try:
            src_resolved = src.resolve(strict=False)
            src_resolved.relative_to(session_dir)
        except Exception:
            skipped.append({"session_id": sid, "reason": "path_escape"})
            continue
        if not src.exists() or not src.is_file():
            skipped.append({"session_id": sid, "reason": "missing_source"})
            continue
        dst = trash_sessions / src.name
        shutil.move(str(src), str(dst))
        moved.append(sid)
        manifest["items"].append({
            "session_id": sid,
            "source_path": str(src),
            "trash_path": str(dst),
            "bytes": candidate.get("bytes", 0),
            "reasons": candidate.get("reasons", []),
        })

    manifest["skipped"] = skipped
    manifest_path = batch_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if moved:
        _invalidate_index(session_dir)
    return {"ok": True, "moved": moved, "skipped": skipped, "manifest_path": str(manifest_path), "state_db_touched": False}


def restore_quarantine(manifest_path: str | Path, *, session_dir: str | Path) -> dict[str, Any]:
    """Restore files listed in a quarantine manifest back to the session dir."""
    session_dir = Path(session_dir).resolve()
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_trash_dir = (manifest_path.parent / "sessions").resolve()
    restored: list[str] = []
    skipped: list[dict[str, str]] = []
    session_dir.mkdir(parents=True, exist_ok=True)
    for item in manifest.get("items", []):
        sid = str(item.get("session_id") or "")
        if not _safe_session_id(sid):
            skipped.append({"session_id": sid, "reason": "invalid_session_id"})
            continue
        raw_src = Path(item.get("trash_path") or manifest_path.parent / "sessions" / f"{sid}.json")
        src = raw_src.resolve()
        source_name = Path(str(item.get("source_path") or f"{sid}.json")).name
        if source_name not in {f"{sid}.json", f"session_{sid}.json"}:
            skipped.append({"session_id": sid, "reason": "source_name_mismatch"})
            continue
        dst = session_dir / source_name
        try:
            src.relative_to(expected_trash_dir)
        except Exception:
            skipped.append({"session_id": sid, "reason": "trash_path_escape"})
            continue
        try:
            dst_resolved = dst.resolve(strict=False)
            dst_resolved.relative_to(session_dir)
        except Exception:
            skipped.append({"session_id": sid, "reason": "path_escape"})
            continue
        if raw_src.is_symlink() or src.is_symlink():
            skipped.append({"session_id": sid, "reason": "symlink_not_allowed"})
            continue
        if not src.exists() or not src.is_file():
            skipped.append({"session_id": sid, "reason": "missing_trash_file"})
            continue
        if dst.exists():
            skipped.append({"session_id": sid, "reason": "destination_exists"})
            continue
        shutil.move(str(src), str(dst))
        restored.append(sid)
    if restored:
        _invalidate_index(session_dir)
    return {"ok": True, "restored": restored, "skipped": skipped, "state_db_touched": False}


def delete_quarantine(manifest_path: str | Path) -> dict[str, Any]:
    """Permanently delete only files listed in a quarantine manifest.

    This is intentionally narrower than deleting a whole quarantine directory:
    stray files, symlinks, escaped paths, and anything outside the manifest's
    own ``sessions/`` directory are skipped. Live ``session_dir`` and state.db
    are never touched by this helper.
    """
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_trash_dir = (manifest_path.parent / "sessions").resolve()
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []

    for item in manifest.get("items", []):
        sid = str(item.get("session_id") or "")
        if not _safe_session_id(sid):
            skipped.append({"session_id": sid, "reason": "invalid_session_id"})
            continue
        raw_path = Path(item.get("trash_path") or expected_trash_dir / f"{sid}.json")
        if raw_path.is_symlink():
            skipped.append({"session_id": sid, "reason": "symlink_not_allowed"})
            continue
        try:
            target = raw_path.resolve(strict=False)
            target.relative_to(expected_trash_dir)
        except Exception:
            skipped.append({"session_id": sid, "reason": "trash_path_escape"})
            continue
        if not target.exists() or not target.is_file():
            skipped.append({"session_id": sid, "reason": "missing_trash_file"})
            continue
        try:
            target.unlink()
            deleted.append(sid)
        except OSError:
            skipped.append({"session_id": sid, "reason": "delete_failed"})

    deletion_manifest = {
        "operation": "delete_quarantine",
        "created_at": time.time(),
        "source_manifest": str(manifest_path),
        "deleted": deleted,
        "skipped": skipped,
        "state_db_touched": False,
    }
    (manifest_path.parent / "deleted-manifest.json").write_text(
        json.dumps(deletion_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "deleted": deleted,
        "skipped": skipped,
        "deletion_manifest_path": str(manifest_path.parent / "deleted-manifest.json"),
        "state_db_touched": False,
    }


def latest_manifest(trash_root: str | Path) -> Path | None:
    trash_root = Path(trash_root)
    manifests = sorted(trash_root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return manifests[0] if manifests else None
