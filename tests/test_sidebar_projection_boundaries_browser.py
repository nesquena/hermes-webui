"""Exercise unread projection, invalidated tall anchors and native focus."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_sidebar_projection_boundaries(tmp_path):
    pytest.importorskip('playwright.sync_api')
    output = tmp_path / 'boundaries'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tests/browser_sidebar_projection_boundaries.py'),
         '--output', str(output)], cwd=ROOT, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / 'report.json').read_text())
    assert len(report['results']) == 14
    assert not report['errors']
    assert all(not scene['failures'] for scene in report['results'])
