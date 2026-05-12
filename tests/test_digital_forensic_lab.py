from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import digital_forensic_lab  # noqa: E402


def test_digital_forensic_lab_current_files_are_usable_phase0():
    result = digital_forensic_lab.validate_lab(ROOT / "company" / "departments" / "digital-forensic-lab")

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["summary"]["lab_artifacts"] >= 6
    assert result["summary"]["synthetic_evidence_items"] >= 1
    assert result["summary"]["custody_events"] >= 1
    assert result["summary"]["receipts"] >= 1
    assert result["summary"]["knowledge_sources"] >= 10
    assert result["summary"]["learning_modules"] >= 7
    assert result["summary"]["growth_loop_steps"] >= 5
    assert result["summary"]["working_library_source_extracts"] >= 10
    assert result["summary"]["working_library_controls"] >= 5
    assert result["summary"]["learning_cycle_receipts"] >= 3
    assert result["summary"]["source_extracts_sha_verified"] >= 10
    assert result["summary"]["source_extracts_with_anchors"] >= 10
    assert result["summary"]["practice_labs"] >= 10
    assert result["summary"]["practice_labs_with_sandbox_policy"] == result["summary"]["practice_labs"]
    assert result["summary"]["model_registry_entries"] >= 28
    assert result["summary"]["custom_workers"] >= 6
    assert result["summary"]["blocked_pending_source"] >= 3
    assert result["summary"]["benchmark_packs"] >= 6
    assert result["summary"]["lane_routes"] >= 14


def test_lab_rejects_real_case_data_and_final_conclusion(tmp_path):
    lab = tmp_path / "digital-forensic-lab"
    lab.mkdir()
    (lab / "lab-charter.yaml").write_text("phase: phase_0_internal_only\nsynthetic_only: true\n", encoding="utf-8")
    (lab / "red-lines.yaml").write_text("red_lines: [real_case_data, final_forensic_claim]\n", encoding="utf-8")
    (lab / "evidence-register.yaml").write_text(
        "evidence_items:\n  - evidence_id: BAD-001\n    synthetic_only: false\n    real_case_data_present: true\n    conclusion_status: final\n    custody_events: []\n",
        encoding="utf-8",
    )

    result = digital_forensic_lab.validate_lab(lab)

    assert result["ok"] is False
    joined = " ".join(result["errors"])
    assert "real_case_data_present" in joined
    assert "final conclusion" in joined


def test_lab_rejects_missing_custody_and_external_actions(tmp_path):
    lab = tmp_path / "digital-forensic-lab"
    lab.mkdir()
    (lab / "lab-charter.yaml").write_text("phase: phase_0_internal_only\nsynthetic_only: true\n", encoding="utf-8")
    (lab / "red-lines.yaml").write_text("red_lines: [external_action]\n", encoding="utf-8")
    (lab / "evidence-register.yaml").write_text(
        "evidence_items:\n  - evidence_id: SYN-001\n    synthetic_only: true\n    real_case_data_present: false\n    conclusion_status: preliminary_internal_non_final\n    custody_events: []\n",
        encoding="utf-8",
    )
    (lab / "forensic-lab-receipts.jsonl").write_text(
        '{"task_id":"bad","external_actions_taken":["email"],"conclusion_status":"preliminary_internal_non_final","policy_violations":[]}\n',
        encoding="utf-8",
    )

    result = digital_forensic_lab.validate_lab(lab)

    assert result["ok"] is False
    joined = " ".join(result["errors"])
    assert "custody" in joined
    assert "external_actions_taken" in joined


def test_model_stack_rejects_auto_download_and_missing_routes(tmp_path):
    lab = tmp_path / "digital-forensic-lab"
    lab.mkdir()
    (lab / "model-registry.yaml").write_text(
        "policy:\n  no_auto_download: false\n  no_real_case_data: true\n  no_offensive_use: true\nmain: []\nlegal_rag: []\nevidence_rag: []\ncyber: []\nprivacy: []\nvisual_deepfake: []\njapanese: []\ncustom_workers: []\n",
        encoding="utf-8",
    )
    (lab / "team-stack-routing.yaml").write_text(
        "routing_policy:\n  every_claim_needs_evidence_id: false\n  final_human_review_required: false\nlane_routes: {}\n",
        encoding="utf-8",
    )
    (lab / "autonomy-improvement-policy.yaml").write_text(
        "autonomy_level: L2\nforbidden_autonomous_actions: []\n",
        encoding="utf-8",
    )
    (lab / "benchmark-suite.yaml").write_text(
        "safety_boundary: []\nbenchmark_packs: []\n",
        encoding="utf-8",
    )

    result = digital_forensic_lab.validate_model_stack(lab)

    assert result["ok"] is False
    joined = " ".join(result["errors"])
    assert "no_auto_download" in joined
    assert "every_claim_needs_evidence_id" in joined
    assert "L1_bounded_internal_synthetic_only" in joined
    assert "benchmark suite" in joined


def test_company_workforce_counts_digital_forensic_team():
    result = digital_forensic_lab.validate_workforce_links(ROOT / "company" / "workforce")

    assert result["ok"] is True
    assert result["summary"]["digital_forensic_personnel"] >= 3
    assert result["summary"]["digital_forensic_department_leads"] >= 1


def test_knowledge_source_map_rejects_unregistered_sources(tmp_path):
    lab = tmp_path / "digital-forensic-lab"
    lab.mkdir()
    (lab / "knowledge-source-map.yaml").write_text(
        "sources:\n  - source_id: missing-book\n    source_file: knowledge/book-expert-factory/sources/missing-book.json\n    role: primary\nlearning_modules: [module_1, module_2, module_3, module_4, module_5]\ngrowth_loop: [read, extract, apply, validate, receipt]\n",
        encoding="utf-8",
    )

    result = digital_forensic_lab.validate_knowledge_bindings(lab, ROOT)

    assert result["ok"] is False
    assert "missing source_file" in " ".join(result["errors"])


def test_lab_charter_must_reference_knowledge_source_map(tmp_path):
    lab = tmp_path / "digital-forensic-lab"
    lab.mkdir()
    (lab / "lab-charter.yaml").write_text("phase: phase_0_internal_only\nsynthetic_only: true\n", encoding="utf-8")
    (lab / "knowledge-source-map.yaml").write_text("sources: []\nlearning_modules: []\ngrowth_loop: []\n", encoding="utf-8")

    result = digital_forensic_lab.validate_knowledge_bindings(lab, ROOT)

    assert result["ok"] is False
    joined = " ".join(result["errors"])
    assert "knowledge-source-map" in joined
    assert "working library" in joined


def test_working_library_rejects_unverified_or_thin_source_extract(tmp_path):
    lab = tmp_path / "digital-forensic-lab"
    extract_dir = lab / "working-library" / "source-extracts"
    control_dir = lab / "working-library" / "controls"
    extract_dir.mkdir(parents=True)
    control_dir.mkdir(parents=True)
    (lab / "working-library" / "library-index.yaml").write_text(
        "safety_boundary: [synthetic, no real data, no final claims, no external actions, no offensive cyber]\n"
        "source_extracts: [working-library/source-extracts/bad.yaml]\n"
        "controls: [working-library/controls/control.yaml]\n"
        "learning_cycles: [cycle1, cycle2, cycle3]\n",
        encoding="utf-8",
    )
    (extract_dir / "bad.yaml").write_text(
        "source_id: bad\nsource_file: knowledge/book-expert-factory/sources/missing.json\n"
        "source_asset_exists: true\nsha256_verified: false\npromoted_to_authority: true\n"
        "evidence_anchors: []\nframework_questions: [one]\nworking_library_contributions: []\n",
        encoding="utf-8",
    )
    (control_dir / "control.yaml").write_text("requires: [one]\nsource_extracts: []\n", encoding="utf-8")
    (lab / "working-library" / "learning-loop-receipts.jsonl").write_text(
        '{"receipt_id":"bad","source_grounded":false,"synthetic_only":false,"external_actions_taken":["email"],"policy_violations":["x"],"conclusion_status":"final"}\n',
        encoding="utf-8",
    )

    result = digital_forensic_lab.validate_working_library(lab, ROOT)

    assert result["ok"] is False
    joined = " ".join(result["errors"])
    assert "sha256_verified" in joined
    assert "evidence anchors" in joined
    assert "promoted to authority" in joined
    assert "external_actions_taken" in joined
