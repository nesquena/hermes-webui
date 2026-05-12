#!/usr/bin/env python3
"""Read-only memory scout for Yuto.

Collects small, redacted signals from raw Hermes sessions, active memory,
Second Brain/CocoIndex, Book Expert Factory, and worker receipts. This script
never writes memory or promotes knowledge; it only prints a JSON snapshot for
Yuto or a scheduled cron agent to review.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


def detect_repo_root() -> Path:
    """Find kei-jarvis even when cron executes a copy from ~/.hermes/scripts."""
    candidates = [
        Path(os.environ.get("YUTO_REPO", "")),
        Path.cwd(),
        Path(__file__).resolve().parents[1],
        Path.home() / "kei-jarvis",
        Path("/Users/kei/kei-jarvis"),
    ]
    for candidate in candidates:
        if candidate and (candidate / "tools" / "second_brain.py").exists():
            return candidate.resolve()
    return Path.cwd().resolve()


ROOT = detect_repo_root()
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
MEMORY_FILE = Path.home() / ".hermes" / "memories" / "MEMORY.md"
USER_FILE = Path.home() / ".hermes" / "memories" / "USER.md"
BOOK_FACTORY = ROOT / "knowledge" / "book-expert-factory"
QUARANTINE = ROOT / ".memory-quarantine"
TEAM_RECEIPTS = ROOT / "knowledge" / "yuto-team-lane-receipts.jsonl"
HR_ROLES_DIR = ROOT / "knowledge" / "company-hr-roles"
HR_RECEIPTS = ROOT / "knowledge" / "company-hr-receipts.jsonl"
WORKFORCE_KIT = ROOT / "company" / "workforce" if (ROOT / "company" / "workforce").exists() else ROOT / "knowledge" / "company-workforce"
DIGITAL_FORENSIC_LAB = ROOT / "company" / "departments" / "digital-forensic-lab" if (ROOT / "company" / "departments" / "digital-forensic-lab").exists() else ROOT / "knowledge" / "digital-forensic-lab"

SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
]

WATCH_TERMS = [
    "Traceback",
    "PermissionError",
    "Operation not permitted",
    "failed",
    "error",
    "last_delivery_error",
    "ลองใหม่",
    "ผิด",
    "ล่าสุด",
    "memory",
    "AI-Books",
    "Book Expert Factory",
    "CocoIndex",
    "worker_receipt",
]


def redact(text: str) -> str:
    out = text
    for pattern in SECRET_PATTERNS:
        out = pattern.sub("[REDACTED_SECRET]", out)
    return out


def compact(text: str, limit: int = 300) -> str:
    return redact(re.sub(r"\s+", " ", text).strip()[:limit])


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return None


def config_memory_limits(config_path: Path | None = None) -> dict[str, int]:
    """Read Hermes built-in memory limits from config.yaml with safe defaults."""
    config_path = config_path or (Path.home() / ".hermes" / "config.yaml")
    defaults = {"user": 1375, "memory": 2200}
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        memory_config = config.get("memory") or {}
        return {
            "user": int(memory_config.get("user_char_limit", defaults["user"])),
            "memory": int(memory_config.get("memory_char_limit", defaults["memory"])),
        }
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return defaults


def memory_pressure() -> dict[str, Any]:
    limits = config_memory_limits()

    def file_info(path: Path, limit: int) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        return {
            "path": str(path),
            "exists": path.exists(),
            "chars": len(text),
            "limit": limit,
            "pct": round((len(text) / limit) * 100, 1) if limit else None,
        }

    entries = []
    if MEMORY_FILE.exists():
        for i, entry in enumerate([e.strip() for e in MEMORY_FILE.read_text(encoding="utf-8").split("§") if e.strip()], start=1):
            if len(entry) >= 140:
                entries.append({"index": i, "chars": len(entry), "preview": compact(entry, 220)})
    return {
        "user": file_info(USER_FILE, limits["user"]),
        "memory": file_info(MEMORY_FILE, limits["memory"]),
        "long_memory_entries": entries,
    }


def recent_session_signals(limit: int = 8) -> list[dict[str, Any]]:
    signals = []
    if not SESSIONS_DIR.exists():
        return signals
    for path in sorted(SESSIONS_DIR.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        data = read_json(path) or {}
        if data:
            message_parts = []
            messages = data.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if isinstance(message, dict):
                        message_parts.append(str(message.get("role", "")))
                        message_parts.append(str(message.get("content", "")))
            text = "\n".join(message_parts)
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
        matched = [term for term in WATCH_TERMS if term.lower() in text.lower()]
        snippets = []
        for term in matched[:8]:
            idx = text.lower().find(term.lower())
            if idx >= 0:
                snippets.append({"term": term, "snippet": compact(text[max(0, idx - 120) : idx + 220])})
        signals.append(
            {
                "path": str(path),
                "session_id": data.get("session_id", path.stem.removeprefix("session_")),
                "last_updated": data.get("last_updated"),
                "message_count": data.get("message_count"),
                "matched_terms": matched,
                "snippets": snippets[:5],
            }
        )
    return signals


def second_brain_status() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "tools/second_brain.py", "status"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": compact(proc.stdout + proc.stderr, 1000)}
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "error": "status output was not JSON", "preview": compact(proc.stdout, 1000)}
    return {
        "ok": True,
        "notes": data.get("notes"),
        "graph": data.get("graph"),
        "cocoindex_health": (data.get("cocoindex") or {}).get("health"),
        "capture": data.get("capture"),
    }


def book_factory_status() -> dict[str, Any]:
    sources = sorted((BOOK_FACTORY / "sources").glob("*.json")) if (BOOK_FACTORY / "sources").exists() else []
    blueprints = sorted((BOOK_FACTORY / "blueprints").glob("*.json")) if (BOOK_FACTORY / "blueprints").exists() else []
    bp = []
    for path in blueprints:
        data = read_json(path) or {}
        bp.append(
            {
                "file": path.name,
                "expert_id": data.get("expert_id"),
                "status": data.get("status"),
                "source_count": data.get("source_count"),
                "skills": data.get("skills"),
            }
        )
    return {"sources": len(sources), "blueprints": bp, "receipts": len(list((BOOK_FACTORY / "receipts").glob("*"))) if (BOOK_FACTORY / "receipts").exists() else 0}


def team_receipt_status() -> dict[str, Any]:
    data: dict[str, Any] = {"team_receipts_file": str(TEAM_RECEIPTS), "exists": TEAM_RECEIPTS.exists()}
    if TEAM_RECEIPTS.exists():
        lines = TEAM_RECEIPTS.read_text(encoding="utf-8", errors="ignore").splitlines()
        data["line_count"] = len(lines)
        data["recent"] = [compact(line, 260) for line in lines[-5:]]
    if QUARANTINE.exists():
        data["quarantine_jsonl_files"] = [str(p.relative_to(QUARANTINE)) for p in sorted(QUARANTINE.rglob("*.jsonl"))[:20]]
    return data


def hr_people_ops_status() -> dict[str, Any]:
    data: dict[str, Any] = {
        "roles_dir": str(HR_ROLES_DIR),
        "roles_dir_exists": HR_ROLES_DIR.exists(),
        "receipts_file": str(HR_RECEIPTS),
        "receipts_exists": HR_RECEIPTS.exists(),
        "role_manifest_count": len(list(HR_ROLES_DIR.glob("*.yaml"))) if HR_ROLES_DIR.exists() else 0,
    }
    if HR_RECEIPTS.exists():
        lines = HR_RECEIPTS.read_text(encoding="utf-8", errors="ignore").splitlines()
        data["receipt_count"] = len([line for line in lines if line.strip()])
        data["recent_receipts"] = [compact(line, 320) for line in lines[-3:]]
    validator = ROOT / "tools" / "company_hr_roles.py"
    if validator.exists():
        proc = subprocess.run(
            [sys.executable, "tools/company_hr_roles.py", "--json", "--summary-receipts"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        if proc.returncode != 0:
            data["validator"] = {"ok": False, "error": compact(proc.stdout + proc.stderr, 1000)}
        else:
            try:
                data["validator"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data["validator"] = {"ok": False, "error": "validator output was not JSON", "preview": compact(proc.stdout, 1000)}
    else:
        data["validator"] = {"ok": False, "error": "tools/company_hr_roles.py not found"}
    return data


def workforce_kit_status() -> dict[str, Any]:
    data: dict[str, Any] = {
        "kit_dir": str(WORKFORCE_KIT),
        "kit_exists": WORKFORCE_KIT.exists(),
        "files": sorted(p.name for p in WORKFORCE_KIT.glob("*.yaml")) if WORKFORCE_KIT.exists() else [],
    }
    validator = ROOT / "tools" / "company_workforce.py"
    if validator.exists():
        proc = subprocess.run(
            [sys.executable, "tools/company_workforce.py", "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        if proc.returncode != 0:
            data["validator"] = {"ok": False, "error": compact(proc.stdout + proc.stderr, 1000)}
        else:
            try:
                data["validator"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data["validator"] = {"ok": False, "error": "validator output was not JSON", "preview": compact(proc.stdout, 1000)}
    else:
        data["validator"] = {"ok": False, "error": "tools/company_workforce.py not found"}
    return data


def digital_forensic_lab_status() -> dict[str, Any]:
    data: dict[str, Any] = {
        "lab_dir": str(DIGITAL_FORENSIC_LAB),
        "lab_exists": DIGITAL_FORENSIC_LAB.exists(),
        "files": sorted(p.name for p in DIGITAL_FORENSIC_LAB.iterdir() if p.is_file()) if DIGITAL_FORENSIC_LAB.exists() else [],
    }
    validator = ROOT / "tools" / "digital_forensic_lab.py"
    if validator.exists():
        proc = subprocess.run(
            [sys.executable, "tools/digital_forensic_lab.py", "--json"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=90,
        )
        if proc.returncode != 0:
            data["validator"] = {"ok": False, "error": compact(proc.stdout + proc.stderr, 1000)}
        else:
            try:
                data["validator"] = json.loads(proc.stdout)
            except json.JSONDecodeError:
                data["validator"] = {"ok": False, "error": "validator output was not JSON", "preview": compact(proc.stdout, 1000)}
    else:
        data["validator"] = {"ok": False, "error": "tools/digital_forensic_lab.py not found"}
    return data


def build_snapshot(session_limit: int) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_memory_scout",
        "repo_root": str(ROOT),
        "rules": [
            "candidate signals only; not authority",
            "do not write USER.md or MEMORY.md automatically",
            "Yuto verifies before promotion/demotion",
            "redacted snippets only",
        ],
        "memory_pressure": memory_pressure(),
        "recent_sessions": recent_session_signals(limit=session_limit),
        "second_brain": second_brain_status(),
        "book_expert_factory": book_factory_status(),
        "team_receipts": team_receipt_status(),
        "hr_people_ops": hr_people_ops_status(),
        "workforce_kit": workforce_kit_status(),
        "digital_forensic_lab": digital_forensic_lab_status(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Yuto memory scout")
    parser.add_argument("--session-limit", type=int, default=8)
    args = parser.parse_args(argv)
    print(json.dumps(build_snapshot(args.session_limit), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
