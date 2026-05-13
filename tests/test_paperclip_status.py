"""Tests for read-only Paperclip org/agent status panel support."""

import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_build_paperclip_status_summarizes_agent_lights(monkeypatch):
    """Paperclip status should be read-only and classify idle/working/blocked/approval lights."""
    from api import paperclip

    company_id = "company-1"
    agent_idle = "agent-idle"
    agent_working = "agent-working"
    agent_blocked = "agent-blocked"
    agent_approval = "agent-approval"

    payloads = {
        "/health": {"status": "ok", "version": "test"},
        "/companies": [{"id": company_id, "name": "Hermes_Strategic", "status": "active", "issuePrefix": "HERA"}],
        f"/companies/{company_id}/agents": [
            {"id": agent_idle, "name": "CEO", "role": "ceo", "title": "Chief Executive", "status": "idle"},
            {"id": agent_working, "name": "CTO", "role": "engineer", "title": "Chief Technology", "status": "idle", "reportsTo": agent_idle},
            {"id": agent_blocked, "name": "Research Lead", "role": "researcher", "title": "Research", "status": "idle", "reportsTo": agent_idle},
            {"id": agent_approval, "name": "AI Scout", "role": "researcher", "title": "Signal", "status": "idle", "reportsTo": agent_blocked},
        ],
        f"/companies/{company_id}/issues?limit=100": [
            {"id": "issue-working", "identifier": "HERA-1", "title": "Build status", "status": "in_progress", "assigneeAgentId": agent_working, "executionRunId": "run-1"},
            {"id": "issue-blocked", "identifier": "HERA-2", "title": "Needs source", "status": "blocked", "assigneeAgentId": agent_blocked},
            {"id": "issue-done", "identifier": "HERA-3", "title": "Done old", "status": "done", "assigneeAgentId": agent_idle},
        ],
        f"/companies/{company_id}/approvals": [
            {"id": "approval-1", "status": "pending", "requestedByAgentId": agent_approval, "type": "hire_agent"},
            {"id": "approval-old", "status": "approved", "requestedByAgentId": agent_idle, "type": "hire_agent"},
        ],
    }

    def fake_get_json(path, timeout=3.0):
        return payloads[path]

    monkeypatch.setattr(paperclip, "_get_json", fake_get_json)

    result = paperclip.build_paperclip_status()

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["company"]["id"] == company_id
    by_name = {agent["name"]: agent for agent in result["agents"]}

    assert by_name["CEO"]["light"] == "green"
    assert by_name["CEO"]["display_status"] == "idle"
    assert by_name["CTO"]["light"] == "yellow"
    assert by_name["CTO"]["display_status"] == "working"
    assert by_name["Research Lead"]["light"] == "green"
    assert by_name["Research Lead"]["display_status"] == "idle"
    assert by_name["Research Lead"]["blocked_issue_count"] == 1
    assert by_name["AI Scout"]["light"] == "blue"
    assert by_name["AI Scout"]["display_status"] == "awaiting_approval"

    assert by_name["CTO"]["active_issues"][0]["identifier"] == "HERA-1"
    assert by_name["AI Scout"]["pending_approval_count"] == 1

    org_chart = result["org_chart"]
    assert [node["name"] for node in org_chart] == ["CEO"]
    ceo_children = {node["name"]: node for node in org_chart[0]["children"]}
    assert set(ceo_children) == {"CTO", "Research Lead"}
    assert ceo_children["Research Lead"]["children"][0]["name"] == "AI Scout"


def test_org_chat_context_includes_agent_hierarchy(monkeypatch):
    """Org Chat context should carry the same reportsTo tree for compact +/- routing."""
    from api import paperclip

    company_id = "company-1"
    ceo_id = "agent-ceo"
    lead_id = "agent-lead"
    scout_id = "agent-scout"
    payloads = {
        "/health": {"status": "ok"},
        "/companies": [{"id": company_id, "name": "Hermes_Strategic", "status": "active", "issuePrefix": "HERA"}],
        f"/companies/{company_id}/agents": [
            {"id": ceo_id, "name": "CEO", "role": "ceo", "title": "Chief Executive", "status": "idle"},
            {"id": lead_id, "name": "Research Lead", "role": "lead", "title": "Research", "status": "idle", "reportsTo": ceo_id},
            {"id": scout_id, "name": "AI Scout", "role": "analyst", "title": "Signal", "status": "idle", "reportsTo": lead_id},
        ],
        f"/companies/{company_id}/issues?limit=25": [],
    }

    def fake_get_json(path, timeout=3.0):
        return payloads[path]

    monkeypatch.setattr(paperclip, "_get_json", fake_get_json)

    result = paperclip.get_org_chat_context()

    assert result["ok"] is True
    assert result["read_only"] is True
    assert [node["name"] for node in result["org_chart"]] == ["CEO"]
    lead = result["org_chart"][0]["children"][0]
    assert lead["name"] == "Research Lead"
    assert lead["children"][0]["name"] == "AI Scout"


def test_paperclip_panel_is_read_only_in_static_ui():
    """The MVP panel should expose refresh/status only, not mutation controls."""
    index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    panels_js = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    assert "panelPaperclip" in index_html
    assert "paperclipAgentList" in index_html
    assert "paperclipOrgChart" in panels_js
    assert "orgChatOrgTree" in index_html
    assert "orgChatSideOrgTree" in index_html
    assert "orgChatCompactTree" in panels_js
    assert "toggleOrgChatNode" in panels_js
    assert "orgchat-node-toggle" in panels_js
    assert "data-symbol-collapsed" in panels_js
    assert "data-symbol-expanded" in panels_js
    assert "collapsed ? '+' : '-'" in panels_js
    assert 'orgchat-tree-hint">+/-' in panels_js
    assert "/api/paperclip/status" in panels_js
    assert "POST /api/paperclip" not in panels_js
    assert "paperclip-create-agent" not in index_html


def test_paperclip_panel_prefers_korean_status_copy():
    """Paperclip read-only UI should localize operational labels for Korean cockpit use."""
    index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    panels_js = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    for expected in [
        "읽기 전용 조직 상태",
        "새로고침",
        "Paperclip 조직",
        "읽기 전용 실시간 스냅샷",
        "Paperclip 상태",
    ]:
        assert expected in index_html

    for expected in [
        "_paperclipKo",
        "_paperclipStatusLabel",
        "대기 중",
        "작업 중",
        "승인 대기",
        "활성 이슈",
        "대기 승인",
        "읽기 전용",
        "스냅샷",
    ]:
        assert expected in panels_js
