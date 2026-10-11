"""CI entry points for real navigation and state-projection browser gates."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('driver', 'cases'), [
    ('browser_session_child_restore.py', 16),
    ('browser_session_state_projection.py', 12),
])
@pytest.mark.timeout(120)
def test_sidebar_restore_and_state_projection(tmp_path, driver, cases):
    pytest.importorskip('playwright.sync_api')
    output = tmp_path / 'report'
    result = subprocess.run([sys.executable, str(ROOT / 'tests' / driver), '--output', str(output)],
                            cwd=ROOT, capture_output=True, text=True, timeout=100)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / 'report.json').read_text())
    assert report['cases'] == len(report['results']) == cases
    assert report['failures'] == 0
    assert not report['errors']
    assert all(not scene['failures'] for scene in report['results'])
