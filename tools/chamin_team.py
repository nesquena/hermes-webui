#!/usr/bin/env python3
"""Operate the Chamin employee-team prototype.

Commands:
- status: validate and list the configured Chamin team.
- route: select a cookbook and smallest useful lane set for a task.
- packet: generate a work packet Chamin can use to run the lanes.
- receipt: append a lightweight run receipt after a team-assisted task.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("requires pyyaml") from exc

try:
    import validate_chamin_team
except ModuleNotFoundError:  # pragma: no cover - package import path in tests
    from tools import validate_chamin_team  # type: ignore


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEAM_ROOT = ROOT / "knowledge" / "chamin-team"
DEFAULT_RECEIPTS = DEFAULT_TEAM_ROOT / "receipts.jsonl"

SCOPE_KEYWORDS = {
    "astro": ["astro", "astro-app", "marketing", "blog", "landing", "course page", "src/pages"],
    "next": ["next", "next-app", "dashboard", "stripe", "supabase", "server action", "route.ts"],
    "shared": ["shared", "readme", "agents.md", "contributing", "root docs", "governance"],
}

LANE_TRIGGERS = {
    "security-reviewer": [
        "security",
        "secret",
        "xss",
        "ssrf",
        "csrf",
        "csp",
        "header",
        "auth",
        "webhook",
        "webhook signature",
        "token",
        "auth bypass",
    ],
    "data-auth-reviewer": [
        "supabase",
        "rls",
        "auth",
        "session",
        "admin",
        "role",
        "payment",
        "stripe",
        "webhook",
        "pii",
        "database",
        "db",
        "tenant",
        "user id",
    ],
    "frontend-ui-a11y-reviewer": [
        "ui",
        "jsx",
        "css",
        "classname",
        "layout",
        "mobile",
        "responsive",
        "a11y",
        "accessibility",
        "focus",
        "contrast",
        "hydration",
        "astro",
    ],
    "content-claims-seo-reviewer": [
        "seo",
        "copy",
        "claim",
        "claims",
        "pricing",
        "price",
        "schema",
        "json-ld",
        "meta",
        "title",
        "description",
        "blog",
        "course",
        "public page",
        "landing",
        "legal-adjacent",
    ],
}

HIGH_RISK_TERMS = [
    "security",
    "auth",
    "payment",
    "stripe",
    "webhook",
    "admin",
    "rls",
    "pii",
    "production",
    "deploy",
    "high risk",
]


@dataclass(frozen=True)
class RouteResult:
    cookbook: str
    scope: str
    selected_lanes: list[str]
    triggered_lanes: list[str]
    deferred_lanes: list[str]
    human_gate: bool
    rationale: list[str]
    verification: list[str]


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_frontmatter(path: Path) -> dict[str, Any]:
    return validate_chamin_team.load_frontmatter(path)


def load_team(team_root: Path = DEFAULT_TEAM_ROOT) -> dict[str, Any]:
    lanes_dir = team_root / "lanes"
    cookbooks_dir = team_root / "cookbooks"
    return {
        "team_root": team_root,
        "employee": load_frontmatter(team_root / "employee.md"),
        "lanes": {path.stem: load_yaml(path) for path in sorted(lanes_dir.glob("*.yaml"))},
        "cookbooks": {path.stem: load_frontmatter(path) for path in sorted(cookbooks_dir.glob("*.md"))},
        "steering": load_yaml(team_root / "steering-examples.yaml"),
    }


def infer_scope(text: str) -> str:
    hay = text.lower()
    matches = [scope for scope, terms in SCOPE_KEYWORDS.items() if any(term in hay for term in terms)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "mixed"
    return "unknown"


def triggered_lanes(text: str) -> list[str]:
    hay = text.lower()
    lanes: list[str] = []
    for lane, terms in LANE_TRIGGERS.items():
        if any(term in hay for term in terms):
            lanes.append(lane)
    return lanes


def route_task(task: str, *, scope: str | None = None, team_root: Path = DEFAULT_TEAM_ROOT) -> RouteResult:
    validation = validate_chamin_team.validate_all(team_root)
    if not validation.ok:
        raise ValueError("Chamin team validation failed: " + "; ".join(validation.errors))

    team = load_team(team_root)
    cookbook_id = "lawlabo-pr-review"
    cookbook = team["cookbooks"][cookbook_id]
    max_workers = int(cookbook.get("max_workers", 3))
    default_lanes = list(cookbook.get("default_lanes", []))
    optional_lanes = set(cookbook.get("optional_lanes", []))

    inferred_scope = scope or infer_scope(task)
    triggers = [lane for lane in triggered_lanes(task) if lane in optional_lanes]
    selected: list[str] = []
    rationale: list[str] = []

    for lane in default_lanes:
        if lane not in selected:
            selected.append(lane)
            rationale.append(f"{lane}: default lane for LawLabo review")

    for lane in triggers:
        if lane not in selected and len(selected) < max_workers:
            selected.append(lane)
            rationale.append(f"{lane}: triggered by task keywords")

    high_risk = any(term in task.lower() for term in HIGH_RISK_TERMS)
    if high_risk and "qa-critic" not in selected and "qa-critic" in optional_lanes and len(selected) < max_workers:
        selected.append("qa-critic")
        rationale.append("qa-critic: high-risk review gate")

    deferred = [lane for lane in triggers if lane not in selected]
    if high_risk and "qa-critic" not in selected:
        deferred.append("qa-critic")

    if inferred_scope == "mixed":
        rationale.append("scope: mixed app-surface signals detected; Chamin should split verdicts or stop")
    elif inferred_scope == "unknown":
        rationale.append("scope: unknown; Chamin must declare Astro/Next/Shared before final review")
    else:
        rationale.append(f"scope: {inferred_scope}")

    verification = [
        "validate Chamin team manifests before routing",
        "inspect changed files before findings",
        "cite evidence refs for every factual claim",
        "state tests or verification not run",
    ]
    if inferred_scope == "mixed":
        verification.append("do not mix Astro, Next, and Shared in one review pass")

    return RouteResult(
        cookbook=cookbook_id,
        scope=inferred_scope,
        selected_lanes=selected,
        triggered_lanes=triggers,
        deferred_lanes=deferred,
        human_gate=high_risk or inferred_scope == "mixed",
        rationale=rationale,
        verification=verification,
    )


def lane_prompt(lane: dict[str, Any], task: str, target_ref: str, scope: str) -> str:
    return f"""## Lane: {lane['id']} - {lane['name']}

Purpose:
{lane['purpose']}

Task:
{task}

Target ref:
{target_ref}

Declared scope:
{scope}

Allowed tools:
{bullet_list(lane.get('allowed_tools', []))}

Forbidden tools:
{bullet_list(lane.get('forbidden_tools', []))}

Safety rules:
{bullet_list(lane.get('safety_rules', []))}

Return:
- structured receipt
- schema-shaped findings only
- evidence refs for every claim
- explicit verification gaps
"""


def bullet_list(items: list[Any]) -> str:
    if not items:
        return "- none"
    return "\n".join(f"- {item}" for item in items)


def build_packet(task: str, *, target_ref: str, scope: str | None = None, team_root: Path = DEFAULT_TEAM_ROOT) -> str:
    route = route_task(task, scope=scope, team_root=team_root)
    team = load_team(team_root)
    lanes = team["lanes"]
    cookbook = team["cookbooks"][route.cookbook]
    today = date.today().isoformat()

    lane_sections = "\n".join(
        lane_prompt(lanes[lane_id], task, target_ref, route.scope) for lane_id in route.selected_lanes
    )

    return f"""# Chamin Team Work Packet

Created: {today}
Employee: chamin
Cookbook: {route.cookbook}
Target: {target_ref}
Scope: {route.scope}

## Task

{task}

## Goal

{cookbook.get('goal')}

## Selected Lanes

{bullet_list(route.selected_lanes)}

## Deferred Lanes

{bullet_list(route.deferred_lanes)}

## Routing Rationale

{bullet_list(route.rationale)}

## Verification Plan

{bullet_list(route.verification)}

## Human Gate

{"required" if route.human_gate else "not required by routing"}

## Lane Instructions

{lane_sections}
## Chamin Final Synthesis Contract

Return one Chamin answer only:
- Conclusion
- Findings first, ordered by severity
- Evidence checked
- Verification run or not run
- Remaining risk

Do not let worker voices reach Kei directly.
"""


def receipt_payload(
    *,
    task_id: str,
    workflow: str,
    lane_set: list[str],
    verification_status: str,
    notes: str,
    artifact_created: bool,
    rework_count: int,
    saved_time_estimate: str,
    quality_gain: str,
    safety_gain: str,
    friction_cost: str,
    reuse_recommendation: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "date": date.today().isoformat(),
        "employee": "chamin",
        "workflow": workflow,
        "lane_set": lane_set,
        "runtime": ["codex"],
        "artifact_created": artifact_created,
        "verification_status": verification_status,
        "rework_count": rework_count,
        "saved_time_estimate": saved_time_estimate,
        "quality_gain": quality_gain,
        "safety_gain": safety_gain,
        "friction_cost": friction_cost,
        "reuse_recommendation": reuse_recommendation,
        "notes": notes,
    }


def validate_receipt(payload: dict[str, Any], team_root: Path = DEFAULT_TEAM_ROOT) -> list[str]:
    team = load_team(team_root)
    errors: list[str] = []
    if payload.get("workflow") not in team["cookbooks"]:
        errors.append(f"unknown workflow: {payload.get('workflow')}")
    lane_ids = set(team["lanes"])
    for lane in payload.get("lane_set", []):
        if lane not in lane_ids:
            errors.append(f"unknown lane: {lane}")
    if payload.get("verification_status") not in {"pass", "partial", "fail"}:
        errors.append("verification_status must be pass, partial, or fail")
    for key in ["saved_time_estimate", "quality_gain", "safety_gain", "friction_cost"]:
        if payload.get(key) not in {"none", "low", "medium", "high"}:
            errors.append(f"{key} must be none, low, medium, or high")
    if payload.get("reuse_recommendation") not in {"keep", "modify", "drop"}:
        errors.append("reuse_recommendation must be keep, modify, or drop")
    if not isinstance(payload.get("rework_count"), int) or payload["rework_count"] < 0:
        errors.append("rework_count must be a non-negative integer")
    return errors


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Operate the Chamin team prototype")
    parser.add_argument("--team-root", type=Path, default=DEFAULT_TEAM_ROOT)
    sub = parser.add_subparsers(dest="command", required=True)

    status_p = sub.add_parser("status", help="validate and summarize the Chamin team")
    status_p.add_argument("--json", action="store_true")

    route_p = sub.add_parser("route", help="route a task to a cookbook and lanes")
    route_p.add_argument("task")
    route_p.add_argument("--scope", choices=["astro", "next", "shared", "mixed", "unknown"])
    route_p.add_argument("--json", action="store_true")

    packet_p = sub.add_parser("packet", help="generate a Chamin team work packet")
    packet_p.add_argument("task")
    packet_p.add_argument("--target-ref", default="unspecified")
    packet_p.add_argument("--scope", choices=["astro", "next", "shared", "mixed", "unknown"])
    packet_p.add_argument("--out", type=Path)
    packet_p.add_argument("--json", action="store_true")

    receipt_p = sub.add_parser("receipt", help="append a Chamin team run receipt")
    receipt_p.add_argument("--task-id", required=True)
    receipt_p.add_argument("--workflow", default="lawlabo-pr-review")
    receipt_p.add_argument("--lanes", required=True, help="comma-separated lane ids")
    receipt_p.add_argument("--status", choices=["pass", "partial", "fail"], required=True)
    receipt_p.add_argument("--notes", required=True)
    receipt_p.add_argument("--artifact-created", action="store_true")
    receipt_p.add_argument("--rework-count", type=int, default=0)
    receipt_p.add_argument("--saved-time", choices=["none", "low", "medium", "high"], default="none")
    receipt_p.add_argument("--quality-gain", choices=["none", "low", "medium", "high"], default="none")
    receipt_p.add_argument("--safety-gain", choices=["none", "low", "medium", "high"], default="none")
    receipt_p.add_argument("--friction-cost", choices=["none", "low", "medium", "high"], default="low")
    receipt_p.add_argument("--reuse", choices=["keep", "modify", "drop"], default="modify")
    receipt_p.add_argument("--receipt-log", type=Path, default=DEFAULT_RECEIPTS)
    receipt_p.add_argument("--dry-run", action="store_true")
    receipt_p.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command == "status":
        validation = validate_chamin_team.validate_all(args.team_root)
        payload = {
            "ok": validation.ok,
            "errors": validation.errors,
            "warnings": validation.warnings,
            "lanes": validation.lanes,
            "cookbooks": validation.cookbooks,
            "files_checked": validation.files_checked,
        }
        if args.json:
            print_json(payload)
        else:
            print("OK" if validation.ok else "FAIL")
            print(f"Cookbooks: {', '.join(validation.cookbooks)}")
            print(f"Lanes: {', '.join(validation.lanes)}")
            for error in validation.errors:
                print(f"ERROR: {error}")
            for warning in validation.warnings:
                print(f"WARN: {warning}")
        return 0 if validation.ok else 1

    if args.command == "route":
        route = route_task(args.task, scope=args.scope, team_root=args.team_root)
        payload = asdict(route)
        if args.json:
            print_json(payload)
        else:
            print(f"Cookbook: {route.cookbook}")
            print(f"Scope: {route.scope}")
            print(f"Selected lanes: {', '.join(route.selected_lanes)}")
            if route.deferred_lanes:
                print(f"Deferred lanes: {', '.join(route.deferred_lanes)}")
            print("Human gate: " + ("required" if route.human_gate else "not required"))
        return 0

    if args.command == "packet":
        packet = build_packet(args.task, target_ref=args.target_ref, scope=args.scope, team_root=args.team_root)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(packet, encoding="utf-8")
        if args.json:
            print_json({"packet": packet, "out": str(args.out) if args.out else None})
        elif not args.out:
            print(packet)
        else:
            print(str(args.out))
        return 0

    if args.command == "receipt":
        lanes = [lane.strip() for lane in args.lanes.split(",") if lane.strip()]
        payload = receipt_payload(
            task_id=args.task_id,
            workflow=args.workflow,
            lane_set=lanes,
            verification_status=args.status,
            notes=args.notes,
            artifact_created=args.artifact_created,
            rework_count=args.rework_count,
            saved_time_estimate=args.saved_time,
            quality_gain=args.quality_gain,
            safety_gain=args.safety_gain,
            friction_cost=args.friction_cost,
            reuse_recommendation=args.reuse,
        )
        errors = validate_receipt(payload, args.team_root)
        if errors:
            print_json({"ok": False, "errors": errors, "receipt": payload})
            return 1
        if not args.dry_run:
            append_jsonl(args.receipt_log, payload)
        if args.json:
            print_json({"ok": True, "dry_run": args.dry_run, "receipt_log": str(args.receipt_log), "receipt": payload})
        else:
            print("OK " + ("dry-run" if args.dry_run else str(args.receipt_log)))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
