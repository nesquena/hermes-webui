"""Regression test for #1464 — workspace dropdown noResults visibility logic.

The contributor's first push had an inverted ternary:
    noResults.style.display = visible ? '' : 'none';

Reading: "if visible items exist, SHOW noResults" — backwards. The empty-state
should appear only when zero items match the filter.

This test pins both ternaries inside renderWorkspaceDropdownInto.filterWs() to
their correct shape, so future edits can't silently re-invert either of them.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_workspace_dropdown_noresults_hides_when_matches_exist():
    panels = (REPO / "static" / "panels.js").read_text(encoding="utf-8")
    fn_start = panels.find("function renderWorkspaceDropdownInto")
    assert fn_start != -1, "renderWorkspaceDropdownInto must exist in panels.js"

    # Locate filterWs body inside the renderWorkspaceDropdownInto function.
    filter_start = panels.find("function filterWs(", fn_start)
    assert filter_start != -1, (
        "filterWs helper must exist inside the dropdown render function"
    )
    filter_end = panels.find("\n  }\n", filter_start)
    assert filter_end != -1, "filterWs body must close cleanly"
    body = panels[filter_start:filter_end]

    # ws-opt items: visible match → show ('' = display unset), else hide.
    assert "opt.style.display=show?'':'none'" in body, (
        "ws-opt items must show on match (truthy) and hide on no-match — "
        "if this assertion fires, either the variable name changed or the "
        "ternary was inverted."
    )

    # noResults: zero matches → show, ≥1 match → hide. Mirror image of opt.
    assert "noResults.style.display=visible?'none':''" in body, (
        "noResults must HIDE when matches exist (visible>0) and SHOW when zero "
        "matches. The opposite ordering ('':'none') was the contributor's "
        "first-push bug — it caused 'No workspaces found' to render alongside "
        "valid filtered results. See PR #1464."
    )

    # Defense-in-depth: the two ternaries must be MIRROR IMAGES of each other.
    # If both ever read 'show?''':'none'' or both 'show?'none':'', the
    # filter+empty-state will be in the same direction and the bug returns.
    opt_idx = body.find("opt.style.display=")
    nr_idx = body.find("noResults.style.display=")
    assert opt_idx < nr_idx, "opt visibility line must come before noResults line"

    opt_line = body[opt_idx : body.find("\n", opt_idx)]
    nr_line = body[nr_idx : body.find("\n", nr_idx)]
    # Each line picks one branch for show/hide; the chosen branch must be
    # opposite. The simplest invariant: the noResults line must NOT be string-
    # equal to the opt line with `opt` swapped for `noResults` and `show` for
    # `visible`.
    parallel = opt_line.replace("opt.style.display", "noResults.style.display").replace(
        "show?", "visible?"
    )
    assert nr_line != parallel, (
        f"opt and noResults visibility ternaries are accidentally parallel — "
        f"they must be mirror images. opt={opt_line!r} noResults={nr_line!r}"
    )
