"""Regression coverage for #6160: optional hiding of the full new-chat welcome panel."""
from pathlib import Path
import json
import shutil
import subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
STYLE = REPO_ROOT / "static" / "style.css"
PANELS = REPO_ROOT / "static" / "panels.js"
BOOT = REPO_ROOT / "static" / "boot.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"


def test_hide_welcome_panel_setting_is_default_off_and_boolean():
    src = CONFIG.read_text(encoding="utf-8")
    assert '"hide_empty_state_panel": False' in src
    assert '"hide_empty_state_panel",' in src


def test_hide_welcome_panel_setting_persists_and_coerces_boolean(monkeypatch, tmp_path):
    import api.config as config

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_path)

    assert config.load_settings()["hide_empty_state_panel"] is False

    saved = config.save_settings({"hide_empty_state_panel": True})
    assert saved["hide_empty_state_panel"] is True
    assert json.loads(settings_path.read_text(encoding="utf-8"))["hide_empty_state_panel"] is True

    saved = config.save_settings({"hide_empty_state_panel": 0})
    assert saved["hide_empty_state_panel"] is False
    assert config.load_settings()["hide_empty_state_panel"] is False


def test_preferences_expose_hide_welcome_panel_toggle():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="settingsHideEmptyStatePanel"' in html
    assert 'data-i18n="settings_label_hide_empty_state_panel"' in html
    assert 'data-i18n="settings_desc_hide_empty_state_panel"' in html


def test_empty_state_has_non_important_full_panel_hide_hook():
    css = STYLE.read_text(encoding="utf-8")
    assert ".empty-state.no-welcome{display:none}" in css
    assert ".empty-state.no-welcome{display:none!important}" not in css


def _boot_function(boot: str, name: str) -> str:
    start = boot.index(f"function {name}()")
    end = boot.index("\n}\n", start) + 2
    return boot[start:end]


def test_boot_preference_hides_and_restores_the_panel_observably():
    node = shutil.which("node")
    assert node is not None, "node is required for the frontend behavior harness"

    boot = BOOT.read_text(encoding="utf-8")
    function_src = _boot_function(boot, "applyEmptyStatePanelPref")
    harness = f"""
const vm=require('vm');
const classes=new Set();
const emptyState={{classList:{{
  toggle(name, enabled){{ if(enabled) classes.add(name); else classes.delete(name); }},
  contains(name){{ return classes.has(name); }}
}}}};
const sandbox={{window:{{_hideEmptyStatePanel:false}}, $:(id)=>id==='emptyState'?emptyState:null}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(function_src)},sandbox);
sandbox.applyEmptyStatePanelPref();
if(emptyState.classList.contains('no-welcome')) throw new Error('default-off hid the panel');
sandbox.window._hideEmptyStatePanel=true;
sandbox.applyEmptyStatePanelPref();
if(!emptyState.classList.contains('no-welcome')) throw new Error('enabled preference did not hide the panel');
sandbox.window._hideEmptyStatePanel=false;
sandbox.applyEmptyStatePanelPref();
if(emptyState.classList.contains('no-welcome')) throw new Error('disabling preference did not restore the panel');
"""
    subprocess.run([node, "-e", harness], check=True, capture_output=True, text=True)

    assert "window._hideEmptyStatePanel=s.hide_empty_state_panel===true" in boot
    assert "window._hideEmptyStatePanel=false" in boot


def test_restoring_panel_preserves_independent_hide_suggestions_preference():
    node = shutil.which("node")
    assert node is not None, "node is required for the frontend behavior harness"

    boot = BOOT.read_text(encoding="utf-8")
    functions = "\n".join(
        _boot_function(boot, name)
        for name in ("applyEmptyStateSuggestionPref", "applyEmptyStatePanelPref")
    )
    harness = f"""
const vm=require('vm');
const classes=new Set();
const emptyState={{classList:{{toggle(name,enabled){{if(enabled)classes.add(name);else classes.delete(name);}}}}}};
const sandbox={{window:{{_hideEmptyStateSuggestions:true,_hideEmptyStatePanel:true}},$:(id)=>id==='emptyState'?emptyState:null}};
vm.createContext(sandbox);
vm.runInContext({json.dumps(functions)},sandbox);
sandbox.applyEmptyStateSuggestionPref();
sandbox.applyEmptyStatePanelPref();
if(!classes.has('no-suggestions')||!classes.has('no-welcome')) throw new Error('combined state was not applied');
sandbox.window._hideEmptyStatePanel=false;
sandbox.applyEmptyStatePanelPref();
if(classes.has('no-welcome')) throw new Error('panel was not restored');
if(!classes.has('no-suggestions')) throw new Error('restoring panel lost the suggestions preference');
"""
    subprocess.run([node, "-e", harness], check=True, capture_output=True, text=True)


def test_panels_round_trip_and_hot_apply_hide_welcome_panel():
    js = PANELS.read_text(encoding="utf-8")
    assert "payload.hide_empty_state_panel=hideEmptyStatePanelCb.checked;" in js
    assert "hideEmptyStatePanelCb.checked=settings.hide_empty_state_panel===true;" in js
    assert "window._hideEmptyStatePanel=hideEmptyStatePanelCb.checked;" in js
    assert "if(typeof applyEmptyStatePanelPref==='function') applyEmptyStatePanelPref();" in js


def test_hide_welcome_panel_copy_covers_every_locale():
    js = I18N.read_text(encoding="utf-8")
    assert js.count("settings_label_hide_empty_state_panel:") == 15
    assert js.count("settings_desc_hide_empty_state_panel:") == 15
    assert "settings_label_hide_empty_state_panel: 'Ukryj panel powitalny nowej konwersacji'" in js
