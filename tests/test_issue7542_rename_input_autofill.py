"""Regression tests for #7542: rename-conversation input must not
trigger the browser's password-save dialog.

Chrome and password-manager extensions (1Password, LastPass, Bitwarden,
Dashlane) mis-classify free-text naming inputs as login forms because
they accept arbitrary user input next to the chat UI. The fix tags
every such input with the standard ``autocomplete="off"`` plus the
extension-specific ignore attributes the WebUI's other
credential-shaped fields already use.

The attribute block lives in one shared helper,
``_markNonCredentialInput(inp)`` in ``static/ui.js``, and is called
from all five ``createElement('input')`` sites that back a
naming/renaming action:

  1. titlebar rename (``static/panels.js``, class ``app-titlebar-rename-input``)
  2. sidebar session rename (``static/sessions.js``, class ``session-title-input``)
  3. project create (``static/sessions.js``, class ``project-create-input``)
  4. project rename (``static/sessions.js``, class ``project-create-input``)
  5. workspace file rename (``static/ui.js``, class ``file-rename-input``)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
PANELS_JS = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO / "static" / "sessions.js").read_text(encoding="utf-8")
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")

# (label, source, class name, file name, end-marker, occurrence) — one entry
# per naming/renaming createElement('input') site the helper must cover.
# end-marker is a unique-in-function string that appears right after the
# input setup block, so the extracted block covers the site's own code
# only (not unrelated inputs further down the file).
SITES = [
    (
        "titlebar rename",
        PANELS_JS,
        "app-titlebar-rename-input",
        "panels.js",
        "// Prevent click/dblclick",
        0,
    ),
    (
        "sidebar rename",
        SESSIONS_JS,
        "session-title-input",
        "sessions.js",
        "['click','mousedown','dblclick','pointerdown']",
        0,
    ),
    (
        "project create",
        SESSIONS_JS,
        "project-create-input",
        "sessions.js",
        "let _finishDone=false;",
        0,
    ),
    (
        "project rename",
        SESSIONS_JS,
        "project-create-input",
        "sessions.js",
        "let _finishDone=false;",
        1,
    ),
    (
        "workspace file rename",
        UI_JS,
        "file-rename-input",
        "ui.js",
        "inp.onclick=(e2)=>e2.stopPropagation();",
        0,
    ),
]


def _site_block(source: str, class_name: str, end_marker: str, occurrence: int = 0) -> str:
    """Return the input-setup block for the nth ``createElement('input')``
    site that assigns ``className = class_name``, bounded by the next
    occurrence of ``end_marker`` in the same function."""
    starts = [
        m.start() for m in re.finditer(r"createElement\(['\"]input['\"]\)", source)
    ]
    assert starts, "no createElement('input') found"
    hits = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(source)
        block = source[start:end]
        if f"className = '{class_name}'" in block or f"className='{class_name}'" in block:
            hits.append(start)
    assert hits, f"no createElement('input') with className {class_name!r}"
    assert len(hits) >= occurrence + 1, (
        f"expected at least {occurrence + 1} sites with className "
        f"{class_name!r}, found {len(hits)}"
    )
    start = hits[occurrence]
    end = source.find(end_marker, start)
    assert end != -1, f"end marker {end_marker!r} not found after the input setup"
    return source[start:end]


# ── Helper contract ─────────────────────────────────────────────────


def _helper_body() -> str:
    m = re.search(r"function _markNonCredentialInput\(inp\)\s*\{", UI_JS)
    assert m, "helper _markNonCredentialInput(inp) not found in ui.js"
    start = m.start()
    # Match braces from the opening brace of the function body.
    i = UI_JS.index("{", start)
    depth = 0
    for j in range(i, len(UI_JS)):
        c = UI_JS[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return UI_JS[start : j + 1]
    raise AssertionError("unterminated helper body")


def test_helper_exists_and_is_guarded():
    """The shared helper must exist in ui.js and tolerate a null/undefined
    argument (it is called from paths where the element is always fresh,
    but defensive is cheap)."""
    body = _helper_body()
    assert "inp.autocomplete" in body
    assert "if(!inp)" in body.replace(" ", "")


@pytest.mark.parametrize(
    "attr",
    [
        "autocorrect",
        "autocapitalize",
        "spellcheck",
        "data-1p-ignore",
        "data-lpignore",
        "data-bwignore",
        "data-form-type",
    ],
)
def test_helper_sets_every_required_attribute(attr):
    """The helper alone must apply the full attribute set (autocomplete=off,
    mobile-keyboard guards, spellcheck, and every password-manager ignore
    attribute) — the five call sites must not re-implement it."""
    body = _helper_body()
    if attr in ("data-1p-ignore", "data-lpignore", "data-bwignore", "data-form-type"):
        assert re.search(rf"setAttribute\(['\"]{re.escape(attr)}['\"]", body), (
            f"helper must set {attr!r}"
        )
    else:
        assert re.search(rf"setAttribute\(['\"]{attr}['\"]", body) or re.search(
            rf"\.{attr}\s*=", body
        ), f"helper must set {attr!r}"
    assert "autocomplete" in body and "'off'" in body.replace('"', "'"), (
        "helper must set autocomplete='off'"
    )
    assert re.search(r"setAttribute\(['\"]spellcheck['\"]\s*,\s*['\"]false['\"]\)", body)


def test_helper_sets_autocomplete_off_exactly_once():
    body = _helper_body()
    assert len(re.findall(r"autocomplete\s*=\s*['\"]off['\"]", body)) == 1


# ── Per-site coverage ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "label,source,class_name,file_name,end_marker,occurrence",
    SITES,
    ids=[s[0] for s in SITES],
)
def test_site_calls_shared_helper(label, source, class_name, file_name, end_marker, occurrence):
    """Every naming/renaming input site must route through the shared
    helper _markNonCredentialInput(inp) rather than an inline attribute
    block — that is the maintainer's fix for #7542, because the original
    patch covered only the titlebar."""
    block = _site_block(source, class_name, end_marker, occurrence=occurrence)
    assert "_markNonCredentialInput(inp)" in block, (
        f"{label} ({file_name}, {class_name}) must call "
        "_markNonCredentialInput(inp) — otherwise Chrome/password managers "
        "surface the credential-save dialog (#7542)"
    )


@pytest.mark.parametrize(
    "label,source,class_name,file_name,end_marker,occurrence",
    SITES,
    ids=[s[0] for s in SITES],
)
def test_site_has_no_inline_attribute_block(label, source, class_name, file_name, end_marker, occurrence):
    """Guard against regression: no site may set the ignore attributes
    inline again; delegating to the helper keeps the attribute set in
    exactly one place."""
    block = _site_block(source, class_name, end_marker, occurrence=occurrence)
    inline = re.findall(
        r"setAttribute\(['\"](?:autocorrect|autocapitalize|spellcheck|"
        r"data-1p-ignore|data-lpignore|data-bwignore|data-form-type)['\"]",
        block,
    )
    assert not inline, (
        f"{label} ({file_name}) sets attributes inline {inline}; "
        "use _markNonCredentialInput(inp) instead"
    )


def test_all_five_sites_exist():
    """The test suite itself must still cover exactly five sites; a
    missing one means the parametrization drifted from the code."""
    assert len(SITES) == 5


def test_sites_reference_helper_defined_in_loaded_script():
    """panels.js / sessions.js / ui.js are all loaded as defer scripts in
    index.html, so the function declared in ui.js is visible at the other
    call sites. Guard against someone reordering the script tags so the
    helper loads after its callers."""
    html = (REPO / "static" / "index.html").read_text(encoding="utf-8")
    order = [
        html.find('src="static/ui.js'),
        html.find('src="static/sessions.js'),
        html.find('src="static/panels.js'),
    ]
    assert all(i != -1 for i in order), "ui.js/sessions.js/panels.js script tags missing"
    assert order[0] < min(order[1:]), (
        "ui.js (which defines _markNonCredentialInput) must load before "
        "the files that call it"
    )


# ── Negative contract: the fix does not regress the save paths ───────


@pytest.mark.parametrize(
    "endpoint,source",
    [
        ("/api/session/rename", PANELS_JS),
        ("/api/projects/create", SESSIONS_JS),
        ("/api/projects/rename", SESSIONS_JS),
        ("/api/file/rename", UI_JS),
    ],
)
def test_save_paths_still_call_their_endpoints(endpoint, source):
    """The fix is purely additive (input attributes); it must not touch
    the save-path code. Regressing that would mean the rename silently
    no-ops."""
    assert f"'{endpoint}'" in source, (
        f"The save path must still call {endpoint}."
    )


def test_rename_input_setup_appears_exactly_once_per_site():
    """Regressing to a duplicated input setup would create a second
    input node. Ensure each site calls the helper at most once and that
    no site sets autocomplete='off' inline (that would mean the
    attribute set drifted back out of the shared helper)."""
    for label, source, class_name, file_name, end_marker, occurrence in SITES:
        block = _site_block(source, class_name, end_marker, occurrence=occurrence)
        assert block.count("_markNonCredentialInput(inp)") == 1, (
            f"{label} must call _markNonCredentialInput(inp) exactly once"
        )
        assert not re.search(r"autocomplete\s*=\s*['\"]off['\"]", block), (
            f"{label} sets autocomplete='off' inline; delegate to the helper "
            "so the attribute set lives in one place"
        )


def test_helper_is_defined_exactly_once():
    """Exactly one definition of the shared helper across static/."""
    defined = [
        rel
        for rel, src in (
            ("ui.js", UI_JS),
            ("sessions.js", SESSIONS_JS),
            ("panels.js", PANELS_JS),
        )
        if re.search(r"function _markNonCredentialInput\(inp\)", src)
    ]
    assert defined == ["ui.js"], (
        f"_markNonCredentialInput must be defined once, in ui.js (got {defined})"
    )
