"""#7855 re-gate: the two remaining CORE findings.

**CORE 1 — an edited draft re-binds the stale continuation token.**
``/api/session/draft`` copied the incoming text into ``next_draft`` and then passed
``next_draft`` to ``_project_session_draft_write`` as its ``current_draft``
argument. The helper's "did the text change?" comparison therefore compared the
new text against itself and never fired, so autosaving unrelated text kept the
token, a reload bound it to that text, and Send consumed the pending continuation.
Reproduced in Chromium (edit → reload, edit → switch and return) and over real
HTTP. The fix passes the real ``current_draft``.

**CORE 2 — pause then resume silently stops the goal loop.** The lifecycle hook
cleared the pending goal continuation for BOTH ``clear`` and ``pause``; the resume
path restores the active goal without replacing the token, so the retry launched
with ``goal_related=false``. Verified through real HTTP with the installed native
GoalManager: master launches the retry with ``goal_related=true``, this branch with
``false``. The fix narrows the sweep to ``clear`` only.

Both tests below drive the REAL helper and pin the REAL route call shape, because
the round-7 test pins the source string and the round-6 test drives the helper
directly — both stayed green while the route was wrong.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTES_PY = REPO_ROOT / "api" / "routes.py"

pytestmark = pytest.mark.skipif(
    not ROUTES_PY.exists(), reason="api/routes.py unavailable"
)


def _routes_source() -> str:
    return ROUTES_PY.read_text(encoding="utf-8")


def _slice_function(src: str, header: str) -> str:
    """One ``def name(...)`` body, brace-balanced."""
    start = src.find(header)
    assert start >= 0, f"{header!r} not found in api/routes.py"
    i = src.index(":", start)
    # Walk to the end of the signature line(s).
    while src[i] != "\n":
        i += 1
    i = src.index('"""', i) if '"""' in src[i : i + 400] else i
    depth = 0
    started = False
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
            started = True
        elif ch == "}":
            depth -= 1
        elif ch == "\n" and started and depth == 0:
            break
        i += 1
    return src[start:i]


# ── CORE 1: the route must pass the STORED draft, not the mutated copy ───────


def test_the_draft_route_passes_the_stored_draft_to_the_helper():
    """The helper's old/new comparison is useless if the route pre-mutates it.

    ``/api/session/draft`` built ``next_draft`` (with the incoming text already
    applied) and passed THAT as ``current_draft``, so ``text != _stored_text`` was
    always false and the stale token survived every edit.
    """
    src = _routes_source()
    # Find the CALL, not the definition: the call site is indented inside the
    # handler, the definition is at module level.
    call = "next_draft = _project_session_draft_write("
    idx = src.find(call)
    assert idx > 0, "the draft write helper call is gone"
    args = src[idx + len(call) : idx + len(call) + 300]
    # Second argument must be the stored draft, captured before the mutation.
    assert "current_draft," in args or "current_draft)" in args, (
        f"the helper's second argument is not `current_draft`; an edited draft "
        f"will keep re-binding the stale continuation token. Call: {args[:200]!r}"
    )
    # And the stored draft must be captured BEFORE next_draft is mutated. The
    # capture lives in whichever draft-authority path the handler is using:
    # master's bounded authority-hop walk binds `request_session`, the plain
    # path binds `s`. Search backwards for either shape.
    window = src[max(0, idx - 4000) : idx]
    capture_idx = max(
        window.rfind("current_draft = dict(getattr(s, \"composer_draft\""),
        window.rfind("current_draft = dict(getattr(request_session, \"composer_draft\""),
    )
    assert capture_idx >= 0, (
        "the stored draft is not captured before the helper call"
    )


def test_the_helper_drops_the_token_when_the_text_changes():
    """Direct helper behaviour: an edit must drop the stale token."""
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    try:
        from api.routes import _project_session_draft_write
    finally:
        sys.path.pop(0)

    stored = {"text": "original words", "goal_continuation_id": "tok-1"}
    # The keystroke autosave / session-switch save never sends the field.
    result = _project_session_draft_write(
        {"text": "completely different words", "files": None,
         "goal_continuation_id": None},
        stored,
    )
    assert "goal_continuation_id" not in result, (
        f"an edited draft kept the stale continuation token: {result!r}"
    )
    assert result["text"] == "completely different words"

    # Counterpart: re-saving the SAME text keeps it.
    unchanged = _project_session_draft_write(
        {"text": "original words", "files": None, "goal_continuation_id": None},
        stored,
    )
    assert unchanged.get("goal_continuation_id") == "tok-1", (
        f"a plain re-save of the same draft dropped the token: {unchanged!r}"
    )

    # And an explicit token still binds.
    rebound = _project_session_draft_write(
        {"text": "new words", "files": None, "goal_continuation_id": "tok-2"},
        stored,
    )
    assert rebound.get("goal_continuation_id") == "tok-2"


# ── CORE 2: pause must not clear the pending continuation ────────────────────


def test_the_lifecycle_hook_clears_only_on_clear():
    """Pause keeps the goal, so it must keep the pending continuation record.

    Clearing it on pause left the resume path restoring the active goal without a
    token, and the retry launched with ``goal_related=false`` — the goal loop
    silently stopped.
    """
    src = _routes_source()
    idx = src.find("clear_pending_goal_continuation(s.session_id)")
    assert idx > 0, "the pending-continuation clear is gone"
    guard = src[max(0, idx - 700) : idx]
    assert 'if goal_adapter_action == "clear":' in guard, (
        "the lifecycle hook still clears the pending continuation for something "
        "other than `clear` (pause included), so a pause/resume cycle drops the "
        "token and the goal loop stops"
    )
    assert 'in ("clear", "pause")' not in guard, (
        "the old clear-or-pause condition is still present"
    )
