from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "chamin_team.py"
SPEC = importlib.util.spec_from_file_location("chamin_team", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
chamin_team = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = chamin_team
SPEC.loader.exec_module(chamin_team)


def test_route_auth_payment_selects_bounded_lanes():
    route = chamin_team.route_task("review this Next auth payment Stripe webhook PR")

    assert route.cookbook == "lawlabo-pr-review"
    assert route.scope == "next"
    assert "correctness-reviewer" in route.selected_lanes
    assert "data-auth-reviewer" in route.selected_lanes
    assert "security-reviewer" in route.selected_lanes
    assert len(route.selected_lanes) <= 3
    assert route.human_gate is True


def test_route_public_copy_selects_content_lane():
    route = chamin_team.route_task("review this Astro course page SEO copy and schema change")

    assert route.scope == "astro"
    assert "content-claims-seo-reviewer" in route.selected_lanes
    assert "frontend-ui-a11y-reviewer" in route.selected_lanes


def test_packet_contains_lane_instructions_and_final_contract():
    packet = chamin_team.build_packet(
        "review this Next auth payment Stripe webhook PR",
        target_ref="PR #123",
        scope="next",
    )

    assert "# Chamin Team Work Packet" in packet
    assert "PR #123" in packet
    assert "data-auth-reviewer" in packet
    assert "security-reviewer" in packet
    assert "Chamin Final Synthesis Contract" in packet
    assert "Do not let worker voices reach Kei directly." in packet


def test_receipt_dry_run_cli_outputs_json(capsys):
    rc = chamin_team.main(
        [
            "receipt",
            "--task-id",
            "task-1",
            "--lanes",
            "correctness-reviewer,qa-critic",
            "--status",
            "pass",
            "--notes",
            "Test receipt.",
            "--quality-gain",
            "medium",
            "--safety-gain",
            "low",
            "--reuse",
            "keep",
            "--dry-run",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["receipt"]["lane_set"] == ["correctness-reviewer", "qa-critic"]


def test_receipt_rejects_unknown_lane(capsys):
    rc = chamin_team.main(
        [
            "receipt",
            "--task-id",
            "task-1",
            "--lanes",
            "missing-lane",
            "--status",
            "pass",
            "--notes",
            "Bad receipt.",
            "--dry-run",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 1
    assert payload["ok"] is False
    assert any("unknown lane" in error for error in payload["errors"])
