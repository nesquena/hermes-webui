import os
import pytest


def test_jira_client_init_from_env(monkeypatch):
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_TOKEN", "fake-token-123")
    from api.jira import JiraClient
    client = JiraClient(base_url="https://test.atlassian.net")
    assert client.base_url == "https://test.atlassian.net"
    assert client._auth_header is not None


def test_jira_client_init_missing_creds(monkeypatch):
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_TOKEN", raising=False)
    from api.jira import JiraClient
    with pytest.raises(ValueError, match="JIRA_EMAIL"):
        JiraClient(base_url="https://test.atlassian.net")
