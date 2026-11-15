"""Regression tests for #9056 - pending-result contract in browser callers and queue distinct keys.

The maintainer review on #9055 requires:
1. Distinct (channel, include_agent) keys arriving while a refresh is in flight must
   be enqueued and served in sequence, never absorbed or dropped.
2. The single-flight flag must be handed off directly to the chained worker.
3. Pending payloads returned to clients must reflect the requested key and never
   cross-contaminate with the running key's targets (webui: None, agent: None, stale_key: True).
4. Manual caller (checkUpdatesNow in static/panels.js) and startup caller (static/boot.js)
   must poll completion when receiving pending: true, render/banner only from completed data,
   and never treat pending as final (e.g. settings_up_to_date or premature checked marker).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from api import updates
from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS_PATH = ROOT / "static" / "panels.js"
BOOT_JS_PATH = ROOT / "static" / "boot.js"
NODE = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"

CACHE = {
    'webui': None,
    'agent': None,
    'checked_at': 0,
    'include_agent': True,
    'channel': 'stable',
}


def _reset_cache(monkeypatch, **overrides):
    """Start every test from an empty cache and clean queue/progress state."""
    cache = dict(CACHE)
    cache.update(overrides)
    monkeypatch.setattr(updates, '_update_cache', dict(cache))
    monkeypatch.setattr(updates, '_check_in_progress', False)
    if hasattr(updates, '_running_key'):
        monkeypatch.setattr(updates, '_running_key', None)
    if hasattr(updates, '_refresh_queue'):
        monkeypatch.setattr(updates, '_refresh_queue', [])
    for attribute in ('_refresh_event', '_refresh_thread'):
        if hasattr(updates, attribute):
            monkeypatch.setattr(updates, attribute, None)
    return cache


def _drain_background():
    """Join all chained background refresh threads until the queue is completely drained."""
    deadline = time.time() + 10
    while time.time() < deadline:
        thread = getattr(updates, '_refresh_thread', None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.1, deadline - time.time()))
        elif not getattr(updates, '_check_in_progress', False):
            break
        else:
            time.sleep(0.01)


def _call(monkeypatch, **kwargs):
    """Call check_for_updates with wait budget, tolerating pre-fix signature."""
    try:
        return updates.check_for_updates(**kwargs)
    except TypeError:
        kwargs.pop('wait', None)
        return updates.check_for_updates(**kwargs)


def _extract_startup_block(boot_js: str) -> str:
    start_marker = "// Non-blocking update check (fire-and-forget, once per tab session)"
    start = boot_js.find(start_marker)
    assert start >= 0, "startup check block not found in boot.js"
    end_marker = "const _bootActiveProfileUnauthRedirectBudget"
    end = boot_js.find(end_marker, start)
    assert end >= 0, "end of startup check block not found in boot.js"
    return boot_js[start:end]


def _run_node(script: str) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
        temp_path = f.name
    try:
        proc = subprocess.run(
            [NODE, temp_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return json.loads(proc.stdout)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


# ── Test 1: Distinct channel key arriving mid-flight is queued and served ─────


def test_distinct_channel_key_arriving_midflight_is_queued_and_served(monkeypatch):
    _reset_cache(monkeypatch)
    active = {'lock': threading.Lock(), 'now': 0, 'peak': 0}
    seen = []
    barrier = threading.Barrier(2)
    ready = threading.Event()

    def fake_check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        with active['lock']:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
            seen.append((name, channel))
        if channel == 'stable':
            ready.set()
            barrier.wait(timeout=5)
        try:
            return {'name': name, 'behind': 1, 'channel': channel, 'stub': True}
        finally:
            with active['lock']:
                active['now'] -= 1

    monkeypatch.setattr(updates, '_check_repo', fake_check)

    res1 = {}

    def run_stable():
        res1['out'] = _call(monkeypatch, force=True, include_agent=False, channel='stable', wait=0.1)

    t1 = threading.Thread(target=run_stable)
    t1.start()
    assert ready.wait(timeout=5), 'stable worker never started'

    # While stable is in flight, experimental arrives:
    res2 = _call(monkeypatch, force=True, include_agent=False, channel='experimental', wait=0.05)
    assert res2.get('pending') is True
    assert res2.get('channel') == 'experimental'
    assert res2.get('webui') is None
    assert res2.get('agent') is None
    assert res2.get('stale_key') is True
    assert res2.get('pending_key') == {'channel': 'experimental', 'include_agent': False}

    barrier.wait(timeout=5)
    t1.join(timeout=10)
    _drain_background()

    # After chain drains, cache holds the experimental payload:
    published = updates.cached_update_status(channel='experimental', include_agent=False)
    assert published.get('channel') == 'experimental'
    assert published.get('webui', {}).get('stub') is True
    assert published.get('webui', {}).get('channel') == 'experimental'
    assert published.get('pending') is not True

    # Plus a later force=False poll answers it non-pending:
    poll_result = updates.check_for_updates(force=False, channel='experimental', include_agent=False)
    assert poll_result.get('pending') is not True
    assert poll_result.get('channel') == 'experimental'
    assert poll_result['webui']['stub'] is True


# ── Test 2: Distinct include_agent key arriving mid-flight is queued and served ─


def test_distinct_include_agent_key_arriving_midflight_is_queued_and_served(monkeypatch):
    _reset_cache(monkeypatch)
    active = {'lock': threading.Lock(), 'now': 0, 'peak': 0}
    seen = []
    barrier = threading.Barrier(2)
    ready = threading.Event()

    def fake_check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        with active['lock']:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
            seen.append((name, channel))
        if name == 'webui' and not ready.is_set():
            ready.set()
            barrier.wait(timeout=5)
        try:
            return {'name': name, 'behind': 1, 'channel': channel, 'stub': True}
        finally:
            with active['lock']:
                active['now'] -= 1

    monkeypatch.setattr(updates, '_check_repo', fake_check)

    res1 = {}

    def run_agent_true():
        res1['out'] = _call(monkeypatch, force=True, include_agent=True, channel='stable', wait=0.1)

    t1 = threading.Thread(target=run_agent_true)
    t1.start()
    assert ready.wait(timeout=5), 'first worker never started'

    # While include_agent=True is in flight, include_agent=False arrives:
    res2 = _call(monkeypatch, force=True, include_agent=False, channel='stable', wait=0.05)
    assert res2.get('pending') is True
    assert res2.get('channel') == 'stable'
    assert res2.get('include_agent') is False
    assert res2.get('webui') is None
    assert res2.get('agent') is None
    assert res2.get('stale_key') is True
    assert res2.get('pending_key') == {'channel': 'stable', 'include_agent': False}

    barrier.wait(timeout=5)
    t1.join(timeout=10)
    _drain_background()

    # After chain drains, cache holds the include_agent=False payload:
    published = updates.cached_update_status(channel='stable', include_agent=False)
    assert published.get('include_agent') is False
    assert published.get('agent', {}).get('ignored') is True
    assert published.get('pending') is not True

    # Plus a later force=False poll answers it non-pending:
    poll_result = updates.check_for_updates(force=False, channel='stable', include_agent=False)
    assert poll_result.get('pending') is not True
    assert poll_result.get('include_agent') is False


# ── Test 3: Pending answer never carries running key's targets ────────────────


def test_pending_answer_never_carries_running_key_targets(monkeypatch):
    _reset_cache(
        monkeypatch,
        channel='stable',
        include_agent=True,
        webui={'name': 'webui', 'behind': 5, 'sha': 'target_sha'},
        agent={'name': 'agent', 'behind': 2, 'sha': 'target_agent_sha'},
        checked_at=time.time() - 100,
    )
    ready = threading.Event()
    barrier = threading.Barrier(2)

    def slow_check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        ready.set()
        barrier.wait(timeout=5)
        return {'name': name, 'behind': 1, 'stub': True}

    monkeypatch.setattr(updates, '_check_repo', slow_check)

    t = threading.Thread(target=lambda: _call(monkeypatch, force=True, channel='stable', include_agent=True, wait=0.1))
    t.start()
    assert ready.wait(timeout=5)

    pending_ans = _call(monkeypatch, force=True, channel='experimental', include_agent=True, wait=0.05)

    assert pending_ans.get('pending') is True
    assert pending_ans.get('pending_key') == {'channel': 'experimental', 'include_agent': True}
    assert pending_ans.get('channel') == 'experimental'
    assert pending_ans.get('include_agent') is True
    assert pending_ans.get('webui') is None
    assert pending_ans.get('agent') is None
    assert pending_ans.get('stale_key') is True

    barrier.wait(timeout=5)
    t.join(timeout=10)
    _drain_background()


# ── Test 4: Control - fresh matching cache answers without pending ───────────


def test_fresh_matching_cache_answers_without_pending(monkeypatch):
    _reset_cache(
        monkeypatch,
        channel='stable',
        include_agent=True,
        webui={'name': 'webui', 'behind': 0},
        agent={'name': 'agent', 'behind': 0},
        checked_at=time.time(),
    )
    scans = []
    monkeypatch.setattr(updates, '_check_repo', lambda *args, **kwargs: scans.append(args))

    result = updates.check_for_updates(force=False, channel='stable', include_agent=True)

    assert result.get('pending') is not True
    assert result.get('cached') is True
    assert len(scans) == 0, 'control: fresh matching cache must perform zero scans'


# ── Test 5: Three distinct keys scheduled together ───────────────────────────


def test_three_distinct_keys_scheduled_together(monkeypatch):
    _reset_cache(monkeypatch)
    active = {'lock': threading.Lock(), 'now': 0, 'peak': 0}
    seen = []
    lock = threading.Lock()

    def fake_check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        with active['lock']:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
        with lock:
            seen.append((name, channel))
        try:
            time.sleep(0.15)
            return {'name': name, 'behind': 1, 'channel': channel, 'stub': True}
        finally:
            with active['lock']:
                active['now'] -= 1

    monkeypatch.setattr(updates, '_check_repo', fake_check)

    threads = [
        threading.Thread(target=lambda: _call(monkeypatch, force=True, channel='stable', include_agent=True, wait=0.05)),
        threading.Thread(target=lambda: _call(monkeypatch, force=True, channel='experimental', include_agent=True, wait=0.05)),
        threading.Thread(target=lambda: _call(monkeypatch, force=True, channel='stable', include_agent=False, wait=0.05)),
    ]
    for t in threads:
        t.start()
        time.sleep(0.02)
    for t in threads:
        t.join(timeout=10)
    _drain_background()

    assert active['peak'] == 1, f'{active["peak"]} scans ran concurrently'
    channels_seen = [c for name, c in seen]
    assert 'experimental' in channels_seen, 'experimental key was never served'
    assert channels_seen.count('stable') >= 3, 'both stable keys were not served'
    assert len(seen) == 5, f'expected 5 repo scans for the 3 keys, got {len(seen)}: {seen}'


# ── Test 6: Manual caller - pending first then completed ─────────────────────


def test_manual_caller_pending_first_then_completed():
    panels_js = PANELS_JS_PATH.read_text(encoding="utf-8")
    extracted_fn = extract_function(panels_js, "checkUpdatesNow", prefix="async function")

    harness = f"""
const requests = [];
let getCount = 0;
async function api(url, opts) {{
  requests.push({{ url, method: opts ? opts.method : 'GET' }});
  if (opts && opts.method === 'POST') {{
    return {{
      pending: true,
      cached: true,
      channel: 'stable',
      include_agent: true,
      webui: null,
      agent: null,
      stale_key: true,
    }};
  }}
  getCount++;
  return {{
    pending: false,
    cached: false,
    channel: 'stable',
    include_agent: true,
    webui: {{ name: 'webui', behind: 2 }},
    agent: {{ name: 'agent', behind: 0 }},
  }};
}}
const elements = {{
  btnCheckUpdatesNow: {{ disabled: false }},
  checkUpdatesLabel: {{ textContent: '' }},
  checkUpdatesSpinner: {{ style: {{ display: 'none' }} }},
  checkUpdatesStatus: {{ textContent: '', style: {{ color: '' }} }},
}};
function $(id) {{ return elements[id] || null; }}
function t(k, arg) {{
  if (k === 'settings_updates_available_targets') return 'Updates available: ' + arg;
  if (k === 'settings_up_to_date') return 'Up to date';
  if (k === 'settings_checking') return 'Checking...';
  if (k === 'settings_check_now') return 'Check now';
  return k;
}}
const bannerCalls = [];
function _showUpdateBanner(d, gen) {{ bannerCalls.push({{ d, gen }}); }}
const window = {{ _updateRecoveryGeneration: 0 }};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_fn}

(async () => {{
  await checkUpdatesNow();
  console.log(JSON.stringify({{
    requestCount: requests.length,
    requests,
    statusText: elements.checkUpdatesStatus.textContent,
    statusColor: elements.checkUpdatesStatus.style.color,
    spinnerDisplay: elements.checkUpdatesSpinner.style.display,
    btnDisabled: elements.btnCheckUpdatesNow.disabled,
    bannerCalls: bannerCalls.length,
  }}));
}})();
"""
    result = _run_node(harness)
    assert result['requestCount'] >= 2, f"expected at least 2 requests, got {result['requestCount']}"
    assert 'Updates available' in result['statusText'], f"expected updates available status, got {result['statusText']}"
    assert 'Up to date' not in result['statusText']
    assert result['spinnerDisplay'] == 'none', "spinner must be hidden at the end"
    assert result['bannerCalls'] == 1, "update banner must fire from completed payload"


# ── Test 7: Manual caller with every answer pending ──────────────────────────


def test_manual_caller_every_answer_pending():
    panels_js = PANELS_JS_PATH.read_text(encoding="utf-8")
    extracted_fn = extract_function(panels_js, "checkUpdatesNow", prefix="async function")

    harness = f"""
const requests = [];
async function api(url, opts) {{
  requests.push({{ url, method: opts ? opts.method : 'GET' }});
  return {{
    pending: true,
    cached: true,
    channel: 'stable',
    include_agent: true,
    webui: null,
    agent: null,
    stale_key: true,
  }};
}}
const elements = {{
  btnCheckUpdatesNow: {{ disabled: false }},
  checkUpdatesLabel: {{ textContent: '' }},
  checkUpdatesSpinner: {{ style: {{ display: 'none' }} }},
  checkUpdatesStatus: {{ textContent: '', style: {{ color: '' }} }},
}};
function $(id) {{ return elements[id] || null; }}
function t(k, arg) {{
  if (k === 'settings_updates_available_targets') return 'Updates available: ' + arg;
  if (k === 'settings_up_to_date') return 'Up to date';
  if (k === 'settings_checking') return 'Checking...';
  if (k === 'settings_check_now') return 'Check now';
  return k;
}}
const bannerCalls = [];
function _showUpdateBanner(d, gen) {{ bannerCalls.push({{ d, gen }}); }}
const window = {{ _updateRecoveryGeneration: 0 }};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_fn}

(async () => {{
  await checkUpdatesNow();
  console.log(JSON.stringify({{
    requestCount: requests.length,
    statusText: elements.checkUpdatesStatus.textContent,
    statusColor: elements.checkUpdatesStatus.style.color,
    spinnerDisplay: elements.checkUpdatesSpinner.style.display,
    btnDisabled: elements.btnCheckUpdatesNow.disabled,
    bannerCalls: bannerCalls.length,
  }}));
}})();
"""
    result = _run_node(harness)
    assert 'Up to date' not in result['statusText'], "pending status must never render settings_up_to_date"
    assert result['bannerCalls'] == 0, "no banner may fire from a pending payload"
    assert result['spinnerDisplay'] == 'none', "spinner must be hidden at the end"
    assert result['btnDisabled'] is False, "button must be re-enabled after bound expires"


# ── Test 8: Startup caller - pending first then completed ────────────────────


def test_startup_caller_pending_first_then_completed():
    boot_js = BOOT_JS_PATH.read_text(encoding="utf-8")
    extracted_block = _extract_startup_block(boot_js)

    harness = f"""
const requests = [];
async function api(url, opts) {{
  requests.push({{ url, method: opts ? opts.method : 'GET' }});
  if (opts && opts.method === 'POST') {{
    return {{
      pending: true,
      cached: true,
      channel: 'stable',
      include_agent: true,
      webui: null,
      agent: null,
      stale_key: true,
    }};
  }}
  return {{
    pending: false,
    cached: false,
    channel: 'stable',
    include_agent: true,
    webui: {{ name: 'webui', behind: 1 }},
    agent: {{ name: 'agent', behind: 0 }},
  }};
}}
const storage = {{}};
const sessionStorage = {{
  getItem: k => storage[k] || null,
  setItem: (k, v) => {{ storage[k] = String(v); }},
  removeItem: k => {{ delete storage[k]; }},
}};
const bannerCalls = [];
function _showUpdateBanner(d, gen) {{ bannerCalls.push({{ d, gen }}); }}
const location = {{ search: '' }};
const _bootSettings = {{ check_for_updates: true }};
const window = {{ _updateRecoveryGeneration: 0 }};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_block}

(async () => {{
  for (let i = 0; i < 30; i++) {{
    await new Promise(r => setImmediate(r));
  }}
  console.log(JSON.stringify({{
    requestCount: requests.length,
    requests,
    marker: sessionStorage.getItem('hermes-update-checked'),
    bannerCalls: bannerCalls.length,
  }}));
}})();
"""
    result = _run_node(harness)
    assert result['requestCount'] >= 2, f"expected at least 2 requests, got {result['requestCount']}"
    assert result['marker'] == '1', "hermes-update-checked must be set after completion"
    assert result['bannerCalls'] == 1, "update banner must fire from completed payload"


# ── Test 9: Startup caller with only pending answers ─────────────────────────


def test_startup_caller_only_pending_answers():
    boot_js = BOOT_JS_PATH.read_text(encoding="utf-8")
    extracted_block = _extract_startup_block(boot_js)

    harness = f"""
const requests = [];
async function api(url, opts) {{
  requests.push({{ url, method: opts ? opts.method : 'GET' }});
  return {{
    pending: true,
    cached: true,
    channel: 'stable',
    include_agent: true,
    webui: null,
    agent: null,
    stale_key: true,
  }};
}}
const storage = {{}};
const sessionStorage = {{
  getItem: k => storage[k] || null,
  setItem: (k, v) => {{ storage[k] = String(v); }},
  removeItem: k => {{ delete storage[k]; }},
}};
const bannerCalls = [];
function _showUpdateBanner(d, gen) {{ bannerCalls.push({{ d, gen }}); }}
const location = {{ search: '' }};
const _bootSettings = {{ check_for_updates: true }};
const window = {{ _updateRecoveryGeneration: 0 }};
setTimeout = (fn, ms) => setImmediate(fn);

{extracted_block}

(async () => {{
  for (let i = 0; i < 30; i++) {{
    await new Promise(r => setImmediate(r));
  }}
  console.log(JSON.stringify({{
    requestCount: requests.length,
    marker: sessionStorage.getItem('hermes-update-checked'),
    bannerCalls: bannerCalls.length,
  }}));
}})();
"""
    result = _run_node(harness)
    assert result['marker'] is None, "hermes-update-checked must remain unset after bound expires"
    assert result['bannerCalls'] == 0, "no banner may fire when all answers are pending"


# ── Test 10: Retain completed answers per channel across queued runs ─────────


def test_completed_answers_retained_per_channel_after_queue_drain(monkeypatch):
    """Case 1: stable+agent enabled published first, experimental queued.

    After queue drain, both completed keys must be retained: cached_update_status
    and check_for_updates must serve each key's distinct answer without starting
    any new repo scans.
    """
    _reset_cache(monkeypatch)
    active = {'lock': threading.Lock(), 'now': 0, 'peak': 0}
    scans = []
    barrier = threading.Barrier(2)
    ready = threading.Event()

    def fake_check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        with active['lock']:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
            scans.append((name, channel))
        if channel == 'stable' and not ready.is_set():
            ready.set()
            barrier.wait(timeout=5)
        try:
            behind = 1 if channel == 'stable' else 3
            return {'name': name, 'behind': behind, 'channel': channel, 'stub': True}
        finally:
            with active['lock']:
                active['now'] -= 1

    monkeypatch.setattr(updates, '_check_repo', fake_check)

    res1 = {}

    def run_stable():
        res1['out'] = _call(monkeypatch, force=True, include_agent=True, channel='stable', wait=0.1)

    t1 = threading.Thread(target=run_stable)
    t1.start()
    assert ready.wait(timeout=5), 'stable worker never started'

    # While stable is in flight, experimental arrives:
    res2 = _call(monkeypatch, force=True, include_agent=True, channel='experimental', wait=0.05)
    assert res2.get('pending') is True
    assert res2.get('channel') == 'experimental'
    assert res2.get('webui') is None
    assert res2.get('agent') is None
    assert res2.get('stale_key') is True

    barrier.wait(timeout=5)
    t1.join(timeout=10)
    _drain_background()

    # 4 scans during background refresh: (webui, stable), (agent, default), (webui, experimental), (agent, default)
    assert len(scans) == 4
    drain_scans = len(scans)

    # 1. Consume stable key: must serve completed stable answer, not pending/stale_key
    cached_stable = updates.cached_update_status(channel='stable', include_agent=True)
    assert cached_stable.get('pending') is not True
    assert cached_stable.get('channel') == 'stable'
    assert cached_stable.get('include_agent') is True
    assert cached_stable.get('webui', {}).get('behind') == 1
    assert cached_stable.get('webui', {}).get('channel') == 'stable'
    assert cached_stable.get('agent', {}).get('behind') == 1
    assert cached_stable.get('agent', {}).get('ignored') is not True

    polled_stable = updates.check_for_updates(force=False, channel='stable', include_agent=True)
    assert polled_stable.get('pending') is not True
    assert polled_stable.get('channel') == 'stable'
    assert polled_stable.get('include_agent') is True
    assert polled_stable.get('webui', {}).get('behind') == 1
    assert polled_stable.get('agent', {}).get('behind') == 1

    # 2. Consume experimental key: must serve completed experimental answer
    cached_exp = updates.cached_update_status(channel='experimental', include_agent=True)
    assert cached_exp.get('pending') is not True
    assert cached_exp.get('channel') == 'experimental'
    assert cached_exp.get('include_agent') is True
    assert cached_exp.get('webui', {}).get('behind') == 3
    assert cached_exp.get('webui', {}).get('channel') == 'experimental'
    assert cached_exp.get('agent', {}).get('behind') == 1

    polled_exp = updates.check_for_updates(force=False, channel='experimental', include_agent=True)
    assert polled_exp.get('pending') is not True
    assert polled_exp.get('channel') == 'experimental'
    assert polled_exp.get('include_agent') is True
    assert polled_exp.get('webui', {}).get('behind') == 3

    # Assert no new repo scans were triggered by the polls
    assert len(scans) == drain_scans, f'polls triggered {len(scans) - drain_scans} unexpected scans'


# ── Test 11: Retain completed answers per include_agent across queued runs ────


def test_completed_answers_retained_per_include_agent_after_queue_drain(monkeypatch):
    """Case 2: stable+agent disabled queued behind stable+agent enabled.

    After queue drain, both completed keys must be retained: the enabled key
    must carry the real Agent payload (not the disabled key's ignored payload),
    the disabled key must carry the ignored payload, and neither poll may start
    a new refresh.
    """
    _reset_cache(monkeypatch)
    active = {'lock': threading.Lock(), 'now': 0, 'peak': 0}
    scans = []
    barrier = threading.Barrier(2)
    ready = threading.Event()

    def fake_check(path, name, channel=updates.DEFAULT_UPDATE_CHANNEL):
        with active['lock']:
            active['now'] += 1
            active['peak'] = max(active['peak'], active['now'])
            scans.append((name, channel))
        if name == 'webui' and not ready.is_set():
            ready.set()
            barrier.wait(timeout=5)
        try:
            return {'name': name, 'behind': 5, 'channel': channel, 'stub': True}
        finally:
            with active['lock']:
                active['now'] -= 1

    monkeypatch.setattr(updates, '_check_repo', fake_check)

    res1 = {}

    def run_agent_enabled():
        res1['out'] = _call(monkeypatch, force=True, include_agent=True, channel='stable', wait=0.1)

    t1 = threading.Thread(target=run_agent_enabled)
    t1.start()
    assert ready.wait(timeout=5), 'first worker never started'

    # While include_agent=True is in flight, include_agent=False arrives:
    res2 = _call(monkeypatch, force=True, include_agent=False, channel='stable', wait=0.05)
    assert res2.get('pending') is True
    assert res2.get('channel') == 'stable'
    assert res2.get('include_agent') is False
    assert res2.get('webui') is None
    assert res2.get('agent') is None
    assert res2.get('stale_key') is True

    barrier.wait(timeout=5)
    t1.join(timeout=10)
    _drain_background()

    # Enabled did 2 scans (webui + agent), disabled did 1 scan (webui only)
    assert len(scans) == 3
    drain_scans = len(scans)

    # 1. Consume enabled key: must carry the REAL Agent payload, NOT ignored
    cached_enabled = updates.cached_update_status(channel='stable', include_agent=True)
    assert cached_enabled.get('pending') is not True
    assert cached_enabled.get('include_agent') is True
    assert cached_enabled.get('agent', {}).get('behind') == 5
    assert cached_enabled.get('agent', {}).get('ignored') is not True
    assert cached_enabled.get('webui', {}).get('behind') == 5

    polled_enabled = updates.check_for_updates(force=False, channel='stable', include_agent=True)
    assert polled_enabled.get('pending') is not True
    assert polled_enabled.get('include_agent') is True
    assert polled_enabled.get('agent', {}).get('behind') == 5
    assert polled_enabled.get('agent', {}).get('ignored') is not True

    # 2. Consume disabled key: must carry the ignored Agent payload
    cached_disabled = updates.cached_update_status(channel='stable', include_agent=False)
    assert cached_disabled.get('pending') is not True
    assert cached_disabled.get('include_agent') is False
    assert cached_disabled.get('agent', {}).get('ignored') is True
    assert cached_disabled.get('webui', {}).get('behind') == 5

    polled_disabled = updates.check_for_updates(force=False, channel='stable', include_agent=False)
    assert polled_disabled.get('pending') is not True
    assert polled_disabled.get('include_agent') is False
    assert polled_disabled.get('agent', {}).get('ignored') is True

    # Assert no new repo scans were triggered by the polls
    assert len(scans) == drain_scans, f'polls triggered {len(scans) - drain_scans} unexpected scans'
