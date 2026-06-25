"""Regression guard for #4891: detail-panel headers must stay visible in read
modes that have a populated title.

#4891 fixed task-detail action buttons being invisible on mobile PWA (the
`:has(.main-view-title:empty)` pseudo-class didn't re-evaluate after dynamic
textContent on some WebViews) by explicitly toggling `header.style.display` in
the per-panel header-button setters. The first cut of that fix introduced two
SILENT regressions (caught by the gate): read-only Memory sections (Project
Context / External Notes) and the Profiles "Profiles vs workspaces" help view
both have a populated title but fell into an `else`/`empty` branch that set the
header to `display:none`, hiding their header+title.

This guards the invariant: any setter branch that corresponds to a populated
read/help view must show the header (flex), never hide it.
"""
from __future__ import annotations

import re
from pathlib import Path

PANELS = (Path(__file__).resolve().parent.parent / "static" / "panels.js").read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    i = PANELS.find(f"function {name}(")
    assert i != -1, f"{name} not found in panels.js"
    # crude brace matcher from the first '{' after the signature
    start = PANELS.index("{", i)
    depth = 0
    for j in range(start, len(PANELS)):
        if PANELS[j] == "{":
            depth += 1
        elif PANELS[j] == "}":
            depth -= 1
            if depth == 0:
                return PANELS[start : j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_memory_read_mode_shows_header_even_for_readonly_sections():
    """_setMemoryHeaderButtons('read') must show the header for ALL read views
    (incl. read-only Project Context / External Notes), only gating the Edit
    button on editability — not hide the whole header (#4891)."""
    body = _fn_body("_setMemoryHeaderButtons")
    # The 'read' branch must set the header to flex unconditionally (the title
    # is populated in every read view).
    m = re.search(r"if\s*\(\s*mode\s*===\s*'read'\s*\)\s*\{", body)
    assert m, "_setMemoryHeaderButtons must have a dedicated mode==='read' branch"
    read_branch = body[m.end(): m.end() + 400]
    assert "header.style.display = 'flex'" in read_branch, (
        "memory 'read' branch must show the header (flex) so read-only sections "
        "keep their header/title (#4891 regression)"
    )
    # The read-only gating must apply to the Edit button, not the whole header.
    assert "external_notes" in read_branch and "readOnly" in read_branch, (
        "memory 'read' branch must gate only the Edit button on read-only/"
        "external_notes, not the header visibility (#4891)"
    )


def test_profile_help_view_keeps_header_visible():
    """The 'Profiles vs workspaces' help view sets a title, so it must use a
    header mode that shows the header — not the 'empty' mode that hides it."""
    help_body = _fn_body("_renderProfileConceptHelp")
    assert "_setProfileHeaderButtons('empty')" not in help_body, (
        "profile help view must NOT call _setProfileHeaderButtons('empty') — "
        "that hides the populated help title (#4891 regression)"
    )
    assert "_setProfileHeaderButtons('help')" in help_body, (
        "profile help view must use the 'help' header mode (shows header, hides "
        "action buttons) (#4891)"
    )
    setter = _fn_body("_setProfileHeaderButtons")
    m = re.search(r"mode\s*===\s*'help'\s*\)\s*\{", setter)
    assert m, "_setProfileHeaderButtons must handle a 'help' mode"
    help_branch = setter[m.end(): m.end() + 240]
    assert "header.style.display = 'flex'" in help_branch, (
        "'help' header mode must show the header (flex) (#4891)"
    )


def test_task_detail_header_shown_in_read_and_edit():
    """The original #4891 fix: task-detail header is explicitly shown in read/
    create/edit and hidden only in the closed/empty state."""
    body = _fn_body("_setCronHeaderButtons")
    assert body.count("header.style.display = 'flex'") >= 2, (
        "_setCronHeaderButtons must explicitly show the header in read and "
        "create/edit modes (#4891)"
    )
    assert "header.style.display = 'none'" in body, (
        "_setCronHeaderButtons must hide the header in the closed/empty state (#4891)"
    )
