"""Regression test for issue #2024.

tools.skills_tool / tools.skill_manager_tool imports must NOT appear
inside an ``_ENV_LOCK`` body in api/streaming.py.  First-time module
imports can be slow (disk I/O, transitive deps, plugin discovery) and
holding the lock during them serialises every concurrent session behind
the slowest import.

The fix introduces ``_prewarm_skill_tool_modules()`` which does the
imports *before* the lock is acquired, and the lock body uses a shared
helper that only performs ``sys.modules.get()`` lookups (O(1) dict lookup,
no import machinery).

These tests are AST/source-level because the actual import targets
(``tools.skills_tool``, ``tools.skill_manager_tool``) live in the
hermes-agent package which may not be installed in the test venv.
"""
import ast
import pathlib
import textwrap

REPO = pathlib.Path(__file__).resolve().parent.parent
STREAMING_PY = REPO / "api" / "streaming.py"
PROFILES_PY = REPO / "api" / "profiles.py"


def _read_streaming() -> str:
    return STREAMING_PY.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AST-level check: walk every ``with`` statement whose context-expression
# references ``_ENV_LOCK`` and ensure no ``Import`` or ``ImportFrom``
# node for the two target modules exists in its body.
# ---------------------------------------------------------------------------

def _find_env_lock_with_bodies(source: str) -> list[list[ast.stmt]]:
    """Return the statement-list bodies of all ``with _ENV_LOCK:`` blocks."""
    tree = ast.parse(source)
    bodies: list[list[ast.stmt]] = []

    class _Visitor(ast.NodeVisitor):
        def visit_With(self, node: ast.With):
            # Check whether any context-expression is a simple Name `_ENV_LOCK`
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Name) and ctx.id == "_ENV_LOCK":
                    bodies.append(node.body)
                    break
            self.generic_visit(node)

    _Visitor().visit(tree)
    return bodies


def _imports_in_body(body: list[ast.stmt], target_modules: set[str]) -> list[str]:
    """Return module names from Import/ImportFrom nodes in *body* that are in *target_modules*."""
    found: list[str] = []
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in target_modules:
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module in target_modules:
                found.append(node.module)
    return found


_TARGET_MODULES = {"tools.skills_tool", "tools.skill_manager_tool"}


class TestNoSkillToolImportsInsideEnvLock:
    """AST-level: no ``import tools.skills_tool`` or ``import tools.skill_manager_tool``
    inside any ``with _ENV_LOCK:`` block."""

    def test_no_skill_imports_in_env_lock(self):
        source = _read_streaming()
        bodies = _find_env_lock_with_bodies(source)
        assert bodies, "Expected at least one `with _ENV_LOCK:` block in streaming.py"
        for body in bodies:
            found = _imports_in_body(body, _TARGET_MODULES)
            assert found == [], (
                f"Found import(s) of {found} inside an `_ENV_LOCK` with-block. "
                "Move them to _prewarm_skill_tool_modules() outside the lock (#2024)."
            )


class TestPrewarmHelperExists:
    """The ``_prewarm_skill_tool_modules`` helper must exist and reference
    both target modules."""

    def test_prewarm_function_defined(self):
        source = _read_streaming()
        tree = ast.parse(source)
        func_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        assert "_prewarm_skill_tool_modules" in func_names, (
            "_prewarm_skill_tool_modules() helper must be defined in streaming.py"
        )

    def test_prewarm_references_both_modules(self):
        source = _read_streaming()
        # Find the function source and check it references both module names.
        # Simple string check is sufficient and more robust than AST for
        # dynamic __import__ calls.
        assert "tools.skills_tool" in source, (
            "streaming.py must reference 'tools.skills_tool'"
        )
        assert "tools.skill_manager_tool" in source, (
            "streaming.py must reference 'tools.skill_manager_tool'"
        )

    def test_prewarm_called_before_env_lock(self):
        """_prewarm_skill_tool_modules() must be called before the first
        ``with _ENV_LOCK:`` in _run_agent_streaming."""
        source = _read_streaming()
        lines = source.splitlines()
        prewarm_line = None
        first_env_lock_line = None
        for i, line in enumerate(lines, 1):
            if "_prewarm_skill_tool_modules()" in line and prewarm_line is None:
                prewarm_line = i
            if "with _ENV_LOCK:" in line and first_env_lock_line is None:
                first_env_lock_line = i
        assert prewarm_line is not None, "_prewarm_skill_tool_modules() call not found"
        assert first_env_lock_line is not None, "with _ENV_LOCK: not found"
        assert prewarm_line < first_env_lock_line, (
            f"_prewarm_skill_tool_modules() (line {prewarm_line}) must appear "
            f"before the first `with _ENV_LOCK:` (line {first_env_lock_line})"
        )


class TestSysModulesLookupInEnvLock:
    """Inside the lock, streaming must use the shared cache patch helper."""

    def test_shared_skill_home_patch_helper_used_in_env_lock(self):
        source = _read_streaming()
        bodies = _find_env_lock_with_bodies(source)
        assert bodies, "Expected at least one `with _ENV_LOCK:` block"

        lines = source.splitlines()
        in_lock = False
        lock_lines: list[str] = []
        depth = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("with _ENV_LOCK:"):
                in_lock = True
                depth = 0
                continue
            if in_lock:
                # Track indentation depth to know when we exit the with-block
                if stripped:
                    # Count leading spaces
                    indent = len(line) - len(line.lstrip())
                    if depth == 0:
                        depth = indent
                    elif indent < depth and stripped:
                        in_lock = False
                        continue
                lock_lines.append(line)

        lock_source = "\n".join(lock_lines)
        assert "patch_skill_home_modules" in lock_source, (
            "Inside `_ENV_LOCK`, streaming must use the shared skill module "
            "cache patch helper instead of duplicating module-specific logic "
            "(#2023/#2024)"
        )

    def test_shared_helper_uses_sys_modules_get_for_both_skill_modules(self):
        source = PROFILES_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        helper = next(
            (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef)
                and node.name == "patch_skill_home_modules"
            ),
            None,
        )
        assert helper is not None, "patch_skill_home_modules() must be defined"

        helper_source = ast.get_source_segment(source, helper) or ""
        assert "sys.modules.get" in helper_source, (
            "patch_skill_home_modules() must use sys.modules.get(), not import, "
            "so env-lock callers do not trigger first-time imports (#2024)"
        )
        assert "HERMES_HOME" in helper_source
        assert "SKILLS_DIR" in helper_source
        assert "tools.skills_tool" in source, (
            "profiles.py must patch tools.skills_tool module-level caches"
        )
        assert "tools.skill_manager_tool" in source, (
            "profiles.py must patch tools.skill_manager_tool module-level caches"
        )

    def test_no_import_statement_for_skill_tools_in_lock(self):
        """Double-check: no bare ``import tools.skills_tool`` or
        ``import tools.skill_manager_tool`` inside the lock body source."""
        source = _read_streaming()
        lines = source.splitlines()
        in_lock = False
        depth = 0
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("with _ENV_LOCK:"):
                in_lock = True
                depth = 0
                continue
            if in_lock:
                if stripped:
                    indent = len(line) - len(line.lstrip())
                    if depth == 0:
                        depth = indent
                    elif indent < depth and stripped:
                        in_lock = False
                        continue
                # Check for import statements targeting our modules
                for mod in _TARGET_MODULES:
                    # Match both `import tools.skills_tool` and `import tools.skills_tool as _sk`
                    if f"import {mod}" in stripped:
                        raise AssertionError(
                            f"Found `import {mod}` inside `_ENV_LOCK` body — "
                            f"use sys.modules.get() instead (#2024). Line: {stripped}"
                        )
