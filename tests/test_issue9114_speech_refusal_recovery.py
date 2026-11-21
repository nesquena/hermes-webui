"""Regression checks for #9114 — a speech refusal warning that outlives its own
recovery.

Maintainer scenario (issue #9114, verbatim shape):

    hydrate 'Old Voice' with writable storage ->
    choose 'New Voice' and complete the save ->
    start refusing writes ->
    choose 'Third Voice' (the mirror write is refused; the desired voice is
    preserved and the unapplied/refusal note is shown) ->
    restore writable storage and fire `voiceschanged` ->
    the later mirror succeeds and stores 'Third Voice', but the control still
    shows the unapplied/refusal warning.

Root cause: the changed-key reader in `_speechSetting` restores warning state
from the per-key `_settingsSpeechRefusedKeys` set.  A later *successful* mirror
only cleared the control's note (`_setControlUnappliedNote(el,false)`) and never
reconciled that per-key refusal metadata, so the warning became a false
positive.  `_captureSpeechPreferenceOwnership` clears the whole set, but that is
"re-capturing ownership", not "one successful mirror".

Fix: a mirror write that finally landed reconciles the refusal metadata *per
key* — the key leaves `_settingsSpeechRefusedKeys`, its stale
`_unappliedSpeechServerValues` entry (which makes unrelated saves omit the key)
is dropped, and the control's note/title are cleared.  Other keys' refusal
state and every explicit desired value are preserved.

These tests reuse the node harness from
`tests/test_issue8708_refused_storage_hydration.py` (real `panels.js` source
blocks executed in node against a `Storage` whose `setItem` throws
`DOMException('QuotaExceededError')`); the refused-storage tests in that file
stay green as the regression control.
"""

from tests import test_issue8708_refused_storage_hydration as _upstream


def test_successful_mirror_after_refusal_clears_note_and_title():
    """#9114 acceptance 1: refusal followed by a successful mirror clears the
    corresponding refusal metadata and the visible note/title."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(voiceSel.value, 'Old Voice');
  assert.strictEqual(rowNote(), null);

  // Choose New Voice and complete the save.
  voiceSel.value = 'New Voice';
  voiceSel.onchange.call(voiceSel);
  assert.strictEqual(store.get('hermes-tts-voice'), 'New Voice');
  assert.strictEqual(rowNote(), null);
  assert.strictEqual(pendingTimers.length, 1, 'autosave armed');
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).tts_voice, 'New Voice');

  // Storage starts refusing; the Third Voice edit is refused.
  REFUSE = true;
  voiceSel.value = 'Third Voice';
  voiceSel.onchange.call(voiceSel);
  assert.strictEqual(store.get('hermes-tts-voice'), 'New Voice', 'refused write leaves the cache');
  assert.ok(rowNote() && rowNote().textContent.includes('settings_unapplied_note'), 'refused edit shows the row note');
  assert.strictEqual(voiceSel.title, 'settings_unapplied_note', 'refused edit sets the title');
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).tts_voice, 'Third Voice', 'the desired voice is still saved');

  // Storage recovers; voiceschanged fires and the later mirror succeeds.
  REFUSE = false;
  voiceschangedListeners.forEach((fn) => fn());
  assert.strictEqual(store.get('hermes-tts-voice'), 'Third Voice', 'the later mirror must land Third Voice');
  assert.strictEqual(voiceSel.value, 'Third Voice', 'selector keeps the desired voice');
  assert.strictEqual(rowNote(), null, 'recovery must clear the row note');
  assert.strictEqual(voiceSel.title, '', 'recovery must clear the title');
  assert.strictEqual(voiceSel.dataset.unapplied, undefined, 'recovery must clear the dataset flag');
  assert.strictEqual(_settingsSpeechRefusedKeys.has('tts_voice'), false,
    'the per-key refusal metadata must be reconciled by the successful mirror');

  // A second repopulation must not resurrect the warning from stale metadata.
  voiceschangedListeners.forEach((fn) => fn());
  assert.strictEqual(rowNote(), null, 'a later repopulation must not resurrect the note');
  assert.strictEqual(voiceSel.title, '', 'a later repopulation must not resurrect the title');
  assert.strictEqual(voiceSel.value, 'Third Voice', 'the explicit desired value is preserved');
  assert.strictEqual(_settingsSpeechDesiredValues.tts_voice, 'Third Voice',
    'recovery must not drop the explicit desired value');

""")


def test_continued_refusal_keeps_the_truthful_warning():
    """#9114 acceptance 2: continued refused writes keep the truthful warning."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');

  REFUSE = true;
  voiceSel.value = 'Third Voice';
  voiceSel.onchange.call(voiceSel);
  assert.strictEqual(store.get('hermes-tts-voice'), 'Old Voice', 'refused write leaves the cache');
  assert.ok(rowNote() && rowNote().textContent.includes('settings_unapplied_note'), 'refused edit shows the row note');
  assert.strictEqual(voiceSel.title, 'settings_unapplied_note');
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).tts_voice, 'Third Voice', 'the desired voice is still saved');

  // voiceschanged while storage still refuses: the warning must stay.
  voiceschangedListeners.forEach((fn) => fn());
  assert.strictEqual(store.get('hermes-tts-voice'), 'Old Voice', 'still refused');
  assert.strictEqual(voiceSel.value, 'Third Voice', 'desired voice preserved while refused');
  assert.ok(rowNote() && rowNote().textContent.includes('settings_unapplied_note'),
    'continued refusal must keep the row note');
  assert.strictEqual(voiceSel.title, 'settings_unapplied_note', 'continued refusal must keep the title');
  assert.strictEqual(_settingsSpeechRefusedKeys.has('tts_voice'), true,
    'continued refusal must keep the per-key metadata');

""")


def test_reconcile_is_per_key_unrelated_refusal_survives():
    """#9114 acceptance 3: reconcile per key — the engine's refusal state and
    warning survive the voice control's recovery."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');

  REFUSE = true;
  // Voice edit refused.
  voiceSel.value = 'Third Voice';
  voiceSel.onchange.call(voiceSel);
  assert.strictEqual(_settingsSpeechRefusedKeys.has('tts_voice'), true);
  // Engine edit refused too.
  els.settingsTtsEngine.value = 'openai';
  els.settingsTtsEngine.onchange.call(els.settingsTtsEngine);
  assert.strictEqual(_settingsSpeechRefusedKeys.has('tts_engine'), true);
  const engineNote = () => els.settingsTtsEngine.children.find((c) => c && c.className === 'settings-unapplied-note') || null;
  assert.ok(engineNote(), 'the refused engine edit must show its note');
  assert.strictEqual(els.settingsTtsEngine.title, 'settings_unapplied_note');
  while(pendingTimers.length) await pendingTimers.shift()();

  // Storage recovers; voiceschanged repopulates the voice selector only.
  REFUSE = false;
  voiceschangedListeners.forEach((fn) => fn());

  // The voice control recovered.
  assert.strictEqual(store.get('hermes-tts-voice'), 'Third Voice', 'the voice mirror landed');
  assert.strictEqual(rowNote(), null, 'the recovered control cleared its note');
  assert.strictEqual(voiceSel.title, '', 'the recovered control cleared its title');
  assert.strictEqual(_settingsSpeechRefusedKeys.has('tts_voice'), false, 'the recovered key left the refused set');

  // The unrelated engine control keeps its refusal state and warning.
  assert.strictEqual(_settingsSpeechRefusedKeys.has('tts_engine'), true,
    'an unrelated key must keep its refusal metadata');
  assert.ok(engineNote(), 'an unrelated control must keep its note');
  assert.strictEqual(engineNote().style.display, 'block', 'the unrelated note stays visible');
  assert.strictEqual(els.settingsTtsEngine.title, 'settings_unapplied_note',
    'an unrelated control must keep its title');

""")


def test_desired_voice_survives_recovery_in_unrelated_save():
    """#9114 acceptance 3/4: after recovery the explicit desired voice is
    preserved — an unrelated save carries 'Third Voice', never the stale
    cache value, and no warning reappears."""
    _upstream._run_native_select_scenario(r"""
  REFUSE = false;
  hydrateNative('browser', 'Old Voice');

  REFUSE = true;
  voiceSel.value = 'Third Voice';
  voiceSel.onchange.call(voiceSel);
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).tts_voice, 'Third Voice');

  REFUSE = false;
  voiceschangedListeners.forEach((fn) => fn());
  assert.strictEqual(voiceSel.value, 'Third Voice', 'desired voice survives recovery');
  assert.strictEqual(_settingsSpeechDesiredValues.tts_voice, 'Third Voice', 'explicit desired value preserved');
  assert.strictEqual(rowNote(), null, 'recovery cleared the note');

  // An unrelated save after recovery still carries the desired voice.
  els.settingsNotificationsEnabled.checked = true;
  const notifListener = els.settingsNotificationsEnabled.listeners.find((l) => l.type === 'change');
  assert.ok(notifListener, 'notification toggle must be wired');
  notifListener.fn();
  await pendingTimers.shift()();
  const body = JSON.parse(posts[posts.length - 1].body);
  assert.strictEqual(body.notifications_enabled, true, 'unrelated save carries the edit');
  assert.strictEqual(body.tts_voice, 'Third Voice', 'unrelated save must carry the desired voice, never the stale cache');
  assert.strictEqual(rowNote(), null, 'no warning reappears after the unrelated save');

""")


def test_hydration_refusal_metadata_cleared_after_successful_mirror():
    """#9114 acceptance 1 (hydration path): a refused hydration mirror records
    the key in `_unappliedSpeechServerValues`, which makes unrelated saves omit
    it.  Once a later mirror succeeds, that stale per-key metadata must be
    reconciled so the durable server value is no longer withheld."""
    _upstream._run_native_select_scenario(r"""
  // Hydration itself is refused: the server value never reaches the cache.
  REFUSE = true;
  hydrateNative('browser', 'Old Voice');
  assert.strictEqual(store.get('hermes-tts-voice'), 'Old Voice', 'refused hydration leaves the pre-seeded cache');
  assert.strictEqual(voiceSel.value, 'Old Voice');
  assert.strictEqual(Object.prototype.hasOwnProperty.call(_unappliedSpeechServerValues, 'tts_voice'), true,
    'a refused hydration mirror records the unapplied server value');

  // An unrelated save must omit the key while the mirror is still refused.
  els.settingsNotificationsEnabled.checked = true;
  const notifListener = els.settingsNotificationsEnabled.listeners.find((l) => l.type === 'change');
  notifListener.fn();
  await pendingTimers.shift()();
  assert.strictEqual(JSON.parse(posts[posts.length - 1].body).notifications_enabled, true);
  assert.ok(!Object.prototype.hasOwnProperty.call(JSON.parse(posts[posts.length - 1].body), 'tts_voice'),
    'while refused, the unrelated save must omit the unapplied key');

  // Storage recovers; voiceschanged fires and the later mirror succeeds.
  REFUSE = false;
  voiceschangedListeners.forEach((fn) => fn());
  assert.strictEqual(store.get('hermes-tts-voice'), 'Old Voice', 'the later mirror landed');
  assert.strictEqual(rowNote(), null, 'no note after the successful mirror');
  assert.strictEqual(Object.prototype.hasOwnProperty.call(_unappliedSpeechServerValues, 'tts_voice'), false,
    'the successful mirror must reconcile the stale per-key metadata');

  // The withheld durable value is no longer omitted from unrelated saves.
  els.settingsNotificationsEnabled.checked = false;
  notifListener.fn();
  await pendingTimers.shift()();
  const body = JSON.parse(posts[posts.length - 1].body);
  assert.strictEqual(body.notifications_enabled, false);
  assert.strictEqual(body.tts_voice, 'Old Voice', 'after the successful mirror the key is no longer withheld');

""")
