"""Regression tests for the v0.50.250 finalizeThinkingCard cross-tab guard.

PR #1366 added an early-return guard to finalizeThinkingCard():
    const _guardTurn = $('liveAssistantTurn');
    if(_guardTurn && S.session && _guardTurn.dataset.sessionId !== S.session.session_id) return;

The guard's correctness depends on `liveAssistantTurn.dataset.sessionId`
being set whenever the turn is created. If it's never set, the
comparison is `undefined !== "<some-id>"` which is always true, and
finalizeThinkingCard() always early-returns — breaking the streaming
UI completely (every assistant turn's thinking card stays open
forever).

These tests pin both invariants:
  1. The guard exists in finalizeThinkingCard()
  2. Every site that creates `liveAssistantTurn` also stamps the
     dataset.sessionId attribute
"""

import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def test_finalize_thinking_card_guard_exists():
    """finalizeThinkingCard() must early-return when displayed session != streaming session."""
    start = UI_JS.find("function finalizeThinkingCard()")
    assert start != -1, "finalizeThinkingCard() must exist"
    end = UI_JS.find("\nfunction ", start + 1)
    body = UI_JS[start:end if end != -1 else len(UI_JS)]
    # The guard must read dataset.sessionId from the live turn AND compare
    # against S.session.session_id. The exact form must early-return.
    assert "dataset.sessionId" in body, (
        "finalizeThinkingCard() must read dataset.sessionId from the live turn "
        "to detect cross-tab/cross-session DOM mismatch."
    )
    assert "S.session.session_id" in body, (
        "finalizeThinkingCard() guard must compare against S.session.session_id."
    )


def test_live_turn_creation_sites_stamp_session_id():
    """Every site that sets `turn.id='liveAssistantTurn'` must also set
    `turn.dataset.sessionId`. If any site forgets the stamp, the guard in
    finalizeThinkingCard() always early-returns at that branch (because
    `undefined !== "<sid>"` is always true), breaking the streaming UI.
    """
    # Find every block that sets the id. Track each occurrence and verify
    # a dataset.sessionId stamp appears within ~5 lines after it.
    sites = []
    for m in re.finditer(r"\.id=['\"]liveAssistantTurn['\"]", UI_JS):
        # Find what variable name was used (e.g. `turn.id=`, `currentAssistantTurn.id=`)
        line_start = UI_JS.rfind("\n", 0, m.start()) + 1
        line = UI_JS[line_start:m.end() + 1]
        # Get the variable name
        var_m = re.search(r"(\w+)\.id=['\"]liveAssistantTurn['\"]", line)
        var_name = var_m.group(1) if var_m else "?"
        # Look at the next ~500 chars for a dataset.sessionId stamp on the same var
        # (500 chars accommodates an explanatory comment block before the stamp).
        window = UI_JS[m.end():m.end() + 500]
        stamped = bool(re.search(rf"{re.escape(var_name)}\.dataset\.sessionId\s*=", window))
        sites.append((var_name, m.start(), stamped))

    assert sites, "Expected at least one site setting `<var>.id='liveAssistantTurn'`"
    unstamped = [(v, p) for v, p, s in sites if not s]
    assert not unstamped, (
        f"Found {len(unstamped)} site(s) where `<var>.id='liveAssistantTurn'` "
        f"is set but `<var>.dataset.sessionId` is NOT stamped within the next "
        f"500 chars: {unstamped}. Without the stamp, the guard in "
        f"finalizeThinkingCard() always early-returns at this branch (because "
        f"undefined !== '<sid>' is always true), breaking the streaming UI. "
        f"Add `if(S.session) <var>.dataset.sessionId=S.session.session_id;` "
        f"after the id assignment."
    )


def test_at_least_three_live_turn_sites():
    """Sanity check: there are at least 3 sites that create the live turn.
    If a future refactor reduces this, the test_live_turn_creation_sites_stamp_session_id
    test still catches missing stamps, but this catches accidental site removal."""
    matches = re.findall(r"\.id=['\"]liveAssistantTurn['\"]", UI_JS)
    assert len(matches) >= 3, (
        f"Expected at least 3 sites assigning liveAssistantTurn id, found {len(matches)}. "
        "If sites were intentionally consolidated, this assertion can be relaxed."
    )
