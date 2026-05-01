import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.yuto_graph.semantic_audit import audit_paths, write_report  # noqa: E402


def test_semantic_audit_flags_duplicate_headings_and_stale_mutable_claims(tmp_path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("# Same Title\n\nLatest result: everything works today.\n", encoding="utf-8")
    b.write_text("# Same Title\n\nSee source: https://example.com.\n", encoding="utf-8")

    report = audit_paths([tmp_path])

    assert report["files_checked"] == 2
    assert report["diagnostics"]["duplicate_titles"]
    assert report["diagnostics"]["stale_mutable_claims"]


def test_semantic_audit_ignores_generated_graph_dirs(tmp_path):
    real = tmp_path / "real.md"
    real.write_text("# Real\n", encoding="utf-8")
    graph = tmp_path / ".graph"
    graph.mkdir()
    (graph / "report.md").write_text("# Real\n\nCurrent status: noisy.\n", encoding="utf-8")

    report = audit_paths([tmp_path])

    assert report["files_checked"] == 1
    assert report["diagnostics"]["duplicate_titles"] == []
    assert report["diagnostics"]["stale_mutable_claims"] == []


def test_semantic_audit_flags_weak_source_trails(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Note\n\nEvidence: user said so\n", encoding="utf-8")

    report = audit_paths([tmp_path])

    assert report["diagnostics"]["weak_source_trails"]


def test_semantic_audit_does_not_flag_policy_words_as_weak_source(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Note\n\nMemory can suggest; evidence must decide.\n", encoding="utf-8")

    report = audit_paths([tmp_path])

    assert report["diagnostics"]["weak_source_trails"] == []


def test_write_report_outputs_json_and_markdown(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("# Note\n\nClaim: useful\n", encoding="utf-8")
    report = audit_paths([tmp_path])
    out = tmp_path / "out"

    write_report(report, out)

    assert json.loads((out / "semantic-audit.json").read_text())["files_checked"] == 1
    md = (out / "semantic-audit.md").read_text()
    assert "# Yuto Semantic Graph Audit" in md
