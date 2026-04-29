"""
Regression tests for issue #3: full-width chat output container.

History
-------
Original feature PR (commit 4cf3c4114ec4 "feat: full-width chat layout +
fix gateway provider test") removed the desktop max-width constraints on
``.messages-inner`` and ``.msg-body`` so the chat area fills the available
panel width. The follow-up upstream merge (commit 887df46444ff) only kept
the test changes and dropped the CSS edits, so the constraints crept back
in. This test guards against another silent revert.

Mobile / small-screen rules under ``@media`` overrides remain free to
re-add max-width — only the *base* (desktop) declarations of these two
selectors are checked here.
"""

import pathlib
import re
import urllib.request

import pytest

from tests.conftest import TEST_BASE


_REPO = pathlib.Path(__file__).resolve().parent.parent
CSS_PATH = _REPO / "static" / "style.css"
CSS = CSS_PATH.read_text()


def _strip_media_blocks(css: str) -> str:
    """Remove the contents of every @media block so we only inspect the
    selectors that apply at desktop width without overrides."""
    out = []
    i = 0
    while i < len(css):
        if css.startswith("@media", i):
            # Find matching brace
            brace = css.find("{", i)
            if brace < 0:
                break
            depth = 1
            j = brace + 1
            while j < len(css) and depth:
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                j += 1
            i = j  # skip the entire media block
            continue
        out.append(css[i])
        i += 1
    return "".join(out)


BASE_CSS = _strip_media_blocks(CSS)


def _selector_bodies(css: str, selector: str) -> list[str]:
    pattern = re.compile(r"(?:^|})\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", re.S)
    return [match.group(1).strip() for match in pattern.finditer(css)]


@pytest.mark.parametrize(
    "selector",
    [".messages-inner", ".msg-body", ".tool-card", ".thinking-card"],
)
def test_chat_container_has_no_base_max_width(selector):
    """The base (non-mobile) declaration must not pin a max-width."""
    matches = _selector_bodies(BASE_CSS, selector)
    assert matches, f"Selector {selector} not found in style.css base rules"
    for body in matches:
        assert "max-width" not in body, (
            f"Base rule for {selector} unexpectedly contains 'max-width:': "
            f"{body!r}. Full-width chat layout requires the desktop "
            "container to fill its panel — narrow it back only inside an "
            "@media (max-width: …) override if you must."
        )


def test_messages_inner_uses_full_width_centering():
    """The base rule for .messages-inner must keep the centred-flex layout
    even after we remove the max-width — width:100% prevents the column
    from collapsing and margin:auto keeps it horizontally aligned."""
    matches = _selector_bodies(BASE_CSS, ".messages-inner")
    assert matches, ".messages-inner must exist in base CSS"
    body = matches[0]
    assert "width:100%" in body, ".messages-inner must keep width:100%"
    assert "margin:0 auto" in body, ".messages-inner must keep margin:0 auto"


def test_no_desktop_breakpoint_reintroduces_msg_max_width():
    """Catch the specific historical revert: desktop @media breakpoints
    that re-pinned .messages-inner to 1100/1200px after the original fix."""
    for bad in ("max-width:1100px", "max-width:1200px"):
        # These two specific upstream values were the ones that came back
        # after the merge. Other values (e.g. mobile overrides) are fine.
        assert f".messages-inner{{{bad}" not in CSS.replace(" ", ""), (
            f"Desktop @media re-pinning .messages-inner to {bad} re-appeared. "
            "This was the silent revert from PR #3 — the chat panel must "
            "stay full-width on wide displays."
        )


def test_served_css_keeps_chat_output_full_width():
    """System-level guard: the shipped CSS served by the app must not re-pin
    the main assistant column or its cards to a fixed desktop width."""
    with urllib.request.urlopen(TEST_BASE + "/static/style.css", timeout=10) as resp:
        served_css = resp.read().decode("utf-8")
    served_base_css = _strip_media_blocks(served_css)
    for selector in (".messages-inner", ".msg-body", ".tool-card", ".thinking-card"):
        matches = _selector_bodies(served_base_css, selector)
        assert matches, f"Served CSS missing selector {selector}"
        for body in matches:
            assert "max-width" not in body, (
                f"Served CSS for {selector} still contains max-width: {body!r}"
            )
