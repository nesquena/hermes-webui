"""#7616: the clarify card's free-text field must accept long text.

The clarify (agent-question) card is the "single-select prompt" the maintainer
scoped in issue #7616. It already renders an "Other" free-text row next to the
single-choice buttons, but the field behind it was a single-line
``<input type="text">`` carrying ``readonly`` + ``onfocus=removeAttribute``:

  * a single-line input cannot hold long/multiline answers at all;
  * ``readonly`` (until the focus handler strips it) is the classic
    password-manager opt-out hack, and it is precisely what makes full text
    selection impossible — Firefox/Safari refuse click-drag selection and
    Ctrl/Cmd+A inside a ``readonly`` control, so replacing existing text with
    a long answer is unusable.

The fix swaps the field for an auto-growing ``<textarea rows="1">`` (long text,
newline via Shift+Enter, scrolls past its max-height) and replaces the
``readonly`` hack with inert autofill opt-out attributes that do not affect
text selection.

These tests are static-contract tests in the shape the repo already uses for
this surface (see tests/test_issue2883_clarify_padding.py and
tests/test_sprint30.py::TestClarifyCardHTML).
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
MESSAGES_JS = (ROOT / "static" / "messages.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _compact(text: str) -> str:
    return "".join(text.split())


def _clarify_field_line(html: str) -> str:
    """Return the single markup line that declares #clarifyInput."""
    for line in html.splitlines():
        if 'id="clarifyInput"' in line:
            return line.strip()
    raise AssertionError("#clarifyInput markup not found")


def test_clarify_free_text_field_is_a_multiline_textarea():
    """#7616: the free-text field must be a textarea, not a single-line input."""
    for name, html in (("index.html", INDEX_HTML), ("_ensureClarifyCardDom", MESSAGES_JS)):
        line = _clarify_field_line(html)
        assert "<textarea" in line, (
            f"{name}: #clarifyInput must be a <textarea> so long/multiline "
            f"answers fit — got: {line}"
        )
        assert "<input" not in line, (
            f"{name}: #clarifyInput is still a single-line <input> — got: {line}"
        )
        assert 'rows="1"' in line, (
            f"{name}: #clarifyInput textarea should start at rows=1 and grow "
            f"with content — got: {line}"
        )


def test_clarify_free_text_field_drops_the_readonly_selection_blocker():
    """#7616: `readonly`-until-focus is what made text selection impossible."""
    line = _clarify_field_line(INDEX_HTML)
    assert "readonly" not in line, (
        "index.html: #clarifyInput must not ship `readonly` — it blocks "
        f"click-drag selection and Ctrl/Cmd+A. Got: {line}"
    )
    assert "removeAttribute" not in line, (
        "index.html: #clarifyInput must not use the onfocus-removeAttribute "
        f"password-manager hack. Got: {line}"
    )
    assert "removeAttribute('readonly')" not in MESSAGES_JS, (
        "messages.js still strips `readonly` from #clarifyInput — the hack is "
        "what #7616 removes"
    )


def test_clarify_free_text_field_uses_inert_autofill_opt_out():
    """#7616: autofill opt-out must not come at the cost of text selection.

    The issue asks for `autocomplete="off"` "also needed for the dropdown input
    fields"; the manager-specific opt-outs are the non-destructive half of it.
    """
    line = _clarify_field_line(INDEX_HTML)
    assert 'autocomplete="off"' in line, (
        f"index.html: #clarifyInput must keep autocomplete=off — got: {line}"
    )
    assert 'data-1p-ignore' in line, (
        f"index.html: #clarifyInput should opt out of 1Password — got: {line}"
    )
    assert 'data-lpignore="true"' in line, (
        f"index.html: #clarifyInput should opt out of LastPass — got: {line}"
    )


def test_clarify_free_text_field_supports_shift_enter_newline():
    """#7616 expected behavior #2/#4: long text needs real newlines.

    Enter-to-send is existing behavior and must not change; Shift+Enter is the
    additive newline path a multiline field requires.
    """
    compact = _compact(MESSAGES_JS)
    assert "input.onkeydown=(e)=>{" in compact, (
        "showClarifyCard must still bind Enter-to-send on #clarifyInput"
    )
    assert "e.key==='Enter'&&!e.shiftKey" in compact, (
        "#7616: Enter sends only without Shift so Shift+Enter inserts a newline"
    )
    assert "e.key==='Enter'&&e.shiftKey" in compact, (
        "#7616: Shift+Enter must be explicitly left to the textarea's default "
        "newline behavior"
    )


def _css_rule(css: str, selector: str) -> str:
    """Return the declaration body of the first `selector{...}` rule."""
    start = css.find(selector + "{")
    assert start >= 0, f"CSS rule for {selector} not found"
    start += len(selector) + 1
    end = css.find("}", start)
    assert end >= 0, f"unterminated CSS rule for {selector}"
    return _compact(css[start:end])


def test_clarify_free_text_field_grows_and_scrolls():
    """#7616 expected behavior #4: long text scrolls within the input area."""
    rule = _css_rule(STYLE_CSS, ".clarify-input")
    assert "resize:none" in rule, (
        "#7616: the auto-growing textarea must not offer a manual resize handle"
    )
    assert "overflow-y:auto" in rule, (
        "#7616: .clarify-input must scroll once the text exceeds its max-height"
    )
    assert "max-height:160px" in rule, (
        "#7616: .clarify-input needs a max-height to bound growth"
    )
    assert "min-height:42px" in rule, (
        "#7616: .clarify-input must keep a one-line floor so it does not collapse"
    )


def test_clarify_free_text_field_autogrows_while_typing():
    """#7616: the textarea must actually grow as the answer gets longer."""
    compact = _compact(MESSAGES_JS)
    assert "function_autoResizeClarifyInput(" in compact, (
        "messages.js needs an auto-resize helper for #clarifyInput"
    )
    assert 'addEventListener("input",_autoResizeClarifyInput)' in compact, (
        "#clarifyInput must auto-resize as the user types"
    )
    assert "_autoResizeClarifyInput();" in compact, (
        "the clarify field must be re-measured whenever the card is shown"
    )


def test_clarify_input_reset_also_clears_the_grown_height():
    """A stale grown height on the next prompt is a visible regression."""
    compact = _compact(MESSAGES_JS)
    hide_body = compact.split("functionhideClarifyCard(", 1)[1].split("function_clarifySetControlsDisabled(", 1)[0]
    assert "_autoResizeClarifyInput();" in hide_body, (
        "hideClarifyCard must collapse the grown #clarifyInput height so the "
        "next prompt does not inherit it"
    )