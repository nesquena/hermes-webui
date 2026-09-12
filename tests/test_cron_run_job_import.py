"""Regression test for #1312 / #1310 — _run_cron_tracked must import run_job.

The function runs inside a worker thread (threading.Thread), so any
names it references must be resolvable from that thread's scope.
Before the fix, run_job was only imported inside _handle_cron_run
(a local scope invisible to _run_cron_tracked), causing NameError.
"""
import ast
import inspect
from pathlib import Path

import pytest

ROUTES_PY = Path(__file__).resolve().parent.parent / "api" / "routes.py"
CRON_RUNTIME_PY = Path(__file__).resolve().parent.parent / "api" / "cron_runtime.py"


def _get_function_source(func_name: str, source_path: Path = ROUTES_PY) -> str:
    """Extract a top-level function's source via AST for stability."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = source_path.read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    pytest.fail(f"Function {func_name} not found in {source_path}")


class TestRunCronTrackedImport:
    """_run_cron_tracked must be self-contained — it runs in a worker thread."""

    def test_cron_operation_imports_scheduler_inside_function(self):
        """The child dispatcher imports the scheduler in its own scope."""
        src = _get_function_source("_invoke_cron_operation", CRON_RUNTIME_PY)
        assert "import cron.scheduler as scheduler" in src
        assert "getattr(scheduler, operation," in src

    def test_handle_cron_run_does_not_import_run_job(self):
        """After the fix, _handle_cron_run should NOT need to import run_job
        itself — it's now _run_cron_tracked's responsibility."""
        src = _get_function_source("_handle_cron_run")
        tree = ast.parse(src)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported_names.add(alias.name if alias.asname is None else alias.asname)
        assert "run_job" not in imported_names, (
            "_handle_cron_run still imports run_job — it should be moved to "
            "_run_cron_tracked to avoid the NameError in worker threads."
        )

    def test_run_cron_tracked_calls_run_job_helper(self):
        """Sanity: the function still delegates to the cron job runner."""
        src = _get_function_source("_run_cron_tracked")
        assert "_run_cron_job_in_profile_subprocess" in src

    def test_cron_operation_dispatches_requested_operation(self):
        """The shared child dispatcher invokes the requested scheduler operation."""
        src = _get_function_source("_invoke_cron_operation", CRON_RUNTIME_PY)
        assert "callable_(job, *args, **kwargs)" in src
