"""Pin the cron-tasks paused-jobs collapsible section wiring (#4026).

Active vs paused cron jobs are now rendered into two buckets in `loadCrons`:
active items go straight into `#cronList`, and paused items go into a
collapsible `<details>` section at the bottom whose open/closed state is
persisted in `localStorage` under the `cron-paused-collapsed` key. Default
is collapsed so the active list isn't drowned by long-paused jobs.

This is a static-string check (same pattern as `test_issue4006_*` and
`test_issue3988_*`) — there's no headless browser harness in the repo, so
we verify the code paths and class names exist in panels.js + style.css.
"""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_load_crons_partitions_active_and_paused():
    src = _read("static/panels.js")
    assert "_activeJobs" in src and "_pausedJobs" in src, (
        "loadCrons must partition jobs into active vs paused buckets"
    )
    assert "status.state === 'paused' ? _pausedJobs : _activeJobs" in src, (
        "partition predicate must read state from _cronStatusMeta (status.state)"
    )


def test_paused_section_uses_details_element():
    src = _read("static/panels.js")
    # Native <details>/<summary> gives expand/collapse + a11y for free.
    assert "createElement('details')" in src
    assert "cron-paused-section" in src and "cron-paused-summary" in src


def test_paused_section_persists_collapse_state_in_localstorage():
    src = _read("static/panels.js")
    assert "localStorage.getItem('cron-paused-collapsed')" in src, (
        "collapse state must be persisted across refreshes (#4026 AC#8)"
    )
    assert "localStorage.setItem('cron-paused-collapsed'" in src, (
        "toggle handler must write the new collapse state"
    )


def test_paused_section_defaults_collapsed():
    """AC#3: section must default to collapsed on first visit."""
    src = _read("static/panels.js")
    # The read uses `!== '0'` so an absent key (first visit) yields `collapsed=true`,
    # and only an explicit '0' opens it. That's the default-collapsed contract.
    assert "localStorage.getItem('cron-paused-collapsed') !== '0'" in src
    # And the `<details>` element is only made open=true when collapsed===false.
    assert "if (!collapsed) details.open = true;" in src


def test_paused_section_skipped_when_no_paused_jobs():
    """AC#9: zero-paused case must not render the section header."""
    src = _read("static/panels.js")
    assert "if (_pausedJobs.length) {" in src, (
        "paused-section block must be guarded by _pausedJobs.length"
    )


def test_cron_list_remains_single_source_of_truth():
    """openCronDetail / _cronNewJobIds / detail-refresh still read _cronList
    (not the partitioned buckets) so click + new-run-dot + refresh keep working
    unchanged when grouping is introduced."""
    src = _read("static/panels.js")
    # The detail-refresh lookup post-render must still scan the whole _cronList.
    assert "_cronList.find(j => _cronJobKey(j) === _currentCronDetailKey)" in src


def test_paused_section_styling_present():
    css = _read("static/style.css")
    assert ".cron-paused-section" in css
    assert ".cron-paused-summary" in css
    # Native marker hidden so the custom rotating triangle takes over.
    assert ".cron-paused-summary::-webkit-details-marker{display:none;}" in css
