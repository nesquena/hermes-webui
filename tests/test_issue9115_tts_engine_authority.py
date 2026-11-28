"""Regression checks for #9115 — the engine selector and the voice catalog must
name the same engine.

Maintainer scenario (issue #9115): after a **refused** explicit engine edit,
the Settings selector shows the desired engine while `_populateTtsVoices()`
chooses its catalog — and the refresh dispatch — from the previous raw
`hermes-tts-engine` cache entry. The refusal note is truthful, but the engine
control and the available voice list describe different engines.

Root cause: the selector reads the speech setting
(`_speechSetting('tts_engine', ...)`, which honours the user's explicit
choice when the mirror write was refused), while the catalog and the
`voiceschanged` dispatch read the raw cache entry directly.

Fix: both read one engine authority. A key the user changed uses the desired
value; everything else uses the effective cache entry. The fix deliberately
does **not** pretend a refused cache write changed the runtime — the raw
cache entry stays as it was.

These tests reuse the node harness from
`tests/test_issue8708_refused_storage_hydration.py` (real `panels.js` source
blocks executed in node against a `Storage` whose `setItem` throws
`DOMException('QuotaExceededError')`).

"""

from tests import test_issue8708_refused_storage_hydration as _upstream


def _voice_values():
    return "Array.from(voiceSel.options||[]).map(function(o){return o.value;})"


def test_voice_catalog_follows_the_selector_after_a_refused_engine_edit():
    """#9115: after a refused engine edit the catalog offers the SELECTED
    engine's voices, not the stale cache entry's."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(els.settingsTtsEngine.value, 'browser');
  const browserVoices = """ + _voice_values() + r""".join(',');
  assert.ok(browserVoices.indexOf('Old Voice') >= 0, 'the browser catalog lists its own voices');

  // A refused engine edit: the selector keeps the desired engine, the raw
  // cache keeps the old one.
  REFUSE = true;
  els.settingsTtsEngine.value = 'edge';
  els.settingsTtsEngine.onchange.call(els.settingsTtsEngine);
  assert.strictEqual(store.get('hermes-tts-engine'), 'browser',
    'the refused write leaves the raw cache entry alone');
  assert.strictEqual(els.settingsTtsEngine.value, 'edge',
    'the selector still shows the desired engine');

  window._populateTtsVoices();
  const edgeVoices = """ + _voice_values() + r""".join(',');
  assert.ok(edgeVoices.indexOf('zh-CN-XiaoxiaoNeural') >= 0,
    'the catalog must offer the SELECTED engine voices, got: ' + edgeVoices);
  assert.ok(edgeVoices.indexOf('Old Voice') < 0,
    'the catalog must not offer the previous engine voices, got: ' + edgeVoices);

""")


def test_the_refused_cache_entry_is_not_pretended_to_have_applied():
    """#9115 acceptance: the fix must not pretend a refused cache write changed
    the runtime — the raw entry stays as it was."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');

  REFUSE = true;
  els.settingsTtsEngine.value = 'edge';
  els.settingsTtsEngine.onchange.call(els.settingsTtsEngine);

  assert.strictEqual(store.get('hermes-tts-engine'), 'browser',
    'a refused write must not change the cache entry');
  assert.strictEqual(els.settingsTtsEngine.value, 'edge',
    'the desired engine is still the explicit user choice');

""")


def test_the_refresh_dispatch_still_decides_on_the_effective_cache_entry():
    """#9115 scope note: the `voiceschanged` dispatch still decides whether to
    repopulate the catalog from the EFFECTIVE cache entry.

    The issue also asks the refresh dispatch to use the selected engine, but
    that reading collides with #9114: the dispatch's `engine === 'browser'`
    test is what repopulates the catalog after a refused VOICE edit recovers.
    Taking the selected (desired) engine there would skip the repopulation
    and the recovered voice's mirror would never land -- exactly the
    regression #9114 pins.

    This test fires the REAL listener the production block registers and
    pins the behaviour that is actually shipped, so the decision point stays
    visible for the maintainer to arbitrate: with the effective cache entry
    still on `browser` the listener DOES rebuild, and the rebuilt catalog
    follows the SELECTED engine (the #9115 fix), not the cache entry.
    """
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');

  REFUSE = true;
  els.settingsTtsEngine.value = 'edge';
  els.settingsTtsEngine.onchange.call(els.settingsTtsEngine);

  // The dispatch reads the effective cache entry, which the refused write
  // left alone.
  assert.strictEqual(store.get('hermes-tts-engine'), 'browser',
    'the refused engine write left the cache entry alone');

  // Instrument the real renderer (after the onchange already repopulated
  // once) so the assertions below see the LISTENER's dispatch, not a direct
  // call to the renderer.
  let populateCalls = 0;
  const realPopulate = window._populateTtsVoices;
  window._populateTtsVoices = function(){ populateCalls += 1; return realPopulate.apply(this, arguments); };

  // Fire the REAL voiceschanged listener registered by the production block.
  assert.ok(voiceschangedListeners.length > 0, 'the voiceschanged listener must be registered');
  voiceschangedListeners.forEach((fn) => fn());

  // The effective cache entry is 'browser', so the listener must rebuild.
  assert.strictEqual(populateCalls, 1,
    'the listener must rebuild the catalog through the effective cache entry');

  // The rebuilt catalog offers the SELECTED engine's voices (#9115), not the
  // cache entry's.
  const rebuilt = """ + _voice_values() + r""".join(',');
  assert.ok(rebuilt.indexOf('zh-CN-XiaoxiaoNeural') >= 0,
    'the rebuilt catalog must offer the SELECTED engine voices, got: ' + rebuilt);
  assert.ok(rebuilt.indexOf('Old Voice') < 0,
    'the rebuilt catalog must not offer the previous engine voices, got: ' + rebuilt);

""")


def test_the_refresh_dispatch_skips_the_browser_refresh_for_a_non_browser_engine():
    """#9115 scope note, other half: with the effective cache entry on a
    NON-browser engine the `voiceschanged` listener must not run the browser
    voice refresh -- even when the user's desired engine is `browser`.

    The dispatch's `engine === 'browser'` test reads the effective cache
    entry; a desired-but-refused engine edit must not pull the refresh onto
    the desired value, because that decision is #9114's recovery path.
    """
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('edge', '');

  REFUSE = true;
  els.settingsTtsEngine.value = 'browser';
  els.settingsTtsEngine.onchange.call(els.settingsTtsEngine);
  assert.strictEqual(store.get('hermes-tts-engine'), 'edge',
    'the refused engine write left the cache entry alone');
  assert.strictEqual(els.settingsTtsEngine.value, 'browser',
    'the selector still shows the desired engine');

  // Instrument after the onchange's own repopulation so only the LISTENER's
  // dispatch is counted.
  let populateCalls = 0;
  const realPopulate = window._populateTtsVoices;
  window._populateTtsVoices = function(){ populateCalls += 1; return realPopulate.apply(this, arguments); };

  assert.ok(voiceschangedListeners.length > 0, 'the voiceschanged listener must be registered');
  voiceschangedListeners.forEach((fn) => fn());

  // The effective cache entry is 'edge', so the listener must not run the
  // browser refresh -- taking the desired 'browser' here would be exactly the
  // dispatch rewrite #9114 pins against.
  assert.strictEqual(populateCalls, 0,
    'a non-browser effective engine must not run the browser voice refresh');

""")


def test_an_unchanged_engine_keeps_using_the_effective_cache_entry():
    """#9115 regression control: with no explicit change the catalog keeps
    reading the effective cache entry."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(els.settingsTtsEngine.value, 'browser');
  const before = """ + _voice_values() + r""".join(',');
  window._populateTtsVoices();
  const after = """ + _voice_values() + r""".join(',');
  assert.strictEqual(after, before, 'an unchanged engine must not change the catalog');

""")


def test_a_refused_engine_edit_keeps_the_truthful_refusal_indication():
    """#9115 regression control: reconciling the engine authority must not
    swallow the refusal indication (#9114's warning stays truthful).

    The engine control carries its own note state (title + dataset flag), so
    the assertion is on the control itself rather than on the voice row.
    """
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');

  REFUSE = true;
  els.settingsTtsEngine.value = 'edge';
  els.settingsTtsEngine.onchange.call(els.settingsTtsEngine);
  assert.strictEqual(els.settingsTtsEngine.title, 'settings_unapplied_note',
    'the refused engine edit must still set the title');
  assert.strictEqual(els.settingsTtsEngine.dataset.unapplied, 'true',
    'the refused engine edit must still mark the control unapplied');

""")


# ── #9242 P1 on the engine key (Greptile discussion_r4651157429) ───────────
# The TTS engine key takes the same near-limit path as the voice key: storage
# can refuse the server's LONGER engine while accepting a rewrite of the cached
# one, and hydration re-syncs that stale value through
# `_syncSpeechPreferenceCache('tts_engine', ...)`. The landed rewrite must not
# release the pending server engine, or an unrelated save persists the stale
# engine over the server preference. The fix itself lives in #9242 (the written
# value is passed into `_reconcileSpeechRefusalOnMirrorLanded`); these tests
# cover the engine path of that shared fix.


def test_stale_engine_rewrite_keeps_the_guard_against_unrelated_saves():
    """#9243 engine-path coverage for #9242's P1: after a refused longer server
    engine, hydration's stale-cache rewrite must keep the per-key guard so an
    unrelated save omits the stale engine instead of overwriting the server."""
    _upstream._run_native_select_scenario(r"""
  const hasKey = (key) => Object.prototype.hasOwnProperty.call(_unappliedSpeechServerValues, key);

  REFUSE = false;
  REFUSE_LONGER = false;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(store.get('hermes-tts-engine'), 'browser');

  // The server's engine is longer than the cached one; near-limit storage
  // refuses growing the entry while still accepting a rewrite of 'browser'.
  REFUSE_LONGER = true;
  const payload = nativePayload('elevenlabs', 'Old Voice');
  __hydrateHead(payload);
  __hydrate(payload);

  assert.strictEqual(store.get('hermes-tts-engine'), 'browser',
    'the longer server engine was refused, so the cache keeps the old engine');
  assert.strictEqual(hasKey('tts_engine'), true,
    'the refused server engine must stay pending after hydration');
  assert.strictEqual(_unappliedSpeechServerValues['tts_engine'], 'elevenlabs',
    'the pending entry holds the refused server engine');

  // Hydration already re-synced the stale engine through
  // `_syncSpeechPreferenceCache('tts_engine', saved)`. That write landed and
  // must NOT release the pending server engine.
  assert.strictEqual(hasKey('tts_engine'), true,
    'a successful stale-cache rewrite must NOT clear the pending server engine');

  // An unrelated save must omit the stale engine so the server keeps its value.
  els.settingsNotificationsEnabled.checked = true;
  const notifListener = els.settingsNotificationsEnabled.listeners.find((l) => l.type === 'change');
  assert.ok(notifListener, 'notification toggle must be wired to the autosave');
  notifListener.fn();
  await pendingTimers.shift()();
  const body = JSON.parse(posts[posts.length - 1].body);
  assert.strictEqual(body.notifications_enabled, true, 'the unrelated edit is saved');
  assert.ok(!Object.prototype.hasOwnProperty.call(body, 'tts_engine'),
    'unrelated save must omit the withheld engine: ' + JSON.stringify(body));
  assert.notStrictEqual(body.tts_engine, 'browser',
    'the stale cached engine must never be written back to the server');

  // The engine control keeps telling the truth about the pending server value.
  assert.ok(els.settingsTtsEngine.title === 'settings_unapplied_note'
    || els.settingsTtsEngine.dataset.unapplied === 'true',
    'the engine control keeps its unapplied indication');

""")


def test_landed_server_engine_releases_the_guard_for_unrelated_saves():
    """#9243 control for the engine path: once the real server engine lands the
    guard releases, so the fix withholds the key only while the server value is
    pending — not forever. The landing sync carries the server value itself, so
    this pins the `writtenValue` match branch (not the hydration-wide recount,
    which `_mirrorSpeechSettingsFromServer` performs on its own)."""
    _upstream._run_native_select_scenario(r"""
  const hasKey = (key) => Object.prototype.hasOwnProperty.call(_unappliedSpeechServerValues, key);

  REFUSE = false;
  REFUSE_LONGER = false;
  hydrateNative('browser', 'Old Voice');

  REFUSE_LONGER = true;
  const payload = nativePayload('elevenlabs', 'Old Voice');
  __hydrateHead(payload);
  __hydrate(payload);
  assert.strictEqual(hasKey('tts_engine'), true, 'the refused server engine is pending');

  // Storage frees up and the real server engine finally lands through a sync
  // that carries the server value itself.
  REFUSE_LONGER = false;
  assert.strictEqual(_syncSpeechPreferenceCache('tts_engine', 'elevenlabs'), true,
    'storage accepts the server engine once it is no longer growing the entry');
  assert.strictEqual(store.get('hermes-tts-engine'), 'elevenlabs',
    'the real server engine landed in the cache');
  assert.strictEqual(hasKey('tts_engine'), false,
    'a landed server engine must release the pending protection');

  els.settingsNotificationsEnabled.checked = true;
  const notifListener = els.settingsNotificationsEnabled.listeners.find((l) => l.type === 'change');
  notifListener.fn();
  await pendingTimers.shift()();
  const body = JSON.parse(posts[posts.length - 1].body);
  assert.ok(Object.prototype.hasOwnProperty.call(body, 'tts_engine'),
    'the landed engine is no longer withheld from unrelated saves: ' + JSON.stringify(body));

""")
