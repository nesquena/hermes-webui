"""Regression test for #4768.

In split-container / minimal Docker deployments the WebUI image may not ship the
agent's ``cron`` package on its import path. Before the fix, ``GET /api/crons``
(the Tasks tab's initial load) did ``from cron.jobs import list_jobs`` with no
guard, so a ``ModuleNotFoundError`` bubbled up as a 500 and broke the whole tab.

The fix degrades gracefully: an empty job list with a ``cron_unavailable`` flag.
"""
import pathlib

REPO = pathlib.Path(__file__).resolve().parent.parent
ROUTES = (REPO / "api" / "routes.py").read_text(encoding="utf-8")


def _api_crons_branch() -> str:
    """Return the source of the `if parsed.path == "/api/crons":` GET branch."""
    marker = ROUTES.index('if parsed.path == "/api/crons":')
    # Stop at the next sibling branch so we only inspect this handler.
    nxt = ROUTES.index('if parsed.path == "/api/crons/output":', marker)
    return ROUTES[marker:nxt]


def test_api_crons_guards_missing_cron_module():
    """The /api/crons GET branch must wrap the cron.jobs import in a guard that
    returns a graceful payload instead of letting ModuleNotFoundError 500 — but
    only for a genuinely-absent cron package, not an internal import bug."""
    branch = _api_crons_branch()
    assert "from cron.jobs import list_jobs" in branch
    assert "except ModuleNotFoundError as exc" in branch, (
        "GET /api/crons must catch ModuleNotFoundError so the Tasks tab does not "
        "500 when the cron package is absent (#4768)."
    )
    # The except path returns an empty list + an unavailable flag (not a re-raise)
    # ONLY for the genuinely-missing cron package...
    assert "cron_unavailable" in branch
    assert 'exc.name in ("cron", "cron.jobs")' in branch, (
        "Must only treat an absent cron package as unavailable; an internal "
        "dependency ImportError of an existing cron/jobs.py must still surface."
    )
    # ...and re-raises everything else (a real cron bug is not swallowed).
    assert "\n            raise" in branch
    # The try guards the import specifically (not some unrelated block).
    try_idx = branch.index("try:")
    import_idx = branch.index("from cron.jobs import list_jobs")
    except_idx = branch.index("except ModuleNotFoundError")
    assert try_idx < import_idx < except_idx


def test_api_crons_still_lists_jobs_on_happy_path():
    """The guard must not remove the normal behavior: when cron imports fine, the
    branch still calls list_jobs(include_disabled=True) under cron_profile_context."""
    branch = _api_crons_branch()
    assert "list_jobs(include_disabled=True)" in branch
    assert "cron_profile_context()" in branch
    assert "_cron_jobs_for_api(" in branch
