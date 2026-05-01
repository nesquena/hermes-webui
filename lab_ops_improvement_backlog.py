#!/usr/bin/env python3
"""Generate Yuto's autonomous improvement backlog from live metrics.

Read-only: consumes LabOps status/graph artifacts and writes backlog artifacts.
It does not execute fixes, edit memory, or promote reflection candidates.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DEFAULT_OPS_GRAPH = ROOT / "lab-ops" / "ops_graph.json"
DEFAULT_REFLECTION_STATUS = ROOT / "lab-ops" / "status" / "reflection_checkpoint_latest.json"
DEFAULT_JSON_OUT = ROOT / "lab-ops" / "status" / "improvement_backlog_latest.json"
DEFAULT_MD_OUT = ROOT / "lab-ops" / "reports" / "improvement_backlog_latest.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def queue_health_from_ops_graph(ops: dict[str, Any]) -> dict[str, Any]:
    summary_qh = (ops.get("summary") or {}).get("queue_health") or {}
    if summary_qh:
        return summary_qh

    metrics = ops.get("metrics") or {}
    db = metrics.get("db") or {}
    qh = db.get("queue_health") or {}
    if qh:
        return qh

    # Fallback for older ops_graph shapes: derive queue counts from nodes.
    qh = {}
    for node in ops.get("nodes") or []:
        if node.get("id") == "queue:ready_for_summary":
            qh["ready_for_summary"] = node.get("count", 0)
        elif node.get("id") == "queue:enrichment_needed":
            qh["enrichment_needed"] = node.get("count", 0)
        elif node.get("id") == "queue:retry_queued":
            qh["retry_queued"] = node.get("count", 0)
    return qh


def latest_fetch_from_ops_graph(ops: dict[str, Any]) -> dict[str, Any]:
    summary_fetch = (ops.get("summary") or {}).get("latest_fetch_log") or {}
    if summary_fetch:
        return summary_fetch
    return ((ops.get("metrics") or {}).get("db") or {}).get("latest_fetch_log") or {}


def make_item(
    item_id: str,
    title: str,
    priority: int,
    primary_metric: str,
    before_metric: dict[str, Any],
    recommended_next_owner: str,
    success_condition: str,
    evidence: list[str],
    action_hint: str,
    safety_gate: str,
) -> dict[str, Any]:
    return {
        "id": item_id,
        "title": title,
        "priority": priority,
        "primary_metric": primary_metric,
        "before_metric": before_metric,
        "recommended_next_owner": recommended_next_owner,
        "success_condition": success_condition,
        "evidence": evidence,
        "action_hint": action_hint,
        "safety_gate": safety_gate,
        "completion_contract_required": True,
        "status": "proposed",
    }


def generate_backlog(
    ops_graph_path: str | Path = DEFAULT_OPS_GRAPH,
    reflection_status_path: str | Path = DEFAULT_REFLECTION_STATUS,
) -> dict[str, Any]:
    ops_graph_path = Path(ops_graph_path)
    reflection_status_path = Path(reflection_status_path)
    ops = read_json(ops_graph_path)
    reflection = read_json(reflection_status_path)
    qh = queue_health_from_ops_graph(ops)
    latest_fetch = latest_fetch_from_ops_graph(ops)

    ready = int(qh.get("ready_for_summary") or 0)
    enrichment = int(qh.get("enrichment_needed") or 0)
    failed = int(qh.get("failed") or 0)
    unique_errors = int(latest_fetch.get("unique_constraint_errors") or 0)

    items: list[dict[str, Any]] = []

    if enrichment > max(ready, 0):
        items.append(
            make_item(
                "lrc-source-enrichment-bottleneck",
                "Reduce LRC source-enrichment backlog before summarization",
                100 + min(enrichment // 100, 50),
                "enrichment_needed",
                {"enrichment_needed": enrichment, "ready_for_summary": ready},
                "source_enrichment_worker",
                "enrichment_needed decreases and ready_for_summary increases without unsafe title-only summarization",
                [str(ops_graph_path), "queue_health.enrichment_needed > queue_health.ready_for_summary"],
                "Run/write-limit source enrichment with before-after DB metrics; improve domain skips if repeated short_text/blocked domains dominate.",
                "backup before DB writes; write only full_text_candidate packets; no legal analysis from title-only/RSS-only leads",
            )
        )

    if ready > 0:
        items.append(
            make_item(
                "lrc-summarization-drain",
                "Drain summary-ready LRC queue with small resumable worker",
                90 + min(ready, 30),
                "ready_for_summary",
                {"ready_for_summary": ready, "completed": qh.get("completed", 0), "failed": failed},
                "summarization_worker",
                "ready_for_summary reaches 0 or each remaining item has a recorded retry/failure reason",
                [str(ops_graph_path), "queue_health.ready_for_summary > 0"],
                "Implement/run limit 1-3 per-article write worker; record retry/failure immediately per article.",
                "Gemma only for Thai summary; no batch coupling; no auto-publish; Completion Contract per run",
            )
        )

    if unique_errors > 0:
        items.append(
            make_item(
                "lrc-dedupe-regression",
                "Fix duplicate content hash regression in latest fetch",
                95 + unique_errors,
                "unique_constraint_errors",
                {"unique_constraint_errors": unique_errors},
                "lrc_pipeline",
                "unique_constraint_errors returns to 0 on latest fetch log",
                [str(ops_graph_path), "latest_fetch_log.unique_constraint_errors > 0"],
                "Inspect latest fetch log and duplicate-content check before patching.",
                "do not delete articles; test duplicate handling before code changes",
            )
        )

    if reflection.get("ok") is True and reflection.get("promotion_status") == "candidate":
        items.append(
            make_item(
                "yuto-reflection-candidate-review",
                "Review latest reflection candidate for durable learning",
                30,
                "candidate_reflection_pending",
                {"candidate_reflection_pending": 1, "auto_promoted": bool(reflection.get("auto_promoted"))},
                "yuto",
                "candidate is promoted/rejected/staled with evidence, or explicitly kept pending with reason",
                [str(reflection_status_path), str(reflection.get("candidate"))],
                "Read candidate and source evidence; promote only user/file/tool/source-verified durable items.",
                "no auto-promotion; review evidence before memory/knowledge/skill writes",
            )
        )

    items.sort(key=lambda item: (-int(item["priority"]), item["id"]))

    return {
        "worker": "improvement_backlog",
        "generated_at": utc_now(),
        "mode": "read_only",
        "db_write": False,
        "inputs": {
            "ops_graph": str(ops_graph_path),
            "reflection_status": str(reflection_status_path),
        },
        "metrics": {
            "ready_for_summary": ready,
            "enrichment_needed": enrichment,
            "unique_constraint_errors": unique_errors,
            "candidate_reflection_pending": int(reflection.get("ok") is True and reflection.get("promotion_status") == "candidate"),
        },
        "selection_policy": "rank metric bottlenecks before artifacts; every item must name primary metric, success condition, owner, safety gate, and Completion Contract requirement",
        "items": items,
    }


def render_markdown(backlog: dict[str, Any]) -> str:
    lines = [
        "# Yuto Autonomous Improvement Backlog",
        "",
        f"Generated: {backlog['generated_at']}",
        "",
        "This backlog is read-only guidance. It does not execute fixes or promote memory.",
        "",
        "## Metrics",
        "",
        "```json",
        json.dumps(backlog["metrics"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Ranked Items",
        "",
    ]
    for i, item in enumerate(backlog["items"], start=1):
        lines.extend(
            [
                f"### {i}. {item['title']}",
                "",
                f"- id: `{item['id']}`",
                f"- priority: `{item['priority']}`",
                f"- primary_metric: `{item['primary_metric']}`",
                f"- before_metric: `{json.dumps(item['before_metric'], ensure_ascii=False)}`",
                f"- owner: `{item['recommended_next_owner']}`",
                f"- success_condition: {item['success_condition']}",
                f"- safety_gate: {item['safety_gate']}",
                f"- action_hint: {item['action_hint']}",
                "- completion_contract_required: `true`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_backlog(backlog: dict[str, Any], json_out: str | Path = DEFAULT_JSON_OUT, md_out: str | Path = DEFAULT_MD_OUT) -> None:
    json_out = Path(json_out)
    md_out = Path(md_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(backlog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_out.write_text(render_markdown(backlog), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Yuto autonomous improvement backlog from LabOps metrics")
    parser.add_argument("--ops-graph", default=str(DEFAULT_OPS_GRAPH))
    parser.add_argument("--reflection-status", default=str(DEFAULT_REFLECTION_STATUS))
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_OUT))
    parser.add_argument("--md-out", default=str(DEFAULT_MD_OUT))
    args = parser.parse_args()

    backlog = generate_backlog(args.ops_graph, args.reflection_status)
    write_backlog(backlog, args.json_out, args.md_out)
    print(json.dumps({"worker": "improvement_backlog", "items": len(backlog["items"]), "top_item": backlog["items"][0]["id"] if backlog["items"] else None, "json_out": args.json_out, "md_out": args.md_out}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
