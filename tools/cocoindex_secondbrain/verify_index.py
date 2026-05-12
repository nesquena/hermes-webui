from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE = ROOT / "knowledge"
INDEX = ROOT / ".cocoindex-secondbrain" / "index"


def main() -> int:
    source_notes = sorted(p for p in KNOWLEDGE.rglob("*.md") if ".graph" not in p.parts and ".graph-core" not in p.parts)
    derived = sorted(INDEX.glob("*.json")) if INDEX.exists() else []
    sample = None
    if derived:
        sample = json.loads(derived[0].read_text(encoding="utf-8"))
    report = {
        "knowledge_notes": len(source_notes),
        "derived_json_files": len(derived),
        "index_dir": str(INDEX),
        "sample": {
            "file": derived[0].name,
            "path": sample.get("path"),
            "title": sample.get("title"),
            "heading_count": len(sample.get("headings", [])),
            "wikilink_count": len(sample.get("wikilinks", [])),
        } if sample else None,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if len(source_notes) != len(derived):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
