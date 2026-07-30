"""Sidebar tooltip contract tests."""
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
SESSIONS_JS_PATH = REPO_ROOT / "static" / "sessions.js"


def _sessions_js() -> str:
    return SESSIONS_JS_PATH.read_text(encoding="utf-8")


def test_session_title_hover_shows_full_title_not_rename_hint():
    """The truncated sidebar title needs a real full-title tooltip.

    The old "Double-click to rename" title hid the only native hover affordance
    that can reveal a long chat title when badges/tags squeeze the row.
    """
    js = _sessions_js()
    assert "Double-click to rename" not in js
    assert "title.title=_sessionFullTitleTooltip(rawTitle,cleanTitle,s);" in js
    assert "function _sessionFullTitleTooltip(rawTitle, cleanTitle, session)" in js


def test_sidebar_status_badges_have_explanatory_tooltips():
    """Compact badges/icons must explain what they mean, not repeat the chip text."""
    js = _sessions_js()
    assert "function _sessionFullTitleTooltip" in js
    assert "function _sessionForkTooltip" in js
    assert "function _sessionLineageBadgeTooltip" in js
    assert "function _sessionChildBadgeTooltip" in js
    assert "function _sessionStateTooltip" in js
    assert "branchInd.title=_sessionForkTooltip(parentLabel);" in js
    assert "segmentCountEl.title=_sessionLineageBadgeTooltip(segmentLabel,canExpandLineageSegments);" in js
    assert "childCountEl.title=_sessionChildBadgeTooltip(childLabel);" in js
    assert "_sessionStateTooltip({isStreaming,hasUnread})" in js


def test_state_tooltip_does_not_clobber_attention_title():
    """The generic running/unread state tooltip must NOT overwrite a more
    specific, localized attention tooltip (pending approval/clarify).

    Regression: the first cut of this feature set ``state.title`` to the
    state tooltip *unconditionally*, two lines after assigning the localized
    ``attention.title`` — which both clobbered the attention tooltip and, for a
    needs-attention session that was not currently streaming, blanked it to ''
    (``_sessionStateTooltip`` returns '' when neither streaming nor unread).
    The attention title must take precedence, and the state tooltip must only
    apply when it is non-empty.
    """
    js = _sessions_js()
    # The attention title still wins.
    assert "if(attention&&attention.title) state.title=attention.title;" in js
    # The state tooltip is applied via an else-if guarded on non-empty, never
    # as an unconditional assignment that could blank the attention title.
    assert "else if(_stateTip) state.title=_stateTip;" in js
    # The old unconditional-clobber line must be gone.
    assert "state.title=_sessionStateTooltip({isStreaming,hasUnread});" not in js


def test_fork_tooltip_preserves_localized_base():
    """The fork tooltip must keep using the localized ``forked_from`` catalog
    key rather than hardcoding an English string — dropping ``t('forked_from')``
    was the one genuine i18n regression in the tooltip rework.
    """
    js = _sessions_js()
    assert "function _sessionForkTooltip" in js
    # Helper resolves the localized key with the usual typeof-t guard.
    assert "t('forked_from')" in js
    # The hardcoded English sentence from the first cut must be gone.
    assert "Forked conversation — parent:" not in js
