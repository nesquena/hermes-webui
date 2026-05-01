import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab_ops_improvement_backlog import generate_backlog  # noqa: E402


def write_ops_graph(path: Path, ready: int = 17, enrichment: int = 2151) -> None:
    graph = {
        "generated_at": "2026-04-26T00:00:00+00:00",
        "metrics": {
            "db": {
                "queue_health": {
                    "ready_for_summary": ready,
                    "enrichment_needed": enrichment,
                    "pending_total": enrichment + ready,
                    "retry_queued": 15,
                    "completed": 407,
                    "failed": 0,
                },
                "latest_fetch_log": {"id": 50, "unique_constraint_errors": 0, "llm_requests": 0},
                "bottlenecks": [
                    {
                        "id": "bottleneck:source_enrichment",
                        "severity": "critical",
                        "evidence": {"ready_for_summary": ready, "enrichment_needed": enrichment},
                    }
                ],
            }
        },
    }
    path.write_text(json.dumps(graph), encoding="utf-8")


def test_generate_backlog_ranks_metric_bottleneck_before_artifacts(tmp_path):
    ops = tmp_path / "ops_graph.json"
    status = tmp_path / "reflection_checkpoint_latest.json"
    write_ops_graph(ops, ready=17, enrichment=2151)
    status.write_text(json.dumps({"ok": True, "auto_promoted": False, "promotion_status": "candidate"}), encoding="utf-8")

    result = generate_backlog(ops_graph_path=ops, reflection_status_path=status)

    assert result["worker"] == "improvement_backlog"
    assert result["db_write"] is False
    assert result["items"]
    top = result["items"][0]
    assert top["id"] == "lrc-source-enrichment-bottleneck"
    assert top["primary_metric"] == "enrichment_needed"
    assert top["before_metric"]["enrichment_needed"] == 2151
    assert top["recommended_next_owner"] == "source_enrichment_worker"
    assert top["completion_contract_required"] is True


def test_generate_backlog_promotes_summarization_drain_when_ready_queue_exists(tmp_path):
    ops = tmp_path / "ops_graph.json"
    status = tmp_path / "reflection_checkpoint_latest.json"
    write_ops_graph(ops, ready=17, enrichment=0)
    status.write_text(json.dumps({"ok": True, "auto_promoted": False, "promotion_status": "candidate"}), encoding="utf-8")

    result = generate_backlog(ops_graph_path=ops, reflection_status_path=status)

    ids = [item["id"] for item in result["items"]]
    assert "lrc-summarization-drain" in ids
    item = next(item for item in result["items"] if item["id"] == "lrc-summarization-drain")
    assert item["primary_metric"] == "ready_for_summary"
    assert item["before_metric"]["ready_for_summary"] == 17
    assert item["success_condition"] == "ready_for_summary reaches 0 or each remaining item has a recorded retry/failure reason"


def test_generate_backlog_reads_ops_graph_summary_shape(tmp_path):
    ops = tmp_path / "ops_graph.json"
    status = tmp_path / "reflection_checkpoint_latest.json"
    ops.write_text(
        json.dumps(
            {
                "summary": {
                    "queue_health": {
                        "ready_for_summary": 5,
                        "enrichment_needed": 0,
                        "completed": 407,
                        "failed": 0,
                    },
                    "latest_fetch_log": {"unique_constraint_errors": 0},
                }
            }
        ),
        encoding="utf-8",
    )
    status.write_text(json.dumps({"ok": False}), encoding="utf-8")

    result = generate_backlog(ops_graph_path=ops, reflection_status_path=status)

    item = next(item for item in result["items"] if item["id"] == "lrc-summarization-drain")
    assert item["before_metric"]["completed"] == 407


def test_generate_backlog_keeps_reflection_review_as_low_risk_when_candidate_pending(tmp_path):
    ops = tmp_path / "ops_graph.json"
    status = tmp_path / "reflection_checkpoint_latest.json"
    write_ops_graph(ops, ready=0, enrichment=0)
    status.write_text(json.dumps({"ok": True, "auto_promoted": False, "promotion_status": "candidate"}), encoding="utf-8")

    result = generate_backlog(ops_graph_path=ops, reflection_status_path=status)

    ids = [item["id"] for item in result["items"]]
    assert "yuto-reflection-candidate-review" in ids
    item = next(item for item in result["items"] if item["id"] == "yuto-reflection-candidate-review")
    assert item["recommended_next_owner"] == "yuto"
    assert item["safety_gate"] == "no auto-promotion; review evidence before memory/knowledge/skill writes"
