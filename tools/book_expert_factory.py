from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "knowledge" / "book-expert-factory" / "sources"

DOMAIN_KEYWORDS = [
    (
        "startup-customer-discovery",
        "customer-discovery-expert",
        ["customer", "interview", "startup", "idea validation", "market risk", "leading question", "past behavior", "assumption"],
    ),
    (
        "strategy",
        "strategy-expert",
        ["strategy", "diagnosis", "guiding policy", "coherent action", "competitive", "advantage", "market", "positioning"],
    ),
    (
        "systems-thinking",
        "systems-thinking-expert",
        ["system", "feedback", "loop", "stock", "flow", "leverage", "boundary", "second-order"],
    ),
    (
        "negotiation",
        "negotiation-expert",
        ["negotiation", "bargain", "counteroffer", "tactical empathy", "calibrated question", "agreement"],
    ),
    (
        "productivity",
        "productivity-coach",
        ["productivity", "time", "focus", "deep work", "habit", "priority", "calendar", "task"],
    ),
    (
        "marketing",
        "marketing-strategist",
        ["marketing", "brand", "positioning", "copy", "audience", "campaign", "channel", "conversion"],
    ),
    (
        "legal-forensic-evidence",
        "legal-forensic-evidence-expert",
        ["evidence", "forensic", "chain of custody", "provenance", "court", "legal", "preservation", "hash"],
    ),
]

METHOD_KEYWORDS = [
    "step",
    "framework",
    "checklist",
    "method",
    "rules",
    "principle",
    "template",
    "questions",
    "avoid",
    "mistake",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9ก-๙]+", "-", value.strip().lower()).strip("-")
    return slug[:100] or "book-source"


def _score_keywords(text: str, keywords: list[str]) -> int:
    hay = text.lower()
    return sum(1 for keyword in keywords if keyword.lower() in hay)


def classify_book_text(*, title: str, text: str) -> dict[str, Any]:
    corpus = f"{title}\n{text}"
    ranked: list[tuple[int, str, str]] = []
    for domain, role, keywords in DOMAIN_KEYWORDS:
        ranked.append((_score_keywords(corpus, keywords), domain, role))
    ranked.sort(reverse=True)
    best_score, domain, role = ranked[0]
    if best_score == 0:
        domain = "general-framework"
        role = "framework-expert"

    method_score = _score_keywords(corpus, METHOD_KEYWORDS)
    if method_score >= 4 or best_score >= 4:
        skillability = "high"
    elif method_score >= 2 or best_score >= 2:
        skillability = "medium"
    else:
        skillability = "low"

    return {
        "primary_domain": domain,
        "role_suggestion": role,
        "skillability": skillability,
        "method_signal_score": method_score,
        "domain_signal_score": best_score,
        "recommended_skill_pair": ["diagnose", "apply"],
        "recommended_update_layer": "web-update-scout",
        "needs_human_framework_review": True,
    }


def source_hierarchy() -> list[str]:
    return [
        "user_context",
        "canon_books_frameworks",
        "official_current_sources",
        "primary_research_or_cases",
        "credible_secondary_sources",
        "team_receipts_and_verified_outputs",
        "model_inference",
    ]


def read_source_sample(path: Path, max_chars: int = 12000) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_chars]
    if suffix == ".pdf":
        try:
            import fitz  # type: ignore
        except ImportError:
            return ""
        doc = fitz.open(path)
        parts = []
        for page in doc[: min(5, doc.page_count)]:
            parts.append(page.get_text("text"))
        return "\n".join(parts)[:max_chars]
    if suffix == ".epub":
        parts = []
        with zipfile.ZipFile(path) as epub:
            for name in epub.namelist():
                if not name.lower().endswith((".xhtml", ".html", ".htm")):
                    continue
                raw = epub.read(name).decode("utf-8", errors="ignore")
                text = re.sub(r"<[^>]+>", " ", raw)
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    parts.append(text)
                if sum(len(part) for part in parts) >= max_chars:
                    break
        return "\n".join(parts)[:max_chars]
    return ""


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def register_book_source(
    source_path: Path,
    *,
    registry_root: Path = DEFAULT_REGISTRY,
    title: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    path = source_path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))
    sample = read_source_sample(path)
    resolved_title = title or path.stem.replace("-", " ").replace("_", " ").title()
    classification = classify_book_text(title=resolved_title, text=sample)
    registry_root.mkdir(parents=True, exist_ok=True)
    source_id = slugify(f"{resolved_title}-{author or 'unknown'}")
    out = registry_root / f"{source_id}.json"
    record = {
        "schema_version": 1,
        "source_id": source_id,
        "source_type": "book",
        "status": "candidate_unverified_framework",
        "version": "0.1.0",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "title": resolved_title,
        "author": author or "unknown",
        "path": str(path),
        "sha256": file_sha256(path),
        "classification": classification,
        "verification": {
            "file_exists": path.exists(),
            "sample_extracted": bool(sample.strip()),
            "copyright_boundary": "paraphrase/framework only; do not copy long passages into skills",
            "framework_reviewed_by_yuto": False,
            "web_update_checked": False,
        },
        "source_hierarchy": source_hierarchy(),
        "next_actions": [
            "extract framework questions/steps/rules/mistakes",
            "compare with other canon books in the same expert domain",
            "create diagnose/apply/update-scout skill drafts only after Yuto verification",
        ],
    }
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"source_id": source_id, "path": str(out), "classification": classification, "version": record["version"]}


def create_expert_blueprint(
    *,
    expert_id: str,
    books: list[dict[str, Any]],
    update_sources: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "expert_id": expert_id,
        "version": "0.1.0",
        "status": "blueprint_unverified",
        "created_at": utc_now(),
        "canon_books": [
            {
                "title": book.get("title", "unknown"),
                "author": book.get("author", "unknown"),
                "domain": book.get("classification", {}).get("primary_domain", "unknown"),
                "role": book.get("classification", {}).get("role_suggestion", "framework-expert"),
            }
            for book in books
        ],
        "skills": [f"{expert_id}-diagnose", f"{expert_id}-apply", f"{expert_id}-update-scout"],
        "source_policy": {
            "book_role": "canon/framework base",
            "web_role": "current facts/update layer",
            "conflict_rule": "books govern reusable method; official/current sources govern current facts; label conflicts explicitly",
            "copyright_rule": "store paraphrased frameworks, not long source text",
            "hierarchy": source_hierarchy(),
        },
        "update_sources": update_sources or [],
        "verification_gate": {
            "required_before_promote": True,
            "checks": [
                "source file verified",
                "framework traceability checked against TOC/sample pages",
                "cross-book agreement/conflict table completed",
                "web update sources verified if current facts are used",
                "positive/negative activation tests passed",
            ],
        },
    }


def validate_team_routing(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("pyyaml is required")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    if not isinstance(data, dict):
        return {"ok": False, "errors": ["routing file must be a mapping"]}
    restricted = set(data.get("restricted_actions") or [])
    promotion_owner = data.get("promotion_owner")
    divisions = data.get("divisions")
    if not isinstance(divisions, list):
        errors.append("divisions must be a list")
        divisions = []
    if len(divisions) != 10:
        errors.append("exactly 10 divisions are required")
    ids: set[str] = set()
    role_count = 0
    for division in divisions:
        if not isinstance(division, dict):
            errors.append("division must be a mapping")
            continue
        div_id = division.get("id")
        if not div_id:
            errors.append("division.id is required")
            continue
        if div_id in ids:
            errors.append(f"duplicate division id: {div_id}")
        ids.add(str(div_id))
        allowed = set(division.get("allowed_actions") or [])
        forbidden = set(division.get("forbidden_actions") or [])
        gates = set(division.get("human_gate") or [])
        roles = division.get("roles") or []
        if not roles:
            errors.append(f"{div_id}: roles are required")
        if division.get("receipt_required") is not True:
            errors.append(f"{div_id}: receipt_required must be true")
        if allowed & forbidden:
            errors.append(f"{div_id}: action cannot be both allowed and forbidden")
        if "promote_expert" in allowed and div_id != promotion_owner:
            errors.append(f"{div_id}: only promotion_owner can allow promote_expert")
        if (allowed & restricted) and not gates:
            errors.append(f"{div_id}: restricted actions require human_gate")
        for role in roles:
            role_count += 1
            if not isinstance(role, dict) or not role.get("id") or not role.get("allowed_actions"):
                errors.append(f"{div_id}: every role needs id and allowed_actions")
    if promotion_owner not in ids:
        errors.append("promotion_owner must be a division id")
    return {
        "ok": not errors,
        "errors": errors,
        "division_count": len(divisions),
        "role_count": role_count,
        "restricted_actions": sorted(restricted),
        "promotion_owner": promotion_owner,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yuto Multi-Book Expert Skill Factory v0.1")
    sub = parser.add_subparsers(dest="command", required=True)

    classify_p = sub.add_parser("classify", help="classify a local book/document into an expert domain candidate")
    classify_p.add_argument("path", type=Path)
    classify_p.add_argument("--title")
    classify_p.add_argument("--author")

    register_p = sub.add_parser("register", help="register a local book/document as an unverified framework candidate")
    register_p.add_argument("path", type=Path)
    register_p.add_argument("--registry-root", type=Path, default=DEFAULT_REGISTRY)
    register_p.add_argument("--title")
    register_p.add_argument("--author")

    hierarchy_p = sub.add_parser("hierarchy", help="print source hierarchy")
    hierarchy_p.add_argument("--json", action="store_true")

    routing_p = sub.add_parser("validate-routing", help="validate team/department routing for book expert usage")
    routing_p.add_argument("path", type=Path, nargs="?", default=ROOT / "knowledge" / "book-expert-factory" / "team-routing.yaml")

    args = parser.parse_args(argv)
    if args.command == "classify":
        path = args.path.expanduser().resolve()
        sample = read_source_sample(path) if path.exists() else ""
        result = classify_book_text(title=args.title or path.stem, text=sample)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "register":
        result = register_book_source(args.path, registry_root=args.registry_root, title=args.title, author=args.author)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "hierarchy":
        hierarchy = source_hierarchy()
        print(json.dumps(hierarchy, ensure_ascii=False, indent=2) if args.json else "\n".join(hierarchy))
        return 0
    if args.command == "validate-routing":
        result = validate_team_routing(args.path)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result["ok"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
