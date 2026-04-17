"""
Smoke tests for /api/stats/* endpoints (section 2 of
openspec/changes/add-dashboards-and-pixel-office/tasks.md).

This file currently covers only the scaffold — module imports cleanly and
the 5 handler stubs exist. Real contract/behaviour tests land with tasks
2.4 through 2.11.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def test_stats_module_importable():
    """api.stats must import without raising."""
    import api.stats  # noqa: F401


def test_stats_handlers_exposed():
    """All 5 handler stubs must be defined as callables."""
    from api import stats
    for name in (
        "handle_stats_summary",
        "handle_stats_timeseries",
        "handle_stats_response_time",
        "handle_stats_heatmap",
        "handle_stats_models",
    ):
        assert callable(getattr(stats, name, None)), f"{name} missing or not callable"


def test_stats_routes_registered():
    """routes.py must dispatch all 5 /api/stats/* paths."""
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for path in (
        '"/api/stats/summary"',
        '"/api/stats/timeseries"',
        '"/api/stats/response-time"',
        '"/api/stats/heatmap"',
        '"/api/stats/models"',
    ):
        assert path in src, f"routes.py missing dispatch for {path}"
