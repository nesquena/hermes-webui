"""Phase 1.5 (#4717): empty/zero-conversation profile shows a quiet empty-state
skeleton on switch instead of a misleading content skeleton.

These are source-level structural guards (the skeleton logic is browser JS; the
behavioural check is a manual browser pass documented in the PR). They pin that
the per-profile count cache + the empty-branch wiring exist and are connected, so
a refactor can't silently drop the empty-profile affordance.
"""
import re
from pathlib import Path

import api.routes as routes  # noqa: F401  (ensures repo import path is set up)

SESSIONS_JS = Path(__file__).resolve().parent.parent / "static" / "sessions.js"
PANELS_JS = Path(__file__).resolve().parent.parent / "static" / "panels.js"
STYLE_CSS = Path(__file__).resolve().parent.parent / "static" / "style.css"


def _sessions():
    return SESSIONS_JS.read_text(encoding="utf-8")


def test_per_profile_count_cache_helpers_exist():
    src = _sessions()
    assert "SESSION_PROFILE_COUNTS_KEY" in src
    assert "function _recordSessionProfileCount(" in src
    assert "function _knownSessionProfileCount(" in src


def test_count_is_recorded_after_render():
    """The render path must persist the active profile's session count so the
    next switch can consult it — only for an unfiltered, single-profile view."""
    src = _sessions()
    assert "_recordSessionProfileCount(_allSessionsScope.profile, _allSessions.length)" in src
    # Gated on !all-profiles AND !filter-active so only an unfiltered total is cached
    # (mirrors the read-side gate; a filtered subset must not cache a misleading 0).
    record_idx = src.index("_recordSessionProfileCount(_allSessionsScope.profile")
    window = src[record_idx - 400:record_idx]
    assert "_showAllProfiles" in window, "count record must be gated on !_showAllProfiles"
    assert "_recordFilterActive" in window, "count record must be gated on !filterActive (write/read parity)"


def test_skeleton_accepts_target_profile_and_has_empty_branch():
    src = _sessions()
    assert "function showSessionListSkeleton(targetProfile)" in src
    # Empty branch keys off a KNOWN zero count (null/unknown keeps content skeleton).
    assert "_knownSessionProfileCount(targetProfile)" in src
    assert "knownCount === 0" in src
    assert "skeleton-list-empty" in src


def test_empty_branch_skipped_when_filter_active():
    """A project/source filter makes the unfiltered per-profile count unreliable,
    so the empty branch must be suppressed when a filter is active."""
    src = _sessions()
    assert "filterActive" in src
    # The guard combines the known-zero count with !filterActive.
    assert "knownCount === 0 && !filterActive" in src


def test_switch_call_site_passes_target_profile():
    src = PANELS_JS.read_text(encoding="utf-8")
    assert "showSessionListSkeleton(name)" in src, "profile switch must pass the target profile to the skeleton"


def test_empty_skeleton_css_respects_reduced_motion():
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert ".skeleton-empty-hint" in css
    # The empty hint must have a reduced-motion override (no animation).
    rm_blocks = re.findall(r"@media\s*\(prefers-reduced-motion:reduce\)\s*\{[^}]*skeleton-empty-hint[^}]*\}", css)
    assert rm_blocks, "skeleton-empty-hint must have a prefers-reduced-motion:reduce override"


def test_virtual_scroll_teardown_runs_for_skeleton():
    """The skeleton builder must tear down virtual-scroll state (the #4662
    Codex-gate guard against stale-row repaint) — done once up front so it
    applies to both the content and empty-state branches."""
    src = _sessions()
    idx = src.index("function showSessionListSkeleton(")
    body = src[idx: idx + 2000]
    assert "cancelAnimationFrame(_sessionVirtualScrollRaf)" in body
    assert "delete list.dataset.sessionVirtualTotal" in body
    assert "delete list.dataset.sessionVirtualActiveAnchor" in body
