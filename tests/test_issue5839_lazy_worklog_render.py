"""Regression coverage for #5839: lazy settled-worklog DOM (compact mode).

Reasoning-heavy turns can carry 80+ activity rows per scene. The settled
compact-worklog render used to build EVERY row's DOM even when the group is
collapsed, so a long history balloons the DOM and a later synchronous layout
(e.g. opening a dropdown) tips the tab into a multi-GB freeze (#5839).

The fix defers row construction for a COLLAPSED settled worklog until first
expand, recovering the rows from the owning message after an HTML-cache restore
(where the JS-property stash is dropped). These are source-assertion tests in
the same style as the other anchor-scene UI tests (no JS engine needed).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body not found")


def test_settled_collapsed_worklog_defers_row_dom():
    body = _function_body(UI_JS, "_renderSettledAnchorSceneForMessage")
    # A collapsed settled worklog must stash rows + mark deferred + NOT build rows.
    assert "group.classList.contains('tool-call-group-collapsed')" in body
    assert "group._deferredWorklogRows=rows" in body
    assert "data-worklog-rows-deferred" in body
    # The expanded/keep-open path still renders eagerly.
    assert "_renderAnchorSceneRowsIntoWorklog(group,rows,{settled:true})" in body


def test_toggle_materializes_deferred_rows_on_expand():
    body = _function_body(UI_JS, "_toggleActivityGroup")
    assert "if(!collapsed) _materializeDeferredWorklogRows(group);" in body


def test_materialize_helper_recovers_rows_after_cache_restore():
    body = _function_body(UI_JS, "_materializeDeferredWorklogRows")
    # Idempotent: gated on the deferred marker, cleared before building.
    assert "data-worklog-rows-deferred" in body
    # Falls back to message-recovery when the JS stash was dropped (innerHTML).
    assert "_deferredWorklogRowsFromGroup(group)" in body
    assert "_renderAnchorSceneRowsIntoWorklog(group,rows,{settled:true})" in body


def test_row_recovery_maps_group_to_message_scene():
    body = _function_body(UI_JS, "_deferredWorklogRowsFromGroup")
    # Recovers rawIdx from the disclosure key and rebuilds from S.messages.
    assert "anchor-scene:" in body
    assert "S.messages" in body
    assert "_anchor_activity_scene" in body
    assert "_anchorSceneRowsForRendering(scene,{settled:true})" in body


def test_cache_restore_rehydrates_deferred_worklogs():
    # The HTML-cache fast-path restore must re-stash deferred rows so the first
    # post-restore expand works (JS properties don't survive innerHTML).
    assert "_rehydrateDeferredWorklogsFromCache(inner);" in UI_JS
    body = _function_body(UI_JS, "_rehydrateDeferredWorklogsFromCache")
    assert '[data-worklog-rows-deferred="1"]' in body
    assert "_deferredWorklogRowsFromGroup(group)" in body
    # If a group can't be recovered, drop the deferred marker (no dead chip).
    assert "removeAttribute('data-worklog-rows-deferred')" in body


def test_blank_turn_reveal_materializes_deferred_rows():
    # The "blank turn" safety reveal force-expands a collapsed worklog; it must
    # also materialize deferred rows or the revealed worklog would be empty.
    assert UI_JS.count("_materializeDeferredWorklogRows(group)") >= 3


def test_materialize_postprocesses_and_restores_disclosure():
    """#5860 gate fix: lazily-materialized rows must get the same post-processing
    (syntax highlight / copy button / mermaid / katex / trees) as the eager path,
    and any captured detail-disclosure state must be re-applied — otherwise
    expanded rows render un-enhanced and an open tool card resets closed."""
    body = _function_body(UI_JS, "_materializeDeferredWorklogRows")
    # post-processing scheduled after materialization (matches eager rebuild paths)
    assert "_postProcessWithAnchorSuppression(group)" in body
    assert "requestAnimationFrame" in body
    # disclosure state stashed at defer/rebuild time is re-applied after rows exist
    assert "_deferredWorklogDisclosure" in body
    assert "_restoreWorklogDetailDisclosureState(group" in body


def test_rebuild_stashes_disclosure_on_deferred_groups():
    """#5860 gate fix: on a transcript rebuild the disclosure-restore pass can't
    reach deferred groups (no rows yet), so the captured state is stashed on each
    still-deferred group for _materializeDeferredWorklogRows to apply on expand."""
    assert "group._deferredWorklogDisclosure=worklogDetailDisclosureState;" in UI_JS
    # stash happens right after the main restore pass over the rebuilt transcript
    assert 'querySelectorAll(\'[data-worklog-rows-deferred="1"]\')' in UI_JS
