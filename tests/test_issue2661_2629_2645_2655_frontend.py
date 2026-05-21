from pathlib import Path


SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
PANELS_JS = Path("static/panels.js").read_text(encoding="utf-8")
WORKSPACE_JS = Path("static/workspace.js").read_text(encoding="utf-8")
INDEX_HTML = Path("static/index.html").read_text(encoding="utf-8")
STYLE_CSS = Path("static/style.css").read_text(encoding="utf-8")
CHANGELOG = Path("CHANGELOG.md").read_text(encoding="utf-8")


def test_session_events_reconnect_uses_jittered_backoff_not_fixed_delay():
    assert "function _sessionEventsReconnectDelayMs()" in SESSIONS_JS
    assert "Math.random()" in SESSIONS_JS
    assert "_sessionEventsReconnectMaxMs" in SESSIONS_JS
    assert "_sessionEventsReconnectAttempt = 0" in SESSIONS_JS
    ensure_fn = SESSIONS_JS[SESSIONS_JS.find("function ensureSessionEventsSSE()") :]
    assert "const delayMs = _sessionEventsReconnectDelayMs();" in ensure_fn
    assert "}, 5000);" not in ensure_fn


def test_cron_expanded_run_renders_full_content_inline():
    assert "const expanded = _cronExpansionGet(_cronRunExpandKey(jobId, filename));" in PANELS_JS
    assert "const output = expanded ? (data.content || data.snippet || '') : (data.snippet || data.content || '');" in PANELS_JS
    assert "if (!expanded && data.content && data.snippet && data.content.length > data.snippet.length)" in PANELS_JS
    assert "_cronExpansionSet(_cronRunExpandKey(jobId, filename), true);" in PANELS_JS


def test_sidebar_has_quick_profile_switcher_synced_to_profile_api():
    assert "id=\"sidebarProfileSelect\"" in INDEX_HTML
    assert "onchange=\"switchToProfile(this.value)\"" in INDEX_HTML
    assert "function refreshQuickProfileSelect(data)" in PANELS_JS
    assert "api('/api/profiles').then(refreshQuickProfileSelect)" in PANELS_JS
    assert "if(quickSel) quickSel.value = S.activeProfile;" in PANELS_JS
    assert ".sidebar-profile-quick" in STYLE_CSS


def test_workspace_artifacts_tab_collects_session_files_and_previews_them():
    assert "id=\"workspaceArtifactsTab\"" in INDEX_HTML
    assert "id=\"workspaceArtifacts\"" in INDEX_HTML
    assert "function collectSessionArtifacts()" in WORKSPACE_JS
    assert "function renderSessionArtifacts()" in WORKSPACE_JS
    assert "function openArtifactPath(path)" in WORKSPACE_JS
    assert "openFile(rel);" in WORKSPACE_JS
    assert "renderSessionArtifacts();" in SESSIONS_JS
    assert ".workspace-artifact-item" in STYLE_CSS


def test_changelog_mentions_the_four_webui_polish_items():
    unreleased = CHANGELOG.split("## [v0.51.103]", 1)[0]
    assert "sidebar quick profile switcher" in unreleased
    assert "Artifacts tab" in unreleased
    assert "bounded jitter/backoff" in unreleased
    assert "Expanded cron run rows" in unreleased
