from pathlib import Path

WORKSPACE_JS = Path("static/workspace.js").read_text(encoding="utf-8")
SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
MESSAGES_JS = Path("static/messages.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")
STYLE_CSS = Path("static/style.css").read_text(encoding="utf-8")
CHANGELOG = Path("CHANGELOG.md").read_text(encoding="utf-8")


def test_workspace_artifacts_tab_collects_session_files_and_previews_them():
    assert 'id="workspaceArtifactsTab"' in INDEX_HTML
    assert 'id="workspaceArtifacts"' in INDEX_HTML
    assert "function collectSessionArtifacts()" in WORKSPACE_JS
    assert "function _artifactCandidatesFromToolCall(tc)" in WORKSPACE_JS
    assert "ARTIFACT_IGNORE_RE" in WORKSPACE_JS
    assert "node_modules" in WORKSPACE_JS and "__pycache__" in WORKSPACE_JS
    assert "function renderSessionArtifacts()" in WORKSPACE_JS
    assert "function scheduleRenderSessionArtifacts()" in WORKSPACE_JS
    assert "function openArtifactPath(path)" in WORKSPACE_JS
    assert "openFile(rel);" in WORKSPACE_JS
    assert "Prose mentions" in WORKSPACE_JS
    assert "/(?:created|wrote|updated|edited|saved|modified)" not in WORKSPACE_JS
    assert "panel.dataset.activeTab = _workspacePanelActiveTab" in WORKSPACE_JS
    assert "renderSessionArtifacts();" in SESSIONS_JS
    assert "typeof scheduleRenderSessionArtifacts==='function'" in MESSAGES_JS
    assert "S.toolCalls=d.session.tool_calls.map" in MESSAGES_JS
    assert ".workspace-artifact-item" in STYLE_CSS


def test_changelog_mentions_workspace_artifacts_tab():
    unreleased = CHANGELOG.split("## [v0.51.103]", 1)[0]
    assert "Artifacts tab" in unreleased
