#!/usr/bin/env python3
"""Semantic graph audit for Yuto knowledge notes.

This complements link hygiene. It does not decide truth; it flags review targets:
- duplicate titles/headings that may indicate duplicated concepts;
- mutable-current claims that may need live verification or expiry;
- source trails that mention claims/evidence but no URL/path-style source pointer.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

MUTABLE_RE = re.compile(
    r"\b(currently|latest|today|now|running|installed|available|works|healthy)\b",
    re.IGNORECASE,
)
SOURCE_WORD_RE = re.compile(r"(?im)^\s*(source|evidence|claim|citation)\s*:")
SOURCE_POINTER_RE = re.compile(r"(https?://|/Users/|/tmp/|~/|\btools/|\bknowledge/|\btests/|\bsession[_-]|\[\[[^\]]+\]\])")
POLICY_RE = re.compile(
    r"\b(must|should|do not|don't|never|always|use|keep|avoid|prefer|require|requires|when|if|before|after|rule|policy|target|status:|enough for now)\b",
    re.IGNORECASE,
)


def iter_markdown_files(paths: list[Path]):
    for root in paths:
        if root.is_file() and root.suffix == ".md":
            if not any(part.startswith(".graph") for part in root.parts):
                yield root
            continue
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            if any(part.startswith(".graph") for part in path.parts):
                continue
            yield path


def title_for(path: Path, text: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip().lower()
    return path.stem.lower()


def audit_paths(paths: list[Path]) -> dict[str, Any]:
    files = list(iter_markdown_files(paths))
    titles: dict[str, list[str]] = defaultdict(list)
    stale_mutable_claims: list[dict[str, Any]] = []
    weak_source_trails: list[dict[str, Any]] = []

    for path in files:
        text = path.read_text(encoding="utf-8")
        title = title_for(path, text)
        titles[title].append(str(path))
        historical_source_note = bool(
            re.search(r"(?im)^\s*source\s*:", text)
            and re.search(r"(?im)^\s*(captured|date)\s*:", text)
        )

        for line_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.endswith(":"):
                continue
            if not historical_source_note and MUTABLE_RE.search(stripped) and not POLICY_RE.search(stripped) and not re.search(r"\b(verify|verified|recheck|expires_at|source_path|source_url)\b", stripped, re.IGNORECASE):
                stale_mutable_claims.append({"file": str(path), "line": line_no, "text": stripped[:220]})

        if SOURCE_WORD_RE.search(text) and not SOURCE_POINTER_RE.search(text):
            weak_source_trails.append({"file": str(path), "reason": "mentions source/evidence/claim but lacks URL/path/session pointer"})

    duplicate_titles = [
        {"title": title, "files": file_list}
        for title, file_list in sorted(titles.items())
        if title and len(file_list) > 1
    ]

    return {
        "files_checked": len(files),
        "diagnostics": {
            "duplicate_titles": duplicate_titles,
            "stale_mutable_claims": stale_mutable_claims,
            "weak_source_trails": weak_source_trails,
        },
        "counts": {
            "duplicate_titles": len(duplicate_titles),
            "stale_mutable_claims": len(stale_mutable_claims),
            "weak_source_trails": len(weak_source_trails),
        },
    }


def write_report(report: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "semantic-audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Yuto Semantic Graph Audit",
        "",
        f"Files checked: {report['files_checked']}",
        "",
        "## Counts",
    ]
    for key, value in report["counts"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    for name, items in report["diagnostics"].items():
        lines += [f"## {name}", ""]
        if not items:
            lines.append("None.")
        else:
            for item in items[:50]:
                lines.append(f"- `{json.dumps(item, ensure_ascii=False)}`")
            if len(items) > 50:
                lines.append(f"- ... {len(items) - 50} more")
        lines.append("")
    (out_dir / "semantic-audit.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="Files or directories to audit")
    parser.add_argument("--out", default="/Users/kei/kei-jarvis/knowledge/.graph")
    args = parser.parse_args(argv)
    report = audit_paths([Path(p).expanduser().resolve() for p in args.paths])
    write_report(report, Path(args.out).expanduser().resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
