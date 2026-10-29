"""Executed run() -> verdict() composition for the #7676 settle probe.

The sibling file tests/test_7676_settle_sample_capture.py covers ``verdict()``
and the readiness predicate as separate units. That separation is exactly the
gap this file closes: both units can be correct while their COMPOSITION passes
a run that never observed what the variant claims to prove. The maintainer's
replay used a controlled Playwright/CDP delivery seam; this file drives the real
``run()`` over an in-process fake browser, so the acquisition order, the
conditional ``after_wait``/``after_idle`` branches, the fixture counter
arithmetic behind ``idle_detail`` and the classifier all execute together.

No production code is mirrored here: ``run``, ``verdict``, ``ASSERTIONS`` and
``RUNTIME_CHECKS`` are imported from the probe itself.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import browser_settle_visibility_7676 as probe  # noqa: E402


def sample(**over):
    """A healthy in-page reading, with the fields verdict() inspects."""
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


class FakeRequest:
    """The route-handler request surface: run()'s _serve_session reads the URL."""

    def __init__(self, url):
        self.request = type('_Req', (), {'url': url})()

    def fulfill(self, **_kw):
        return None


class FakePage:
    """Enough of playwright's Page to execute run()'s real acquisition loop.

    ``schedule`` decides which of the four observations the "browser" ever
    delivers. That is the seam: run()'s waits, its ``settled``/``after_wait``
    conditional, the fixture's fetch/load counters and verdict()'s required set
    are all real code, driven by a delivery pattern we choose.
    """

    def __init__(self, schedule, idle_probe='none', idle_result=True):
        self.schedule = dict(schedule)
        self.idle_probe = idle_probe
        self.idle_result = idle_result
        self.routes: dict = {}
        self.readings = 0
        self.samples: dict = {}

    # --- playwright surface used by run() -------------------------------
    def on(self, *_a, **_k):
        pass

    def route(self, pattern, handler=None):
        if handler is not None:
            self.routes[pattern] = handler
        return lambda *a, **k: None

    def goto(self, *_a, **_k):
        pass

    def wait_for_function(self, *_a, **_k):
        return True

    def wait_for_timeout(self, *_a, **_k):
        return None

    def close(self):
        pass

    def evaluate(self, expr, arg=None):
        expr = expr if isinstance(expr, str) else ''

        if '_bootReady' in expr:                       # boot gate
            return True
        if '__settleSamples' in expr and 'requestAnimationFrame(tick)' in expr:
            self.samples = {'at_settle': None, 'first_frame': None}
            return None
        if '__settleSamples.at_settle' in expr and 'first_frame' in expr:
            self._fill('at_settle')
            self._fill('first_frame')
            return bool(self.samples.get('at_settle')
                        and self.samples.get('first_frame'))
        if 'window.__settleSamples||{}' in expr:
            return dict(self.samples)
        if expr.strip() in ('()=>S.busy', '()=>!!S.busy'):
            return self.schedule.get('busy', False)
        if 'S.session&&S.session.message_count' in expr:
            return 2
        if '_reconcileActiveSessionIdleStateFromList' in expr:
            self._reconcile()
            return self.idle_result
        if '_normalizeTransferredLiveProse' in expr:   # RUNTIME_CHECKS
            return {'normalize': 'ok', 'finalize': 'ok'}
        if expr == probe.ASSERTIONS:
            # Exact match, deliberately: the emitted final answer contains the
            # marker string, and the sampler-install expression embeds
            # ASSERTIONS verbatim. Substring matching silently consumed
            # __readings on those two and mislabelled every later reading.
            return self._assertion()
        return None

    # --- helpers --------------------------------------------------------
    def _fill(self, slot):
        """Deliver a stored settle frame, per the schedule."""
        if self.schedule.get('never_settled') or self.schedule.get('no_sampler_delivery'):
            return
        if slot == 'first_frame' and not self.schedule.get('deliver_first_frame', True):
            return
        if self.samples.get(slot) is None:
            self.samples[slot] = sample()

    def _assertion(self):
        """run() takes ASSERTIONS three times: live_before, after_wait, after_idle."""
        idx, self.readings = self.readings, self.readings + 1
        if idx == 0:
            return sample()                                   # live_before
        key = 'after_wait' if idx == 1 else 'after_idle'
        if not self.schedule.get(f'deliver_{key}', True):
            return None            # the observation was never captured
        return sample()

    def _reconcile(self):
        """Drive run()'s REAL fixture counters through the stored route handler.

        idle_detail is derived by run() from fetches/loads deltas, so the
        metadata probe and (for 'changed') the full reload are issued here
        rather than injected as a canned dict.
        """
        serve = self.routes.get('**/api/session?*')
        if serve is None:
            return
        serve(FakeRequest('http://fixture.invalid/api/session'
                          '?session_id=settle-session&messages=0'))
        if self.idle_probe == 'changed':
            serve(FakeRequest('http://fixture.invalid/api/session'
                              '?session_id=settle-session'))


class FakeBrowser:
    def __init__(self, page):
        self._page = page

    def new_context(self, **_k):
        return self

    def add_init_script(self, *_a, **_k):
        pass

    def new_page(self):
        return self._page

    def close(self):
        pass


class FakePW:
    def __init__(self, page):
        self.chromium = self
        self._page = page

    def launch(self, **_k):
        return FakeBrowser(self._page)


def drive(schedule, idle_probe='none', idle_result=True):
    """Run the REAL run() over the fake browser; return (res, verdict(res))."""
    page = FakePage(schedule, idle_probe=idle_probe, idle_result=idle_result)
    res = probe.run(FakePW(page), 'http://fixture.invalid', {}, True, True,
                    idle_probe, 4)
    return res, probe.verdict(res)


class TestRunToVerdictComposition:
    """Acquisition and classification, executed together.

    Each test names the schedule that breaks the promise the variant makes, so
    a regression is reported at the stage that actually dropped the
    observation.
    """

    def test_complete_delivery_is_clean(self):
        """Positive control: every promised observation arrives."""
        res, bad = drive({}, idle_probe='equal')
        assert res['same_frame'] and res['first_frame']
        assert res['after_wait'], 'after_wait should be delivered'
        assert res['after_idle'], 'after_idle should be delivered'
        assert bad == [], bad

    def test_never_settled_run_fails(self):
        """A run where settlement never happened must not pass."""
        res, bad = drive({'never_settled': True, 'busy': True})
        assert bad, 'a never-settled run must fail'
        assert any('never captured' in b for b in bad), bad

    def test_no_sampler_delivery_fails(self):
        """The in-page sampler recorded neither frame."""
        res, bad = drive({'no_sampler_delivery': True, 'busy': False})
        assert res['same_frame'] is None and res['first_frame'] is None
        assert bad, 'a run with no sampler delivery must fail'
        assert any('same_frame' in b for b in bad), bad
        assert any('first_frame' in b for b in bad), bad

    def test_missing_first_frame_fails(self):
        """Settle frame captured, first animation frame still pending."""
        res, bad = drive({'deliver_first_frame': False})
        assert res['first_frame'] is None
        assert bad, 'a missing first frame must fail'
        assert any('first_frame' in b for b in bad), bad

    def test_uncaptured_after_wait_fails(self):
        """The settle succeeded, so the 1500ms dwell reading is owed.

        This is the schedule the maintainer's replay proved returns
        ``verdict=[]`` on the previous head.
        """
        res, bad = drive({'deliver_after_wait': False})
        assert 'error' not in (res['settled'] or {})
        assert res['after_wait'] is None
        assert bad, 'an uncaptured after_wait must fail'
        assert any('after_wait' in b for b in bad), bad

    def test_missing_after_idle_fails_when_idle_probe_requested(self):
        """idle_probe='equal'/'changed' promises an idle reading."""
        for idle_probe in ('equal', 'changed'):
            res, bad = drive({'deliver_after_idle': False},
                             idle_probe=idle_probe)
            assert res['after_idle'] is None, idle_probe
            assert bad, f'{idle_probe}: an uncaptured after_idle must fail'
            assert any('after_idle' in b for b in bad), bad

    def test_after_idle_optional_without_idle_probe(self):
        """idle_probe='none' promises nothing, so after_idle stays optional."""
        res, bad = drive({'deliver_after_idle': False}, idle_probe='none')
        assert res['after_idle'] is None
        assert bad == [], bad

    def test_idle_detail_comes_from_real_counter_arithmetic(self):
        """Guards the seam itself: idle_detail must not be a canned dict."""
        res, bad = drive({}, idle_probe='changed')
        assert res['idle_detail']['metadataProbes'] >= 1
        assert res['idle_detail']['fullLoads'] >= 1
        assert res['idle_detail']['reconcileChanged'] is True
        assert bad == [], bad


class TestVerdictRequiredSet:
    """The required-observation contract, as a pure classifier check.

    Composed behavior is proven above; these pin the contract itself so a
    future edit cannot quietly re-optionalize a promised observation.
    """

    @staticmethod
    def _res(**over):
        out = {
            'runtime_checks': {'normalize': 'ok', 'finalize': 'ok'},
            'settled': sample(),
            'same_frame': sample(),
            'first_frame': sample(),
            'after_wait': sample(),
            'idle_probe': 'none',
            'idle_detail': None,
            'after_idle': None,
            'pageerrors': [],
        }
        out.update(over)
        return out

    def test_after_wait_is_required(self):
        assert any('after_wait' in b
                   for b in probe.verdict(self._res(after_wait=None)))
        assert any('after_wait' in b
                   for b in probe.verdict(self._res(after_wait={})))

    def test_after_idle_required_only_for_idle_probes(self):
        for idle in ('equal', 'changed'):
            bad = probe.verdict(self._res(after_idle=None, idle_probe=idle))
            assert any('after_idle' in b for b in bad), bad
        assert probe.verdict(self._res(after_idle=None,
                                       idle_probe='none')) == []

    def test_after_wait_not_double_reported_when_settle_errored(self):
        """One failure, one message: the settle error is the cause."""
        res = self._res(
            same_frame=None,
            first_frame=None,
            after_wait=None,
            settled={'error': 'in-page settle sample never captured',
                     'busy': True},
        )
        bad = probe.verdict(res)
        assert any('never captured' in b for b in bad), bad
        assert not any(b.startswith('after_wait') for b in bad), bad

    @pytest.mark.parametrize('label', ['same_frame', 'first_frame', 'after_wait'])
    def test_each_promised_observation_fails_closed(self, label):
        assert any(label in b for b in probe.verdict(self._res(**{label: None})))