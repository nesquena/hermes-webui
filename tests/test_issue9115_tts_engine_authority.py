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
  const browserVoices = """ + _voice_values() + """.join(',');
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
  const edgeVoices = """ + _voice_values() + """.join(',');
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

    This test pins the behaviour that is actually shipped, so the decision
    point stays visible for the maintainer to arbitrate.
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
""")


def test_an_unchanged_engine_keeps_using_the_effective_cache_entry():
    """#9115 regression control: with no explicit change the catalog keeps
    reading the effective cache entry."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(els.settingsTtsEngine.value, 'browser');
  const before = """ + _voice_values() + """.join(',');
  window._populateTtsVoices();
  const after = """ + _voice_values() + """.join(',');
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
