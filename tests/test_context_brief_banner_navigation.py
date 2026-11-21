"""Static regression test — the context-brief banner button must open its container.

Repo convention (AGENTS.md / panels.js conventions): programmatic navigation to
a panel must open the panel's container first. On mobile the panels render
inside the sidebar drawer; on desktop they need an expanded rail. A handler
that only calls ``switchPanel('context')`` activates the panel invisibly and
the user sees nothing happen (the exact complaint that motivated this fix:
tapping "Brief contexte" on a phone did nothing visible).
"""
from __future__ import annotations

from html.parser import HTMLParser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANELS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class _PanelNestingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, str | None]] = []
        self.ancestors_by_id: dict[str, tuple[str, ...]] = {}

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs_dict = dict(attrs)
        element_id = attrs_dict.get("id")
        if element_id:
            self.ancestors_by_id[element_id] = tuple(
                parent_id for _, parent_id in self.stack if parent_id
            )
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, element_id))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


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


def test_context_panel_closes_before_the_adjacent_insights_panel():
    parser = _PanelNestingParser()
    parser.feed(INDEX_HTML)

    assert "panelContext" not in parser.ancestors_by_id["panelInsights"], (
        "panelInsights must be a sibling of panelContext, not nested inside it"
    )
