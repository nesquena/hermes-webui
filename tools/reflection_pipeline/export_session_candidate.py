#!/usr/bin/env python3
"""Export a Hermes session into a redacted archive plus candidate-memory template.

This script is deliberately conservative:
- it does not call an LLM;
- it redacts common secret patterns before writing Markdown;
- it writes candidate memory as `promotion_status: candidate`, never promoted.

Raw session JSON remains the source of truth under ~/.hermes/sessions/.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SESSIONS = Path.home() / ".hermes" / "sessions"
DEFAULT_ARCHIVE = REPO / "conversation-archive" / "redacted"
DEFAULT_CANDIDATES = REPO / "conversation-reflections" / "candidate"

SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "sk-[REDACTED]"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9_]{20,}"), "gh[REDACTED]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{20,}"), "xox-[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((api[_-]?key|token|secret|password)\s*[:=]\s*)['\"]?[^'\"\s]{8,}"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def latest_session_path(sessions_dir: Path = DEFAULT_SESSIONS) -> Path:
    candidates = sorted(sessions_dir.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise SystemExit(f"No session_*.json found in {sessions_dir}")
    return candidates[0]


def load_session(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def render_archive(session: dict[str, Any], source: Path) -> str:
    session_id = session.get("session_id") or source.stem
    created = session.get("session_start") or session.get("created_at") or "unknown"
    updated = session.get("last_updated") or "unknown"
    model = session.get("model") or "unknown"
    messages = session.get("messages") or []

    lines = [
        f"# Redacted Conversation Archive: {session_id}",
        "",
        f"source_path: `{source}`",
        f"session_start: `{created}`",
        f"last_updated: `{updated}`",
        f"model: `{model}`",
        f"message_count: `{len(messages)}`",
        "",
        "> Redacted export for evidence/review only. Raw log is archive, not memory.",
        "",
    ]
    for i, msg in enumerate(messages, start=1):
        role = msg.get("role", "unknown")
        text = redact(message_text(msg)).strip()
        if not text:
            continue
        lines.extend([f"## {i}. {role}", "", text, ""])
    return "\n".join(lines).rstrip() + "\n"


def render_candidate(session: dict[str, Any], source: Path, archive_path: Path) -> str:
    session_id = session.get("session_id") or source.stem
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return f"""---
source_log: {source}
redacted_archive: {archive_path}
created_at: {now}
model: none-script-template
confidence: low
promotion_status: candidate
---

# Candidate Memory Review: {session_id}

This file is a quarantine template. Fill or replace `items` only after reviewing the redacted archive and, when needed, the raw source.

```yaml
items:
  - memory_type: semantic|episodic|procedural
    type: preference|decision|lesson|open_task|workflow_candidate|project_context|risk
    claim: ""
    evidence: ""
    trust_level: model_inferred
    source_path: {source}
    source_url: null
    source_quote: ""
    verified_at: null
    expires_at: null
    promotion_status: candidate
    recommended_destination: archive_only
    promotion_reason: ""
    risk: unverified
    canary: ""
```

Review rules:

- Do not promote raw archive text directly.
- Do not promote `model_inferred` claims without verification.
- Mutable/current-state claims require `expires_at` or live recheck.
- If rejected, move this file to `conversation-reflections/rejected/` with a short reason.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", nargs="?", help="Path to Hermes session JSON. Defaults to newest ~/.hermes/sessions/session_*.json")
    parser.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--candidate-dir", default=str(DEFAULT_CANDIDATES))
    args = parser.parse_args()

    source = Path(args.session).expanduser() if args.session else latest_session_path()
    source = source.resolve()
    session = load_session(source)
    session_id = session.get("session_id") or source.stem
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(session_id))[:120]

    archive_dir = Path(args.archive_dir).expanduser().resolve()
    candidate_dir = Path(args.candidate_dir).expanduser().resolve()
    archive_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    reflections_root = candidate_dir.parent
    for folder in ("promoted", "rejected", "stale"):
        (reflections_root / folder).mkdir(parents=True, exist_ok=True)
        gitkeep = reflections_root / folder / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")

    archive_path = archive_dir / f"{safe_id}.redacted.md"
    candidate_path = candidate_dir / f"{safe_id}.candidate.md"

    archive_path.write_text(render_archive(session, source), encoding="utf-8")
    candidate_path.write_text(render_candidate(session, source, archive_path), encoding="utf-8")

    print(json.dumps({
        "source": str(source),
        "archive": str(archive_path),
        "candidate": str(candidate_path),
        "message_count": len(session.get("messages") or []),
        "promotion_status": "candidate",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
