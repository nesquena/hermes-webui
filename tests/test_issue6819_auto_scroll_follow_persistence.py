"""Regression coverage for #6819 — the Auto-follow toggle must survive
transient settings-fetch failures and partial settings bodies.

Root cause fixed: the boot settings-fetch-failure path hardcoded
`window._autoScrollFollow=true`, silently clobbering an explicit OFF for the
whole session (post-turn scroll yanks until refresh), and
`_applySavedSettingsUi` assigned `body.auto_scroll_follow!==false`
unconditionally — a partial body without the key evaluates `undefined!==false`
→ `true`, re-enabling follow mid-session.

The fix mirrors the resolved value into PROFILE-NAMESPACED localStorage
(one JSON map keyed by profile name), makes the boot fallback read the mirror
instead of hardcoding ON, and only overrides the runtime flag when a settings
body actually owns the key.
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── Source-shape guards (what the code must contain) ────────────────────────

def test_boot_has_profile_namespaced_mirror_helpers():
    src = _read("static/boot.js")
    assert "_persistAutoScrollFollow" in src, "persist helper must exist"
    assert "_readPersistedAutoScrollFollow" in src, "read helper must exist"
    assert "_autoScrollFollowProfileKey" in src, (
        "mirror must be profile-namespaced (maintainer review requirement)"
    )
    assert "S.activeProfile" in src or "activeProfile" in src, (
        "profile key must derive from the active profile"
    )
    assert "_AUTO_SCROLL_FOLLOW_KEY" in src, "storage key constant must exist"
    # P1 (#6856 review): the profile key must come from the hermes_profile
    # cookie (readable before S.activeProfile is initialized at boot), with
    # S.activeProfile as fallback — otherwise a non-default profile's failed
    # settings fetch would read the 'default' mirror entry.
    assert "_PROFILE_COOKIE_NAME" in src and "hermes_profile" in src, (
        "profile key must read the hermes_profile cookie (P1)"
    )
    assert "document.cookie" in src, "profile key must read document.cookie (P1)"


def test_boot_success_path_persists_mirror():
    src = _read("static/boot.js")
    assert "window._autoScrollFollow=_persistAutoScrollFollow(" in src, (
        "boot settings path must write the resolved value into the mirror"
    )


def test_boot_fallback_reads_mirror_not_hardcoded_true():
    src = _read("static/boot.js")
    assert "window._autoScrollFollow=_readPersistedAutoScrollFollow()" in src, (
        "fallback must read the mirror instead of hardcoding true"
    )
    # The old hardcoded fallback must be gone (ui.js keeps its own
    # undefined-before-init default at _autoScrollFollow===undefined, #6614 —
    # a different, legitimate case not touched by #6819).
    assert "_sessionEndlessScrollEnabled=false;\n    // #6819" in src, (
        "fallback comment anchor missing"
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
    assert "_persistAutoScrollFollow(window._autoScrollFollow)" in src, (
        "settings-save apply path must persist the resolved value"
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
let _cookie = 'hermes_profile=maintainer; other=1';
const document = { get cookie(){ return _cookie; } };
const S = { activeProfile: 'default' };   // stale: boot has NOT resolved yet
""" + block + r"""
// 0. P1: cookie namespaces the key BEFORE S.activeProfile is initialized —
//    a non-default profile's boot fallback must read ITS OWN mirror entry.
if (_autoScrollFollowProfileKey() !== 'maintainer') throw new Error('P1: cookie must win over stale S.activeProfile');
// 1. fresh user: default ON
if (_readPersistedAutoScrollFollow() !== true) throw new Error('fresh default must be ON');
// 2. user turns OFF -> persisted (under maintainer, from cookie)
if (_persistAutoScrollFollow(false) !== false) throw new Error('persist OFF failed');
if (_readPersistedAutoScrollFollow() !== false) throw new Error('persisted OFF must be honored');
// 3. profile-namespacing: switching profile keeps its own value
_cookie = '';
S.activeProfile = 'default';
if (_readPersistedAutoScrollFollow() !== true) throw new Error('default profile must keep default ON (no cookie, no entry)');
if (_persistAutoScrollFollow(true) !== true) throw new Error('persist ON failed');
_cookie = 'hermes_profile=maintainer; other=1';
S.activeProfile = 'default';  // cookie still authoritative
if (_readPersistedAutoScrollFollow() !== false) throw new Error('maintainer OFF must survive switch back');
_cookie = '';
if (_readPersistedAutoScrollFollow() !== true) throw new Error('no-cookie falls back to S.activeProfile/default');
// 4. storage is one JSON map keyed by profile
const parsed = JSON.parse(store['hermes-auto-scroll-follow']);
if (JSON.stringify(Object.keys(parsed).sort()) !== JSON.stringify(['default','maintainer'])) throw new Error('keys mismatch: '+JSON.stringify(parsed));
if (parsed['default'] !== 1 || parsed['maintainer'] !== 0) throw new Error('values mismatch: '+JSON.stringify(parsed));
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
