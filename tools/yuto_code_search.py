#!/usr/bin/env python3
"""Yuto-owned hybrid codebase search.

This wraps CocoIndex Code semantic search with a small local lexical scorer so
Yuto can inspect its own project accurately without delegating core-system work.
It is read-only: no indexing beyond optional `ccc search --refresh`, no edits.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

DEFAULT_ROOT = Path("/Users/kei/kei-jarvis")
INCLUDE_SUFFIXES = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".md", ".mdx", ".txt", ".rst", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".sh", ".bash", ".zsh", ".html", ".css",
}
EXCLUDE_DIRS = {
    ".git", ".cocoindex_code", ".cocoindex-secondbrain", ".memory-quarantine",
    "__pycache__", "node_modules", "dist", "build", "target", "vendor", "static",
    "docs",
}
EXCLUDE_FILES = {"CHANGELOG.md", "README.md", "yuto_code_search_benchmark.py", "test_yuto_code_search.py", "test_yuto_code_search_benchmark.py"}
EXCLUDE_FILE_PREFIXES = ("hermes_conversation_",)
TOKEN_RE = re.compile(r"[A-Za-z0-9_\-/]+|[\u0E00-\u0E7F]+")
CCC_FILE_RE = re.compile(r"^File: ([^:\n]+)(?::(\d+)-(\d+))?", re.M)


@dataclass
class SearchHit:
    path: str
    score: float
    source: str
    line: int | None = None
    snippet: str = ""


def query_terms(query: str) -> list[str]:
    terms = []
    for token in TOKEN_RE.findall(query):
        token = token.strip().lower()
        if len(token) >= 3 and token not in {"the", "and", "that", "with", "for", "how", "where"}:
            terms.append(token)
    # Preserve order while deduping.
    return list(dict.fromkeys(terms))


def iter_candidate_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS or part.startswith(".") for part in rel.parts):
            continue
        if path.name in EXCLUDE_FILES or any(path.name.startswith(prefix) for prefix in EXCLUDE_FILE_PREFIXES):
            continue
        if path.suffix.lower() not in INCLUDE_SUFFIXES:
            continue
        yield path


def domain_boost(rel: str, terms: list[str]) -> float:
    joined = " ".join(terms)
    boost = 0.0
    if {"latest", "raw", "session"} & set(terms) and (
        rel == "tools/second_brain.py" or rel in {"knowledge/memory-system.md", "knowledge/second-brain-dashboard.md"}
    ):
        boost += 0.45
    if "cocoindex" in terms and "brain" in terms and (
        rel == "tools/second_brain.py" or rel.startswith("tools/cocoindex_secondbrain/") or rel == "knowledge/second-brain-dashboard.md"
    ):
        boost += 0.35
    if "team" in terms and "receipt" in joined and (
        rel == "tools/memory_scout.py" or rel == "knowledge/yuto-team-lane-receipts.jsonl" or rel.startswith("knowledge/yuto-team-lanes")
    ):
        boost += 0.45
    if "memory" in terms and "scout" in terms and rel == "tools/memory_scout.py":
        boost += 0.25
    return boost


def compact(text: str, limit: int = 360) -> str:
    return re.sub(r"\s+", " ", text).strip()[:limit]


def lexical_search(query: str, root: Path, limit: int = 12) -> list[SearchHit]:
    terms = query_terms(query)
    if not terms:
        return []
    hits: list[SearchHit] = []
    for path in iter_candidate_files(root):
        rel = str(path.relative_to(root))
        rel_lower = rel.lower().replace("_", "-")
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hay = text.lower().replace("_", "-")
        matched_terms = [term for term in terms if term in hay or term in rel_lower]
        if not matched_terms:
            continue
        # Reward multiple-term coverage, filename/path hits, and exact phrase matches.
        coverage = len(matched_terms) / len(terms)
        path_hits = sum(1 for term in terms if term in rel_lower)
        phrase_bonus = 0.2 if query.lower() in hay else 0.0
        score = coverage + min(0.35, path_hits * 0.08) + phrase_bonus
        if rel.startswith("tools/"):
            score += 0.28
        elif rel.startswith("tests/"):
            score += 0.18
        elif rel.startswith("knowledge/"):
            score += 0.06
        if rel.endswith(('.json', '.jsonl', '.yaml', '.yml')) and coverage < 0.5:
            score -= 0.08
        score += domain_boost(rel, terms)
        first_line = None
        snippet = ""
        for idx, line in enumerate(text.splitlines(), start=1):
            line_norm = line.lower().replace("_", "-")
            if any(term in line_norm for term in matched_terms):
                first_line = idx
                start = max(0, idx - 3)
                lines = text.splitlines()[start : idx + 4]
                snippet = compact("\n".join(lines))
                break
        hits.append(SearchHit(path=rel, score=round(score, 3), source="lexical", line=first_line, snippet=snippet))
    hits.sort(key=lambda h: (h.score, -len(h.path)), reverse=True)
    return hits[:limit]


def cocoindex_search(query: str, root: Path, limit: int = 12, refresh: bool = False) -> list[SearchHit]:
    cmd = ["ccc", "search", query, "--limit", str(limit)]
    if refresh:
        cmd.append("--refresh")
    try:
        proc = subprocess.run(cmd, cwd=root, text=True, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    hits = []
    for rank, match in enumerate(CCC_FILE_RE.finditer(proc.stdout), start=1):
        path = match.group(1)
        line = int(match.group(2)) if match.group(2) else None
        # Extract text after this match until next result marker.
        end = CCC_FILE_RE.search(proc.stdout, match.end())
        chunk = proc.stdout[match.end() : end.start() if end else len(proc.stdout)]
        hits.append(SearchHit(path=path, score=round(1.0 / rank, 3), source="cocoindex", line=line, snippet=compact(chunk)))
    return hits


def merge_hits(*hit_groups: list[SearchHit], limit: int = 10) -> list[SearchHit]:
    by_path: dict[str, SearchHit] = {}
    for group in hit_groups:
        for hit in group:
            current = by_path.get(hit.path)
            if current is None:
                by_path[hit.path] = hit
                continue
            current.score = round(current.score + hit.score, 3)
            current.source = "+".join(sorted(set(current.source.split("+") + hit.source.split("+"))))
            if not current.snippet and hit.snippet:
                current.snippet = hit.snippet
            if current.line is None:
                current.line = hit.line
    merged = sorted(by_path.values(), key=lambda h: h.score, reverse=True)
    return merged[:limit]


def search(query: str, root: Path = DEFAULT_ROOT, limit: int = 10, refresh: bool = False) -> list[SearchHit]:
    root = root.resolve()
    return merge_hits(
        lexical_search(query, root, limit=max(limit, 12)),
        cocoindex_search(query, root, limit=max(limit, 12), refresh=refresh),
        limit=limit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hybrid local codebase search for Yuto self-improvement")
    parser.add_argument("query", nargs="+", help="search query")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="repo root")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--refresh", action="store_true", help="ask CocoIndex Code to refresh before semantic search")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    hits = search(" ".join(args.query), Path(args.root), limit=args.limit, refresh=args.refresh)
    if args.json:
        print(json.dumps([asdict(hit) for hit in hits], ensure_ascii=False, indent=2))
    else:
        for i, hit in enumerate(hits, start=1):
            loc = f":{hit.line}" if hit.line else ""
            print(f"{i}. {hit.path}{loc} [{hit.source}] score={hit.score}")
            if hit.snippet:
                print(f"   {hit.snippet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
