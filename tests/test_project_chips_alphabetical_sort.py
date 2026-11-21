"""Regression tests for alphabetical project chip & picker sorting (#6393, PR #6577).

Covers:
- Shipped presentation helper _projectsSortedByName() extracted directly from static/sessions.js
- Case-insensitive base sensitivity collation and deterministic project_id tie-breaker
- Insertion of Zulu, alpha, Bravo, Alpha, and an accented name (Éclair)
- Source array immutability and new array reference (spread copy)
- Consumer function binding across _showBatchProjectPicker, renderSessionListFromCache, and _showProjectPicker
- Project picker refresh recompute binding
- Foreign profile filtering contract executed via extracted production helper and filter
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS_PATH = ROOT / "static" / "sessions.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _read_sessions_js() -> str:
    return SESSIONS_JS_PATH.read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        [NODE, "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_consumer_functions_bound_to_projects_sorted_by_name():
    src = _read_sessions_js()
    consumer_names = [
        "_showBatchProjectPicker",
        "renderSessionListFromCache",
        "_showProjectPicker",
    ]
    for name in consumer_names:
        body = extract_function(src, name)
        assert "for(const p of _projectsSortedByName())" in body, (
            f"{name} does not call _projectsSortedByName() in its loop"
        )
        assert "for(const p of _allProjects)" not in body, (
            f"{name} still references raw _allProjects in loop"
        )
        assert "for(const p of(_allProjects||[]))" not in body, (
            f"{name} still references fallback _allProjects loop"
        )

    # Refresh path inside _showProjectPicker recomputes visibleProjects
    show_picker_body = extract_function(src, "_showProjectPicker")
    assert "_projectsSortedByName(_allProjects)" in show_picker_body, (
        "_showProjectPicker refreshChoices does not use _projectsSortedByName(_allProjects)"
    )
    assert "visibleProjects=_allProjects.filter" not in show_picker_body, (
        "_showProjectPicker refreshChoices reverted to plain _allProjects.filter"
    )


def test_projects_sorted_by_name_ordering_and_immutability():
    src = _read_sessions_js()
    sort_helper = extract_function(src, "_projectsSortedByName")

    input_projects = [
        {"project_id": "p-zulu", "name": "Zulu"},
        {"project_id": "p-alpha-2", "name": "alpha"},
        {"project_id": "p-bravo", "name": "Bravo"},
        {"project_id": "p-alpha-1", "name": "Alpha"},
        {"project_id": "p-eclair", "name": "Éclair"},
    ]

    script = f"""
    {sort_helper}

    const input = {json.dumps(input_projects)};
    const inputBefore = JSON.stringify(input);
    const sorted = _projectsSortedByName(input);
    const inputAfter = JSON.stringify(input);

    let _allProjects = input;
    const sortedDefaultParam = _projectsSortedByName();

    console.log(JSON.stringify({{
      notMutated: inputBefore === inputAfter,
      isDifferentArray: sorted !== input,
      sortedNames: sorted.map(p => `${{p.name}}:${{p.project_id}}`),
      sortedDefaultParamNames: sortedDefaultParam.map(p => `${{p.name}}:${{p.project_id}}`),
    }}));
    """
    out = _run_node(script)

    assert out["notMutated"] is True
    assert out["isDifferentArray"] is True

    expected_order = [
        "Alpha:p-alpha-1",
        "alpha:p-alpha-2",
        "Bravo:p-bravo",
        "Éclair:p-eclair",
        "Zulu:p-zulu",
    ]
    assert out["sortedNames"] == expected_order
    assert out["sortedDefaultParamNames"] == expected_order


def test_projects_sorted_by_name_tie_break_and_base_sensitivity():
    """Verify tie-break order when names differ only by case.

    Raw collation puts lowercase 'alpha' before uppercase 'Alpha' ('Alpha'.localeCompare('alpha') === 1).
    With {sensitivity: 'base'}, both compare equal and the project_id tie-breaker decides.
    """
    src = _read_sessions_js()
    sort_helper = extract_function(src, "_projectsSortedByName")

    # input A: tie-break order (p-a < p-b) is opposite of raw collation order (alpha < Alpha)
    input_a = [
        {"name": "Alpha", "project_id": "p-a"},
        {"name": "alpha", "project_id": "p-b"},
    ]
    # input B: starts with p-b first, proving tie-break reorders even if comparator returned 0 without tie-breaker
    input_b = [
        {"name": "alpha", "project_id": "p-b"},
        {"name": "Alpha", "project_id": "p-a"},
    ]

    script = f"""
    {sort_helper}

    const sortedA = _projectsSortedByName({json.dumps(input_a)});
    const sortedB = _projectsSortedByName({json.dumps(input_b)});

    console.log(JSON.stringify({{
      idsA: sortedA.map(p => p.project_id),
      idsB: sortedB.map(p => p.project_id),
    }}));
    """
    out = _run_node(script)

    assert out["idsA"] == ["p-a", "p-b"]
    assert out["idsB"] == ["p-a", "p-b"]


def test_foreign_profile_filtering_contract():
    """Verify foreign profile filtering using extracted production helper and filter."""
    src = _read_sessions_js()
    sort_helper = extract_function(src, "_projectsSortedByName")
    picker_body = extract_function(src, "_showProjectPicker")

    match_hides = re.search(
        r"const _profileHidesProject\s*=\s*\(projProfile\)\s*=>\s*\{[\s\S]*?\};",
        picker_body,
    )
    assert match_hides, "_profileHidesProject definition not found in _showProjectPicker"
    profile_hides_def = match_hides.group(0)

    match_vis = re.search(
        r"const visibleProjects\s*=\s*_projectsSortedByName\(_allProjects\)\s*\.filter\(p\s*=>\s*!_profileHidesProject\(p\.profile\)\);",
        picker_body,
    )
    assert match_vis, "visibleProjects pipeline statement not found in _showProjectPicker"
    visible_projects_stmt = match_vis.group(0)

    projects_with_profiles = [
        {"project_id": "p-zulu", "name": "Zulu", "profile": "work"},
        {"project_id": "p-alpha-2", "name": "alpha", "profile": "work"},
        {"project_id": "p-bravo", "name": "Bravo", "profile": "personal"},
        {"project_id": "p-alpha-1", "name": "Alpha", "profile": "work"},
        {"project_id": "p-eclair", "name": "Éclair", "profile": "default"},
    ]

    script = f"""
    {sort_helper}

    const session = {{ profile: "work" }};
    const sessionProfile = session ? (session.profile || undefined) : undefined;
    {profile_hides_def}

    const _allProjects = {json.dumps(projects_with_profiles)};
    {visible_projects_stmt}

    console.log(JSON.stringify({{
      visibleLabels: visibleProjects.map(p => `${{p.name}}:${{p.project_id}}`),
    }}));
    """
    out = _run_node(script)

    # 'personal' (Bravo) dropped; 'default' (Éclair) and matching 'work' kept in sorted order
    assert out["visibleLabels"] == [
        "Alpha:p-alpha-1",
        "alpha:p-alpha-2",
        "Éclair:p-eclair",
        "Zulu:p-zulu",
    ]
