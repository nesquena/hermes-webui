"""Exercise grouped active anchoring and preview heights in a real browser."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_grouped_active_anchor_and_offscreen_preview_heights(tmp_path):
    pytest.importorskip('playwright.sync_api')
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tests/browser_session_virtual_geometry.py'),
         '--output', str(tmp_path / 'geometry')],
        cwd=ROOT, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'geometry/report.json').read_text())
    assert len(report['results']) == 32
    assert sum(scene['scene'] == 'selection' for scene in report['results']) == 4
    assert not report['errors']
    assert all(not scene['failures'] for scene in report['results'])


@pytest.mark.parametrize(('script', 'case', 'count'), [
    ('browser_session_virtual_geometry.py', 'projects', 4),
    # The full locale/skin/font matrix can exceed the global 60s limit on
    # shared CI runners; retain a bounded timeout without dropping scenes.
    pytest.param('browser_archived_child_label.py', None, 15588,
                 marks=pytest.mark.timeout(180)),
    ('browser_session_virtual_settle.py', None, 22),
])
def test_project_controls_and_reference_only_labels(tmp_path, script, case, count):
    pytest.importorskip('playwright.sync_api')
    command = [sys.executable, str(ROOT / 'tests' / script), '--output', str(tmp_path / 'report')]
    if case:
        command += ['--case', case]
    driver_timeout = 150 if script == 'browser_archived_child_label.py' else 90
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=driver_timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'report/report.json').read_text())
    assert len(report['results']) == count
    assert not report['errors']
    assert all(not scene['failures'] for scene in report['results'])
