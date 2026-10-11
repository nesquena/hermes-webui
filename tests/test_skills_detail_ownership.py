"""Profile transitions own the complete Skills DOM and async continuations."""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# All 132 browser schedules run in one item. Allow the bounded 180-second
# controller to finish (or report its own timeout) under CI's --timeout=60.
@pytest.mark.timeout(240)
def test_production_skills_detail_and_mutation_ownership():
    pytest.importorskip('playwright', reason='Python Playwright browser prerequisite is unavailable')
    # CI installs Python Playwright, whose driver includes the version-matched
    # Node runtime and JS implementation. Do not probe an undeclared npm package.
    from playwright._impl._driver import compute_driver_executable

    node, driver = compute_driver_executable()
    proc = subprocess.run([node, str(ROOT / 'tests/skills_detail_ownership.cjs'), str(ROOT),
                           str(Path(driver).parent)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr
    reports = json.loads(proc.stdout)
    assert len(reports) == 132
    for report in reports:
        if 'happy' in report:
            assert report['editing'] is report['pre'] is None, report
            assert report['profile'] == 'B', report
            expected_requests = [] if report['happy'] == 'delete-cancel' else [
                {'ordinal': report['switchOrdinal'] + 3, 'path': '/api/skills/' + report['happy'],
                 'profile': 'B'}
            ]
            assert report['mutationRequests'] == expected_requests, report
            if report['happy'] == 'toggle':
                assert report['data'][0]['disabled'] is False, report
                assert 'class="skill-toggle enabled"' in report['list'], report
            elif report['happy'] == 'save':
                assert report['mode'] == 'read', report
                assert 'saved private content' in report['body'], report
                assert ['toast', 'skill_updated'] in report['events'], report
            elif report['happy'] == 'delete-cancel':
                assert report['mode'] == 'read', report
                assert 'linked private content' in report['body'], report
            else:
                assert report['mode'] == 'empty', report
                assert report['detail'] is None, report
                assert report['body'] == '', report
                assert report['data'] == [], report
                assert ['toast', 'skill_deleted'] in report['events'], report
            continue
        if 'scenario' in report:
            if report['scenario'].startswith('panel-'):
                assert report['immediate']['data'] is report['immediate']['detail'] is None, report
                assert report['immediate']['body'] == report['immediate']['list'] == '', report
                assert report['after'] == report['before'], report
                assert report['after']['data'][0]['category'] == 'B', report
                assert report['destinationRequests'] == 1, report
                assert report['mutationsAfterSwitch'] == [], report
                continue
            assert report['after'] == report['before'], report
            if 'refetches' in report:
                applied = report['scenario'] in ('toggle', 'save', 'delete') and not report['error']
                assert report['refetches'] == (1 if applied else 0), report
            if report['scenario'].startswith('transport-'):
                assert report['fetches'] == 1, report
                assert report['mutationsAfterSwitch'] == [], report
            continue
        immediate = report['immediate']
        assert immediate['data'] is None, report
        assert immediate['detail'] is None, report
        assert immediate['pre'] is None, report
        assert immediate['editing'] is None, report
        assert immediate['mode'] == 'empty', report
        assert immediate['collapsed'] == [], report
        assert immediate['list'] == immediate['body'] == immediate['title'] == '', report
        assert immediate['bodyDisplay'] == 'none', report
        assert immediate['emptyDisplay'] == '', report
        assert all(display == 'none' for display in immediate['buttons']), report
        if report['oldFirst']:
            assert report['neutral'] == immediate, report
        assert report['after'] == report['before'], report
        assert report['after']['data'][0]['disabled'] is True, report
        expected = 'A-new' if report['returnA'] else 'B'
        assert expected + ' private content' in report['after']['body'], report
        assert report['mutationsAfterSwitch'] == [], report
