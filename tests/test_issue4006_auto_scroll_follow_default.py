"""Pin the auto_scroll_follow setting's default + hydration consistency (#4006).

The viewport-follow default is `True` (Codex/Claude-Code-style sticky bottom:
follow new output to the bottom while streaming, but a deliberate scroll-up
unpins and is respected). This pins the default in every place it is read so a
future edit can't silently flip it or, worse, default it ON in config.py while
hydrating it OFF in the browser (the classic default-mismatch bug, where an
existing user with no saved value sees the feature as disabled).
"""
import json
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_auto_scroll_follow_default_is_true_in_config():
    src = _read("api/config.py")
    assert re.search(r'["\']auto_scroll_follow["\']\s*:\s*True', src), (
        "auto_scroll_follow must default to True in _SETTINGS_DEFAULTS "
        "(sticky-bottom follow; scroll-up unpins)"
    )


def test_auto_scroll_follow_in_bool_keys():
    src = _read("api/config.py")
    m = re.search(r"_SETTINGS_BOOL_KEYS\s*=\s*\{([^}]+)\}", src, re.DOTALL)
    assert m, "_SETTINGS_BOOL_KEYS not found"
    assert "auto_scroll_follow" in m.group(1), (
        "auto_scroll_follow must be in _SETTINGS_BOOL_KEYS so it round-trips as a bool"
    )


def test_boot_hydration_defaults_true_when_setting_absent():
    """boot.js must hydrate _autoScrollFollow as True when the saved settings
    omit the key — `!!s.auto_scroll_follow` would wrongly default it OFF for
    every existing user, contradicting the config.py default."""
    src = _read("static/boot.js")
    # Settings path: default-true read (=== false), not the truthy-coerce form,
    # and the resolved value is persisted into the #6819 mirror (global —
    # matches the one global settings.json, per maintainer review on #6856).
    assert "window._autoScrollFollow=_persistAutoScrollFollow(s.auto_scroll_follow!==false)" in src, (
        "boot.js settings path must default _autoScrollFollow True when absent "
        "(and persist the resolved value into the #6819 mirror)"
    )
    assert "window._autoScrollFollow=!!s.auto_scroll_follow" not in src, (
        "boot.js must not use !!s.auto_scroll_follow — that defaults the True "
        "setting OFF for users with no saved value"
    )
    # Fallback (no-settings) path must also default true for a fresh user
    # (no persisted mirror), while honoring a persisted OFF (#6819).
    assert "window._autoScrollFollow=_readPersistedAutoScrollFollow()" in src, (
        "boot.js fallback path must read the persisted auto-follow mirror "
        "(defaults true for fresh users, honors saved OFF after a transient "
        "settings-fetch failure)"
    )


def test_settings_checkbox_renders_checked_by_default():
    """The Appearance checkbox must render checked when the setting is absent,
    matching the True default (panels.js settings-load)."""
    src = _read("static/panels.js")
    assert "autoScrollFollowCb.checked=settings.auto_scroll_follow!==false" in src, (
        "the auto-follow checkbox must default checked (=== false), not "
        "!!settings.auto_scroll_follow which would render it unchecked by default"
    )


def test_follow_gate_references_auto_scroll_follow_and_unpin():
    """The DOM-replace follow gate must consult both _autoScrollFollow (the
    setting) and _messageUserUnpinned (the user's scroll-up), so the opt-out and
    the read-while-streaming behaviors both hold."""
    src = _read("static/ui.js")
    assert "_shouldFollowMessagesOnDomReplace" in src
    assert "_autoScrollFollow" in src and "_messageUserUnpinned" in src, (
        "the follow gate must reference both _autoScrollFollow and _messageUserUnpinned"
    )


def test_ui_sync_init_defaults_auto_scroll_follow_before_consumers():
    """ui.js must establish window._autoScrollFollow synchronously at the
    top-level scroll-state section — before boot.js hydrates it from the awaited
    settings request. ui.js/messages.js load before boot.js (index.html), so any
    scroll listener / settle path / scrollIfPinned / DOM-replace gate running in
    that window reads an undeclared binding and throws ReferenceError (#6606).

    The typeof guard makes the default `true` WITHOUT clobbering an explicit
    saved `false` if boot.js already hydrated it."""
    src = _read("static/ui.js")
    assert "if(typeof window._autoScrollFollow==='undefined'){ window._autoScrollFollow=true; }" in src, (
        "ui.js must synchronously default window._autoScrollFollow to true before "
        "boot.js hydration (bare reads otherwise throw ReferenceError pre-boot)"
    )
    # The sync init must sit BEFORE every consumer it protects in the file.
    init_idx = src.index("window._autoScrollFollow=true;")
    scroll_listener_idx = src.index("!movedUp && window._autoScrollFollow && _scrollPinned")
    gate_idx = src.index("window._autoScrollFollow && !_messageUserUnpinned")
    settle_idx = src.index("!window._autoScrollFollow&&!explicit")
    pinned_idx = src.index("function scrollIfPinned")
    for label, idx in (
        ("scroll listener", scroll_listener_idx),
        ("DOM-replace gate", gate_idx),
        ("settle guard", settle_idx),
        ("scrollIfPinned", pinned_idx),
    ):
        assert init_idx < idx, f"sync init must precede the {label} consumer"


def test_ui_readers_use_explicit_window_owner():
    """Every _autoScrollFollow reader in ui.js must go through the explicit
    window._autoScrollFollow owner — no bare undeclared reads that can throw
    ReferenceError before boot.js assigns the global (#6606)."""
    src = _read("static/ui.js")
    # Any bare `_autoScrollFollow` token not preceded by `window.` is a bug.
    for m in re.finditer(r"(?<![\w.])_autoScrollFollow", src):
        before = src[max(0, m.start() - 7):m.start()]
        assert before == "window.", (
            f"bare _autoScrollFollow read at ui.js offset {m.start()}: "
            f"...{src[max(0, m.start()-40):m.start()+20]!r}"
        )


@pytest.mark.skipif(shutil.which("node") is None, reason="node required for behavioral test")
def test_auto_scroll_follow_undefined_true_false_behavior():
    """Behavioral: extract the real follow expressions from ui.js and evaluate
    them in Node across undefined / true / false:

    - undefined must NOT throw (property read, not a bare undeclared binding)
      and must not block an explicit bottom settlement;
    - false must suppress automatic following (DOM-replace gate, settle guard,
      scrollIfPinned) WITHOUT suppressing explicit bottom settlement;
    - true keeps following enabled."""
    src = _read("static/ui.js")
    # Extract the exact live expressions (not hand-copied replicas).
    dom_replace_expr = "window._autoScrollFollow && !_messageUserUnpinned && (_scrollPinned || _isMessagePaneNearBottom(120))"
    assert dom_replace_expr in src, "DOM-replace follow gate expression changed"
    settle_expr = "(!window._autoScrollFollow&&!explicit)||!_scrollPinned||_messageUserUnpinned||_recentNonMessageScrollIntent()"
    assert settle_expr in src, "settle guard expression changed"
    pinned_guard_expr = "!window._autoScrollFollow"
    assert f"if({pinned_guard_expr}) return;" in src, "scrollIfPinned guard changed"

    def run(expr, follow, explicit=False, pinned=True, unpinned=False, near=True):
        follow_js = "undefined" if follow is None else ("true" if follow else "false")
        harness = textwrap.dedent(f"""
            const window = {{ _autoScrollFollow: {follow_js} }};
            let _scrollPinned = {str(pinned).lower()};
            let _messageUserUnpinned = {str(unpinned).lower()};
            let explicit = {str(explicit).lower()};
            function _isMessagePaneNearBottom(px){{ return {str(near).lower()}; }}
            function _recentNonMessageScrollIntent(){{ return false; }}
            let _out;
            try {{
              _out = ({expr});
            }} catch (e) {{
              console.error('THREW: ' + e);
              process.exit(2);
            }}
            console.log(JSON.stringify(_out === undefined ? '__UNDEF__' : _out));
        """)
        res = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        val = json.loads(res.stdout.strip())
        return None if val == "__UNDEF__" else val

    # undefined: property read → falsy, never throws, explicit settle still allowed.
    assert not run(dom_replace_expr, None)          # undefined && … → undefined (falsy)
    assert run(settle_expr, None) is True          # suppress implicit settle
    assert run(settle_expr, None, explicit=True) is False  # explicit settle proceeds
    assert run(pinned_guard_expr, None) is True    # scrollIfPinned returns early
    # true: follow enabled everywhere.
    assert run(dom_replace_expr, True) is True
    assert run(settle_expr, True) is False         # implicit settle proceeds
    assert run(pinned_guard_expr, True) is False   # scrollIfPinned does NOT early-return
    # false: automatic follow suppressed…
    assert run(dom_replace_expr, False) is False
    assert run(settle_expr, False) is True
    assert run(pinned_guard_expr, False) is True
    # …but an EXPLICIT bottom settlement still runs (false must not block ↓/open).
    assert run(settle_expr, False, explicit=True) is False
    # Unpinned reader is never re-followed, regardless of the setting.
    assert run(dom_replace_expr, True, unpinned=True) is False
