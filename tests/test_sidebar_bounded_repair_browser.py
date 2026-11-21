"""Real-page acceptance for compact counts and chip focus/scroll work."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.timeout(120)
def test_sidebar_scrollbar_focus_and_compact_counts(tmp_path):
    pytest.importorskip('playwright.sync_api')
    output = tmp_path / 'repair'
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tests/browser_sidebar_bounded_repair.py'), '--output', str(output)],
        cwd=ROOT, capture_output=True, text=True, timeout=100,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / 'report.json').read_text())
    assert report['cases'] == 94
    assert not report['errors']
    assert not report['failures']
    assert len({r['locale'] for r in report['results'] if r['scene'] == 'readable-count'}) == 15
    assert all(not r['failures'] for r in report['results'])
