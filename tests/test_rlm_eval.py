import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "rlm_eval.py"

spec = importlib.util.spec_from_file_location("rlm_eval", SCRIPT)
rlm_eval = importlib.util.module_from_spec(spec)
sys.modules["rlm_eval"] = rlm_eval
spec.loader.exec_module(rlm_eval)


def valid_entry(**overrides):
    entry = {
        "date": "2026-04-29",
        "task": "Evaluate a source-backed research answer",
        "mode": "RESEARCH",
        "external_context": "https://example.com/source",
        "rlm_style_checks": [1, 2, 3, 4],
        "source_grounding": 3,
        "context_efficiency": 2.5,
        "answer_usefulness": 2.5,
        "verification_closure": 3,
        "rework_count": 0,
        "evaluator": "yuto-self",
        "evidence_link": "/Users/kei/kei-jarvis/knowledge/sources.md",
        "notes": "test fixture",
    }
    entry.update(overrides)
    return entry


def test_validate_entry_accepts_rlm_style_entry():
    result = rlm_eval.validate_entry(valid_entry())

    assert result.ok is True
    assert result.rlm_style is True
    assert result.errors == []


def test_validate_entry_flags_non_rlm_style_without_failing_scores():
    result = rlm_eval.validate_entry(valid_entry(rlm_style_checks=[1, 2, 3]))

    assert result.ok is True
    assert result.rlm_style is False
    assert result.warnings


def test_validate_entry_rejects_bad_scores_and_modes():
    result = rlm_eval.validate_entry(valid_entry(mode="CHAT", source_grounding=4, rework_count=-1))

    assert result.ok is False
    assert any("mode" in err for err in result.errors)
    assert any("source_grounding" in err for err in result.errors)
    assert any("rework_count" in err for err in result.errors)


def test_validate_entry_rejects_missing_evaluator_and_evidence_link():
    result = rlm_eval.validate_entry(valid_entry(evaluator="", evidence_link=""))

    assert result.ok is False
    assert any("evaluator" in err for err in result.errors)
    assert any("evidence_link" in err for err in result.errors)


def test_validate_entry_rejects_placeholder_evidence_link():
    result = rlm_eval.validate_entry(valid_entry(evidence_link="todo"))

    assert result.ok is False
    assert any("evidence_link" in err for err in result.errors)


def test_summary_reports_evaluator_counts():
    entries = [valid_entry(evaluator="yuto-self"), valid_entry(evaluator="codex-review")]

    summary = rlm_eval.summarize(entries)

    assert summary["evaluator_counts"] == {"codex-review": 1, "yuto-self": 1}


def test_summary_requires_ten_tasks_before_effective():
    entries = [valid_entry() for _ in range(3)]

    summary = rlm_eval.summarize(entries)

    assert summary["status"] == "collect_more_data"
    assert summary["thresholds_met"] is False
    assert summary["rlm_style_rate"] == 1.0
    assert summary["tasks_remaining_to_effective_review"] == 7


def test_summary_marks_effective_after_thresholds_and_ten_tasks():
    entries = [valid_entry() for _ in range(10)]

    summary = rlm_eval.summarize(entries)

    assert summary["status"] == "effective"
    assert summary["thresholds_met"] is True


def test_summary_marks_needs_patch_when_scores_low_after_ten_tasks():
    entries = [
        valid_entry(source_grounding=2, context_efficiency=2, answer_usefulness=1, verification_closure=2, rework_count=2)
        for _ in range(10)
    ]

    summary = rlm_eval.summarize(entries)

    assert summary["status"] == "needs_workflow_patch"
    assert summary["thresholds_met"] is False


def test_cli_append_and_summary(tmp_path, capsys):
    entry_path = tmp_path / "entry.json"
    log_path = tmp_path / "log.jsonl"
    entry_path.write_text(json.dumps(valid_entry()), encoding="utf-8")

    assert rlm_eval.main(["append", str(entry_path), str(log_path)]) == 0
    assert log_path.exists()

    assert rlm_eval.main(["summary", str(log_path)]) == 0
    out = capsys.readouterr().out
    assert "collect_more_data" in out
