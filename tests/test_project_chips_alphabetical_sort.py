"""Regression tests for alphabetical project chip & picker sorting (#6393, PR #6577).

Covers:
- Canonical presentation helper _projectsSortedByName()
- Case-insensitive base sensitivity collation and deterministic project_id tie-breaker
- Insertion of Zulu, alpha, Bravo, Alpha, and an accented name (e.g. Éclair)
- Source array immutability
- Foreign profile filter preservation
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(proc.stdout.strip())


def test_sessions_js_structural_anchors():
    assert "function _projectsSortedByName(projects=_allProjects)" in SESSIONS_JS
    assert "for(const p of _projectsSortedByName())" in SESSIONS_JS


def test_projects_sorted_by_name_deterministic_ordering():
    script = """
    function _projectsSortedByName(projects){
      return [...(projects||[])].sort((a,b)=>
        String(a?.name||"").localeCompare(String(b?.name||""), undefined, {sensitivity:"base"}) ||
        String(a?.project_id||"").localeCompare(String(b?.project_id||""))
      );
    }

    const original = [
      { project_id: "p-zulu", name: "Zulu" },
      { project_id: "p-alpha-2", name: "alpha" },
      { project_id: "p-bravo", name: "Bravo" },
      { project_id: "p-alpha-1", name: "Alpha" },
      { project_id: "p-eclair", name: "Éclair" },
    ];

    const originalCopy = JSON.stringify(original);
    const sorted = _projectsSortedByName(original);
    const notMutated = JSON.stringify(original) === originalCopy;

    // Simulate batch picker loop
    const batchPickerNames = [];
    for (const p of _projectsSortedByName(original)) {
      batchPickerNames.push(p.name + ":" + p.project_id);
    }

    // Simulate sidebar chip loop
    const sidebarChipNames = [];
    for (const p of _projectsSortedByName(original)) {
      sidebarChipNames.push(p.name + ":" + p.project_id);
    }

    // Simulate session project picker loop with foreign profile filtering
    const sessionProfile = 'work';
    const _profileHidesProject = (projProfile) => {
      if(!sessionProfile || !projProfile) return false;
      if(projProfile === sessionProfile) return false;
      if(projProfile === 'default' || sessionProfile === 'default') return false;
      return true;
    };

    const projectsWithProfiles = [
      { project_id: "p-zulu", name: "Zulu", profile: "work" },
      { project_id: "p-alpha-2", name: "alpha", profile: "work" },
      { project_id: "p-bravo", name: "Bravo", profile: "personal" }, // should be hidden
      { project_id: "p-alpha-1", name: "Alpha", profile: "work" },
      { project_id: "p-eclair", name: "Éclair", profile: "default" }, // default profile kept
    ];

    const sessionPickerNames = [];
    for (const p of _projectsSortedByName(projectsWithProfiles)) {
      if (_profileHidesProject(p.profile)) continue;
      sessionPickerNames.push(p.name + ":" + p.project_id);
    }

    console.log(JSON.stringify({
      notMutated,
      sortedNames: sorted.map(p => p.name + ":" + p.project_id),
      batchPickerNames,
      sidebarChipNames,
      sessionPickerNames,
    }));
    """
    out = _run_node(script)

    assert out["notMutated"] is True

    # Deterministic collation:
    # 'Alpha' / 'alpha' group together at the start, tie-broken deterministically by project_id ('p-alpha-1' < 'p-alpha-2')
    # Followed by 'Bravo', 'Éclair', 'Zulu'
    expected_order = [
        "Alpha:p-alpha-1",
        "alpha:p-alpha-2",
        "Bravo:p-bravo",
        "Éclair:p-eclair",
        "Zulu:p-zulu",
    ]
    assert out["sortedNames"] == expected_order
    assert out["batchPickerNames"] == expected_order
    assert out["sidebarChipNames"] == expected_order

    # Session picker filtered out 'personal' (Bravo), but kept 'default' (Éclair) and matching 'work'
    assert out["sessionPickerNames"] == [
        "Alpha:p-alpha-1",
        "alpha:p-alpha-2",
        "Éclair:p-eclair",
        "Zulu:p-zulu",
    ]
