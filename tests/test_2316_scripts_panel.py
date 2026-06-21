"""Tests for #2316: Scripts panel — list and raw endpoint for ~/.hermes/scripts/."""

import json
import os
from pathlib import Path
import shutil
import urllib.error
import urllib.request

import pytest

from tests.conftest import TEST_STATE_DIR, TEST_BASE

pytestmark = pytest.mark.usefixtures("test_server")
PANELS = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")


def _clear_scripts_dir():
    """Clear the scripts directory before test."""
    scripts_dir = TEST_STATE_DIR / "scripts"
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir)


def test_scripts_list_empty():
    """GET /api/scripts/list should return empty array if directory doesn't exist."""
    _clear_scripts_dir()
    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())
    assert data["scripts"] == []


def test_scripts_list_with_python_and_shell():
    """GET /api/scripts/list should return .py and .sh files with docstrings."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create a Python script with a docstring
    py_script = scripts_dir / "hello.py"
    py_script.write_text(
        '"""Say hello to the user."""\nprint("Hello world")\n',
        encoding="utf-8"
    )

    # Create a shell script with leading comments
    sh_script = scripts_dir / "backup.sh"
    sh_script.write_text(
        "#!/bin/bash\n# Backup the project\n# Run this daily\ntar -czf backup.tar.gz .\n",
        encoding="utf-8"
    )

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert len(data["scripts"]) == 2
    scripts_by_name = {s["name"]: s for s in data["scripts"]}

    assert "hello.py" in scripts_by_name
    assert scripts_by_name["hello.py"]["description"] == "Say hello to the user."

    assert "backup.sh" in scripts_by_name
    assert scripts_by_name["backup.sh"]["description"] == "Backup the project Run this daily"


def test_scripts_list_filters_non_script_files():
    """GET /api/scripts/list should ignore non-script file types."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create various files
    (scripts_dir / "script.py").write_text('"""A script."""\npass', encoding="utf-8")
    (scripts_dir / "readme.txt").write_text("Not a script", encoding="utf-8")
    (scripts_dir / "config.json").write_text("{}", encoding="utf-8")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert len(data["scripts"]) == 1
    assert data["scripts"][0]["name"] == "script.py"


def test_scripts_list_skips_symlink_escape():
    """GET /api/scripts/list must not follow a symlinked entry outside scripts/."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    outside = TEST_STATE_DIR / "outside-secret.py"
    outside.write_text('"""Outside."""\npass\n', encoding="utf-8")

    link = scripts_dir / "leak.py"
    try:
        os.symlink(str(outside), str(link))
    except (OSError, NotImplementedError):
        pytest.skip("platform does not support symlinks")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    assert data["scripts"] == []


def test_scripts_raw_returns_source():
    """GET /api/scripts/raw?path=<name> should return file source."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    content = "#!/bin/bash\necho 'test'\n"
    (scripts_dir / "test.sh").write_text(content, encoding="utf-8")

    url = TEST_BASE + "/api/scripts/raw?path=test.sh"
    with urllib.request.urlopen(url, timeout=5) as r:
        data = json.loads(r.read())

    assert data["name"] == "test.sh"
    assert data["source"] == content


def test_scripts_raw_path_traversal_blocked():
    """GET /api/scripts/raw?path=../../../etc/passwd should return 400."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    url = TEST_BASE + "/api/scripts/raw?path=../../../etc/passwd"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_missing_path_param():
    """GET /api/scripts/raw without ?path should return 400."""
    _clear_scripts_dir()
    url = TEST_BASE + "/api/scripts/raw"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 400


def test_scripts_raw_nonexistent_file():
    """GET /api/scripts/raw?path=nonexistent should return 404."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    url = TEST_BASE + "/api/scripts/raw?path=nonexistent.py"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)

    assert exc_info.value.code == 404


def test_scripts_list_returns_sorted_order():
    """GET /api/scripts/list should return scripts in alphabetical order."""
    _clear_scripts_dir()
    scripts_dir = TEST_STATE_DIR / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    # Create scripts in non-alphabetical order
    for name in ["zebra.sh", "apple.py", "middle.bash"]:
        (scripts_dir / name).write_text("#!/bin/bash\n# Script\n", encoding="utf-8")

    with urllib.request.urlopen(TEST_BASE + "/api/scripts/list", timeout=5) as r:
        data = json.loads(r.read())

    names = [s["name"] for s in data["scripts"]]
    assert names == ["apple.py", "middle.bash", "zebra.sh"]


def test_profile_switch_invalidates_scripts_cache_before_panel_reload():
    """switchToProfile() must clear the scripts cache before reloading panels."""
    switch_idx = PANELS.find("async function switchToProfile(name) {")
    assert switch_idx != -1, "switchToProfile() not found in panels.js"
    panel_reload_idx = PANELS.find("await _profileSwitchPanelLoad();", switch_idx)
    assert panel_reload_idx != -1, "_profileSwitchPanelLoad() call not found in switchToProfile()"
    switch_block = PANELS[switch_idx:panel_reload_idx + len("await _profileSwitchPanelLoad();")]

    assert "_scriptsData = null;" in switch_block, (
        "Profile switches must invalidate _scriptsData before reloading panel content, "
        "or the Scripts subtab can keep showing the prior profile's list."
    )


def test_profile_switch_reloads_scripts_subtab_instead_of_crons():
    """_profileSwitchPanelLoad() must reload Scripts when Tasks is on the scripts subtab."""
    fn_start = PANELS.find("async function _profileSwitchPanelLoad(){")
    assert fn_start != -1, "_profileSwitchPanelLoad() not found in panels.js"
    fn_end = PANELS.find("\n}\n\nfunction _refreshProfileSwitchBackground", fn_start)
    assert fn_end != -1, "Could not isolate _profileSwitchPanelLoad() body"
    fn = PANELS[fn_start:fn_end]

    assert "if (_currentPanel === 'tasks' && _tasksSubtab === 'scripts') await loadScripts();" in fn, (
        "When the Tasks panel is showing the Scripts subtab, a profile switch must refetch "
        "scripts instead of leaving the previous profile's cached list in place."
    )
    assert "else if (_currentPanel === 'tasks') await loadCrons();" in fn, (
        "Jobs subtab reload behavior must stay intact when the Scripts-specific branch is added."
    )


def test_scripts_panel_caches_loaded_source_on_the_script_record():
    """Expanded source must persist on `s` so rerenders don't blank loaded cards."""
    render_idx = PANELS.find("function _renderScriptsList(scripts) {")
    assert render_idx != -1, "_renderScriptsList() not found in panels.js"
    loaded_idx = PANELS.find("s._loaded = true;", render_idx)
    assert loaded_idx != -1, "scripts load success path not found in _renderScriptsList()"
    render_block = PANELS[render_idx:loaded_idx + len("s._loaded = true;")]

    assert "s.source = r.source || '';" in render_block, (
        "Loaded script source must be written back to `s.source`, or rerendering the cards "
        "after a panel switch leaves an empty code block with `_loaded` already set."
    )
    assert "sourceEl.querySelector('code').textContent = s.source;" in render_block, (
        "The DOM update should read from the cached `s.source` value so the render path "
        "and the persisted script record stay in sync."
    )
