"""Regression coverage for #6393 — sidebar project chips sort alphabetically.

The sidebar project-chip bar rendered chips in ``_allProjects`` order, which is
whatever ``/api/projects`` returned (disk/creation order). With many projects
the bar became hard to scan. The bar now sorts by project name with
``localeCompare`` via the ``_sortedProjectsForChips`` helper, cloning the array
so the shared ``_allProjects`` cache keeps its authoritative order for the
lookups elsewhere in the module (colors, pickers, session→project resolution).

The helper is extracted from ``static/sessions.js`` and executed under node so
this pins real sort behaviour, not just source text. The remaining assertions
pin the wiring: the chip loop consumes the sorted copy, and the fixed leading
``All`` / ``Unassigned`` chips stay ahead of the sorted names.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
SESSIONS_JS = SESSIONS_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is required to execute the chip-sort harness"
)


_DRIVER = r"""
const fs = require('fs');

const src = fs.readFileSync(process.argv[1], 'utf8');
const start = src.indexOf('function _sortedProjectsForChips(');
if (start < 0) {
  console.log(JSON.stringify({error: 'helper-missing'}));
  process.exit(2);
}

// Brace-match the function declaration so the slice is exactly the helper.
let brace = src.indexOf('{', start);
let depth = 0;
let end = -1;
for (let i = brace; i < src.length; i += 1) {
  const ch = src[i];
  if (ch === '{') depth += 1;
  else if (ch === '}') {
    depth -= 1;
    if (depth === 0) { end = i + 1; break; }
  }
}
if (end < 0) {
  console.log(JSON.stringify({error: 'unbalanced-helper'}));
  process.exit(2);
}

const fnSrc = src.slice(start, end);
const helper = eval('(' + fnSrc + ')');

const scenarios = [
  {name: 'creation-order', projects: [
    {project_id: 'p3', name: 'zeta'},
    {project_id: 'p1', name: 'Alpha'},
    {project_id: 'p2', name: 'beta'},
  ]},
  {name: 'already-sorted', projects: [
    {project_id: 'a', name: 'api'},
    {project_id: 'b', name: 'build'},
    {project_id: 'c', name: 'cron'},
  ]},
  {name: 'missing-names', projects: [
    {project_id: 'x', name: 'mid'},
    {project_id: 'y'},
    {project_id: 'z', name: null},
    {project_id: 'w', name: 'alpha'},
  ]},
  {name: 'stable-duplicates', projects: [
    {project_id: 'first', name: 'same'},
    {project_id: 'second', name: 'same'},
    {project_id: 'aaa', name: 'aaa'},
  ]},
  {name: 'empty', projects: []},
];

const out = {scenarios: []};
for (const scenario of scenarios) {
  const input = scenario.projects.map(p => Object.assign({}, p));
  const inputBefore = JSON.stringify(input);
  const result = helper(input);
  out.scenarios.push({
    name: scenario.name,
    ids: result.map(p => p.project_id),
    names: result.map(p => p.name),
    inputUnchanged: JSON.stringify(input) === inputBefore,
    returnsNewArray: result !== input,
  });
}
console.log(JSON.stringify(out));
"""


def _run_helper() -> dict:
    completed = subprocess.run(
        [NODE, "-e", _DRIVER, str(SESSIONS_JS_PATH)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, (
        f"node driver failed:\nSTDOUT={completed.stdout}\nSTDERR={completed.stderr}"
    )
    payload = json.loads(completed.stdout)
    assert "error" not in payload, payload["error"]
    return {s["name"]: s for s in payload["scenarios"]}


class TestSortedProjectsForChips:
    """Behaviour of the helper that feeds the sidebar project-chip bar."""

    def test_sorts_by_name_alphabetically(self):
        result = _run_helper()["creation-order"]
        assert result["ids"] == ["p1", "p2", "p3"], (
            "chips must render alphabetically by name, not in creation order"
        )
        assert result["names"] == ["Alpha", "beta", "zeta"]

    def test_already_sorted_input_is_unchanged(self):
        result = _run_helper()["already-sorted"]
        assert result["ids"] == ["a", "b", "c"]

    def test_missing_or_null_names_do_not_throw(self):
        """`/api/projects` rows may lack a name; sort must not blow up the bar."""
        result = _run_helper()["missing-names"]
        # A missing/null name compares as the empty string, so those rows sort
        # ahead of the named ones and keep their relative input order.
        assert result["names"][:2] == [None, None]
        assert result["ids"][:2] == ["y", "z"]
        assert result["names"][2:] == ["alpha", "mid"]
        assert len(result["ids"]) == 4

    def test_sort_is_stable_for_duplicate_names(self):
        result = _run_helper()["stable-duplicates"]
        assert result["ids"] == ["aaa", "first", "second"]

    def test_empty_list(self):
        assert _run_helper()["empty"]["ids"] == []

    def test_does_not_mutate_the_shared_cache(self):
        """`_allProjects` is used for lookups elsewhere; sorting it in place
        would silently reorder color assignment and the pickers."""
        scenarios = _run_helper()
        for name, result in scenarios.items():
            assert result["inputUnchanged"], f"{name}: helper mutated its input array"
            assert result["returnsNewArray"], f"{name}: helper must return a copy"


class TestChipBarWiring:
    """Source-level wiring: the sidebar loop consumes the sorted copy."""

    def _chip_loop_region(self) -> str:
        """The project-chip bar block, from the bar element through the chips.

        Anchored on the bar element (not the "// Project chips" comment) so the
        region also covers the fixed 'All' / 'Unassigned' chips that are
        appended before the project names.
        """
        idx = SESSIONS_JS.find("bar.className='project-bar'")
        assert idx != -1, "project chip bar not found in static/sessions.js"
        return SESSIONS_JS[idx: idx + 3000]

    def test_chip_loop_iterates_sorted_projects(self):
        region = self._chip_loop_region()
        assert "_sortedProjectsForChips(_allProjects)" in region, (
            "the sidebar project-chip loop must iterate the alphabetically "
            "sorted copy (#6393)"
        )
        assert "for(const p of _allProjects)" not in region, (
            "the chip loop still iterates raw _allProjects — chips would render "
            "in creation/disk order"
        )

    def test_shared_cache_is_never_sorted_in_place(self):
        assert "_allProjects.sort(" not in SESSIONS_JS, (
            "_allProjects is the shared project cache; sort a copy instead"
        )

    def test_all_and_unassigned_chips_still_lead_the_bar(self):
        region = self._chip_loop_region()
        sorted_idx = region.find("_sortedProjectsForChips(_allProjects)")
        assert sorted_idx != -1
        assert region.find("project_chip_all") < sorted_idx, (
            "the 'All' chip must stay pinned ahead of the sorted project names"
        )
        assert region.find("project_chip_unassigned") < sorted_idx, (
            "the 'Unassigned' chip must stay pinned ahead of the sorted project names"
        )
