"""#3571 saved-prompts library — structural + mobile-visibility guards.

The saved-prompts composer affordance is a desktop-only feature: per Nathan
(2026-06-09) it must be hidden on mobile (too much for the narrow composer).
These tests pin the mobile-hide rule and the core wiring so a future refactor
can't silently regress either.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_saved_prompts_button_hidden_on_mobile():
    """#btnSavedPrompts (and its popup) must be display:none inside a mobile
    max-width:640px @media block — desktop-only affordance. Verified by walking
    each media block and confirming the hide rule lives inside a 640px-max one."""
    css = read("static/style.css")
    found = False
    for m in re.finditer(r"@media([^{]*)\{", css):
        cond = m.group(1)
        if "max-width" not in cond or "640px" not in cond:
            continue
        # Slice to this block's matching close brace.
        depth = 0
        i = m.end() - 1
        end = len(css)
        while i < len(css):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        block = css[m.end():end]
        if re.search(r"#btnSavedPrompts[^{]*\{[^}]*display:\s*none", block):
            found = True
            break
    assert found, (
        "#btnSavedPrompts must be hidden on mobile inside a max-width:640px block "
        "(desktop-only saved-prompts feature)"
    )


def test_saved_prompts_backend_caps_present():
    """The POST /api/prompts route must cap text length and total count so
    saved_prompts.json can't grow unbounded."""
    routes = read("api/routes.py")
    assert "text too long" in routes, "POST /api/prompts must cap text length"
    assert re.search(r"len\(prompts\)\s*>=\s*\d+", routes), (
        "POST /api/prompts must cap the total number of saved prompts"
    )


def test_saved_prompts_core_wiring_present():
    """The composer must expose the saved-prompts toggle + popup and the
    load/save/delete API calls."""
    js = read("static/messages.js")
    assert "toggleSavedPromptsPopup" in js
    assert "insertSavedPromptIntoComposer" in js
    assert re.search(r"api\('/api/prompts',\s*\{method:'POST'", js), "save wiring (POST) missing"
    assert re.search(r"api\('/api/prompts',\s*\{method:'DELETE'", js), "delete wiring (DELETE) missing"
    html = read("static/index.html")
    assert 'id="btnSavedPrompts"' in html
    assert 'id="savedPromptsPopup"' in html
