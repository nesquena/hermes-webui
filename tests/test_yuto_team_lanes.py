from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import yuto_team_lanes  # noqa: E402


def lane_yaml(lane_id: str = "example-lane", *, allowed=None, forbidden=None, write_holder=False, handoffs=None) -> str:
    allowed = allowed or ["read_file"]
    forbidden = forbidden or ["write_file", "patch", "send_message", "deploy"]
    handoffs = handoffs or ["qa-critic"]
    write_holder_line = "write_holder: true\n" if write_holder else ""
    return f"""id: {lane_id}
name: Example Lane
purpose: Validate an example lane.
owner: yuto-control
runtime_options:
  - hermes_delegate_task
when_to_use:
  - example work
when_not_to_use:
  - tiny work
allowed_tools:
{chr(10).join('  - ' + x for x in allowed)}
forbidden_tools:
{chr(10).join('  - ' + x for x in forbidden)}
input_schema:
  type: object
  required: [source_ref]
  additionalProperties: false
  properties:
    source_ref: {{type: string}}
output_schema:
  type: object
  required: [status]
  additionalProperties: false
  properties:
    status: {{enum: [pass, fail]}}
safety_rules:
  - stay scoped
verification_gate:
  required_by: yuto-control
  checks:
    - schema valid
handoff_allowed_to:
{chr(10).join('  - ' + x for x in handoffs)}
receipt_required: true
human_gate: false
{write_holder_line}"""


def write_valid_lane_set(root: Path) -> Path:
    lanes = root / "lanes"
    lanes.mkdir()
    (lanes / "qa-critic.yaml").write_text(lane_yaml("qa-critic", handoffs=["yuto-control"]), encoding="utf-8")
    (lanes / "example-lane.yaml").write_text(lane_yaml(), encoding="utf-8")
    (lanes / "steering-examples.yaml").write_text(
        """examples:
  - id: example
    request: Do example work.
    lanes:
      - example-lane
      - qa-critic
    stop_condition: Validated output exists.
    human_gate: false
""",
        encoding="utf-8",
    )
    return lanes


def test_validate_current_lane_manifests():
    result = yuto_team_lanes.validate_all(
        yuto_team_lanes.DEFAULT_LANES_DIR,
        yuto_team_lanes.DEFAULT_SWARM,
    )
    assert result.ok, result.errors
    assert "evidence-doc-reader" in result.lane_ids
    assert "qa-critic" in result.lane_ids


def test_validate_minimal_valid_lane_set(tmp_path):
    lanes = write_valid_lane_set(tmp_path)

    result = yuto_team_lanes.validate_all(lanes, swarm_path=None)

    assert result.ok, result.errors
    assert sorted(result.lane_ids) == ["example-lane", "qa-critic"]


def test_rejects_allowed_forbidden_overlap(tmp_path):
    lanes = write_valid_lane_set(tmp_path)
    (lanes / "example-lane.yaml").write_text(
        lane_yaml(allowed=["read_file", "write_file"], forbidden=["write_file", "send_message", "deploy"]),
        encoding="utf-8",
    )

    result = yuto_team_lanes.validate_all(lanes, swarm_path=None)

    assert not result.ok
    assert any("both allowed and forbidden" in e for e in result.errors)


def test_rejects_non_write_holder_with_write_tool(tmp_path):
    lanes = write_valid_lane_set(tmp_path)
    (lanes / "example-lane.yaml").write_text(
        lane_yaml(allowed=["read_file", "patch"], forbidden=["send_message", "deploy"]),
        encoding="utf-8",
    )

    result = yuto_team_lanes.validate_all(lanes, swarm_path=None)

    assert not result.ok
    assert any("non-write-holder lane allows write tools" in e for e in result.errors)


def test_rejects_unknown_handoff_target(tmp_path):
    lanes = write_valid_lane_set(tmp_path)
    (lanes / "example-lane.yaml").write_text(lane_yaml(handoffs=["missing-lane"]), encoding="utf-8")

    result = yuto_team_lanes.validate_all(lanes, swarm_path=None)

    assert not result.ok
    assert any("handoff target does not exist" in e for e in result.errors)


def test_rejects_loose_output_schema(tmp_path):
    lanes = write_valid_lane_set(tmp_path)
    text = lane_yaml().replace("\n  additionalProperties: false\n  properties:\n    status", "\n  properties:\n    status")
    (lanes / "example-lane.yaml").write_text(text, encoding="utf-8")

    result = yuto_team_lanes.validate_all(lanes, swarm_path=None)

    assert not result.ok
    assert any("output_schema.additionalProperties must be false" in e for e in result.errors)


def test_receipt_validation_and_summary(tmp_path):
    lanes = write_valid_lane_set(tmp_path)
    result = yuto_team_lanes.validate_all(lanes, swarm_path=None)
    assert result.ok
    receipt = {
        "task_id": "task-1",
        "date": "2026-05-11",
        "lane_set": ["example-lane", "qa-critic"],
        "runtime": "hermes_main",
        "artifact_created": True,
        "verification_status": "pass",
        "rework_count": 0,
        "saved_time_estimate": "medium",
        "quality_gain": "medium",
        "safety_gain": "low",
        "reuse_recommendation": "keep",
        "notes": "Validated example receipt.",
    }

    errors = yuto_team_lanes.validate_receipt(receipt, set(result.lane_ids))
    assert errors == []
    log = tmp_path / "receipts.jsonl"
    yuto_team_lanes.append_receipt(log, receipt)
    receipts = yuto_team_lanes.load_receipts(log)
    summary = yuto_team_lanes.summarize_receipts(receipts)

    assert summary["count"] == 1
    assert summary["status_counts"]["pass"] == 1
    assert summary["lane_counts"]["example-lane"] == 1
    assert summary["needs_more_data"] is True


def test_receipt_rejects_unknown_lane(tmp_path):
    lanes = write_valid_lane_set(tmp_path)
    result = yuto_team_lanes.validate_all(lanes, swarm_path=None)
    receipt = {
        "task_id": "task-1",
        "date": "2026-05-11",
        "lane_set": ["missing-lane"],
        "runtime": "hermes_main",
        "artifact_created": False,
        "verification_status": "pass",
        "rework_count": 0,
        "saved_time_estimate": "none",
        "quality_gain": "none",
        "safety_gain": "none",
        "reuse_recommendation": "drop",
        "notes": "Bad lane.",
    }

    errors = yuto_team_lanes.validate_receipt(receipt, set(result.lane_ids))

    assert any("unknown lane" in e for e in errors)
