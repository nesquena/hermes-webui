"""Regression guards for fast profile dropdown opening.

The user-visible failure was: clicking the composer/titlebar profile chip waited
for a cold /api/profiles request before showing the menu. On machines where the
profile metadata scan is slow, that made the click feel frozen for seconds.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")


def _function_body(src: str, marker: str, next_marker: str | None = None) -> str:
    start = src.index(marker)
    if next_marker is not None:
        end = src.index(next_marker, start)
        return src[start:end]
    depth = 0
    opened = False
    for idx, ch in enumerate(src[start:], start):
        if ch == "{":
            depth += 1
            opened = True
        elif ch == "}":
            depth -= 1
            if opened and depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"Could not extract function body for {marker}")


def test_profile_dropdown_opens_shell_before_network_fetch():
    body = _function_body(PANELS_JS, "function toggleProfileDropdown(e) {")
    cache_idx = body.index("const cached = _profileDropdownBestCachedData();")
    open_idx = body.index("_openProfileDropdownShell();")
    fetch_idx = body.index("_profileDropdownFetchFresh().then")
    assert cache_idx < open_idx < fetch_idx, (
        "toggleProfileDropdown must render/open from cache or loading shell before "
        "awaiting the slow /api/profiles refresh"
    )


def test_profile_dropdown_uses_shared_fetch_promise_and_local_storage_cache():
    fetch_body = _function_body(PANELS_JS, "function _profileDropdownFetchFresh(){")
    assert "if(_profileDropdownFetchPromise) return _profileDropdownFetchPromise;" in fetch_body
    assert "api('/api/profiles', {timeoutToast:false})" in fetch_body
    assert "_profileDropdownWriteStoredCache(data);" in fetch_body

    read_body = _function_body(PANELS_JS, "function _profileDropdownReadStoredCache(){")
    assert "localStorage.getItem(PROFILE_DROPDOWN_CACHE_KEY)" in read_body
    assert "PROFILE_DROPDOWN_CACHE_TTL_MS" in read_body


def test_profile_dropdown_closing_invalidates_inflight_refresh():
    close_body = _function_body(PANELS_JS, "function closeProfileDropdown() {")
    assert "_profileDropdownOpenGeneration++;" in close_body

    toggle_body = _function_body(PANELS_JS, "function toggleProfileDropdown(e) {")
    assert "const openGen = ++_profileDropdownOpenGeneration;" in toggle_body
    assert "if(openGen !== _profileDropdownOpenGeneration) return;" in toggle_body


def test_profiles_panel_refresh_updates_dropdown_cache():
    body = _function_body(PANELS_JS, "async function loadProfilesPanel() {")
    api_idx = body.index("const data = await api('/api/profiles');")
    cache_idx = body.index("_profileDropdownWriteStoredCache(data);")
    render_idx = body.index("panel.innerHTML = '';")
    assert api_idx < cache_idx < render_idx


def test_profile_dropdown_prefetches_after_page_load():
    assert "function _warmProfileDropdownCache(){" in PANELS_JS
    assert "window.addEventListener('load'" in PANELS_JS
    assert "_warmProfileDropdownCache();" in PANELS_JS
