#!/usr/bin/env python3
"""Benchmark Yuto's hybrid CocoIndex Code search canaries."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path("/Users/kei/kei-jarvis")

CANARIES: list[tuple[str, list[str]]] = [
    ("memory scout root detection", ["tools/memory_scout.py", "tests/test_memory_scout.py", "knowledge/yuto-memory-scout.md"]),
    ("script that checks Second Brain status in memory scout", ["tools/memory_scout.py"]),
    ("memory palace doctor missing paths duplicate ids", ["tools/second_brain.py", "tests/test_second_brain.py", "knowledge/memory-palace.md"]),
    ("Book Expert Factory blueprint verify before promoting skills", ["tools/book_expert_factory.py", "knowledge/yuto-multi-book-expert-skill-factory.md"]),
    ("latest recall raw Hermes sessions by mtime", ["tools/second_brain.py", "knowledge/memory-system.md", "knowledge/second-brain-dashboard.md"]),
    ("Memory Scout reads Hermes config memory_char_limit 3000", ["tools/memory_scout.py", "tests/test_memory_scout.py"]),
    ("CocoIndex second brain derived index health check", ["tools/second_brain.py", "tools/cocoindex_secondbrain/README.md", "knowledge/second-brain-dashboard.md"]),
    ("team lane receipts worker receipt status", ["tools/memory_scout.py", "knowledge/yuto-team-lanes.md", "knowledge/yuto-team-lane-receipts.jsonl"]),
]


def score_rank(rank: int | None) -> float:
    if rank == 1:
        return 1.0
    if rank is not None and rank <= 3:
        return 0.9
    if rank is not None and rank <= 5:
        return 0.7
    if rank is not None:
        return 0.4
    return 0.0


def run_benchmark(root: Path = ROOT, limit: int = 8) -> dict[str, Any]:
    results = []
    for query, expected in CANARIES:
        proc = subprocess.run(
            ["python", "tools/yuto_code_search.py", query, "--limit", str(limit), "--json"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if proc.returncode != 0:
            results.append({"query": query, "expected": expected, "top_files": [], "match": [], "score": 0.0, "error": proc.stderr.strip()})
            continue
        hits = json.loads(proc.stdout)
        files = [hit["path"] for hit in hits]
        matches = []
        for i, file in enumerate(files, start=1):
            if any(file == exp or file.startswith(exp + ":") or exp in file for exp in expected):
                matches.append([i, file])
        top_rank = matches[0][0] if matches else None
        results.append({"query": query, "expected": expected, "top_files": files[:5], "match": matches[:3], "score": score_rank(top_rank)})
    avg = sum(item["score"] for item in results) / len(results)
    return {"root": str(root), "canaries": len(results), "score_10": round(avg * 10, 2), "pass": avg * 10 >= 9.0, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Yuto hybrid code search")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--min-score", type=float, default=9.0)
    args = parser.parse_args()
    report = run_benchmark(Path(args.root))
    report["pass"] = report["score_10"] >= args.min_score
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
