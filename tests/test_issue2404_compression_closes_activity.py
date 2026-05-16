"""Regression test for #2404 — auto-compression card closes live Activity group.

Before this fix, if `compressing` and `compressed` fired between two tool bursts
in the same live assistant turn — with no intervening `interim_assistant` —
the post-compression tool would attach to the pre-compression Activity row.
Compression mid-turn is a real timeline boundary (context changed) and the
visible Activity grouping must reflect that.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_append_live_compression_card_closes_activity_group():
    """appendLiveCompressionCard must remove data-live-activity-current from any
    open Activity group BEFORE inserting the compression card, so subsequent
    tool calls start a fresh Activity row rather than joining the pre-comp
    group."""
    # Locate the function body
    fn_start = UI_JS.index("function appendLiveCompressionCard(state){")
    fn_end = UI_JS.index("\nfunction ", fn_start + 1)
    fn_body = UI_JS[fn_start:fn_end]

    # The close call must come BEFORE the existing/inner.appendChild path
    close_call = "querySelectorAll('.tool-call-group[data-live-tool-call-group=\"1\"][data-live-activity-current=\"1\"]')"
    assert close_call in fn_body, "Must querySelectorAll on the live current activity group"

    close_idx = fn_body.index(close_call)
    # The forEach must remove the attribute
    forEach_section = fn_body[close_idx:close_idx + 400]
    assert "removeAttribute('data-live-activity-current')" in forEach_section

    # And it must come BEFORE the existing/appendChild branch
    existing_idx = fn_body.index("const existing=inner.querySelector('[data-live-compression-card")
    assert close_idx < existing_idx, "Activity close must run BEFORE compression card insert/replace"


def test_compression_card_close_call_uses_correct_selector():
    """Selector must match the exact attributes ensureActivityGroup sets on the
    open live group — both data-live-tool-call-group=1 AND data-live-activity-current=1.
    A looser selector could close a non-live or already-closed group; tighter
    would miss the case the fix targets."""
    fn_start = UI_JS.index("function appendLiveCompressionCard(state){")
    fn_end = UI_JS.index("\nfunction ", fn_start + 1)
    fn_body = UI_JS[fn_start:fn_end]

    # Must include both attribute matchers, not just one
    assert 'data-live-tool-call-group="1"' in fn_body
    assert 'data-live-activity-current="1"' in fn_body


def test_ensure_activity_group_sets_both_live_attributes():
    """Sanity check the writer side — ensureActivityGroup sets both attrs that
    the close call queries. If this writer changes its attributes, the close
    selector above must change too (or this test will catch the drift)."""
    fn_start = UI_JS.index("function ensureActivityGroup(inner, opts){")
    fn_end = UI_JS.index("\nfunction ", fn_start + 1)
    writer = UI_JS[fn_start:fn_end]

    assert "setAttribute('data-live-tool-call-group','1')" in writer
    assert "setAttribute('data-live-activity-current','1')" in writer
