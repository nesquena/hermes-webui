"""Live-rebuild-budget warning rate-limit — Q-2979-A3.

Per Copilot discussion_r3305864400 the budget-exceeded warning at
api/config.py is potentially high-volume: a hung upstream probe or a sustained
burst of cold callers could flood the log at warning level. The fix wraps the
warning with ``_should_warn_budget``: the FIRST hit in a cooldown window logs
at warning, subsequent hits in the same window log at info — so the signal is
retained but the volume is bounded.
"""
from __future__ import annotations

import time


def test_should_warn_budget_first_call_returns_true():
    from api import config as cfg

    # Isolate per-test state.
    cfg._BUDGET_WARN_STATE.pop("unit-test-reason-A", None)

    assert cfg._should_warn_budget("unit-test-reason-A", cooldown_s=300.0) is True


def test_should_warn_budget_inside_cooldown_returns_false():
    from api import config as cfg

    cfg._BUDGET_WARN_STATE.pop("unit-test-reason-B", None)

    assert cfg._should_warn_budget("unit-test-reason-B", cooldown_s=300.0) is True
    # Second hit within cooldown — must be False (caller should demote to info).
    assert cfg._should_warn_budget("unit-test-reason-B", cooldown_s=300.0) is False
    assert cfg._should_warn_budget("unit-test-reason-B", cooldown_s=300.0) is False


def test_should_warn_budget_after_cooldown_returns_true_again():
    from api import config as cfg

    cfg._BUDGET_WARN_STATE.pop("unit-test-reason-C", None)

    assert cfg._should_warn_budget("unit-test-reason-C", cooldown_s=0.05) is True
    assert cfg._should_warn_budget("unit-test-reason-C", cooldown_s=0.05) is False
    time.sleep(0.1)
    # Cooldown elapsed — warning level resumes.
    assert cfg._should_warn_budget("unit-test-reason-C", cooldown_s=0.05) is True


def test_should_warn_budget_distinct_reasons_have_independent_windows():
    from api import config as cfg

    for k in ("unit-test-reason-D1", "unit-test-reason-D2"):
        cfg._BUDGET_WARN_STATE.pop(k, None)

    assert cfg._should_warn_budget("unit-test-reason-D1", cooldown_s=300.0) is True
    # A different reason MUST get its own first-hit warning even while D1 is
    # still inside cooldown.
    assert cfg._should_warn_budget("unit-test-reason-D2", cooldown_s=300.0) is True
    # Both are now inside cooldown — both demote to info.
    assert cfg._should_warn_budget("unit-test-reason-D1", cooldown_s=300.0) is False
    assert cfg._should_warn_budget("unit-test-reason-D2", cooldown_s=300.0) is False
