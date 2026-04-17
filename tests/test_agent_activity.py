"""
Smoke tests for agent-activity + surfaces endpoints (sections 3-4 of
openspec/changes/add-dashboards-and-pixel-office/tasks.md).

Covers only the scaffold: module imports, handler stubs exist, routes
are dispatched. Contract tests follow with tasks 3.9 and 4.5.
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent.resolve()


def test_agent_activity_module_importable():
    """api.agent_activity must import without raising."""
    import api.agent_activity  # noqa: F401


def test_agent_activity_handlers_exposed():
    """Handler stubs + pure-function builders must be defined as callables."""
    from api import agent_activity
    for name in (
        "derive_state",
        "build_surface_snapshot",
        "build_surfaces_cards",
        "build_surface_expand",
        "handle_agent_activity",
        "handle_agent_activity_stream",
        "handle_surfaces",
    ):
        assert callable(getattr(agent_activity, name, None)), f"{name} missing or not callable"


def test_agent_activity_routes_registered():
    """routes.py must dispatch the 3 new agent-activity paths."""
    src = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    for path in (
        '"/api/agent-activity"',
        '"/api/agent-activity/stream"',
        '"/api/surfaces"',
    ):
        assert path in src, f"routes.py missing dispatch for {path}"


def test_agent_activity_contract_no_runtime_fields():
    """
    Scaffold-level contract check (task 3.9): the module docstring must
    explicitly state that current_tool / pending_count are NOT exposed.
    A stronger integration test will verify the live response body once
    handlers are implemented.
    """
    import api.agent_activity as m
    doc = (m.__doc__ or "").lower()
    assert "current_tool" in doc or "current tool" in doc, \
        "module docstring must mention current_tool exclusion"
    assert "pending" in doc, \
        "module docstring must mention pending_count exclusion"
