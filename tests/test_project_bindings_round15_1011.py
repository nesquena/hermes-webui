"""Round-15 re-gate (maintainer review 5481568957 @ 2026-10-11T02:08:20Z).

The review asked for a scope split — "Please move auto-assign into a follow-up
PR ... You offered to split on our word, and this is it" — plus the delete fix
that ``test_project_bindings_round14_1011.py`` pins behaviourally.

These guards keep the split honest, because the code the split removes is code
no existing test can fail on any more:

* every auto-assign entry point (the sweep and its registry, the workspace-keyed
  filing of new sessions, the counted preview endpoint, the dialog toggle and
  the confirm it opened) is gone from the shipped backend and frontend;
* the stored ``auto_assign`` field is accepted, shape-checked and stored, but
  NOTHING reads it (the same dormant-field rule ``reasoning_effort`` follows);
* the delete handler still re-reads the projects catalog INSIDE its critical
  section — the stale-list regression whose only test was the drain-window test
  that went out with the sweep.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Every symbol the auto-assign feature owned. The sweep's registry, its
# admission/join machinery, the workspace-keyed lookup for a new session, the
# claim helper and the counted preview all moved to the follow-up PR.
_REMOVED_SYMBOLS = (
    "_apply_project_auto_assign",
    "_auto_assign_sweep_begin",
    "_auto_assign_start_sweep",
    "_auto_assign_sweep_end",
    "_auto_assign_sweep_cancelled",
    "_auto_assign_cancel_sweeps",
    "_auto_assign_abort_deleting",
    "_auto_assign_finish_deleting",
    "_auto_assign_sweep_body",
    "_auto_assign_candidate_count",
    "_auto_assign_project_for_workspace",
    "_auto_assign_claim_session",
    "_auto_assign_live_binding",
    "_auto_assign_live_binding_locked",
    "_auto_assign_target_is_view_only",
    "_AUTO_ASSIGN_SWEEPS",
    "_AUTO_ASSIGN_DELETING",
    "auto-assign-preview",
)


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def test_no_auto_assign_entry_point_remains_in_the_backend():
    src = _read("api/routes.py")
    leftover = [name for name in _REMOVED_SYMBOLS if name in src]
    assert not leftover, (
        "the auto-assign sweep and its entry points were split into a follow-up "
        "PR; these are still shipped here: %s" % leftover
    )


def test_the_toggle_its_confirm_and_their_ui_are_gone():
    sessions_js = _read("static/sessions.js")
    style_css = _read("static/style.css")
    i18n = _read("static/i18n.js")
    for fragment, where in (
        ("pb_auto_assign", "static/i18n.js"),
        ("pb_auto_assign", "static/sessions.js"),
        ("aaCb", "static/sessions.js"),
        ("project-bindings-auto-assign", "static/style.css"),
        ("fields.auto_assign", "static/sessions.js"),
    ):
        src = {"static/i18n.js": i18n, "static/sessions.js": sessions_js,
               "static/style.css": style_css}[where]
        assert fragment not in src, f"{fragment!r} still in {where}"
    # The toggle's own markup block is gone with it, not merely unreferenced.
    assert "aa-hint" not in style_css
    assert "aa-label" not in style_css


def test_the_stored_auto_assign_field_is_accepted_but_dormant():
    """Like ``reasoning_effort``: the round trip survives, nothing acts on it."""
    src = _read("api/routes.py")
    # Still accepted + shape-checked + stored by /api/projects/bind.
    assert '"auto_assign" in body' in src
    assert '"auto_assign must be a boolean"' in src
    assert 'proj["auto_assign"] = True' in src
    # ...and nothing READS a stored value anywhere (no sweep, no lookup, no
    # toggle): the only `.get("auto_assign")` reads are the REQUEST body's.
    hits = [m.start() for m in re.finditer(r'\.get\(\s*"auto_assign"\s*\)', src)]
    assert hits, "the bind handler no longer reads the field at all"
    for i in hits:
        assert src[max(0, i - 4):i] == "body", (
            "a stored auto_assign value is read at %r" % src[i - 40:i + 30]
        )


def test_delete_reloads_the_catalog_inside_its_critical_section():
    """The stale-list regression the drain test used to cover.

    The handler reads the catalog once for its ownership check, then must
    re-read it INSIDE the catalog critical section before saving the filtered
    list — reusing the earlier snapshot erased a project created meanwhile.
    """
    src = _read("api/routes.py")
    start = src.index('if parsed.path == "/api/projects/delete":')
    end = src.index("\n    if parsed.path ==", start)
    body = src[start:end]
    lock_i = body.index("with _PROJECTS_CATALOG_LOCK:")
    reload_i = body.index("load_projects()", lock_i)
    assert lock_i < reload_i, (
        "the delete saves a catalog list read outside its critical section"
    )
    # The filtered list is built from THAT fresh read, not from the earlier one.
    assert re.search(r"for p in load_projects\(\)", body[lock_i:]), body[lock_i:lock_i + 400]
