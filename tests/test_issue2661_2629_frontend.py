from pathlib import Path

SESSIONS_JS = Path("static/sessions.js").read_text(encoding="utf-8")
PANELS_JS = Path("static/panels.js").read_text(encoding="utf-8")
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


def test_changelog_mentions_session_and_cron_polish():
    unreleased = CHANGELOG.split("## [v0.51.103]", 1)[0]
    assert "bounded jitter/backoff" in unreleased
    assert "Expanded cron run rows" in unreleased
    assert "no longer drops content when Markdown rendering is unavailable" in unreleased
