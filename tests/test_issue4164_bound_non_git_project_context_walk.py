"""Regression for #4164: bound the project-context file walk in non-git workspaces.

Before this fix, ``_project_context_candidates`` mirrored the agent's
``_find_hermes_md`` exactly — including the bug that ``stop_at = git_root``
is ``None`` in a non-git tree, which caused the ``for directory in [cwd,
*cwd.parents]: ... if stop_at and directory == stop_at: break`` walk to
never break and run all the way to filesystem root. A workspace at
``/tmp/x/project/subdir`` could then surface ``/tmp/x/HERMES.md`` (a file
*outside* the user's workspace) in the Project Context tab.

The fix bounds the walk at the workspace itself when no git root is found:
the cwd is still scanned, but parents are skipped. This is the minimal,
non-breaking bound — git workspaces are unchanged, and in-workspace files
still load.
"""
from __future__ import annotations

import pathlib

import api.routes as routes


def _ctx(workspace):
    return routes._read_active_project_context(pathlib.Path(workspace))


def test_non_git_workspace_does_not_walk_above_workspace(tmp_path):
    """Sentinel HERMES.md placed *above* the workspace in a non-git tree must
    not be surfaced — the pre-fix walk would have loaded it because stop_at
    was None and the loop ran to filesystem root."""
    above = tmp_path / "outside"
    above.mkdir()
    (above / "HERMES.md").write_text("LEAKED FROM OUTSIDE", encoding="utf-8")

    workspace = above / "project" / "subdir"
    workspace.mkdir(parents=True)
    # Intentionally no .git anywhere in the tree, and no HERMES/AGENTS file
    # inside the workspace — so any non-empty result means we walked above.

    data = _ctx(workspace)

    assert "LEAKED FROM OUTSIDE" not in data["content"], (
        "Non-git workspace walk leaked an HERMES.md above the workspace root"
    )
    assert data["path"] == "", (
        f"Expected empty path (no in-workspace context file); got {data['path']!r}"
    )


def test_non_git_workspace_still_reads_in_workspace_context(tmp_path):
    """The bound must only block *parents* — files inside the workspace itself
    must still load exactly as before."""
    workspace = tmp_path / "no-git-here"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("# In-workspace rules", encoding="utf-8")

    data = _ctx(workspace)

    assert "In-workspace rules" in data["content"]
    assert data["path"].endswith("AGENTS.md")
    assert data["name"] == "AGENTS.md"


def test_git_workspace_walk_to_git_root_is_unchanged(tmp_path):
    """Git workspaces must keep walking up to the git root — the bound only
    activates when git_root is None. This duplicates the pre-existing
    ``test_project_context_walks_hermes_md_to_git_root_but_not_agents_md``
    intent specifically against the #4164 patch site."""
    root = tmp_path / "repo"
    nested = root / "src" / "deep" / "pkg"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "HERMES.md").write_text("# Root-of-repo context", encoding="utf-8")

    data = _ctx(nested)

    assert "Root-of-repo context" in data["content"]
    assert data["path"].endswith("HERMES.md")


def test_non_git_workspace_bound_is_workspace_not_first_parent(tmp_path):
    """Edge case: if the workspace itself contains a HERMES.md AND a parent
    also contains one, the workspace copy must win and the parent copy must
    NOT leak in via the candidate list (i.e. the bound triggers immediately,
    not after one extra parent step)."""
    parent = tmp_path / "container"
    parent.mkdir()
    (parent / "HERMES.md").write_text("PARENT COPY", encoding="utf-8")

    workspace = parent / "ws"
    workspace.mkdir()
    (workspace / "HERMES.md").write_text("WORKSPACE COPY", encoding="utf-8")

    data = _ctx(workspace)

    assert "WORKSPACE COPY" in data["content"]
    assert "PARENT COPY" not in data["content"]
    # And the parent copy must NOT appear as a "shadowed" hit either —
    # it's not just lower priority, it's out of scope entirely.
    assert all("PARENT COPY" not in (s.get("path") or "") for s in data["shadowed"])
    assert not any(
        str(parent / "HERMES.md") == s.get("path") for s in data["shadowed"]
    )
