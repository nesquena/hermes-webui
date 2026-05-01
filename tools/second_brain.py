#!/usr/bin/env python3
"""Small CLI entrypoint for Yuto's Markdown second brain.

Source of truth stays in Markdown. This tool only makes lookup, status checks,
and note capture easier.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KNOWLEDGE = ROOT / "knowledge"
USER_MEMORY = Path.home() / ".hermes" / "memories" / "USER.md"
YUTO_MEMORY = Path.home() / ".hermes" / "memories" / "MEMORY.md"
CORE_SKILL_ROOTS = [
    Path.home() / ".hermes" / "skills" / "software-development",
    Path.home() / ".hermes" / "skills" / "yuto",
    Path.home() / ".hermes" / "skills" / "yuto-maintenance-audit",
]
GRAPH_OUT = KNOWLEDGE / ".graph"


@dataclass(frozen=True)
class SearchHit:
    path: Path
    line: int
    preview: str


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

    search_p = sub.add_parser("search", help="search knowledge Markdown notes")
    search_p.add_argument("query")
    search_p.add_argument("--limit", type=int, default=12)

    open_p = sub.add_parser("path", help="print an important second-brain path")
    open_p.add_argument("name", choices=["root", "index", "dashboard", "graph", "obsidian"])

    new_p = sub.add_parser("new", help="create a small evidence-first note")
    new_p.add_argument("title")
    new_p.add_argument("--type", default="note", choices=["note", "source", "decision", "workflow", "product"])
    new_p.add_argument("--why", default="")
    new_p.add_argument("--evidence", default="")
    new_p.add_argument("--next", default="")

    args = parser.parse_args(argv)
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == "search":
        print_hits(search_notes(args.query, limit=args.limit))
    elif args.command == "path":
        paths = {
            "root": KNOWLEDGE,
            "index": KNOWLEDGE / "index.md",
            "dashboard": KNOWLEDGE / "second-brain-dashboard.md",
            "graph": GRAPH_OUT / "report.md",
            "obsidian": Path.home() / "Documents" / "Obsidian Vault" / "Yuto Second Brain.md",
        }
        print(paths[args.name])
    elif args.command == "new":
        print(create_note(args.title, args.type, args.why, args.evidence, args.next))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
