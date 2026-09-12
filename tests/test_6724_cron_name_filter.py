"""Regression coverage for the Scheduled Jobs sidebar name filter (#6724)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


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


def test_cron_search_has_an_explicit_clear_control():
    """The shared CSS hides the native WebKit clear (x), so an explicit control is required.

    .sidebar-search input::-webkit-search-cancel-button is disabled for every panel
    (style.css), so type="search" alone does not give the user a way to clear the
    query in Chromium/Safari. The Chat filter compensates with #sessionSearchClear;
    the cron filter must follow the same pattern with #cronSearchClear.
    """
    assert 'id="cronSearchClear"' in INDEX_HTML, (
        "no explicit clear button -> user has no way to clear the query, since the native "
        "WebKit clear (x) is disabled by .sidebar-search input::-webkit-search-cancel-button"
    )
    assert 'onclick="clearCronSearch()"' in INDEX_HTML, (
        "clear button must call clearCronSearch() -> otherwise the button renders but does "
        "nothing when clicked"
    )
    assert '<div class="cron-search-field">' in INDEX_HTML, (
        "input and clear button must share the .cron-search-field wrapper, mirroring "
        ".session-search-field -> needed for the clear button's absolute positioning"
    )
    assert "function clearCronSearch(" in PANELS_JS, (
        "clearCronSearch must be defined -> the clear button's onclick would be a no-op "
        "otherwise"
    )
    assert "function syncCronSearchClear(" in PANELS_JS, (
        "syncCronSearchClear must be defined -> without it the clear button never becomes "
        "visible when the user types, or never hides again after clearing"
    )
    assert ".cron-search-clear" in STYLE_CSS, (
        "no CSS for the clear button -> it would render unstyled or overlap the input text, "
        "since .session-search-clear's rules are scoped to .session-search only"
    )


def test_cron_filter_is_case_insensitive_name_only_locale_safe_and_uses_cached_jobs():
    """Filter must read from the cached _cronList (no API call), match name only, and
    avoid toLocaleLowerCase()'s Turkish-locale dotless-i bug (e.g. 'I' -> 'ı', not 'i'),
    which would silently hide a valid case-insensitive match for Turkish-locale users.
    """
    assert "if (!Array.isArray(_cronList)) return;" in PANELS_JS, (
        "filterCrons must guard against _cronList being null -> typing before the initial "
        "/api/crons request resolves would otherwise throw '_cronList is not iterable'"
    )
    assert "loadCrons(false, true)" in PANELS_JS, (
        "filterCrons must call loadCrons with useCached=true -> otherwise every keystroke "
        "re-fetches /api/crons instead of filtering the cached list"
    )
    assert "const query = ($('cronSearch')?.value || '').trim().toLowerCase();" in PANELS_JS, (
        "query must use toLowerCase(), not toLocaleLowerCase() -> under the Turkish locale, "
        "toLocaleLowerCase() maps 'I' to dotless 'ı' instead of 'i', silently breaking matches "
        "like searching 'import' against a job named 'IMPORT'"
    )
    assert "String(job.name || '').toLowerCase().includes(query)" in PANELS_JS, (
        "filter must match job.name only, using locale-independent toLowerCase() -> matching "
        "status/profile too would produce surprising hits, e.g. a job named 'Cleanup after "
        "errors' matching a search for 'error'; toLocaleLowerCase() would also reintroduce the "
        "Turkish dotless-i bug on this side of the comparison"
    )
    assert "toLocaleLowerCase" not in PANELS_JS, (
        "toLocaleLowerCase() must not reappear anywhere in panels.js -> it silently breaks "
        "case-insensitive matching under the Turkish locale"
    )


def test_cron_load_uses_a_token_to_resolve_deferred_first_load_ordering():
    """A stale in-flight request must never overwrite a newer one's result.

    Scenario: the initial (useCached=false) /api/crons fetch is still in flight
    when the user types and then clears the search box before it resolves. Both
    the type and the clear trigger their own loadCrons(false, true) calls, which
    run synchronously against whatever _cronList currently holds. Without an
    ownership check, the initial fetch could resolve afterward and either get
    silently discarded by a later synchronous render, or (worse) each call could
    stomp on the others' render in an order that leaves the sidebar showing a
    stale or incomplete job list instead of the just-fetched one.
    """
    assert "let _cronLoadToken = 0;" in PANELS_JS, (
        "missing the load-ordering token -> nothing distinguishes a stale, "
        "still-in-flight loadCrons() call from the most recent one"
    )
    assert "const myToken = useCached ? _cronLoadToken : ++_cronLoadToken;" in PANELS_JS, (
            "only network fetches (useCached=false) may claim a new token -> if cached "
            "filter/clear calls also incremented it, a keystroke during an in-flight "
            "refresh would outrank that refresh's own token, causing its fresh response "
            "to be discarded as 'stale' even though it's the most recent legitimate fetch"
    )
    assert "if (myToken !== _cronLoadToken) return;" in PANELS_JS, (
        "the awaited /api/crons response must check it's still the newest call "
        "before writing to _cronList or rendering -> otherwise a slow initial "
        "fetch can overwrite state that a later type/clear already rendered"
    )
    assert "} else if (myToken !== _cronLoadToken) {" in PANELS_JS, (
        "cached (useCached=true) calls -- the type/clear path -- must also bail "
        "out if a newer call has since claimed the token, so a burst of rapid "
        "typing and clearing can't render out of order"
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
