"""Regression coverage for transcript virtualization preference (#4325 + #4343 + #6151).

The stream-end freeze/jump fix (#4328, semantic viewport anchoring) is covered by
test_issue500_message_list_virtualization.py. This file covers the Preferences
toggle and its contract changes:

- #4325 added an opt-OUT toggle (default ON).
- #4343 flipped it to EXPERIMENTAL / opt-IN (default OFF) because virtualization
  caused a scroll-up flicker on long sessions, with a force-off-for-everyone
  migration: a stored virtualize_transcript=True from the #4325 window is reset
  to off unless an explicit post-flip opt-in marker (virtualize_transcript_optin)
  is present.
- #6151 re-enables DOM virtualization as the DEFAULT (ON) after the #4346
  Phase B footer-jitter fix resolved the scroll flicker root cause. The
  force-off migration and the opt-in marker are removed: the server defaults to
  True, an unset user gets ON, and a stored False (explicit opt-out) is honored.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
PANELS = REPO_ROOT / "static" / "panels.js"
BOOT = REPO_ROOT / "static" / "boot.js"
UI = REPO_ROOT / "static" / "ui.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"


def test_virtualize_transcript_setting_is_default_on_and_allowed():
    """#6151 default-ON contract: default True, bool-allowlisted, and the
    #4343 opt-in marker is demoted from a live default to a legacy key that is
    dropped on load (so it can never be exposed or re-persisted)."""
    src = CONFIG.read_text(encoding="utf-8")
    assert '"virtualize_transcript": True' in src, "must default ON (#6151)"
    assert '"virtualize_transcript",' in src, "must be in _SETTINGS_BOOL_KEYS"
    # The opt-in marker is gone from the defaults and bool-key tables...
    assert '"virtualize_transcript_optin":' not in src, "opt-in marker must not be a live default"
    # ...but MUST be listed in _SETTINGS_LEGACY_DROP_KEYS so an existing
    # marker from the #4343 era is dropped on load (review #6155).
    legacy_start = src.index("_SETTINGS_LEGACY_DROP_KEYS = {")
    legacy_end = src.index("_COMPOSER_CONTROL_ORDER_KEYS", legacy_start)
    legacy_block = src[legacy_start:legacy_end]
    assert '"virtualize_transcript_optin",' in legacy_block, "opt-in marker must be in _SETTINGS_LEGACY_DROP_KEYS"


def test_settings_preferences_expose_virtualize_toggle_default_on():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsVirtualizeTranscript"' in html
    assert 'data-i18n="settings_label_virtualize_transcript"' in html
    assert 'data-i18n="settings_desc_virtualize_transcript"' in html
    # The checkbox is hydrated from the server value (!==false) on load, so the
    # static HTML must NOT pre-check it — a default-on user gets checked via JS.
    cb_line = next(l for l in html.splitlines() if 'id="settingsVirtualizeTranscript"' in l)
    assert "checked" not in cb_line, "default-on is applied by JS hydration, not a static checked attribute"


def test_boot_applies_saved_virtualize_preference_default_on():
    js = BOOT.read_text(encoding="utf-8")
    # #6151 default-on semantics: !==false (only an explicit false disables it).
    assert "window._virtualizeTranscript=s.virtualize_transcript!==false" in js
    # #6151 review: a settings-load failure must fail CLOSED — never enable the
    # long-transcript renderer when the user's saved preference cannot be
    # recovered (an explicit saved false is not fail-safe otherwise).
    assert "window._virtualizeTranscript=false" in js


def test_ui_gate_forces_full_render_when_disabled():
    js = UI.read_text(encoding="utf-8")
    start = js.index("function _currentMessageVirtualWindow(")
    body = js[start:start + 900]
    assert "_virtualizeTranscript===false" in body
    assert "virtualized:false" in body


def _ui_function(ui: str, name: str) -> str:
    """Extract `function <name>(...) { ... }` from ui.js via brace matching."""
    start = ui.index(f"function {name}(")
    open_idx = ui.index("{", start)
    depth = 0
    i = open_idx
    while i < len(ui):
        if ui[i] == "{":
            depth += 1
        elif ui[i] == "}":
            depth -= 1
            if depth == 0:
                return ui[start:i + 1]
        i += 1
    raise AssertionError(f"could not extract function {name}")


def _locale_bundle(js: str, locale: str) -> str:
    """Extract the `locale: { ... }` block from i18n.js via brace matching."""
    start = js.index(f"\n  {locale}: {{")
    open_idx = js.index("{", start)
    depth = 0
    i = open_idx
    while i < len(js):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
        i += 1
    raise AssertionError(f"could not extract locale bundle {locale!r} from i18n.js")


def test_boot_failure_fails_closed_and_returns_full_range():
    """#6151 review: when the settings request fails, the broad boot catch must
    fail CLOSED (window._virtualizeTranscript=false), and the UI gate must then
    return virtualized:false with the complete range — never a partial window."""
    node = shutil.which("node")
    assert node is not None, "node is required for the frontend behavior harness"

    boot = BOOT.read_text(encoding="utf-8")
    # The catch path after a rejected settings load must fail closed.
    assert "window._virtualizeTranscript=false" in boot

    ui = UI.read_text(encoding="utf-8")
    fn = _ui_function(ui, "_currentMessageVirtualWindow")
    harness = """
const vm=require('vm');
const sandbox={
  window:{_virtualizeTranscript:false},
  $:()=>null,
  _syncMessageVirtualHeightCache:()=>{},
};
vm.createContext(sandbox);
vm.runInContext(__FN__,sandbox);
const win=sandbox._currentMessageVirtualWindow([{},{},{}],0);
if(win.virtualized!==false) throw new Error('fail-closed gate did not disable virtualization');
if(win.start!==0||win.end!==3||win.total!==3) throw new Error('fail-closed gate did not return the complete range');
if(win.topPad!==0||win.bottomPad!==0) throw new Error('fail-closed gate must not pad a full render');
if(win.tailStart!==3) throw new Error('fail-closed gate tailStart mismatch');
""".replace("__FN__", json.dumps(fn))
    subprocess.run([node, "-e", harness], check=True, capture_output=True, text=True)


def test_panels_round_trip_and_hot_apply_virtualize_toggle():
    js = PANELS.read_text(encoding="utf-8")
    assert "const virtualizeTranscriptCb=$('settingsVirtualizeTranscript');" in js
    assert "payload.virtualize_transcript=virtualizeTranscriptCb.checked;" in js
    # #6151: the #4343 opt-in save marker is gone (no force-off migration).
    assert "payload.virtualize_transcript_optin" not in js
    # #6151: checkbox load reflects the default-on contract (!==false), so a
    # default-on user sees it checked and a saved opt-out shows unchecked.
    assert "virtualizeTranscriptCb.checked=settings.virtualize_transcript!==false;" in js
    assert "window._virtualizeTranscript=virtualizeTranscriptCb.checked;" in js
    # Hot-apply: toggling re-renders the open transcript immediately.
    assert "renderMessages({preserveScroll:true})" in js


def test_virtualize_toggle_i18n_all_locales():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_virtualize_transcript:") == 15
    assert js.count("settings_desc_virtualize_transcript:") == 15
    # #6151 review (#6155): the stale-copy negatives are scoped to the
    # virtualization label/description entries ONLY. A whole-catalog negative
    # is invalid: the generic "off by default" phrases legitimately occur in
    # unrelated settings that genuinely default to off (e.g. French
    # settings_desc_conversation_outline, Czech/Vietnamese quota/outline
    # descriptions), so `assert old_phrase not in js` is guaranteed-red.
    stale_by_locale = {
        "ja": ("（実験的）", "デフォルトではオフになっています"),
        "fr": ("Virtualiser les longs historiques (expérimental)", "Désactivé par défaut"),
        "cs": ("Virtualizovat dlouhé transcripty (experimentální)", "Standardně vypnuto"),
        "vi": ("Ảo hóa transcript dài (thử nghiệm)", "Mặc định tắt"),
    }
    for locale, stale_phrases in stale_by_locale.items():
        bundle = _locale_bundle(js, locale)
        for key in ("settings_label_virtualize_transcript", "settings_desc_virtualize_transcript"):
            m = re.search(rf"{key}:\s*'((?:[^'\\]|\\.)*)'", bundle)
            assert m is not None, f"{locale}: missing {key} entry"
            for old_phrase in stale_phrases:
                assert old_phrase not in m.group(1), (
                    f"{locale} {key} still carries stale default-OFF copy: {old_phrase!r}"
                )
    # ...and each of the four bundles now carries default-ON copy.
    for new_phrase in (
        "デフォルトではオンです",  # ja: on by default
        "Activé par défaut",  # fr: enabled by default
        "Standardně zapnuto",  # cs: enabled by default
        "Bật theo mặc định",  # vi: on by default
    ):
        assert new_phrase in js, f"default-ON copy missing: {new_phrase!r}"


# ── #6151 default-on load behavior (load_settings) ──────────────────────────


@pytest.fixture
def _settings_env(tmp_path, monkeypatch):
    """Point load_settings at an isolated settings.json under tmp."""
    import api.config as config

    sf = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", sf)
    return config, sf


def _write(sf, payload):
    sf.write_text(json.dumps(payload), encoding="utf-8")


def test_migration_unset_defaults_on(_settings_env):
    """No stored value (fresh / pre-#4325 install) → on (#6151 default)."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_stored_true_is_honored(_settings_env):
    """A stored virtualize_transcript=True (from the #4325 window or a later
    explicit enable) is honored — with default-ON there is no force-off reset."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": True})
    assert config.load_settings()["virtualize_transcript"] is True


def test_migration_stored_false_optout_is_honored(_settings_env):
    """A stored False (explicit opt-out) stays off — the saved opt-out is never
    overridden by the default-ON contract."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, "virtualize_transcript": False})
    assert config.load_settings()["virtualize_transcript"] is False


def test_migration_legacy_optin_marker_dropped_and_optout_preserved(_settings_env):
    """#6151 review: a settings.json that still carries the #4343 opt-in marker
    (virtualize_transcript_optin) plus a stored explicit False must preserve the
    opt-out and drop the marker from loaded, returned, and rewritten settings."""
    config, sf = _settings_env
    _write(sf, {
        "onboarding_completed": True,
        "virtualize_transcript": False,
        "virtualize_transcript_optin": True,
    })
    loaded = config.load_settings()
    assert loaded["virtualize_transcript"] is False, "explicit opt-out must be preserved"
    assert "virtualize_transcript_optin" not in loaded, "legacy marker must be dropped from loaded settings"
    saved = config.save_settings({"show_tps": True})
    assert "virtualize_transcript_optin" not in saved, "legacy marker must be absent from the returned settings"
    raw = json.loads(sf.read_text(encoding="utf-8"))
    assert "virtualize_transcript_optin" not in raw, "legacy marker must not be re-persisted on save"
    assert raw.get("virtualize_transcript") is False, "opt-out must survive a save"
