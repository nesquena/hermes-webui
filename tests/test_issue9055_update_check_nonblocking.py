"""Regression tests for #9055 - the update check must not block the request path.

A refresh shells out to ``git fetch`` in both checkouts. On a host whose Agent
clone is large that is tens of seconds, and the POST used to hold the request
for the whole time. These tests pin the three behaviours of the fix:

* a slow refresh answers inside the wait budget and reports ``pending``;
* the refresh keeps running in the background and publishes into the cache;
* two concurrent checks never run two refreshes at once;
* the fast path still returns fresh data untouched (control).
"""

import threading
import time

from api import updates


CACHE = {'webui': None, 'agent': None, 'checked_at': 0, 'include_agent': True, 'channel': 'stable'}


def _reset_cache(monkeypatch, **overrides):
    """Start every test from an empty cache and a clean in-progress flag."""
    cache = dict(CACHE)
    cache.update(overrides)
    monkeypatch.setattr(updates, '_update_cache', dict(cache))
    monkeypatch.setattr(updates, '_check_in_progress', False)
    # Pre-fix heads have no background-refresh state at all: only reset what exists
    # so the RED lands on the timing assertion rather than on an AttributeError.
    for attribute in ('_refresh_event', '_refresh_thread'):
        if hasattr(updates, attribute):
            monkeypatch.setattr(updates, attribute, None)
    return cache


def _drain_background():
    """Join the refresh thread so a straggler cannot leak into the next test."""
    thread = getattr(updates, '_refresh_thread', None)
    if thread is not None and thread.is_alive():
        thread.join(timeout=10)


def _call(monkeypatch, **kwargs):
    """Call check_for_updates with the wait budget, tolerating the pre-fix signature.

    On the pre-fix head ``wait`` does not exist: the call is retried without it so
    the RED lands on the timing assertion instead of a TypeError.
    """
    try:
        return updates.check_for_updates(**kwargs)
    except TypeError:
        kwargs.pop('wait', None)
        return updates.check_for_updates(**kwargs)


def _slow_check_repo(seconds, seen, active):
    def fake(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        with active['lock']:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
            seen.append(name)
        try:
            time.sleep(seconds)
            return {'name': name, 'behind': 1, 'stub': True}
        finally:
            with active['lock']:
                active['now'] -= 1
    return fake


def test_slow_refresh_answers_inside_the_budget(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(
        updates, '_check_repo', _slow_check_repo(3.0, [], {'lock': threading.Lock(), 'now': 0, 'peak': 0}),
    )

    started = time.time()
    result = _call(monkeypatch, force=True, include_agent=False, wait=0.3)
    elapsed = time.time() - started

    assert elapsed < 1.5, f'the request waited {elapsed:.2f}s for a 3s refresh'
    assert result.get('pending') is True, 'a request that answered from cache must say so'
    assert result.get('cached') is True
    _drain_background()


def test_refresh_keeps_running_and_publishes(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(
        updates, '_check_repo', _slow_check_repo(0.6, [], {'lock': threading.Lock(), 'now': 0, 'peak': 0}),
    )

    started = time.time()
    _call(monkeypatch, force=True, include_agent=False, wait=0.05)
    elapsed = time.time() - started
    _drain_background()

    assert elapsed < 0.4, f'the request held the caller for {elapsed:.2f}s'
    published = updates.cached_update_status(include_agent=False)
    assert published.get('webui', {}).get('stub') is True, 'the background refresh never published'
    assert published.get('pending') is not True, 'the cache should no longer look busy'


def test_concurrent_checks_do_not_start_two_refreshes(monkeypatch):
    _reset_cache(monkeypatch)
    active = {'lock': threading.Lock(), 'now': 0, 'peak': 0}
    seen = []
    monkeypatch.setattr(updates, '_check_repo', _slow_check_repo(0.4, seen, active))

    results = {}

    def run(name, channel):
        results[name] = _call(
            monkeypatch, force=True, include_agent=False, channel=channel, wait=0.05,
        )

    threads = [
        threading.Thread(target=run, args=('stable', 'stable')),
        threading.Thread(target=run, args=('experimental', 'experimental')),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    _drain_background()

    assert active['peak'] == 1, f'{active["peak"]} refreshes ran at the same time'
    assert len(results) == 2
    # A refresh is already in flight, so the second caller must NOT queue another
    # one behind it: exactly one repo scan runs for the whole pair (the lock alone
    # would serialise two scans and pay the fetch twice).
    time.sleep(0.8)
    assert seen == ['webui'], f'the second caller started its own refresh: {seen}'


def test_fast_refresh_still_returns_fresh_data(monkeypatch):
    _reset_cache(monkeypatch)
    monkeypatch.setattr(
        updates, '_check_repo', lambda path, name, channel=updates.DEFAULT_UPDATE_CHANNEL: {'name': name, 'behind': 3, 'stub': True},
    )

    result = _call(monkeypatch, force=True, include_agent=False)

    assert result['webui']['behind'] == 3, 'the fast path must return the refresh it waited for'
    assert result.get('pending') is not True
    _drain_background()
