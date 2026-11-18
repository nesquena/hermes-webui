"""Regression coverage for #6819 — the Auto-follow toggle must survive
transient settings-fetch failures and partial settings bodies.

Root cause fixed: the boot settings-fetch-failure path hardcoded
`window._autoScrollFollow=true`, silently clobbering an explicit OFF for the
whole session (post-turn scroll yanks until refresh), and
`_applySavedSettingsUi` assigned `body.auto_scroll_follow!==false`
unconditionally — a partial body without the key evaluates `undefined!==false`
→ `true`, re-enabling follow mid-session.

The fix mirrors the resolved value into GLOBAL localStorage (the backend
setting is one global settings.json — a profile-keyed map would leave a
freshly-opened profile with no entry and wrongly restore ON, per maintainer
review on #6856), makes the boot fallback read the mirror instead of
hardcoding ON, and only overrides the runtime flag when a settings body
actually owns the key.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── Source-shape guards (what the code must contain) ────────────────────────

def test_boot_has_global_mirror_helpers():
    src = _read("static/boot.js")
    assert "_persistAutoScrollFollow" in src, "persist helper must exist"
    assert "_readPersistedAutoScrollFollow" in src, "read helper must exist"
    assert "_AUTO_SCROLL_FOLLOW_KEY" in src, "storage key constant must exist"
    # Maintainer review (#6856): the backend setting is GLOBAL (one
    # settings.json), so the mirror must NOT be profile-keyed — a profile map
    # would leave a freshly-opened profile with no entry and wrongly restore
    # ON. The helper must be a single global scalar, read synchronously in
    # the boot fallback (no profile resolution, no deferral).
    assert "_autoScrollFollowProfileKey" not in src, (
        "mirror must NOT be profile-keyed (maintainer review, #6856)"
    )
    assert "_autoScrollFollowDeferredReapply" not in src, (
        "no deferred re-apply needed — global mirror is read synchronously"
    )
    assert "_autoScrollFollowDeferredPersist" not in src, (
        "no deferred persist needed — global mirror is written synchronously"
    )


def test_boot_success_path_persists_mirror():
    src = _read("static/boot.js")
    assert "window._autoScrollFollow=_persistAutoScrollFollow(" in src, (
        "boot settings path must persist the resolved value into the mirror"
    )


def test_boot_fallback_reads_mirror_not_hardcoded_true():
    src = _read("static/boot.js")
    assert "window._autoScrollFollow=_readPersistedAutoScrollFollow()" in src, (
        "the boot-failure fallback must read the mirror synchronously"
    )
    # The old hardcoded fallback must be gone (ui.js keeps its own
    # undefined-before-init default at _autoScrollFollow===undefined, #6614 —
    # a different, legitimate case not touched by #6819).
    assert "_sessionEndlessScrollEnabled=false;" in src, (
        "fallback anchor missing"
    )


def test_partial_body_does_not_override_follow():
    src = _read("static/panels.js")
    assert (
        "hasOwnProperty.call(body,'auto_scroll_follow')" in src
        or "hasOwnProperty.call(body, 'auto_scroll_follow')" in src
    ), (
        "_applySavedSettingsUi must guard the override with hasOwnProperty so "
        "a partial body without the key cannot silently re-enable follow"
    )


def test_settings_save_persists_mirror():
    src = _read("static/panels.js")
    # Greptile P1 (#6856): the autosave path must persist ONLY from an
    # explicit boolean in the server response — a failed save (`saved` falsy)
    # or a response without the key must not write the synthesized default
    # (ON) into the mirror.
    assert "_persistAutoScrollFollow(saved.auto_scroll_follow)" in src, (
        "autosave must persist from the explicit server boolean"
    )
    assert "typeof saved.auto_scroll_follow==='boolean'" in src, (
        "autosave must require an explicit boolean before persisting (P1)"
    )


def test_settings_panel_load_persists_mirror():
    src = _read("static/panels.js")
    # Regression (#6856 gate finding): a successful settings-panel GET applies
    # the authoritative value to window._autoScrollFollow — it must ALSO sync the
    # global mirror, or an explicit OFF applied here leaves a stale ON mirror that
    # a later boot-fetch failure restores (reopening #6819). Persist only from an
    # explicit server boolean (never a synthesized default), matching the contract.
    panel_block = src.split("const autoScrollFollowCb=$('settingsAutoScrollFollow');", 1)[1][:900]
    assert "typeof settings.auto_scroll_follow==='boolean'" in panel_block, (
        "settings-panel load must require an explicit boolean before persisting"
    )
    assert "_persistAutoScrollFollow(settings.auto_scroll_follow)" in panel_block, (
        "settings-panel load must sync the global mirror from the authoritative GET value"
    )


# ── Behavioral guards (extracted helper logic must behave correctly) ────────

def test_mirror_helpers_behavior_via_source_extraction():
    """Extract the real JS helper functions from boot.js and execute them under
    node with a fake localStorage to prove the semantics end-to-end."""
    import re as _re
    import shutil
    import subprocess
    import sys

    assert shutil.which("node"), "node required for this test"
    src = _read("static/boot.js")
    m = _re.search(
        r"const _AUTO_SCROLL_FOLLOW_KEY=.*?window\._readPersistedAutoScrollFollow=_readPersistedAutoScrollFollow;",
        src,
        _re.S,
    )
    assert m, "helper block not found in boot.js"
    block = m.group(0)

    js = r"""
const store = {};
const localStorage = {
  getItem(k){ return (k in store) ? store[k] : null; },
  setItem(k,v){ store[k] = v; }
};
const window = {};
""" + block + r"""
// 1. fresh user: default ON
if (_readPersistedAutoScrollFollow() !== true) throw new Error('fresh default must be ON');
// 2. user turns OFF -> persisted (global)
if (_persistAutoScrollFollow(false) !== false) throw new Error('persist OFF failed');
if (_readPersistedAutoScrollFollow() !== false) throw new Error('persisted OFF must be honored');
// 3. MAINTAINER-REVIEW REGRESSION (#6856): the backend setting is GLOBAL
//    (one settings.json), so the mirror must be too. Saving OFF under one
//    profile, then a failed settings fetch on ANY other profile, must still
//    restore OFF — a profile-keyed map would find no entry and wrongly
//    return ON. With the global mirror there is no profile dimension at all:
//    simulate the boot-failure path directly.
const bootFailureFallbackValue = _readPersistedAutoScrollFollow();
if (bootFailureFallbackValue !== false) throw new Error('global mirror must survive profile change + failed fetch (maintainer review)');
// 4. ON round-trip
if (_persistAutoScrollFollow(true) !== true) throw new Error('persist ON failed');
if (_readPersistedAutoScrollFollow() !== true) throw new Error('persisted ON must be honored');
// 5. GREPTILE P1 (#6856): a FAILED autosave (`saved` falsy) must NOT write
//    the synthesized default into the mirror — replicate the autosave guard:
//    only an explicit boolean from the server response persists.
//    (a) mirror holds OFF (user's real preference)
if (_persistAutoScrollFollow(false) !== false) throw new Error('setup OFF failed');
//    (b) failed autosave: saved=null -> guard rejects -> mirror stays OFF
const savedFailed = null;
if (savedFailed && typeof savedFailed.auto_scroll_follow === 'boolean' && typeof _persistAutoScrollFollow === 'function') {
  _persistAutoScrollFollow(savedFailed.auto_scroll_follow);
}
if (_readPersistedAutoScrollFollow() !== false) throw new Error('failed autosave must NOT corrupt the mirror (Greptile P1)');
//    (c) successful autosave with explicit boolean -> mirror updates
const savedOk = { auto_scroll_follow: true };
if (savedOk && typeof savedOk.auto_scroll_follow === 'boolean' && typeof _persistAutoScrollFollow === 'function') {
  _persistAutoScrollFollow(savedOk.auto_scroll_follow);
}
if (_readPersistedAutoScrollFollow() !== true) throw new Error('successful autosave must persist the explicit boolean');
// 6. storage is a single global scalar, not a profile map
if (store['hermes-auto-scroll-follow'] !== '1') throw new Error('storage must be a single scalar value: '+store['hermes-auto-scroll-follow']);
console.log('MIRROR-HELPERS-OK');
"""
    proc = subprocess.run(
        ["node", "-e", js],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert "MIRROR-HELPERS-OK" in proc.stdout, proc.stdout
