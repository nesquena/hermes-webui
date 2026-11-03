"""Focused regression for the PR #6836 re-gate review of 2026-10-08T02:11:02Z.

That review (id 5450591636, submitted against head ``129b127c``) is the one the
earlier watchdog rounds never saw: PR #6836 carries 32 reviews and the
un-paginated ``pulls/<n>/reviews`` call returns one page of 30.

CORE 1  Escape with a combobox dropdown open must close ONLY the dropdown, not
        the whole Project settings dialog: the dialog's document-capture
        ``_onKey`` runs before the trigger's own keydown handler and used to
        close the dialog with unsaved edits.

(The review's other four items — the cross-profile preview confirmation, the
closed-dialog guard, the detached sweep's profile scope and the decline path
that must not persist a hard ``false`` — were all about the auto-assign sweep
and its toggle, which moved to a follow-up PR together with their tests
(maintainer re-gate 2026-10-11T02:08:20Z).)
"""

from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_static(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "static" / name).read_text(encoding="utf-8")


def _read_sessions_js() -> str:
    return _read_static("sessions.js")




def _dialog_source() -> str:
    """Only the _showProjectBindingsDialog body (up to the next top-level fn)."""
    src = _read_sessions_js()
    start = src.index("function _showProjectBindingsDialog(proj){")
    end = src.index("function _startProjectRename(proj, chip){")
    return src[start:end]






def test_escape_with_an_open_combobox_closes_only_the_dropdown():
    """The dropdown owns Escape; the dialog (and unsaved edits) stay open.

    ``trigger.onkeydown`` handles Escape itself, but the dialog listener is
    installed on the document in the CAPTURE phase, so it runs FIRST — pressing
    Escape with the Model dropdown open closed the whole dialog. The dropdown is
    closed and the key consumed by the dialog's own handler instead.
    """
    seg = _dialog_source()
    on_key = seg.index("function _onKey(e){")
    guard = seg.index("if(e.defaultPrevented||_isAppDialogOpen()) return;", on_key)
    combo_branch = seg.index("if(e.key==='Escape'&&_closeOpenCombo()){", guard)
    dialog_close = seg.index("if(e.key==='Escape'){", guard)
    assert guard < combo_branch < dialog_close, (
        "the stack guard still runs first, then the dropdown claims Escape, "
        "then the dialog-close branch"
    )
    # The key is CONSUMED: the dialog-close path must not run as well.
    assert "e.preventDefault();e.stopPropagation();return;" in seg[combo_branch:dialog_close]

    # The helper still closes what the combo's own _close() closes and only
    # reports True when a dropdown was actually open — but it now DELEGATES to the
    # shared closer instead of hand-rolling the teardown, so the shared
    # open-combo slot and the a11y state (aria-activedescendant) are released with
    # the CSS class (Greptile P2 2026-10-10T04:21:38Z).
    helper = seg.index("function _closeOpenCombo(){")
    helper_seg = seg[helper:seg.index("closeBtn.onclick=", helper)]
    assert "overlay.querySelector('.project-bindings-combo-menu.open')" in helper_seg
    assert "if(!menu) return false;" in helper_seg
    assert "return _closeBindingsComboMenu(menu);" in helper_seg

    # ...and the teardown that closes exactly what the combo's _close() closes now
    # lives in that shared module-level helper (prefer the component, keep a DOM
    # fallback for a menu whose owner is gone).
    js = _read_sessions_js()
    closer_start = js.index("function _closeBindingsComboMenu(menu){")
    closer = js[closer_start:js.index("\n}\n", closer_start) + 3]
    assert "_openBindingsCombo" in closer
    assert "owner.close()" in closer
    assert "if(!menu) return false;" in closer
    assert "menu.classList.remove('open')" in closer
    assert "trigger.classList.remove('open')" in closer
    assert "trigger.setAttribute('aria-expanded','false')" in closer
    assert "return true;" in closer

    # ...the combo really does mark an open dropdown with those exact classes,
    # so the selector above matches a live dropdown (and nothing else).
    assert "menu.classList.add('open');" in js
    assert "trigger.classList.add('open');" in js


# ---------------------------------------------------------------------------
# [SILENT] 2 — a foreign-profile preview cannot back this profile's confirmation
# ---------------------------------------------------------------------------














