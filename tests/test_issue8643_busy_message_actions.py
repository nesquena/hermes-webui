"""#8643, behaviour 1 — Regenerate / Undo / Edit during a running turn.

While `S.busy`, the message action bar still painted Regenerate and Undo on the
last assistant row and Edit on the last user row, but every one of those
actions returns immediately on `S.busy` (`editMessage`, `regenerateResponse`,
`submitEdit`), so the tap did nothing and said nothing. On a phone the footer
is at full opacity without a hover (`.msg-foot .msg-actions{opacity:1}`), so a
button that cannot work was one tap away at all times.

The maintainer's ask: hide (or disable with a tooltip) Regenerate, Undo and
Edit while `S.busy`, and offer Regenerate only on the turn's final assistant
message.

Two layers of coverage, in the repo's usual split:

* ``tests/browser_8643_busy_message_actions.py`` drives the real page: a
  persisted turn with an interim answer and a final answer, loaded through the
  normal session path, then re-rendered with ``S.busy`` on and off.
* the source contract below pins the gate itself, so the busy flag and the
  turn-final reuse cannot be dropped without a browser being involved.

If playwright or a browser is unavailable the driver exits 2 and this module
reports a skip rather than a pass — the gate must never go green having
checked nothing (the #7676 lesson).

Undo is only covered at the source: its ``undoBtn`` const in
``renderMessages()`` is never inserted into ``footHtml``, so no Undo button
reaches the DOM in any state (the busy gate on it is still pinned below, so it
is correct the day someone wires it up).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
DRIVER = Path(__file__).resolve().parent / "browser_8643_busy_message_actions.py"


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin:source.index(end, begin)]


# The action-bar block of renderMessages(), from the display-only flag to the
# copy button that follows the three gated ones.
ACTION_BAR = _between(UI_JS, "const isDisplayOnlyRow=!!(m&&m._displayOnlyRow);", "const copyBtn  =")


def _run_driver() -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")
    env.setdefault("TMPDIR", "/tmp")
    return subprocess.run(
        [sys.executable, str(DRIVER)],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def test_regenerate_undo_and_edit_are_gated_on_busy():
    """All three read the same busy flag the Worklog fold reads."""
    assert "const isBusy=!!S.busy;" in ACTION_BAR, (
        "renderMessages() must derive one busy flag for the action bar"
    )
    assert "const undoBtn  = (isLastAssistant&&!isBusy) ?" in ACTION_BAR
    assert "const editBtn  = isEditableUser ? ((isBusy||isDisplayOnlyRow) ? '' :" in ACTION_BAR
    # Every one of the three, and nothing else, is behind it.
    for name in ("undoBtn", "editBtn", "retryBtn"):
        line = next(
            raw for raw in ACTION_BAR.splitlines() if raw.strip().startswith(f"const {name}")
        )
        assert "isBusy" in line, f"{name} is not gated on the running turn: {line.strip()}"


def test_regenerate_reuses_the_existing_turn_final_predicate():
    """Regenerate is offered on the turn's final assistant message only.

    ``isTurnFinalAssistant`` is the line the Worklog fold
    (``messageBelongsInWorklog``) and the compression-recovery payload already
    draw between an interim answer and the turn's final one; the button must
    not re-derive "final" of its own.
    """
    assert (
        "const isTurnFinalAssistant=!isUser&&(!nextRendered||!nextRendered.m||nextRendered.m.role!=='assistant');"
        in UI_JS
    ), "the shared turn-final predicate moved; re-point this gate at the new one"
    assert "const retryBtn = (isLastAssistant&&isTurnFinalAssistant&&!isBusy) ?" in ACTION_BAR
    assert "isTurnFinalAssistant" not in next(
        raw for raw in ACTION_BAR.splitlines() if raw.strip().startswith("const undoBtn")
    ), "Undo must stay on the row it had, not move with the turn-final rule"


def test_busy_turn_paints_no_dead_action_buttons():
    """The real page, with a real running turn's rows on screen."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed")
    result = _run_driver()
    if result.returncode == 2:
        pytest.skip(f"browser gate could not run: {result.stderr.strip()[-500:]}")
    assert result.returncode == 0, (
        f"driver failed ({result.returncode})\n"
        f"---- stdout ----\n{result.stdout}\n---- stderr ----\n{result.stderr}"
    )
    assert "RUNNING-TURN ACTION BUTTONS GATE PASSED" in result.stdout
    for line in (
        "idle — Regenerate only on the turn's final assistant row",
        "busy — Regenerate and Edit are gone while the turn runs",
        "idle again — both buttons return once the turn is over",
    ):
        assert f"OK  {line}" in result.stdout, line
