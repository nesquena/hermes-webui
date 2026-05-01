#!/usr/bin/env python3
"""Validate Reflection Pipeline candidate/promoted files.

This is a small runtime canary, not a full memory system. It catches the most
important contaminated-memory failures:
- promoted files without provenance;
- promoted files still marked model_inferred;
- candidate files accidentally marked promoted;
- missing canary fields.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "conversation-reflections"
REQUIRED_FIELDS = [
    "memory_type",
    "trust_level",
    "source_path",
    "source_quote",
    "verified_at",
    "promotion_status",
    "canary",
]


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def has_field(text: str, field: str) -> bool:
    return re.search(rf"(^|\n)\s*(?:-\s*)?{re.escape(field)}\s*:", text) is not None


def validate_file(path: Path, folder: str) -> list[str]:
    text = read_text(path)
    errors: list[str] = []

    for field in REQUIRED_FIELDS:
        if not has_field(text, field):
            errors.append(f"missing field: {field}")

    if folder == "candidate":
        if not re.search(r"promotion_status\s*:\s*candidate\b", text):
            errors.append("candidate file must contain promotion_status: candidate")
    elif folder == "promoted":
        if re.search(r"trust_level\s*:\s*model_inferred\b", text):
            errors.append("promoted file cannot rely on trust_level: model_inferred")
        if re.search(r"verified_at\s*:\s*(null|None|)\s*(\n|$)", text):
            errors.append("promoted file requires verified_at")
        if not re.search(r"promotion_status\s*:\s*promoted\b", text):
            errors.append("promoted file must contain promotion_status: promoted")
        if not (has_field(text, "source_path") or has_field(text, "source_url")):
            errors.append("promoted file requires source_path or source_url")
    elif folder in {"rejected", "stale"}:
        if not re.search(rf"promotion_status\s*:\s*{folder}\b", text):
            errors.append(f"{folder} file should contain promotion_status: {folder}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(ROOT))
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()

    report: dict[str, object] = {"root": str(root), "checked": 0, "errors": []}
    all_errors: list[dict[str, object]] = []

    for folder in ["candidate", "promoted", "rejected", "stale"]:
        directory = root / folder
        if not directory.exists():
            all_errors.append({"file": str(directory), "errors": ["missing directory"]})
            continue
        for path in sorted(directory.glob("*.md")):
            report["checked"] = int(report["checked"]) + 1
            errors = validate_file(path, folder)
            if errors:
                all_errors.append({"file": str(path), "errors": errors})

    report["errors"] = all_errors
    report["ok"] = not all_errors
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not all_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
