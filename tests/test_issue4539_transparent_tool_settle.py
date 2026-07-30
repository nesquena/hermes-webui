"""Regression tests for issue #4539: transparent-stream tool-call rows
vanish on turn settle, reappear only after tab/session switch."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _settled_cleanup_selector():
    """Extract the settled-node cleanup querySelectorAll string."""
    anchor = ".tool-worklog-group:not([data-compression-card])"
    idx = UI_JS.index(anchor)
    marker = ".forEach(el=>el.remove())"
    end = UI_JS.index(marker, idx) + len(marker)
    line_start = UI_JS.rfind("\n", 0, idx) + 1
    return UI_JS[line_start:end]


def _tool_bucketing_block():
    """Extract the tool-call bucketing region including burst-ID pre-scan."""
    start = UI_JS.index("const knownBurstIds=")
    end = UI_JS.index("\n    }", UI_JS.index("for(const tc of (S.toolCalls||[])){", start)) + len("\n    }")
    return UI_JS[start:end]


class TestCleanupSelectorProtectsTransparentToolRows:
    """Fix A: the settled-node cleanup must not remove transparent tool rows."""

    def test_tool_card_row_arm_excludes_event_type_tool(self):
        selector = _settled_cleanup_selector()
        assert ".tool-card-row:not([data-compression-card]):not([data-event-type=\"tool\"])" in selector, (
            "The .tool-card-row cleanup arm must exclude [data-event-type='tool'] "
            "rows to protect transparent tool-call rows from deletion"
        )

    def test_thinking_arm_still_excludes_event_type_thinking(self):
        selector = _settled_cleanup_selector()
        assert ":not([data-event-type=\"thinking\"])" in selector, (
            "The .agent-activity-thinking arm must still exclude "
            "[data-event-type='thinking'] rows"
        )

    def test_tool_and_thinking_arms_follow_same_event_type_pattern(self):
        selector = _settled_cleanup_selector()
        assert ":not([data-event-type=\"tool\"])" in selector
        assert ":not([data-event-type=\"thinking\"])" in selector


class TestBurstIdFallbackWhenSegmentLacksBurstAttribute:
    """Fix B: bucketing must fall back to assistant:${aIdx} when no segment
    carries a matching data-activity-burst-id."""

    def test_burst_resolvable_check_before_burst_key(self):
        block = _tool_bucketing_block()
        assert "burstResolvable" in block, (
            "The tool-call bucketing loop must check burst-ID resolvability "
            "before using 'burst:' as the bucket key"
        )

    def test_burst_ids_pre_scanned_from_assistant_segments(self):
        block = _tool_bucketing_block()
        assert "assistantSegments.values()" in block, (
            "knownBurstIds must be built from assistantSegments before the loop"
        )
        assert "knownBurstIds.has(burstId)" in block, (
            "burstResolvable must use the pre-scanned knownBurstIds set"
        )

    def test_fallback_uses_burst_resolvable_not_raw_burst_id(self):
        block = _tool_bucketing_block()
        assert re.search(r"burstResolvable\?.*burst:", block), (
            "The bucket key must use burstResolvable (not raw burstId) "
            "as the guard for the burst: key path"
        )

    def test_assistant_fallback_preserved(self):
        block = _tool_bucketing_block()
        assert "`assistant:${aIdx}`" in block, (
            "The assistant:${aIdx} fallback must still be present"
        )


class TestDecorateTransparentEventRowSetsEventType:
    """Confirms the decorator still sets the attributes the cleanup
    selector depends on for its exclusion."""

    def test_decorator_sets_data_event_type(self):
        start = UI_JS.index("function _decorateTransparentEventRow(row, opts){")
        end = UI_JS.index("\nfunction ", start + 1)
        block = UI_JS[start:end]
        assert "row.setAttribute('data-event-type',type)" in block

    def test_tool_type_triggers_transparent_event_card(self):
        start = UI_JS.index("function _decorateTransparentEventRow(row, opts){")
        end = UI_JS.index("\nfunction ", start + 1)
        block = UI_JS[start:end]
        assert "if(type==='tool')" in block
