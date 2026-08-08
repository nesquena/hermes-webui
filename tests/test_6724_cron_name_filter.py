"""Regression coverage for the Scheduled Jobs sidebar name filter (#6724)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")


def test_cron_search_input_uses_the_shared_sidebar_search_pattern():
    """The cron filter must reuse .sidebar-search, not a bespoke input."""
    assert 'id="cronSearch"' in INDEX_HTML, "input removed -> no way to type a filter query"
    assert '<div class="cron-search sidebar-search">' in INDEX_HTML, (
        "not using the shared sidebar-search wrapper -> loses existing CSS, breaks parity with "
        "Chat/Kanban/Skills filters"
    )
    assert 'type="search"' in INDEX_HTML, "wrong input type -> loses native clear (x) button"
    assert 'oninput="filterCrons()"' in INDEX_HTML, (
        "missing oninput handler -> typing does not filter the list live"
    )
    assert 'data-1p-ignore data-lpignore="true" data-bwignore="true"' in INDEX_HTML, (
        "missing password-manager opt-out attrs -> browsers may offer to autofill credentials "
        "into a search box"
    )


def test_cron_filter_is_case_insensitive_name_only_and_uses_cached_jobs():
    """Filter must read from the cached _cronList (no API call) and match name only."""
    assert "function filterCrons() { loadCrons(false, true); }" in PANELS_JS, (
        "filterCrons must call loadCrons with useCached=true -> otherwise every keystroke "
        "re-fetches /api/crons instead of filtering the cached list"
    )
    assert "const query = ($('cronSearch')?.value || '').trim().toLocaleLowerCase();" in PANELS_JS, (
        "query must be trimmed and lowercased -> without this, trailing whitespace or case "
        "differences would hide matching jobs"
    )
    assert "String(job.name || '').toLocaleLowerCase().includes(query)" in PANELS_JS, (
        "filter must match job.name only -> matching status/profile too would produce "
        "surprising hits, e.g. a job named 'Cleanup after errors' matching a search for 'error'"
    )


def test_cron_filter_preserves_active_paused_partition_and_matched_count():
    """Filtering must not break the active/paused split or the paused count from #4026."""
    assert "status.state === 'paused' ? _pausedJobs : _activeJobs" in PANELS_JS, (
        "partition removed -> paused jobs would drown the active list again, regressing #4026"
    )
    assert "summary.textContent = `${headerLabel} (${_pausedJobs.length})`;" in PANELS_JS, (
        "paused count must reflect the post-filter length -> otherwise the collapsed summary "
        "shows a stale count that doesn't match what's actually inside"
    )
    assert "if (!_activeJobs.length && !_pausedJobs.length)" in PANELS_JS, (
        "empty-match check must test the filtered partitions, not the raw _cronList -> testing "
        "_cronList.length instead would render a blank sidebar when a filter matches nothing, "
        "since the unfiltered list is never empty"
    )


def test_cron_filter_has_localized_empty_state_and_skips_api_fetch():
    """No-match state must be localized, and cached filtering must never hit the network."""
    assert "cron_no_matching_jobs: 'No jobs match your search.'" in I18N_JS, (
        "no-match string missing from the canonical English locale block"
    )
    assert "t('cron_no_matching_jobs')" in PANELS_JS, (
        "empty state must go through t() -> a hardcoded string would not localize for other "
        "languages the way filter_conversations already does"
    )
    assert "if (!useCached) {" in PANELS_JS, (
        "the API fetch must stay gated behind useCached -> filterCrons() passes useCached=true, "
        "so this guard is what actually prevents a network call on every keystroke"
    )
    assert "const data = await api('/api/crons' + allProfilesQS);" in PANELS_JS, (
        "confirms the fetch call still lives inside the useCached guard above, not outside it"
    )
