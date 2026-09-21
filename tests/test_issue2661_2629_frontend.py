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
    # The renderer must read the expansion state for this exact run and
    # must fall back to ``data.content`` when expanded and no
    # response-first projection exists.
    assert "const expanded = _cronExpansionGet(_cronRunExpandKey(jobId, filename));" in PANELS_JS
    assert (
        "output = expanded ? (data.content || data.snippet || '') : (data.snippet || data.content || '');"
        in PANELS_JS
    )
    # #7303: response-first runs take the projection branch instead of
    # the raw-content branch above.
    assert "const parsed = data.parsed || null;" in PANELS_JS
    assert "const showResponseFirst = !!(parsed && parsed.has_response_boundary);" in PANELS_JS
    assert "output = expanded ? parsed.response : (data.snippet || parsed.response || '');" in PANELS_JS
    # "View full output" affordance (legacy path only).
    assert "if (!showResponseFirst && !expanded && data.content && data.snippet && data.content.length > data.snippet.length)" in PANELS_JS
    assert "_cronExpansionSet(_cronRunExpandKey(jobId, filename), true);" in PANELS_JS


def test_cron_run_body_renderer_is_shared_by_fetch_and_toggle():
    """#7303: expanding an already-open run must re-render the mounted
    body. A bare ``classList.toggle`` leaves the truncated snippet
    mounted while expanded and the full response mounted while
    collapsed. The renderer must be a named helper called from both the
    fetch path and the toggle.
    """
    assert "function _renderCronRunBody(body, data, jobId, filename){" in PANELS_JS
    assert "_renderCronRunBody(body, data, jobId, filename);" in PANELS_JS
    # The toggle re-renders from the cached projection instead of a
    # second round-trip when the row is already open.
    assert "const cached = _cronRunBodyCache[key];" in PANELS_JS
    assert "if (body && item && item.classList.contains('open') && cached) {" in PANELS_JS


def test_changelog_mentions_session_and_cron_polish():
    unreleased = CHANGELOG.split("## [v0.51.103]", 1)[0]
    assert "bounded jitter/backoff" in unreleased
    assert "Expanded cron run rows" in unreleased
    assert "no longer drops content when Markdown rendering is unavailable" in unreleased
