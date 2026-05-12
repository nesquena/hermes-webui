from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "validate_chamin_team.py"
SPEC = importlib.util.spec_from_file_location("validate_chamin_team", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
validate_chamin_team = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = validate_chamin_team
SPEC.loader.exec_module(validate_chamin_team)


def test_validate_current_chamin_team():
    result = validate_chamin_team.validate_all()

    assert result.ok, result.errors
    assert "lawlabo-pr-review" in result.cookbooks
    assert "correctness-reviewer" in result.lanes
    assert "qa-critic" in result.lanes


def write_minimal_team(root: Path) -> Path:
    team = root / "chamin-team"
    (team / "lanes").mkdir(parents=True)
    (team / "cookbooks").mkdir()
    (team / "README.md").write_text("# Test Team\n", encoding="utf-8")
    (team / "employee.md").write_text(
        """---
id: chamin
name: Chamin
role: Lead
owner: kei
user_facing: true
primary_runtime: [codex]
source_of_truth: [test]
authority:
  can_route_workers: true
  can_edit_files_when_asked: true
  can_publish_or_send: false
  can_delete_or_destruct: false
human_gate:
  - destructive action
---
# Employee
""",
        encoding="utf-8",
    )
    object_schema = """type: object
required: []
additionalProperties: false
properties: {}
"""
    (team / "receipt-schema.yaml").write_text(object_schema, encoding="utf-8")
    (team / "handoff-schema.yaml").write_text(object_schema, encoding="utf-8")
    lane = """id: qa-critic
name: QA Critic
purpose: Check output.
owner_employee: chamin
lane_type: critic
standing_worker: false
when_to_use: [review]
when_not_to_use: [tiny]
input_schema:
  type: object
  required: []
  additionalProperties: false
  properties: {}
output_schema:
  type: object
  required: []
  additionalProperties: false
  properties: {}
allowed_tools: [read_file]
forbidden_tools: [write_file, patch, send_message, deploy, destructive_ops]
allowed_context: [receipts]
forbidden_context: [raw secrets]
safety_rules: [verify evidence]
handoff_allowed_to: []
verification_gate:
  required_by: chamin
  checks: [schema]
receipt_required: true
human_gate: false
"""
    (team / "lanes" / "qa-critic.yaml").write_text(lane, encoding="utf-8")
    (team / "cookbooks" / "lawlabo-pr-review.md").write_text(
        """---
id: lawlabo-pr-review
name: LawLabo PR Review
owner_employee: chamin
goal: Review.
non_goals: [edit]
triggers: [review]
default_lanes: [qa-critic]
optional_lanes: []
max_workers: 1
max_writer_lanes: 0
human_gates: []
output_shape: [conclusion]
---
# Cookbook
""",
        encoding="utf-8",
    )
    (team / "steering-examples.yaml").write_text(
        """examples:
  - id: example
    request: review
    cookbook: lawlabo-pr-review
    required_lanes: [qa-critic]
    optional_lanes: []
    stop_condition: done
    human_gate: false
""",
        encoding="utf-8",
    )
    return team


def test_validate_minimal_team(tmp_path):
    team = write_minimal_team(tmp_path)

    result = validate_chamin_team.validate_all(team)

    assert result.ok, result.errors
    assert result.lanes == ["qa-critic"]


def test_rejects_lane_write_tool_without_write_holder(tmp_path):
    team = write_minimal_team(tmp_path)
    lane_path = team / "lanes" / "qa-critic.yaml"
    text = lane_path.read_text(encoding="utf-8").replace(
        "allowed_tools: [read_file]",
        "allowed_tools: [read_file, patch]",
    )
    lane_path.write_text(text, encoding="utf-8")

    result = validate_chamin_team.validate_all(team)

    assert not result.ok
    assert any("non-write-holder lane allows write tools" in error for error in result.errors)


def test_rejects_unknown_cookbook_lane(tmp_path):
    team = write_minimal_team(tmp_path)
    cookbook_path = team / "cookbooks" / "lawlabo-pr-review.md"
    text = cookbook_path.read_text(encoding="utf-8").replace(
        "default_lanes: [qa-critic]",
        "default_lanes: [missing-lane]",
    )
    cookbook_path.write_text(text, encoding="utf-8")

    result = validate_chamin_team.validate_all(team)

    assert not result.ok
    assert any("unknown lane reference" in error for error in result.errors)


def test_rejects_unknown_steering_reference(tmp_path):
    team = write_minimal_team(tmp_path)
    steering_path = team / "steering-examples.yaml"
    text = steering_path.read_text(encoding="utf-8").replace(
        "required_lanes: [qa-critic]",
        "required_lanes: [missing-lane]",
    )
    steering_path.write_text(text, encoding="utf-8")

    result = validate_chamin_team.validate_all(team)

    assert not result.ok
    assert any("references unknown lane" in error for error in result.errors)
