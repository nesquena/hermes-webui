"""Inventory coverage for windowless production Git subprocesses on Windows."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_GIT_RUNS = Counter(
    {
        "api/agent_runtime.py": 3,
        "api/rollback.py": 4,
        "api/updates.py": 1,
        "api/workspace.py": 1,
        "api/workspace_git.py": 2,
        "api/worktrees.py": 2,
    }
)


def _is_subprocess_run(node: ast.Call) -> bool:
    """Return whether ``node`` is a direct ``subprocess.run(...)`` boundary."""
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr == "run"
    )


def _git_argv_kind(node: ast.AST) -> str | None:
    """Recognize the production argv forms used by direct Git subprocess calls."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _git_argv_kind(node.left)
    if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
        executable = node.elts[0]
        if isinstance(executable, ast.Constant) and executable.value == "git":
            return "git"
        if isinstance(executable, ast.Name) and executable.id in {"git", "git_executable"}:
            return executable.id
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_hardened_git_argv"
    ):
        return node.func.id
    return None


def _production_git_runs() -> list[tuple[str, ast.Call]]:
    """Inventory every recognizable direct Git ``subprocess.run`` in production code."""
    calls: list[tuple[str, ast.Call]] = []
    production_files = [*sorted((ROOT / "api").glob("*.py")), ROOT / "bootstrap.py", ROOT / "server.py"]
    for path in production_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative_path = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _is_subprocess_run(node)
                and node.args
                and _git_argv_kind(node.args[0]) is not None
            ):
                calls.append((relative_path, node))
    return calls


def test_git_subprocess_inventory_uses_shared_windows_flags() -> None:
    """All production Git boundaries must use the one dependency-light helper."""
    calls = _production_git_runs()
    assert Counter(path for path, _node in calls) == EXPECTED_GIT_RUNS

    missing_shared_helper: list[str] = []
    for path, node in calls:
        creationflags = next(
            (keyword.value for keyword in node.keywords if keyword.arg == "creationflags"),
            None,
        )
        if not (
            isinstance(creationflags, ast.Call)
            and isinstance(creationflags.func, ast.Name)
            and creationflags.func.id == "windows_hide_flags"
        ):
            missing_shared_helper.append(f"{path}:{node.lineno}")

    assert not missing_shared_helper, (
        "Git subprocess.run calls must pass creationflags=windows_hide_flags(): "
        + ", ".join(missing_shared_helper)
    )

    helper_definitions: list[str] = []
    for path in sorted((ROOT / "api").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"windows_hide_flags", "_windows_hide_flags"}
            for node in ast.walk(tree)
        ):
            helper_definitions.append(path.relative_to(ROOT).as_posix())
    assert helper_definitions == ["api/subprocess_utils.py"]


def test_windows_hide_flags_returns_win32_sentinel(monkeypatch) -> None:
    """The helper must expose the platform's CREATE_NO_WINDOW value on Win32."""
    from api import subprocess_utils

    sentinel = 0x08000000
    monkeypatch.setattr(subprocess_utils, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        SimpleNamespace(CREATE_NO_WINDOW=sentinel),
    )

    assert subprocess_utils.windows_hide_flags() == sentinel


def test_windows_hide_flags_is_zero_on_posix(monkeypatch) -> None:
    """Passing creationflags=0 must remain a genuine no-op off Windows."""
    from api import subprocess_utils

    monkeypatch.setattr(subprocess_utils, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        SimpleNamespace(CREATE_NO_WINDOW=0x08000000),
    )

    assert subprocess_utils.windows_hide_flags() == 0
