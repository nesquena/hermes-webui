"""Minimum-width title-group clipping through the real sidebar renderer and CSS."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.timeout(120)
def test_child_status_fits_approval_parent_title_group(tmp_path):
    pytest.importorskip('playwright.sync_api')
    output = tmp_path / 'title-group'
    result = subprocess.run([sys.executable, str(ROOT / 'tests/browser_child_title_group.py'),
                             '--output', str(output)], cwd=ROOT,
                            capture_output=True, text=True, timeout=100)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / 'report.json').read_text())
    assert report['cases'] == 1728
    assert report['failures'] == 0
    assert report['errors'] == []
