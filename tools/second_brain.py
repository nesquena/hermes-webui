#!/usr/bin/env python3
"""Small CLI entrypoint for Yuto's Markdown second brain.

Source of truth stays in Markdown. This tool only makes lookup, status checks,
and note capture easier.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
KNOWLEDGE = ROOT / "knowledge"
USER_MEMORY = Path.home() / ".hermes" / "memories" / "USER.md"
YUTO_MEMORY = Path.home() / ".hermes" / "memories" / "MEMORY.md"
CORE_SKILL_ROOTS = [
    Path.home() / ".hermes" / "skills" / "software-development",
    Path.home() / ".hermes" / "skills" / "yuto",
    Path.home() / ".hermes" / "skills" / "yuto-maintenance-audit",
]
GRAPH_OUT = KNOWLEDGE / ".graph"
COCOINDEX_SANDBOX = ROOT / "tools" / "cocoindex_secondbrain"
COCOINDEX_OUT = ROOT / ".cocoindex-secondbrain" / "index"
COCOINDEX_DB = ROOT / ".cocoindex-secondbrain" / "cocoindex.db"
MEMORY_QUARANTINE = ROOT / ".memory-quarantine"
SESSIONS_DIR = Path.home() / ".hermes" / "sessions"
MEMORY_PALACE = KNOWLEDGE / "memory-palace.json"


@dataclass(frozen=True)
class SearchHit:
    path: Path
    line: int
    preview: str


@dataclass(frozen=True)
class CocoHit:
    path: str
    title: str
    field: str
    preview: str
    line: int | None = None


@dataclass(frozen=True)
class SessionHit:
    path: Path
    session_id: str
    last_updated: str
    message_count: int
    preview: str
    matched_terms: list[str]


@dataclass(frozen=True)
class MemoryEntry:
    index: int
    chars: int
    text: str
    recommendation: str


@dataclass(frozen=True)
class PalaceHit:
    palace_id: str
    wing: str
    room: str
    title: str
    summary: str
    paths: list[str]
    commands: list[str]


def slugify(title: str) -> str:
    slug = title.strip().lower()
    slug = re.sub(r"[^a-z0-9ก-๙]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    if not slug:
        raise ValueError("title must contain at least one letter or number")
    return slug


def iter_markdown_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        [p for p in root.rglob("*.md") if ".graph" not in p.parts and ".graph-core" not in p.parts],
        key=lambda p: str(p).lower(),
    )


def search_notes(query: str, root: Path = KNOWLEDGE, limit: int = 12) -> list[SearchHit]:
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        raise ValueError("query must not be empty")
    hits: list[SearchHit] = []
    for path in iter_markdown_files(root):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        hay_path = str(path.relative_to(root)).lower()
        for idx, line in enumerate(lines, start=1):
            hay = f"{hay_path} {line.lower()}"
            if all(term in hay for term in terms):
                hits.append(SearchHit(path=path, line=idx, preview=line.strip()[:180]))
                break
        if len(hits) >= limit:
            break
    return hits


def load_cocoindex_metadata() -> list[dict[str, object]]:
    if not COCOINDEX_OUT.exists():
        return []
    rows: list[dict[str, object]] = []
    for path in sorted(COCOINDEX_OUT.glob("*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def cocoindex_json_name(note_path: Path) -> str:
    rel = note_path.relative_to(KNOWLEDGE)
    return "__".join(rel.parts) + ".json"


def cocoindex_health() -> dict[str, object]:
    """Compare Markdown source notes with the disposable CocoIndex JSON cache."""
    source_notes = iter_markdown_files(KNOWLEDGE)
    derived_by_name = {p.name: p for p in COCOINDEX_OUT.glob("*.json")} if COCOINDEX_OUT.exists() else {}
    missing: list[str] = []
    stale: list[str] = []

    for note in source_notes:
        rel = note.relative_to(KNOWLEDGE)
        json_name = cocoindex_json_name(note)
        derived = derived_by_name.get(json_name)
        if derived is None:
            missing.append(str(rel))
            continue
        try:
            row = json.loads(derived.read_text(encoding="utf-8"))
            digest = hashlib.sha256(note.read_bytes()).hexdigest()
        except (OSError, json.JSONDecodeError):
            stale.append(str(rel))
            continue
        if row.get("path") != str(rel) or row.get("sha256") != digest:
            stale.append(str(rel))

    expected_names = {cocoindex_json_name(note) for note in source_notes}
    orphan_derived = sorted(name for name in derived_by_name if name not in expected_names)
    return {
        "source_notes": len(source_notes),
        "derived_json_files": len(derived_by_name),
        "missing": missing,
        "stale": stale,
        "orphan_derived": orphan_derived,
        "ok": not missing and not stale and not orphan_derived and len(source_notes) == len(derived_by_name),
    }


def _matching_body_line(body: str, terms: list[str]) -> tuple[int, str] | None:
    for idx, line in enumerate(body.splitlines(), start=1):
        hay = line.lower()
        if all(term in hay for term in terms):
            return idx, line.strip()[:220]
    return None


def search_cocoindex_metadata(query: str, limit: int = 12) -> list[CocoHit]:
    terms = [t.lower() for t in query.split() if t.strip()]
    if not terms:
        raise ValueError("query must not be empty")
    hits: list[CocoHit] = []
    for row in load_cocoindex_metadata():
        path = str(row.get("path", ""))
        title = str(row.get("title", ""))
        headings = [str(h.get("text", "")) for h in row.get("headings", []) if isinstance(h, dict)]
        wikilinks = [str(w) for w in row.get("wikilinks", [])]
        body = str(row.get("body", ""))
        candidates = [
            ("path", path),
            ("title", title),
            ("heading", " | ".join(headings[:20])),
            ("wikilink", " | ".join(wikilinks)),
        ]
        for field, value in candidates:
            hay = value.lower()
            if all(term in hay for term in terms):
                hits.append(CocoHit(path=path, title=title, field=field, preview=value[:220]))
                break
        else:
            body_hit = _matching_body_line(body, terms)
            if body_hit is not None:
                line, preview = body_hit
                hits.append(CocoHit(path=path, title=title, field="body", preview=preview, line=line))
        if len(hits) >= limit:
            break
    return hits


def print_coco_hits(hits: list[CocoHit]) -> None:
    if not hits:
        print("No matches")
        return
    for hit in hits:
        location = f"{hit.path}:{hit.line}" if hit.line is not None else hit.path
        print(f"{location} | {hit.title} | {hit.field}: {hit.preview}")


def _session_text(data: dict[str, object]) -> str:
    parts: list[str] = []
    for key in ("session_id", "title", "session_start", "last_updated"):
        value = data.get(key)
        if value:
            parts.append(str(value))
    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            parts.append(str(message.get("role", "")))
            content = message.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            else:
                parts.append(json.dumps(content, ensure_ascii=False, default=str))
    return "\n".join(parts)


def recent_sessions(*, limit: int = 8, query: str | None = None, sessions_dir: Path = SESSIONS_DIR) -> list[SessionHit]:
    terms = [term.lower() for term in query.split() if term.strip()] if query else []
    hits: list[SessionHit] = []
    if not sessions_dir.exists():
        return hits
    for path in sorted(sessions_dir.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        text = _session_text(data)
        hay = text.lower()
        matched = [term for term in terms if term in hay]
        if terms and len(matched) != len(terms):
            continue
        preview = re.sub(r"\s+", " ", text).strip()[:240]
        hits.append(
            SessionHit(
                path=path,
                session_id=str(data.get("session_id") or path.stem.removeprefix("session_")),
                last_updated=str(data.get("last_updated") or data.get("session_start") or ""),
                message_count=int(data.get("message_count") or 0),
                preview=preview,
                matched_terms=matched,
            )
        )
        if len(hits) >= limit:
            break
    return hits


def print_session_hits(hits: list[SessionHit]) -> None:
    if not hits:
        print("No recent session matches")
        return
    for hit in hits:
        terms = f" terms={','.join(hit.matched_terms)}" if hit.matched_terms else ""
        print(f"{hit.path} | id={hit.session_id} | updated={hit.last_updated} | messages={hit.message_count}{terms}\n  {hit.preview}")


def memory_entries(memory_file: Path = YUTO_MEMORY) -> list[MemoryEntry]:
    if not memory_file.exists():
        return []
    text = memory_file.read_text(encoding="utf-8")
    entries = [entry.strip() for entry in text.split("§") if entry.strip()]
    results: list[MemoryEntry] = []
    for index, entry in enumerate(entries, start=1):
        if any(keyword in entry.lower() for keyword in ("path", "/users/kei", "knowledge/", "skill", "cocoindex", "book expert")):
            recommendation = "keep pointer or shorten path label"
        elif len(entry) > 140:
            recommendation = "demote detail to knowledge note; replace with short pointer"
        else:
            recommendation = "keep"
        results.append(MemoryEntry(index=index, chars=len(entry), text=entry, recommendation=recommendation))
    return results


def memory_demote_candidates(memory_file: Path = YUTO_MEMORY, *, min_chars: int = 140) -> list[MemoryEntry]:
    return [entry for entry in memory_entries(memory_file) if entry.chars >= min_chars]


def print_memory_entries(entries: list[MemoryEntry]) -> None:
    if not entries:
        print("No memory demotion candidates")
        return
    for entry in entries:
        print(f"{entry.index:02d} chars={entry.chars} recommendation={entry.recommendation}\n  {entry.text}")


def load_memory_palace(path: Path = MEMORY_PALACE) -> list[dict[str, object]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("memory palace must be a list of entries")
    return [entry for entry in data if isinstance(entry, dict)]


def search_memory_palace(query: str, *, path: Path = MEMORY_PALACE, limit: int = 12) -> list[PalaceHit]:
    terms = [term.lower() for term in query.split() if term.strip()]
    if not terms:
        raise ValueError("query must not be empty")
    hits: list[PalaceHit] = []
    for entry in load_memory_palace(path):
        hay = json.dumps(entry, ensure_ascii=False).lower()
        if not all(term in hay for term in terms):
            continue
        hits.append(
            PalaceHit(
                palace_id=str(entry.get("palace_id", "")),
                wing=str(entry.get("wing", "")),
                room=str(entry.get("room", "")),
                title=str(entry.get("title", "")),
                summary=str(entry.get("summary", "")),
                paths=[str(p) for p in entry.get("paths", []) if p],
                commands=[str(c) for c in entry.get("commands", []) if c],
            )
        )
        if len(hits) >= limit:
            break
    return hits


def memory_palace_doctor(path: Path = MEMORY_PALACE) -> dict[str, object]:
    entries = load_memory_palace(path)
    duplicate_ids: list[str] = []
    seen: set[str] = set()
    missing_paths: list[dict[str, str]] = []
    for entry in entries:
        palace_id = str(entry.get("palace_id", ""))
        if palace_id in seen:
            duplicate_ids.append(palace_id)
        seen.add(palace_id)
        for raw_path in entry.get("paths", []) if isinstance(entry.get("paths", []), list) else []:
            p = Path(str(raw_path))
            if not p.is_absolute():
                p = ROOT / p
            if not p.exists():
                missing_paths.append({"palace_id": palace_id, "path": str(raw_path)})
    return {"entries": len(entries), "duplicate_ids": duplicate_ids, "missing_paths": missing_paths, "ok": not duplicate_ids and not missing_paths}


def print_palace_hits(hits: list[PalaceHit]) -> None:
    if not hits:
        print("No palace matches")
        return
    for hit in hits:
        print(f"{hit.palace_id} | {hit.wing}/{hit.room} | {hit.title}\n  {hit.summary}")
        for path in hit.paths[:4]:
            print(f"  path: {path}")
        for command in hit.commands[:3]:
            print(f"  cmd: {command}")


def palace_entries_as_hits(path: Path = MEMORY_PALACE) -> list[PalaceHit]:
    return [
        PalaceHit(
            palace_id=str(entry.get("palace_id", "")),
            wing=str(entry.get("wing", "")),
            room=str(entry.get("room", "")),
            title=str(entry.get("title", "")),
            summary=str(entry.get("summary", "")),
            paths=[str(p) for p in entry.get("paths", []) if p],
            commands=[str(c) for c in entry.get("commands", []) if c],
        )
        for entry in load_memory_palace(path)
    ]


def update_cocoindex() -> int:
    script = COCOINDEX_SANDBOX / "run.sh"
    if not script.exists():
        raise FileNotFoundError(f"missing CocoIndex run script: {script}")
    proc = subprocess.run([str(script)], cwd=COCOINDEX_SANDBOX, text=True, check=False)
    return proc.returncode


def note_template(title: str, kind: str, why: str, evidence: str, next_action: str) -> str:
    today = date.today().isoformat()
    return f"""# {title}

Created: {today}
Type: {kind}

Conclusion:
- {why or 'TBD'}

Evidence:
- {evidence or 'TBD'}

Use / Retrieval:
- Ask Yuto: "ค้น second brain เรื่อง {title}"
- CLI: `python3 tools/second_brain.py search {slugify(title)}`

Next:
- {next_action or 'Review and connect to index/sources/workflows if it becomes durable.'}

Related: [[second-brain-dashboard]]
"""


def create_note(title: str, kind: str = "note", why: str = "", evidence: str = "", next_action: str = "") -> Path:
    path = KNOWLEDGE / f"{slugify(title)}.md"
    if path.exists():
        raise FileExistsError(f"note already exists: {path}")
    path.write_text(note_template(title, kind, why, evidence, next_action), encoding="utf-8")
    return path


def run_graph() -> str:
    cmd = [
        sys.executable,
        "-m",
        "tools.yuto_graph.build_graph",
        "--root",
        str(KNOWLEDGE),
        "--memory-file",
        str(USER_MEMORY),
        "--memory-file",
        str(YUTO_MEMORY),
    ]
    for root in CORE_SKILL_ROOTS:
        cmd.extend(["--extra-root", str(root)])
    cmd.extend(["--out", str(GRAPH_OUT)])
    proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stdout + proc.stderr).strip())
    return proc.stdout.strip()


def cocoindex_status() -> dict[str, object]:
    derived_files = sorted(COCOINDEX_OUT.glob("*.json")) if COCOINDEX_OUT.exists() else []
    return {
        "sandbox": str(COCOINDEX_SANDBOX),
        "installed": (COCOINDEX_SANDBOX / ".venv" / "bin" / "cocoindex").exists(),
        "db_exists": COCOINDEX_DB.exists(),
        "db": str(COCOINDEX_DB),
        "index_dir": str(COCOINDEX_OUT),
        "derived_json_files": len(derived_files),
        "health": cocoindex_health(),
    }


def capture_status() -> dict[str, object]:
    from tools.memory_capture.capture import quarantine_doctor

    return quarantine_doctor(MEMORY_QUARANTINE)


def capture_items(kind: str | None = None) -> list[dict[str, object]]:
    from tools.memory_capture.capture import list_quarantine_items

    return list_quarantine_items(MEMORY_QUARANTINE, kind=kind)


def capture_promote(
    item_id: str,
    *,
    reviewer: str,
    rationale: str,
    destination: str = "kg-draft",
    force_reviewed: bool = False,
) -> dict[str, object]:
    from tools.memory_capture.capture import promote_quarantine_item

    return promote_quarantine_item(
        root=MEMORY_QUARANTINE,
        knowledge_root=KNOWLEDGE,
        item_id=item_id,
        destination=destination,  # type: ignore[arg-type]
        reviewer=reviewer,
        rationale=rationale,
        force_reviewed=force_reviewed,
    )


def status() -> dict[str, object]:
    graph_summary = run_graph()
    knowledge_notes = iter_markdown_files(KNOWLEDGE)
    obsidian_bridge = Path.home() / "Documents" / "Obsidian Vault" / "Yuto Second Brain.md"
    return {
        "knowledge_root": str(KNOWLEDGE),
        "notes": len(knowledge_notes),
        "graph": graph_summary,
        "dashboard": str(KNOWLEDGE / "second-brain-dashboard.md"),
        "index": str(KNOWLEDGE / "index.md"),
        "cocoindex": cocoindex_status(),
        "capture": capture_status(),
        "obsidian_bridge_exists": obsidian_bridge.exists(),
        "obsidian_bridge": str(obsidian_bridge),
    }


def print_hits(hits: list[SearchHit], root: Path = KNOWLEDGE) -> None:
    if not hits:
        print("No matches")
        return
    for hit in hits:
        rel = hit.path.relative_to(root)
        print(f"{rel}:{hit.line}: {hit.preview}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Use and inspect Yuto's second brain")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="rebuild graph and show current entrypoints")

    recent_p = sub.add_parser("recent", help="inspect latest raw Hermes session files before session_search summaries")
    recent_p.add_argument("--query", default="")
    recent_p.add_argument("--limit", type=int, default=8)

    search_p = sub.add_parser("search", help="search knowledge Markdown notes")
    search_p.add_argument("query")
    search_p.add_argument("--limit", type=int, default=12)

    coco_p = sub.add_parser("coco", help="use the CocoIndex derived metadata index")
    coco_sub = coco_p.add_subparsers(dest="coco_command", required=True)
    coco_sub.add_parser("update", help="refresh the CocoIndex derived index")
    coco_search_p = coco_sub.add_parser("search", help="search CocoIndex derived records: path/title/headings/wikilinks/body")
    coco_search_p.add_argument("query")
    coco_search_p.add_argument("--limit", type=int, default=12)
    coco_sub.add_parser("status", help="show CocoIndex derived-index status")
    coco_sub.add_parser("doctor", help="check source/index drift without updating")

    capture_p = sub.add_parser("capture", help="inspect Yuto memory quarantine capture layer")
    capture_sub = capture_p.add_subparsers(dest="capture_command", required=True)
    capture_sub.add_parser("doctor", help="validate quarantine JSON/JSONL files")
    capture_sub.add_parser("status", help="show quarantine capture status")
    capture_list_p = capture_sub.add_parser("list", help="list quarantined capture items")
    capture_list_p.add_argument("--kind", choices=["tool_error", "session_summary", "worker_receipt"])
    capture_promote_p = capture_sub.add_parser("promote", help="promote one reviewed quarantine item to a KG draft note")
    capture_promote_p.add_argument("item_id")
    capture_promote_p.add_argument("--destination", choices=["kg-draft"], default="kg-draft")
    capture_promote_p.add_argument("--reviewer", required=True)
    capture_promote_p.add_argument("--rationale", required=True)
    capture_promote_p.add_argument("--force-reviewed", action="store_true")

    memory_p = sub.add_parser("memory", help="inspect active memory pressure and demotion candidates")
    memory_sub = memory_p.add_subparsers(dest="memory_command", required=True)
    memory_sub.add_parser("entries", help="list MEMORY.md entries with sizes")
    memory_candidates_p = memory_sub.add_parser("candidates", help="list long entries that should be shortened or demoted")
    memory_candidates_p.add_argument("--min-chars", type=int, default=140)

    palace_p = sub.add_parser("palace", help="search or validate the durable memory-palace retrieval map")
    palace_sub = palace_p.add_subparsers(dest="palace_command", required=True)
    palace_search_p = palace_sub.add_parser("search", help="search memory-palace entries")
    palace_search_p.add_argument("query")
    palace_search_p.add_argument("--limit", type=int, default=12)
    palace_sub.add_parser("doctor", help="validate memory-palace IDs and referenced paths")
    palace_sub.add_parser("list", help="list memory-palace entries")

    open_p = sub.add_parser("path", help="print an important second-brain path")
    open_p.add_argument("name", choices=["root", "index", "dashboard", "graph", "obsidian", "cocoindex", "quarantine", "palace"])

    new_p = sub.add_parser("new", help="create a small evidence-first note")
    new_p.add_argument("title")
    new_p.add_argument("--type", default="note", choices=["note", "source", "decision", "workflow", "product"])
    new_p.add_argument("--why", default="")
    new_p.add_argument("--evidence", default="")
    new_p.add_argument("--next", default="")

    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "recent":
        print_session_hits(recent_sessions(limit=args.limit, query=args.query or None))
    elif args.command == "search":
        print_hits(search_notes(args.query, limit=args.limit))
    elif args.command == "coco":
        if args.coco_command == "update":
            return update_cocoindex()
        if args.coco_command == "search":
            print_coco_hits(search_cocoindex_metadata(args.query, limit=args.limit))
        elif args.coco_command == "status":
            print(json.dumps(cocoindex_status(), ensure_ascii=False, indent=2))
        elif args.coco_command == "doctor":
            health = cocoindex_health()
            print(json.dumps(health, ensure_ascii=False, indent=2))
            return 0 if health["ok"] else 1
    elif args.command == "capture":
        if args.capture_command == "list":
            print(json.dumps(capture_items(args.kind), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        if args.capture_command == "promote":
            promoted = capture_promote(
                args.item_id,
                destination=args.destination,
                reviewer=args.reviewer,
                rationale=args.rationale,
                force_reviewed=args.force_reviewed,
            )
            print(json.dumps(promoted, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        health = capture_status()
        print(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if health["ok"] else 1
    elif args.command == "memory":
        if args.memory_command == "entries":
            print_memory_entries(memory_entries())
        elif args.memory_command == "candidates":
            print_memory_entries(memory_demote_candidates(min_chars=args.min_chars))
    elif args.command == "palace":
        if args.palace_command == "search":
            print_palace_hits(search_memory_palace(args.query, limit=args.limit))
        elif args.palace_command == "list":
            print_palace_hits(palace_entries_as_hits())
        elif args.palace_command == "doctor":
            health = memory_palace_doctor()
            print(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if health["ok"] else 1
    elif args.command == "path":
        paths = {
            "root": KNOWLEDGE,
            "index": KNOWLEDGE / "index.md",
            "dashboard": KNOWLEDGE / "second-brain-dashboard.md",
            "graph": GRAPH_OUT / "report.md",
            "obsidian": Path.home() / "Documents" / "Obsidian Vault" / "Yuto Second Brain.md",
            "cocoindex": COCOINDEX_SANDBOX,
            "quarantine": MEMORY_QUARANTINE,
            "palace": MEMORY_PALACE,
        }
        print(paths[args.name])
    elif args.command == "new":
        print(create_note(args.title, args.type, args.why, args.evidence, args.next))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
