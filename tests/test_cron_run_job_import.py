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


def _get_function_source(func_name: str) -> str:
    """Extract a top-level function's source via AST for stability."""
    tree = ast.parse(ROUTES_PY.read_text(encoding="utf-8"))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = ROUTES_PY.read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    pytest.fail(f"Function {func_name} not found in {ROUTES_PY}")


class TestRunCronTrackedImport:
    """_run_cron_tracked must be self-contained — it runs in a worker thread."""

    def test_run_job_imported_inside_function(self):
        """run_job must be imported inside _run_cron_tracked, not relied on
        from a caller's local scope."""
        src = _get_function_source("_run_cron_tracked")
        tree = ast.parse(src)
        names_used = set()

        class NameCollector(ast.NodeVisitor):
            def visit_Name(self, node):
                names_used.add(node.id)

        ImportCollector = type(
            "ImportCollector",
            (ast.NodeVisitor,),
            {
                "imports": set(),
                "visit_ImportFrom": lambda self, node: (
                    self.imports.add(a.name for a in node.names),
                ),
            },
        )

        # Collect all names referenced in the function body
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names_used.add(node.id)

        # Collect imports inside the function
        func_imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    func_imports.add(alias.name if alias.asname is None else alias.asname)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    func_imports.add(alias.name if alias.asname is None else alias.asname)

        # run_job is referenced → must be imported inside the function
        if "run_job" in names_used:
            assert "run_job" in func_imports, (
                "_run_cron_tracked references run_job but does not import it locally. "
                "It runs in a worker thread and cannot rely on caller's local imports."
            )

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

    def test_run_cron_tracked_calls_run_job(self):
        """Sanity: the function still actually calls run_job."""
        src = _get_function_source("_run_cron_tracked")
        assert "run_job" in src, "_run_cron_tracked should call run_job"
