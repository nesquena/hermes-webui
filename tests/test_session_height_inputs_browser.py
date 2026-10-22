"""Browser regression for transient height inputs on offscreen parents."""
import json
import subprocess
import sys
from pathlib import Path

import pytest


def test_offscreen_batch_and_report_height_inputs(tmp_path):
    pytest.importorskip('playwright.sync_api')
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / 'tests/browser_session_height_inputs.py'),
         '--output', str(tmp_path / 'heights')],
        cwd=root, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / 'heights/report.json').read_text())
    assert len(report['results']) == 10
    assert not report['errors']
    assert all(not scene['failures'] for scene in report['results'])
