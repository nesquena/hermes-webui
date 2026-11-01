"""Contract tests for the placement rule: what the module may and may not do.

These are the assertions that make the invariant checkable without a fixture:
the creation path spawns no directory walk and no index/content query, mutates
nothing it did not create, returns a closed-set payload, and records a refused
relocation as a value instead of raising or nesting.

Findings 5, 6 and 8 (rounds 10-18) are properties of the *absence* of code, so
they are pinned here as AST assertions over the modules themselves.
"""

from __future__ import annotations

import ast
from pathlib import Path


from api.worktree_placement import (
    PLACEMENT_MAIN,
    PLACEMENT_MOVE_FAILED,
    PLACEMENT_UNRECORDED,
    PLACEMENTS,
    primary_worktree,
    relocate_worktree,
)
from api.worktrees import create_worktree_for_workspace
from tests import _worktree_git as wt

ROOT = Path(__file__).resolve().parents[1]
PLACEMENT_MODULE = ROOT / "api" / "worktree_placement.py"
WORKTREES_MODULE = ROOT / "api" / "worktrees.py"

CREATION_PATH_FUNCTIONS = (
    # Every function on the creation path that receives the raw user path: the
    # repository lookup runs FIRST and is the easiest place to sneak a walk in,
    # and the Agent helper is handed the selected checkout. Both are pinned here
    # so the property is enforced by the test rather than by inspection.
    "find_git_repo_root",
    "_setup_agent_worktree",
    "create_worktree_for_workspace",
    "_gitignore_bytes",
    "_restore_gitignore",
    "_ensure_worktrees_ignored",
)

#: Git verbs that would mean the WebUI assembled, adjudicated or re-derived the
#: tree instead of just placing it.
FORBIDDEN_VERBS = {
    "reset",
    "--hard",
    "clean",
    "cherry",
    "merge-base",
    "ls-files",
    "diff-files",
    "cat-file",
    "ls-tree",
    "update-index",
    "--refresh",
    "show",
    "cherry-pick",
}

WALK_CALLS = {"walk", "scandir", "listdir", "rglob", "glob", "iterdir"}


def _function_nodes(path: Path, names: tuple[str, ...]) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    missing = set(names) - set(found)
    assert not missing, f"{path.name} is missing {sorted(missing)}"
    return found


def _code_strings(node: ast.AST) -> list[str]:
    """String constants in *node*, excluding its own docstring."""
    body = getattr(node, "body", [])
    docstrings = set()
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        docstrings.add(id(body[0].value))
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and id(child) not in docstrings
    ]


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


# ── finding 5: no directory walk, no .git-shape test ────────────────────────


def test_module_performs_no_directory_walk():
    """Finding 5 (rounds 10-18, ``a3c42e9b``): a depth cutoff authorized the
    storage dir and filtering an rglob did not bound the traversal. There is no
    traversal at all — no ``walk``, ``scandir``, ``listdir``, ``rglob``, ``glob``
    or ``iterdir`` anywhere in the placement module or the creation path, and no
    ``.git``-shaped string to test a path against.
    """
    placement_tree = ast.parse(PLACEMENT_MODULE.read_text(encoding="utf-8"))
    nodes: list[tuple[str, ast.AST]] = [("api/worktree_placement.py", placement_tree)]
    nodes.extend(_function_nodes(WORKTREES_MODULE, CREATION_PATH_FUNCTIONS).items())
    offenders: list[str] = []
    for label, node in nodes:
        for name in _called_names(node):
            if name in WALK_CALLS:
                offenders.append(f"{label}: {name}")
        for text in _code_strings(node):
            if text in {".git", ".git/", ".gitignore/"}:
                offenders.append(f"{label}: literal {text!r}")
    assert offenders == [], offenders


# ── findings 6 and 8: no index, no content, no base re-derivation ───────────


def test_creation_path_queries_nothing_but_placement():
    """Findings 6 and 8 (rounds 10-18, ``308e09a6`` / ``7fe4bedd``): the index and
    the working tree's content must never be read on the creation path, and no
    base ref may be re-derived there — the Agent's helper owns the base.
    """
    nodes = [node for node in _function_nodes(WORKTREES_MODULE, CREATION_PATH_FUNCTIONS).values()]
    nodes.append(ast.parse(PLACEMENT_MODULE.read_text(encoding="utf-8")))
    offenders: list[str] = []
    for node in nodes:
        label = getattr(node, "name", PLACEMENT_MODULE.name)
        for text in _code_strings(node):
            if text in FORBIDDEN_VERBS:
                offenders.append(f"{label}: {text!r}")
    assert offenders == [], offenders

    # And the relocation itself only ever unlocks, moves and re-locks.
    relocate = _function_nodes(PLACEMENT_MODULE, ("relocate_worktree",))["relocate_worktree"]
    verbs = {
        text
        for text in _code_strings(relocate)
        if text in {"unlock", "move", "add", "remove", "lock", "prune"}
    }
    assert verbs <= {"unlock", "move", "lock"}, verbs


# ── the payload is a closed set ─────────────────────────────────────────────


def test_payload_is_a_closed_set(tmp_path):
    """The result payload is testable because its keys and its ``placement``
    values are a closed set — no free-form "which root did we guess" field.
    """
    repo = wt.init_repo(tmp_path / "repo")
    producer = wt.agent_producer()

    info = create_worktree_for_workspace(repo, setup=producer)

    assert set(info) == {
        "path",
        "branch",
        "repo_root",
        "placement",
        "placement_reason",
        "created_at",
    }
    assert info["placement"] in PLACEMENTS
    assert info["placement"] == PLACEMENT_MAIN
    assert isinstance(info["created_at"], float)


def test_unrecorded_payload_still_usable(tmp_path):
    """The no-claim outcome is first-class: the tree exists, ``repo_root`` names
    the checkout it really lives under (so removal works), and the payload says
    the placement was unrecorded rather than naming a guessed root.
    """
    checkout, _metadata = wt.separate_git_dir(tmp_path, dot_git=False)
    session = wt.linked_worktree(checkout, tmp_path / "session")

    info = create_worktree_for_workspace(session, setup=wt.agent_producer())

    assert info["placement"] == PLACEMENT_UNRECORDED
    assert info["placement_reason"] == "metadata-unrecorded"
    assert Path(info["repo_root"]) == session.resolve()
    assert Path(info["path"]).is_dir()
    assert wt.head(Path(info["path"])) == wt.head(session)
    # The tree lives *inside* the selected checkout here, so the Agent's own
    # ``.worktrees/`` entry is kept (it is what keeps the tree out of the
    # session's status) — only a moved tree has that edit undone.
    status = wt.git(session, "status", "--porcelain", "--untracked-files=all").stdout
    assert ".worktrees/" not in status


# ── an existing destination is refused, never nested ────────────────────────


def test_relocation_refuses_an_existing_destination(tmp_path):
    """Finding 11 (rounds 10-22) / the "must never happen" list: ``git worktree
    move`` onto an existing directory *nests* inside it instead of failing, so a
    taken destination must be refused up front and reported — not nested, and not
    raised at the caller as a crash.
    """
    primary = wt.init_repo(tmp_path / "primary")
    session = wt.linked_worktree(primary, tmp_path / "session")
    squatter = primary / ".worktrees" / "hermes-taken"
    squatter.mkdir(parents=True)
    (squatter / "keep.txt").write_text("not mine\n", encoding="utf-8")

    info = create_worktree_for_workspace(
        session, setup=wt.agent_producer(name="hermes-taken")
    )

    assert info["placement"] == PLACEMENT_MOVE_FAILED
    assert Path(info["path"]).parent == session.resolve() / ".worktrees"
    assert [path.name for path in squatter.iterdir()] == ["keep.txt"]
    assert Path(info["repo_root"]) == session.resolve()
    # The refused tree is still a real, usable worktree of the same repository.
    assert wt.head(Path(info["path"])) == wt.head(session)


def test_relocate_returns_an_error_instead_of_raising(tmp_path):
    """The relocation primitive answers ``(None, reason)``; a locked tree is a
    reported refusal, not an exception escaping into the HTTP handler.
    """
    primary = wt.init_repo(tmp_path / "primary")
    created = wt.linked_worktree(primary, tmp_path / "created", branch="hermes-locked")
    wt.git(primary, "worktree", "lock", "--reason", "held", str(created))
    existing = primary / ".worktrees" / "created"
    existing.mkdir(parents=True)

    destination, reason = relocate_worktree(created, primary)

    assert destination is None
    assert reason and "destination exists" in reason
    assert created.is_dir()


# ── a bare repository never owns a session's tree ───────────────────────────


def test_bare_repository_worktree_has_no_claim(tmp_path):
    """A worktree of a *bare* repository: git's first record is the bare dir and
    carries the ``bare`` flag, so there is no main worktree to place under.
    """
    _bare, tree = wt.bare_with_worktree(tmp_path)

    placement = primary_worktree(tree)
    info = create_worktree_for_workspace(tree, setup=wt.agent_producer())

    assert placement.primary is None
    assert placement.reason == "bare-repository"
    assert info["placement"] == PLACEMENT_UNRECORDED
    assert Path(info["path"]).parent == tree.resolve() / ".worktrees"
