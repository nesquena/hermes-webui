"""Static regression test — the context-brief banner button must open its container.

Repo convention (AGENTS.md / panels.js conventions): programmatic navigation to
a panel must open the panel's container first. On mobile the panels render
inside the sidebar drawer; on desktop they need an expanded rail. A handler
that only calls ``switchPanel('context')`` activates the panel invisibly and
the user sees nothing happen (the exact complaint that motivated this fix:
tapping "Brief contexte" on a phone did nothing visible).
"""
from __future__ import annotations

import re
from pathlib import Path

PANELS = (Path(__file__).resolve().parent.parent / "static" / "panels.js").read_text(
    encoding="utf-8"
)


def _brief_banner_handler() -> str:
    """Extract the onclick handler body of the context-brief banner button."""
    anchor = PANELS.find("context_brief_banner_btn")
    assert anchor != -1, "context brief banner button must exist in panels.js"
    segment = PANELS[anchor : anchor + 1200]
    m = re.search(r"btn\.onclick\s*=\s*\(\)\s*=>\s*\{(.*?)\n  \};", segment, re.S)
    assert m, "brief banner button must have a block onclick handler"
    return m.group(1)


def test_brief_banner_opens_mobile_drawer_before_switching_panel():
    body = _brief_banner_handler()
    assert "toggleMobileSidebar" in body, (
        "on mobile the context panel renders inside the sidebar drawer; the "
        "banner button must open the drawer or the panel switch is invisible"
    )
    assert "mobile-open" in body, (
        "the drawer must only be toggled when it is not already open"
    )


def test_brief_banner_expands_desktop_rail_before_switching_panel():
    body = _brief_banner_handler()
    assert "_isSidebarCollapsed" in body and "expandSidebar" in body, (
        "on desktop a collapsed rail hides panel content; the banner button "
        "must expand it before switching to the context panel"
    )


def test_brief_banner_still_switches_to_context_panel():
    body = _brief_banner_handler()
    assert re.search(r"switchPanel\(\s*'context'\s*\)", body), (
        "the banner button must still navigate to the context panel"
    )
