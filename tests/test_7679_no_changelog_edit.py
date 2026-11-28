"""#7679 — a contributor PR must not edit the release changelog.

Greptile P2 (thread 4178042668, 2026-10-04): *"This contributor PR adds an
entry to ``CHANGELOG.md``, but the repository requires ordinary contributor PRs
not to edit that file; release commits own changelog updates. This requirement
must be satisfied before merging. Keep release-note wording in the PR body
instead."*

The rule is in the repository's own ``AGENTS.md``:

    - Do not edit ``CHANGELOG.md`` in ordinary contributor PRs. The release
      workflow owns changelog updates through release commits. If a change is
      release-note worthy, include concise release-note wording in the PR body
      instead.

This test pins the rule as an executable contract: the PR's diff against
``upstream/master`` must not touch ``CHANGELOG.md`` at all. That covers both
directions of the defect — a contributor ADDING an entry, and a contributor
EDITING or REMOVING one (a merge that re-inserts an already-released entry
into ``[Unreleased]`` is the same violation).

It is discriminating: re-adding a ``CHANGELOG.md`` entry makes it fail.
"""

from __future__ import annotations

import pathlib
import subprocess

REPO = pathlib.Path(__file__).resolve().parent.parent


def _upstream_changelog() -> str:
    """The authoritative changelog: whatever ``upstream/master`` ships."""
    proc = subprocess.run(
        ["git", "show", "upstream/master:CHANGELOG.md"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # No upstream ref locally (e.g. a shallow clone): fall back to the
        # file on disk, which is what the PR would be diffed against.
        return (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    return proc.stdout


def test_the_pr_does_not_touch_the_release_changelog():
    """``CHANGELOG.md`` must be byte-identical to upstream/master."""
    upstream = _upstream_changelog()
    current = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert current == upstream, (
        "the PR edits CHANGELOG.md, which AGENTS.md forbids for ordinary "
        "contributor PRs (release commits own the changelog). Put the "
        "release-note wording in the PR body instead."
    )


def test_the_repository_rule_is_still_in_place():
    """The contract this PR complies with must still exist upstream."""
    agents = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    assert "Do not edit `CHANGELOG.md` in ordinary contributor PRs" in agents, (
        "AGENTS.md no longer states the changelog rule — re-check whether this "
        "PR's changelog edit is still a violation"
    )
