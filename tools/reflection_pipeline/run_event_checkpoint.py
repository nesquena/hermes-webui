#!/usr/bin/env python3
"""Run an event-driven Reflection Pipeline checkpoint.

This is intentionally small and conservative:
- creates redacted archive + candidate template only;
- never promotes memory;
- is idempotent for the same session;
- writes a machine-readable status report for Yuto/LabOps.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.reflection_pipeline.export_session_candidate import (
    DEFAULT_ARCHIVE,
    DEFAULT_SESSIONS,
    load_session,
    latest_session_path,
    render_archive,
    render_candidate,
)

DEFAULT_REFLECTIONS = REPO / "conversation-reflections"
DEFAULT_REPORT = REPO / "lab-ops" / "status" / "reflection_checkpoint_latest.json"
VALID_TRIGGERS = {
    "manual",
    "after-complex-task",
    "context-compression",
    "model-switch",
    "session-close",
    "user-correction",
    "background-agent-finished",
}


def safe_session_id(session: dict[str, Any], source: Path) -> str:
    session_id = session.get("session_id") or source.stem
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id))[:120]


def ensure_reflection_dirs(reflections_root: Path) -> None:
    for folder in ("candidate", "promoted", "rejected", "stale"):
        directory = reflections_root / folder
        directory.mkdir(parents=True, exist_ok=True)
        gitkeep = directory / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")


def run_checkpoint(
    session: str | Path | None = None,
    archive_dir: str | Path = DEFAULT_ARCHIVE,
    reflections_root: str | Path = DEFAULT_REFLECTIONS,
    report_path: str | Path = DEFAULT_REPORT,
    trigger: str = "manual",
) -> dict[str, Any]:
    if trigger not in VALID_TRIGGERS:
        raise ValueError(f"unsupported trigger: {trigger}")

    source = Path(session).expanduser().resolve() if session else latest_session_path(DEFAULT_SESSIONS)
    archive_dir = Path(archive_dir).expanduser().resolve()
    reflections_root = Path(reflections_root).expanduser().resolve()
    report_path = Path(report_path).expanduser().resolve()

    data = load_session(source)
    safe_id = safe_session_id(data, source)
    ensure_reflection_dirs(reflections_root)
    archive_dir.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    archive_path = archive_dir / f"{safe_id}.redacted.md"
    candidate_path = reflections_root / "candidate" / f"{safe_id}.candidate.md"
    created_archive = False
    created_candidate = False

    if not archive_path.exists():
        archive_path.write_text(render_archive(data, source), encoding="utf-8")
        created_archive = True

    if not candidate_path.exists():
        candidate_path.write_text(render_candidate(data, source, archive_path), encoding="utf-8")
        created_candidate = True

    result: dict[str, Any] = {
        "ok": True,
        "trigger": trigger,
        "source": str(source),
        "archive": str(archive_path),
        "candidate": str(candidate_path),
        "created_archive": created_archive,
        "created_candidate": created_candidate,
        "message_count": len(data.get("messages") or []),
        "promotion_status": "candidate",
        "auto_promoted": False,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "next_review_step": "Yuto must review candidate evidence before any memory/knowledge/skill promotion.",
    }
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run safe event-driven reflection checkpoint")
    parser.add_argument("session", nargs="?", help="Path to Hermes session JSON; defaults to latest session")
    parser.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--reflections-root", default=str(DEFAULT_REFLECTIONS))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--trigger", default="manual", choices=sorted(VALID_TRIGGERS))
    args = parser.parse_args()

    result = run_checkpoint(
        session=args.session,
        archive_dir=args.archive_dir,
        reflections_root=args.reflections_root,
        report_path=args.report,
        trigger=args.trigger,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
