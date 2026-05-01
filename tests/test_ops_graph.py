import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "lab-ops"


def load_ops_graph_module():
    module_path = LAB / "ops_graph.py"
    spec = importlib.util.spec_from_file_location("ops_graph", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ops_graph_module_builds_operational_nodes_and_edges():
    module = load_ops_graph_module()
    graph = module.build_graph(root=ROOT)

    assert graph["graph_type"] == "labops_operational_kg"
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = graph["edges"]

    required_nodes = {
        "cron:legal-research-pipeline",
        "worker:source_enrichment",
        "worker:news_health",
        "worker:summarization",
        "db:research.articles",
        "queue:ready_for_summary",
        "queue:enrichment_needed",
        "artifact:ops_graph.json",
        "handoff:jarvis_taeyoon",
        "bottleneck:source_enrichment",
    }
    assert required_nodes.issubset(nodes)
    assert any(e["source"] == "cron:legal-research-pipeline" and e["type"] == "triggers" for e in edges)
    assert any(e["type"] == "blocked_by" and e["target"] == "bottleneck:source_enrichment" for e in edges)
    assert any(e["type"] == "verifies" and e["source"] == "worker:news_health" for e in edges)


def test_ops_graph_cli_writes_json_and_markdown(tmp_path):
    json_out = tmp_path / "ops_graph.json"
    md_out = tmp_path / "ops_graph.md"
    result = subprocess.run(
        [
            sys.executable,
            str(LAB / "ops_graph.py"),
            "--json-output",
            str(json_out),
            "--md-output",
            str(md_out),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    graph = json.loads(json_out.read_text())
    md = md_out.read_text()
    assert graph["graph_type"] == "labops_operational_kg"
    assert graph["summary"]["top_bottleneck_id"] == "bottleneck:source_enrichment"
    assert "ข่าวเข้าจากไหน" in md
    assert "ติดตรงไหน" in md
    assert "bottleneck:source_enrichment" in md
    assert "OPS_GRAPH" in result.stdout
