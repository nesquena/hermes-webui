from __future__ import annotations

import ast
from pathlib import Path

import pytest

import api.worktree_gc_git as git_backend
from scripts import worktree_gc


@pytest.fixture(scope="session", autouse=True)
def test_server():
    """These static safety tests do not need the HTTP server fixture."""


def _source_path(module) -> Path:
    return Path(module.__file__).resolve()


def test_git_backend_does_not_export_a_collector():
    assert not hasattr(git_backend, "collect_git_worktree")


def test_application_modules_have_no_destructive_filesystem_calls():
    forbidden_attributes = {
        ("shutil", "rmtree"),
    }
    for module in (git_backend, worktree_gc):
        tree = ast.parse(_source_path(module).read_text(encoding="utf-8"))
        calls = {
            (node.func.value.id, node.func.attr)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        }
        assert calls.isdisjoint(forbidden_attributes)


def test_git_backend_contains_no_destructive_git_command():
    source = _source_path(git_backend).read_text(encoding="utf-8")
    forbidden_fragments = (
        '"worktree", "unlock"',
        '"worktree", "remove"',
        '"branch", "-d"',
        '"branch", "-D"',
        '"update-ref", "-d"',
        '"worktree", "prune"',
    )
    assert all(fragment not in source for fragment in forbidden_fragments)


def test_git_runner_rejects_commands_outside_read_only_allowlist(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        git_backend.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail(
            "rejected commands must not reach Git"
        ),
    )

    with pytest.raises(RuntimeError) as exc:
        git_backend._run_git(
            ["worktree", "remove", str(tmp_path / "candidate")],
            tmp_path,
        )

    assert getattr(exc.value, "code", None) == "git_command_not_allowed"


def test_cli_contains_no_collection_path_or_recursive_delete():
    source = _source_path(worktree_gc).read_text(encoding="utf-8")
    assert "collect_git_worktree" not in source
    assert "rmtree" not in source
    assert "def _collect" not in source
    assert "args.collect" not in source
    assert '"collection"' not in source
