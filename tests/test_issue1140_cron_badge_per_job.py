"""Tests for issue #1140 — Cron completion badge per-job indicator.

Verifies that:
1. _cronNewJobIds tracks job IDs with new completions
2. loadCrons() renders a dot indicator for new-run jobs
3. openCronDetail() clears the unread state for the viewed job
4. Badge only clears when all unread jobs are viewed (not on panel open)
5. _renderCronDetail() adds has-new-run class to Last Output card
"""


# ── Static file tests (no server needed) ──


def test_cron_new_job_ids_tracking_in_panels_js():
    """panels.js should declare _cronNewJobIds Set and populate it in startCronPolling."""
    with open("static/panels.js") as f:
        src = f.read()

    # _cronNewJobIds declared as Set
    assert "_cronNewJobIds" in src, "_cronNewJobIds not found in panels.js"
    assert "new Set()" in src, "_cronNewJobIds should be initialized as Set()"

    # In startCronPolling, job IDs are added to the set
    assert "_cronNewJobIds.add(String(c.job_id))" in src, (
        "startCronPolling should add job_id to _cronNewJobIds"
    )


def test_cron_dot_indicator_rendered_in_load_crons():
    """loadCrons() should render a .cron-new-dot for jobs in _cronNewJobIds."""
    with open("static/panels.js") as f:
        src = f.read()

    # Dot indicator in cron-item rendering
    assert "cron-new-dot" in src, "cron-new-dot class not found"
    assert "_cronNewJobIds.has(String(job.id))" in src, (
        "loadCrons should check _cronNewJobIds for each job"
    )


def test_open_cron_detail_clears_unread():
    """openCronDetail() should mark job as read and remove the dot."""
    with open("static/panels.js") as f:
        src = f.read()

    # _clearCronUnreadForJob called in openCronDetail
    assert "_clearCronUnreadForJob" in src, "_clearCronUnreadForJob function not found"
    # Dot removal in openCronDetail
    assert "target.querySelector('.cron-new-dot')" in src, (
        "openCronDetail should remove the dot element"
    )


def test_clear_cron_unread_for_job_function():
    """_clearCronUnreadForJob should delete from set and refresh badge.

    _cronUnreadCount is derived from _cronNewJobIds.size in updateCronBadge,
    so the function only needs to delete from the set and trigger a badge sync.
    """
    with open("static/panels.js") as f:
        src = f.read()

    # Locate the function body to make assertions order-dependent
    start = src.find("function _clearCronUnreadForJob(")
    assert start != -1, "_clearCronUnreadForJob should be defined"
    body = src[start : start + 400]
    assert "_cronNewJobIds.delete(id)" in body, (
        "_clearCronUnreadForJob should delete from _cronNewJobIds"
    )
    assert "updateCronBadge()" in body, (
        "_clearCronUnreadForJob should call updateCronBadge to re-sync count"
    )


def test_switch_panel_no_longer_clears_badge():
    """switchPanel override should NOT clear badge on tasks panel open."""
    with open("static/panels.js") as f:
        src = f.read()

    # The old pattern "if(name==='tasks'){_cronUnreadCount=0" should NOT exist
    assert "if(name==='tasks'){_cronUnreadCount=0" not in src, (
        "switchPanel should NOT clear _cronUnreadCount on tasks open"
    )


def test_has_new_run_class_in_render_detail():
    """_renderCronDetail() should add has-new-run class to Last Output card."""
    with open("static/panels.js") as f:
        src = f.read()

    # Check has-new-run class in the cronDetailRuns div
    assert "has-new-run" in src, "has-new-run class not found"


def test_cron_css_classes_exist():
    """style.css should contain .cron-new-dot and .has-new-run styles."""
    with open("static/style.css") as f:
        src = f.read()

    assert ".cron-new-dot{" in src, ".cron-new-dot CSS rule not found"
    assert ".has-new-run{" in src, ".has-new-run CSS rule not found"
    assert "cron-dot-pulse" in src, "cron-dot-pulse animation not found"
