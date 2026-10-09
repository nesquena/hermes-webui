"""Background resort and real new-tab fixture compatibility browser gate."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_background_resort_and_new_tab_consumption(tmp_path):
    pytest.importorskip('playwright.sync_api')
    output = tmp_path / 'background-sort'
    result = subprocess.run([sys.executable, str(ROOT / 'tests/browser_session_background_sort.py'),
                             '--output', str(output)], cwd=ROOT,
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / 'report.json').read_text())
    assert report['cases'] == 18
    assert report['failures'] == 0
    assert report['page_errors'] == []
    assert sum(r['transition'] == 'navigation' for r in report['results']) == 2
