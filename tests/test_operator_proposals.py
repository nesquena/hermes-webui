import importlib
import io
import json
import subprocess
import sys
import types
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch


DEFAULT_ACTIONS = [
    {
        "id": "reverse_prompt_v0",
        "rank": 1,
        "type": "quick_win",
        "title": "Manual Reverse Prompt v0",
        "summary": "Read known source files and propose useful Hermes workflows without executing.",
        "owner": "future Hermes session",
        "side_effect_level": "read-only until approved",
        "status": "proposal",
    },
    {
        "id": "webui_proof_freshness_labels",
        "rank": 2,
        "type": "quick_win",
        "title": "Proof/freshness labels in WebUI",
        "summary": "Add source/timestamp/stale/receipt chips to important WebUI cards.",
        "owner": "future Hermes WebUI task",
        "side_effect_level": "code change",
        "status": "proposal",
    },
    {
        "id": "goal_cron_safety_template",
        "rank": 3,
        "type": "quick_win",
        "title": "Goal/cron safety template",
        "summary": "Long-running goals/crons need explicit scope, gates, receipts, and stop conditions.",
        "owner": "future skill/WebUI task",
        "side_effect_level": "docs/skill change",
        "status": "proposal",
    },
    {
        "id": "kanban_operator_panel",
        "rank": 4,
        "type": "medium_build",
        "title": "Kanban operator panel for hermes-operator",
        "summary": "Surface task states, assignees, dependencies, scratch paths, receipts, and blocked reasons.",
        "owner": "future Hermes WebUI task",
        "side_effect_level": "code change",
        "status": "proposal",
    },
]


def _source_tree(tmp_path, *, actions=None, omit_action_summary=False, malformed_action_summary=False):
    root = tmp_path / "workspace"
    active_plan = root / "obsidian-vault" / "Agent-Shared" / "ACTIVE PLAN.md"
    wake_state = root / "obsidian-vault" / "Agent-Kimi" / "WAKE_STATE.md"
    kanban_hardening = root / "obsidian-vault" / "Agent-Kimi" / "Hermes Kanban Pilot Hardening.md"
    action_summary = root / "artifacts" / "hermes-video-RoBD7Lc-0MI" / "action-summary.json"

    active_plan.parent.mkdir(parents=True, exist_ok=True)
    wake_state.parent.mkdir(parents=True, exist_ok=True)
    action_summary.parent.mkdir(parents=True, exist_ok=True)

    active_plan.write_text("# Active Plan\nDo not restart AIM Labs. PRIVATE MARKDOWN BODY SHOULD NOT LEAK.\n", encoding="utf-8")
    wake_state.write_text("generated_at: 2026-05-21T01:17:12Z\nLive services must be probed before use.\n", encoding="utf-8")
    kanban_hardening.write_text("# Hermes Kanban Pilot Hardening\nScratch paths must be safe.\n", encoding="utf-8")

    if malformed_action_summary:
        action_summary.write_text("{not json", encoding="utf-8")
    elif not omit_action_summary:
        action_summary.write_text(
            json.dumps(
                {
                    "source_url": "https://youtu.be/RoBD7Lc-0MI?si=fZqI80VZGaovwspS",
                    "video_id": "RoBD7Lc-0MI",
                    "date": "2026-05-26",
                    "brief_path": str(root / "obsidian-vault" / "Agent-Kimi" / "Deep Research Briefs" / "brief.md"),
                    "artifact_dir": str(action_summary.parent),
                    "ranked_actions": list(actions if actions is not None else DEFAULT_ACTIONS),
                    "avoid": [
                        "Reviving AIM Labs automation or old AIM crons",
                        "Fake/demo/stale dashboard data",
                        "Kanban scratch workspaces pointed at real project directories",
                    ],
                }
            ),
            encoding="utf-8",
        )
    return {
        "active_plan": active_plan,
        "wake_state": wake_state,
        "kanban_hardening": kanban_hardening,
        "action_summary": action_summary,
    }


def _patch_sources(monkeypatch, proposals, source_paths):
    monkeypatch.setattr(proposals, "SOURCE_SPECS", source_paths, raising=False)


def _patch_truth(monkeypatch, *, status="live"):
    import api.operator_truth as operator_truth

    def fake_truth_payload(*, session_id=None, ui_board_hint=None, now=None):
        return {
            "version": 1,
            "verified_at": now,
            "status": status,
            "summary": f"Truth {status}",
            "chips": [],
            "sources": [],
            "issues": [] if status == "live" else [f"truth {status}"],
        }

    monkeypatch.setattr(operator_truth, "build_operator_truth_payload", fake_truth_payload, raising=False)


def test_operator_proposal_payload_has_version_status_sources_and_no_execution(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    sources = _source_tree(tmp_path)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")

    payload = proposals.build_operator_proposal_payload(now=123.0)

    assert payload["version"] == 1
    assert payload["generated_at"] == 123.0
    assert payload["mode"] == "manual-read-only"
    assert payload["would_execute"] is False
    assert payload["status"] in {"live", "stale", "unknown"}
    assert payload["proposals"]
    assert len(payload["proposals"]) <= 3
    assert all(p["would_execute"] is False for p in payload["proposals"])
    assert all(p["approval"]["kind"] == "draft_only" and p["approval"]["executes"] is False for p in payload["proposals"])
    assert all(p["decline"]["kind"] == "client_only" and p["decline"]["executes"] is False for p in payload["proposals"])
    assert {s["id"] for s in payload["sources"]} >= {"active_plan", "wake_state", "kanban_hardening", "action_summary"}


def test_operator_proposals_selects_top_three_ranked_actions_from_action_summary(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    shuffled = [DEFAULT_ACTIONS[3], DEFAULT_ACTIONS[1], DEFAULT_ACTIONS[2], DEFAULT_ACTIONS[0]]
    sources = _source_tree(tmp_path, actions=shuffled)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")

    payload = proposals.build_operator_proposal_payload(now=1.0)

    assert [p["id"] for p in payload["proposals"]] == [
        "reverse_prompt_v0",
        "webui_proof_freshness_labels",
        "goal_cron_safety_template",
    ]
    assert [p["rank"] for p in payload["proposals"]] == [1, 2, 3]


def test_operator_proposals_missing_action_summary_returns_unknown_without_fake_actions(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    sources = _source_tree(tmp_path, omit_action_summary=True)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")

    payload = proposals.build_operator_proposal_payload(now=1.0)

    assert payload["status"] == "unknown"
    assert payload["proposals"] == []
    assert any("action_summary" in issue for issue in payload["issues"])


def test_operator_proposals_malformed_action_summary_returns_unknown(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    sources = _source_tree(tmp_path, malformed_action_summary=True)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")

    payload = proposals.build_operator_proposal_payload(now=1.0)

    assert payload["status"] == "unknown"
    assert payload["proposals"] == []
    assert any("malformed" in issue.lower() or "json" in issue.lower() for issue in payload["issues"])


def test_operator_proposals_non_file_action_summary_returns_unknown(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    sources = _source_tree(tmp_path, omit_action_summary=True)
    sources["action_summary"].mkdir(parents=True)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")

    payload = proposals.build_operator_proposal_payload(now=1.0)

    assert payload["status"] == "unknown"
    assert payload["proposals"] == []
    assert any("not a regular file" in issue.lower() for issue in payload["issues"])


def test_operator_proposals_invalid_ranked_action_fields_return_unknown(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    bad_actions = [
        dict(DEFAULT_ACTIONS[0], id="duplicate_action"),
        dict(DEFAULT_ACTIONS[1], id="duplicate_action", title=""),
    ]
    sources = _source_tree(tmp_path, actions=bad_actions)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")

    payload = proposals.build_operator_proposal_payload(now=1.0)

    assert payload["status"] == "unknown"
    assert payload["proposals"] == []
    assert any("duplicate" in issue.lower() or "non-empty" in issue.lower() for issue in payload["issues"])


def test_operator_proposals_includes_operator_truth_as_evidence_and_marks_stale(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    sources = _source_tree(tmp_path)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="stale")

    payload = proposals.build_operator_proposal_payload(session_id="abc123", ui_board_hint="hermes-operator", now=1.0)

    assert payload["truth"]["status"] == "stale"
    assert payload["status"] == "stale"
    assert any(ev.get("source_id") == "operator_truth" for ev in payload["proposals"][0]["evidence"])
    assert payload["proposals"], "stale truth should warn, not force fake-empty proposals"


def test_operator_proposals_do_not_return_full_markdown_source_bodies(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    sources = _source_tree(tmp_path)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")

    payload = proposals.build_operator_proposal_payload(now=1.0)
    serialized = json.dumps(payload)

    assert "PRIVATE MARKDOWN BODY SHOULD NOT LEAK" not in serialized
    assert "Do not restart AIM Labs" not in serialized
    assert "Scratch paths must be safe" not in serialized


def test_operator_proposals_never_imports_or_calls_dispatch_cron_or_shell(monkeypatch, tmp_path):
    proposals = importlib.import_module("api.operator_proposals")
    sources = _source_tree(tmp_path)
    _patch_sources(monkeypatch, proposals, sources)
    _patch_truth(monkeypatch, status="live")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("proposal builder must be read-only and shell-free")

    fake_kanban_bridge = types.ModuleType("api.kanban_bridge")
    fake_cron = types.ModuleType("cron.jobs")
    for name in ("dispatch", "dispatch_ready_tasks", "claim", "complete", "create_task"):
        setattr(fake_kanban_bridge, name, forbidden)
    setattr(fake_cron, "create", forbidden)
    monkeypatch.setitem(sys.modules, "api.kanban_bridge", fake_kanban_bridge)
    monkeypatch.setitem(sys.modules, "cron.jobs", fake_cron)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    payload = proposals.build_operator_proposal_payload(now=1.0)

    assert payload["would_execute"] is False
    assert calls == []


def test_operator_proposals_route_returns_json(monkeypatch):
    import api.routes as routes

    expected = {"version": 1, "status": "unknown", "proposals": [], "sources": []}
    captured = {}

    def fake_j(handler, payload, status=200, extra_headers=None):
        captured["status"] = status
        captured["payload"] = payload
        return True

    with patch("api.operator_proposals.build_operator_proposal_payload", return_value=expected) as build_payload, patch(
        "api.routes.j", side_effect=fake_j
    ):
        handled = routes.handle_get(
            types.SimpleNamespace(wfile=io.BytesIO()),
            urlparse("/api/operator/proposals?session_id=abc123&ui_board=hermes-operator"),
        )

    assert handled is True
    assert captured["status"] == 200
    assert captured["payload"] == expected
    build_payload.assert_called_once_with(session_id="abc123", ui_board_hint="hermes-operator")
