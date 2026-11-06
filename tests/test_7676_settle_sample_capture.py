#!/usr/bin/env python3
"""Regression coverage for the #7676 settle probe's IN-PAGE sample capture.

``tests/browser_settle_visibility_7676.py`` moved its settle measurements into
the page (installed before ``done`` is emitted) so a transient blank reply can
no longer be painted and repaired inside a python-side poll window.  That moved
the measurement, but left two ways for the gate to pass without ever having
measured anything (Greptile P2 discussion_r4302597979):

1. ``run()`` read ``window.__settleSamples`` as soon as ``S.busy`` cleared, but
   ``at_settle``/``first_frame`` are filled by an ``await``ed in-page sampler
   and a following ``requestAnimationFrame``.  The busy->idle edge is observed
   by python *before* the second frame has run, so ``first_frame`` is still
   ``null``; that early snapshot was kept for the whole run.
2. ``verdict()`` skipped any falsy sample (``if not r: continue``), so a sample
   that was never captured counted as a pass.  The "in-page settle sample never
   captured" error dict was built in ``run()`` but never returned, so it never
   reached ``verdict()`` at all.

So CI could go green having checked neither required frame.  These tests pin the
gate itself (``verdict()`` and the capture contract in ``run()``) -- they run in
milliseconds, so the blind spot is closed without booting a browser.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from browser_settle_visibility_7676 import verdict  # noqa: E402


PROBE = 'tests/browser_settle_visibility_7676.py'


def read_probe() -> str:
    root = os.environ.get(
        'WEBUI_TEST_ROOT',
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    with open(os.path.join(root, PROBE), encoding='utf-8') as fh:
        return fh.read()


def sample(**over):
    """A passing in-page sample, with the fields verdict() inspects."""
    out = {
        'visibleOwnerCount': 1,
        'hiddenOwnerCount': 0,
        'hiddenOwnerOnlyClasses': [],
        'hiddenLiveOnlyTurnCount': 0,
        'liveOwnsSettled': False,
        'msgCount': 2,
        'busy': False,
    }
    out.update(over)
    return out


def result(**over):
    out = {
        'runtime_checks': {'normalize': 'ok', 'finalize': 'ok'},
        'same_frame': sample(),
        'first_frame': sample(),
        'after_wait': sample(),
        'idle_probe': 'none',
        'idle_result': None,
        'idle_detail': None,
        'after_idle': None,
        'pageerrors': [],
    }
    out.update(over)
    return out


class TestProbeImportsWithoutPlaywright:
    """Importing the probe must not require Playwright (Greptile P1).

    ``verdict()`` and ``variants()`` are pure python and are imported here at
    module load. ``requirements-dev.txt`` deliberately does NOT install
    playwright — only the browser workflows do — so a clean
    ``./scripts/test.sh`` run has no playwright. A module-level
    ``from playwright.sync_api import sync_playwright`` therefore raised
    ModuleNotFoundError while *collecting* this file, which stops the whole
    test run before a single test executes.

    The browser-only import belongs at the browser entry point (``main()``).
    """

    @staticmethod
    def _import_without_playwright() -> str:
        """Import the probe in a subprocess where playwright is unimportable.

        Runs out-of-process on purpose: an import guard installed in-process
        would poison ``sys.modules`` for every other test in the session.
        """
        script = textwrap.dedent(f"""
            import sys
            sys.path.insert(0, {json.dumps(os.path.dirname(os.path.abspath(__file__)))})

            class _Block:
                def find_spec(self, name, path=None, target=None):
                    if name == 'playwright' or name.startswith('playwright.'):
                        raise ModuleNotFoundError("No module named 'playwright'")
                    return None

            sys.meta_path.insert(0, _Block())
            import browser_settle_visibility_7676 as probe
            # Pure-python surface still fully usable without a browser: verdict()
            # is the unit-tested gate, variants() is the matrix it runs over.
            bad = probe.verdict({{}})
            assert isinstance(bad, list) and all(isinstance(b, str) for b in bad), bad
            assert any(b.startswith('runtime:normalize') for b in bad), bad
            assert len(probe.variants()) == 12, probe.variants()
            print('OK')
        """)
        out = subprocess.run(
            [sys.executable, '-c', script], capture_output=True, text=True, timeout=120,
        )
        assert out.returncode == 0, f'stdout={out.stdout}\nstderr={out.stderr}'
        return out.stdout

    def test_probe_module_imports_with_playwright_absent(self):
        assert 'OK' in self._import_without_playwright()


class TestVerdictRequiresBothSamples:
    """verdict() must FAIL on a missing sample, not skip it."""

    def test_healthy_run_is_clean(self):
        assert verdict(result()) == []

    def test_missing_settle_sample_fails(self):
        bad = verdict(result(same_frame=None))
        assert any('same_frame' in b for b in bad), bad

    def test_missing_first_frame_sample_fails(self):
        bad = verdict(result(first_frame=None))
        assert any('first_frame' in b for b in bad), bad

    def test_settle_never_captured_error_fails(self):
        """run()'s 'in-page settle sample never captured' must reach verdict()."""
        res = result(
            same_frame={'error': 'in-page settle sample never captured', 'busy': False},
            first_frame=None,
            after_wait=None,
        )
        bad = verdict(res)
        assert any('never captured' in b for b in bad), bad

    def test_both_samples_missing_fails_both_labels(self):
        bad = verdict(result(same_frame=None, first_frame=None))
        assert any('same_frame' in b for b in bad), bad
        assert any('first_frame' in b for b in bad), bad

    def test_empty_dict_sample_is_still_a_missing_sample(self):
        """An empty dict is falsy -- it must not read as 'nothing to check'."""
        bad = verdict(result(first_frame={}))
        assert any('first_frame' in b for b in bad), bad


class TestOptionalSamplesStayOptional:
    """Only the two in-page samples are required; the rest are conditional."""

    def test_after_idle_omitted_without_idle_probe_is_clean(self):
        assert verdict(result(after_idle=None, idle_probe='none')) == []

    def test_after_wait_omitted_when_settle_errored_is_still_reported(self):
        res = result(
            same_frame={'error': 'in-page settle sample never captured', 'busy': False},
            after_wait=None,
        )
        bad = verdict(res)
        assert any('never captured' in b for b in bad), bad


class TestRunWaitsForBothSamples:
    """run() must wait for BOTH saved samples before reading them."""

    @staticmethod
    def _readiness_predicate() -> str:
        """Extract the JS predicate run() polls with, from the real source.

        Source-string matching is not enough here: the predicate is written as
        implicit Python string concatenation, so the JS only exists after the
        concatenation is performed. Parse it out of the AST instead, then
        execute the result so this asserts BEHAVIOR (does the gate actually
        require the second frame?) rather than formatting.
        """
        src = read_probe()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == 'evaluate'):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            js = node.args[0].value
            if isinstance(js, str) and '__settleSamples' in js and 'first_frame' in js:
                return js
        raise AssertionError(
            'run() must poll with a predicate requiring BOTH at_settle and '
            'first_frame before reading __settleSamples'
        )

    def test_readiness_predicate_requires_both_frames(self):
        predicate = self._readiness_predicate()
        cases = [
            # (at_settle, first_frame, expected_ready, why)
            (None, None, False, 'nothing captured yet'),
            ({'ok': 1}, None, False, 'settle frame only — first frame pending'),
            (None, {'ok': 1}, False, 'first frame without the settle edge'),
            ({'ok': 1}, {'ok': 1}, True, 'both captured'),
        ]
        script = textwrap.dedent(f"""
            const predicate = {predicate};
            const results = [];
            for (const [a, b] of {json.dumps([[c[0], c[1]] for c in cases])}) {{
              global.window = {{__settleSamples: {{at_settle: a, first_frame: b}}}};
              results.push(predicate());
            }}
            console.log('OK ' + JSON.stringify(results));
        """)
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert 'OK' in out.stdout, out.stdout
        got = json.loads(out.stdout.split('OK', 1)[1].strip())
        for ready, (_a, _b, want, why) in zip(got, cases, strict=True):
            assert ready is want, f'{why}: predicate returned {ready}, want {want}'

    def test_readiness_predicate_tolerates_absent_container(self):
        """A page that never got __settleSamples must read as not-ready."""
        predicate = self._readiness_predicate()
        script = textwrap.dedent(f"""
            const predicate = {predicate};
            global.window = {{}};
            console.log('OK ' + predicate());
        """)
        out = subprocess.run(
            ['node', '-e', script], capture_output=True, text=True, timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().endswith('OK false'), out.stdout

    def test_run_returns_the_settle_capture_error(self):
        """The capture-error dict must be returned, not built and dropped.

        run() builds `{'error': 'in-page settle sample never captured'}` when
        the sampler never observed the busy->idle edge; if that dict never
        reaches verdict() the failure is silently discarded.
        """
        src = read_probe()
        assert 'settled' in ast.dump(ast.parse(src)) or "'settled':" in src
        ret = re.search(r"return \{\s*'virt':.*?\n    \}", src, re.S)
        assert ret, 'run() return dict not found'
        assert "'settled':" in ret.group(0), (
            "run() must return the settle capture error so verdict() sees it"
        )

    def test_print_loop_does_not_use_a_stale_label(self):
        """main() printed 'next_frame', which is not a key of the result."""
        src = read_probe()
        labels = re.findall(r"for label in \(([^)]*)\):", src)
        assert labels, 'print loop not found'
        assert "'next_frame'" not in labels[-1], (
            "main() must not print a 'next_frame' label that run() never returns"
        )