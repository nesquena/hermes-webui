"""Every repo-root module the entry points import is packaged and syntax-gated.

``pyproject.toml`` ships the flat root modules through ``[tool.setuptools]
py-modules``. A root module that ``server.py``, ``bootstrap.py`` or
``mcp_server.py`` imports (directly, inside a function, or through another
root module) but that is missing from that list is left out of a
non-editable wheel, and ``import server`` fails after ``pip install``.

The CI syntax gate in ``.github/workflows/tests.yml`` names root modules one
by one too, so every listed module must appear on its ``compileall`` line.

Static and offline: ``ast``, ``tomllib`` and file reads only.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTRY_MODULES = ("server", "bootstrap", "mcp_server")
TESTS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _setuptools_config() -> tuple[list[str], list[str]]:
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        setuptools = tomllib.load(fh)["tool"]["setuptools"]
    return list(setuptools.get("py-modules", [])), list(setuptools.get("packages", []))


def _imported_root_modules(path: Path) -> set[str]:
    """Top-level names *path* imports anywhere (module level, in functions,
    or as a literal ``import_module``/``__import__`` argument) that resolve
    to a ``<name>.py`` file at the repo root."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
        elif (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) in ("import_module", "__import__")
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    roots = {name.split(".")[0] for name in names}
    return {root for root in roots if (REPO_ROOT / f"{root}.py").is_file()}


def _root_modules_reachable_from_entry_points() -> set[str]:
    found: set[str] = set()
    pending: list[str] = list(ENTRY_MODULES)
    while pending:
        for name in _imported_root_modules(REPO_ROOT / f"{pending.pop()}.py") - found:
            found.add(name)
            pending.append(name)
    return found


def test_scan_sees_the_startup_modules_server_imports():
    # Keeps the guard below from passing vacuously if the scan breaks.
    reachable = _root_modules_reachable_from_entry_points()
    assert {"startup_memory_limits", "managed_agent_startup"} <= reachable, reachable


def test_every_root_module_the_entry_points_import_is_packaged():
    py_modules, packages = _setuptools_config()
    missing = sorted(_root_modules_reachable_from_entry_points() - set(py_modules) - set(packages))
    assert not missing, (
        f"{missing} imported by {ENTRY_MODULES} but missing from [tool.setuptools] py-modules "
        "in pyproject.toml; a wheel install would fail to import server"
    )


def test_entry_modules_and_listed_py_modules_exist():
    py_modules, _ = _setuptools_config()
    assert set(ENTRY_MODULES) <= set(py_modules), py_modules
    assert [m for m in py_modules if not (REPO_ROOT / f"{m}.py").is_file()] == []


def _compileall_targets() -> set[str]:
    targets: set[str] = set()
    for line in TESTS_WORKFLOW.read_text(encoding="utf-8").splitlines():
        _, sep, args = line.partition("-m compileall")
        if sep:
            targets.update(arg for arg in args.split() if not arg.startswith("-"))
    return targets


def test_ci_syntax_gate_compiles_every_packaged_module():
    py_modules, _ = _setuptools_config()
    targets = _compileall_targets()
    assert targets, "tests.yml no longer runs python3 -m compileall"
    missing = sorted({f"{m}.py" for m in py_modules} - targets)
    assert not missing, f"add {missing} to the compileall line in .github/workflows/tests.yml"
