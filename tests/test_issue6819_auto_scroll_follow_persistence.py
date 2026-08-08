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
    # P1 follow-up (#6856 review): the hermes_profile cookie is HttpOnly so
    # document.cookie can NEVER read it — the profile key must rely on
    # S.activeProfile, and the boot-failure path must DEFER its mirror read
    # until after the profile resolves asynchronously (/api/profile/active).
    assert "_autoScrollFollowDeferredReapply" in src, (
        "boot fallback must defer the mirror read (HttpOnly cookie, P1)"
    )
    # The helper must NOT attempt a document.cookie read (dead code — cookie
    # is HttpOnly; a cookie-based key would silently select 'default').
    m = re.search(
        r"function _autoScrollFollowProfileKey\(\)\{.*?\n\}",
        src,
        re.S,
    )
    assert m, "_autoScrollFollowProfileKey not found"
    assert "document.cookie" not in m.group(0), (
        "profile key must not read document.cookie (HttpOnly, P1 follow-up)"
    )


def test_boot_success_path_persists_mirror():
    src = _read("static/boot.js")
    assert "window._autoScrollFollow=_persistAutoScrollFollow(" in src, (
        "boot settings path must write the resolved value into the mirror"
    )


def test_boot_fallback_reads_mirror_not_hardcoded_true():
    src = _read("static/boot.js")
    assert "window._autoScrollFollow=_readPersistedAutoScrollFollow()" in src, (
        "the deferred re-apply (after profile resolution) must read the mirror"
    )
    assert "window._autoScrollFollowDeferredReapply" in src, (
        "boot fallback must set the deferred re-apply flag"
    )
    # The deferred read must run after S.activeProfile resolves, never in the
    # synchronous fallback (where the profile is still 'default').
    fallback_idx = src.find("_sessionEndlessScrollEnabled=false;")
    profile_idx = src.find("S.activeProfile = activeProfileState.profile;")
    reapply_idx = src.find("_autoScrollFollowDeferredReapply){")
    assert fallback_idx != -1 and profile_idx != -1 and reapply_idx != -1, (
        "deferred re-apply anchors missing"
    )
    assert fallback_idx < profile_idx < reapply_idx, (
        "deferred re-apply must run AFTER S.activeProfile resolves (P1 follow-up)"
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
const document = { cookie: '' };  // hermes_profile is HttpOnly — never readable
let S = { activeProfile: 'default' };
""" + block + r"""
// 0. P1 follow-up: profile key comes from S.activeProfile (cookie is HttpOnly
//    and unusable — a cookie-based key would silently select 'default').
if (_autoScrollFollowProfileKey() !== 'default') throw new Error('profile key must use S.activeProfile');
// 1. fresh user: default ON
if (_readPersistedAutoScrollFollow() !== true) throw new Error('fresh default must be ON');
// 2. user turns OFF -> persisted (under maintainer)
S = { activeProfile: 'maintainer' };
if (_persistAutoScrollFollow(false) !== false) throw new Error('persist OFF failed');
if (_readPersistedAutoScrollFollow() !== false) throw new Error('persisted OFF must be honored');
// 3. deferred re-apply semantics: boot failure sets safe ON + flag, then the
//    profile resolves and the mirror is read with the CORRECT profile.
S = { activeProfile: 'default' };   // boot-failure fallback state
window._autoScrollFollow = true;    // safe default set by the fallback
window._autoScrollFollowDeferredReapply = true;
S = { activeProfile: 'maintainer' };  // profile resolves asynchronously
if (window._autoScrollFollowDeferredReapply) {
  window._autoScrollFollow = _readPersistedAutoScrollFollow();
  window._autoScrollFollowDeferredReapply = false;
}
if (window._autoScrollFollow !== false) throw new Error('deferred re-apply must read the maintainer OFF entry');
if (window._autoScrollFollowDeferredReapply !== false) throw new Error('deferred flag must clear');
// 4. per-profile isolation via S.activeProfile
S = { activeProfile: 'default' };
if (_readPersistedAutoScrollFollow() !== true) throw new Error('default profile must keep default ON');
if (_persistAutoScrollFollow(true) !== true) throw new Error('persist default ON failed');
S = { activeProfile: 'maintainer' };
if (_readPersistedAutoScrollFollow() !== false) throw new Error('maintainer OFF must survive switch back');
// 5. storage is one JSON map keyed by profile
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
