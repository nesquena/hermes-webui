"""Tests for Paperclip Org Chat MVP support."""

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(REPO_ROOT))


def test_get_org_chat_context_returns_agents_issues_and_recent_comments(monkeypatch):
    """Org Chat context should be read-oriented and compact comments from recent issues."""
    from api import paperclip

    company_id = "company-1"
    issue_id = "issue-1"
    payloads = {
        "/health": {"status": "ok"},
        "/companies": [{"id": company_id, "name": "Hermes_Strategic", "status": "active", "issuePrefix": "HERA"}],
        f"/companies/{company_id}/agents": [{"id": "agent-1", "name": "CEO", "title": "CEO", "status": "idle"}],
        f"/companies/{company_id}/issues?limit=25": [
            {"id": issue_id, "identifier": "HERA-1", "title": "Org topic", "status": "blocked", "assigneeAgentId": "agent-1", "updatedAt": "2026-05-04T00:00:00Z"},
        ],
        f"/issues/{issue_id}/comments": [
            {"id": "comment-1", "issueId": issue_id, "authorAgentId": "agent-1", "authorUserId": None, "body": "Agent reply", "createdAt": "2026-05-04T00:01:00Z"},
            {"id": "comment-2", "issueId": issue_id, "authorAgentId": None, "authorUserId": "local-board", "body": "Hermes note", "createdAt": "2026-05-04T00:02:00Z"},
        ],
    }

    monkeypatch.setattr(paperclip, "_get_json", lambda path, timeout=3.0: payloads[path])

    result = paperclip.get_org_chat_context(limit=25, comments_per_issue=5)

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["company"]["id"] == company_id
    assert result["agents"][0]["name"] == "CEO"
    assert result["issues"][0]["identifier"] == "HERA-1"
    assert [m["body"] for m in result["messages"]] == ["Agent reply", "Hermes note"]
    assert result["messages"][0]["author_name"] == "CEO"
    assert result["messages"][1]["author_name"] == "Hermes/User"


def test_send_org_chat_message_posts_comment_only_and_verifies_readback(monkeypatch):
    """Org Chat send should append an issue comment, not checkout or execute agents."""
    from api import paperclip

    posted = []
    company_id = "company-1"
    issue_id = "issue-1"
    comment_id = "comment-new"
    payloads = {
        "/companies": [{"id": company_id, "name": "Hermes_Strategic", "status": "active", "issuePrefix": "HERA"}],
        f"/issues/{issue_id}": {"id": issue_id, "identifier": "HERA-1", "title": "Org topic", "status": "blocked"},
        f"/issues/{issue_id}/comments": [
            {"id": comment_id, "issueId": issue_id, "authorAgentId": None, "authorUserId": "local-board", "body": "[Org Chat → CEO]\n\nhello org", "createdAt": "2026-05-04T00:03:00Z"},
        ],
    }

    def fake_post_json(path, payload, timeout=4.0):
        posted.append((path, payload))
        return {"id": comment_id, "issueId": issue_id, "body": payload["body"]}

    monkeypatch.setattr(paperclip, "_get_json", lambda path, timeout=3.0: payloads[path])
    monkeypatch.setattr(paperclip, "_post_json", fake_post_json)

    result = paperclip.send_org_chat_message(issue_id=issue_id, message="hello org", target_label="CEO")

    assert result["ok"] is True
    assert result["read_only"] is False
    assert result["mutation"] == "comment_only"
    assert posted == [(f"/issues/{issue_id}/comments", {"body": "[Org Chat → CEO]\n\nhello org"})]
    assert result["comment"]["id"] == comment_id
    assert result["verified"] is True


def test_org_chat_static_ui_prefers_korean_boundary_copy():
    """Org Chat should explain the comment-only Paperclip bridge in Korean."""
    index_html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    panels_js = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    for expected in [
        "조직 댓글",
        "기존 Paperclip 이슈에 댓글만 추가합니다.",
        "기존 이슈 댓글만",
        "이슈 선택",
        "댓글 작성",
        "경계: 댓글 전용",
    ]:
        assert expected in index_html

    for expected in [
        "기존 이슈",
        "댓글 없음",
        "Org Chat을 불러오는 중...",
        "이슈와 메시지가 필요합니다.",
        "댓글을 게시하고 확인했습니다.",
        "전송 실패",
    ]:
        assert expected in panels_js


def test_send_org_chat_message_rejects_empty_issue(monkeypatch):
    from api.paperclip import send_org_chat_message

    result = send_org_chat_message("", "hello")

    assert result["ok"] is False
    assert result["mutation"] == "comment_only"


def test_send_org_chat_message_rejects_overlong_message(monkeypatch):
    from api.paperclip import send_org_chat_message

    def fail_post(*_args, **_kwargs):
        raise AssertionError("overlong comments must not reach Paperclip")

    monkeypatch.setattr("api.paperclip._post_json", fail_post)
    result = send_org_chat_message("issue-1", "x" * 12001)

    assert result["ok"] is False
    assert "too long" in result["error"]
