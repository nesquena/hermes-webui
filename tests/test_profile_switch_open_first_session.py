"""Regression coverage for opening the first profile session on profile switch.

The preference is opt-in and global to WebUI navigation. The selected session
must be the first visible sidebar row owned by the destination profile; rows
from other profiles must never be opened implicitly.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(src: str, name: str) -> str:
    match = re.search(rf"(?:async\s+)?function\s+{re.escape(name)}\s*\(", src)
    assert match is not None, f"{name}() not found"
    start = match.start()
    paren = src.index("(", match.start())
    paren_depth = 1
    i = paren + 1
    while paren_depth and i < len(src):
        if src[i] == "(":
            paren_depth += 1
        elif src[i] == ")":
            paren_depth -= 1
        i += 1
    assert paren_depth == 0, f"could not parse {name}() parameters"
    brace = src.index("{", i)
    depth = 1
    i = brace + 1
    while depth and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    assert depth == 0, f"could not extract {name}()"
    return src[start:i]


def _run_node(source: str) -> dict:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return json.loads(result.stdout)


def test_setting_is_registered_as_default_off_boolean():
    import api.config as config

    assert config._SETTINGS_DEFAULTS.get("open_first_session_on_profile_switch") is False
    assert "open_first_session_on_profile_switch" in config._SETTINGS_BOOL_KEYS


def test_setting_round_trips_through_global_settings_file(tmp_path, monkeypatch):
    import api.config as config

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    saved = config.save_settings({"open_first_session_on_profile_switch": True})
    assert saved["open_first_session_on_profile_switch"] is True
    assert config.load_settings()["open_first_session_on_profile_switch"] is True


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_first_visible_session_skips_other_profiles_and_preserves_sidebar_order():
    selector = _extract_function(SESSIONS_JS, "_profileSwitchFirstSession")
    script = f"""
{selector}
const sessions = [
  {{session_id:'foreign', profile:'other'}},
  {{session_id:'pinned', profile:'work', pinned:true}},
  {{session_id:'recent', profile:'work'}}
];
const selected = _profileSwitchFirstSession(
  sessions,
  ['foreign', 'pinned', 'recent'],
  'work',
  false
);
console.log(JSON.stringify(selected));
"""
    selected = _run_node(script)
    assert selected["session_id"] == "pinned"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_root_profile_alias_is_eligible_for_named_default_profile():
    selector = _extract_function(SESSIONS_JS, "_profileSwitchFirstSession")
    script = f"""
{selector}
const selected = _profileSwitchFirstSession(
  [{{session_id:'root-session', profile:'default'}}],
  ['root-session'],
  'renamed-root',
  true
);
console.log(JSON.stringify(selected));
"""
    selected = _run_node(script)
    assert selected["session_id"] == "root-session"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_switch_opener_returns_false_without_an_eligible_session():
    selector = _extract_function(SESSIONS_JS, "_profileSwitchFirstSession")
    opener = _extract_function(SESSIONS_JS, "_openFirstSessionForActiveProfile")
    script = f"""
{selector}
{opener}
global._allSessions = [{{session_id:'foreign', profile:'other'}}];
global._sessionVisibleSidebarIds = ['foreign'];
global.S = {{activeProfile:'work', activeProfileIsDefault:false}};
global._openSidebarSession = async function(){{ throw new Error('must not open'); }};
(async()=>{{
  const opened = await _openFirstSessionForActiveProfile();
  console.log(JSON.stringify({{opened}}));
}})().catch(err=>{{ console.error(err); process.exit(1); }});
"""
    assert _run_node(script) == {"opened": False}


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_profile_switch_opener_suppresses_cross_profile_draft_write():
    selector = _extract_function(SESSIONS_JS, "_profileSwitchFirstSession")
    opener = _extract_function(SESSIONS_JS, "_openFirstSessionForActiveProfile")
    script = f"""
{selector}
{opener}
global._allSessions = [{{session_id:'first', profile:'work'}}];
global._sessionVisibleSidebarIds = ['first'];
global.S = {{activeProfile:'work', activeProfileIsDefault:false}};
let received = null;
global._openSidebarSession = async function(session, opts){{ received={{session, opts}}; }};
(async()=>{{
  const opened = await _openFirstSessionForActiveProfile({{source:'profile-switch'}});
  console.log(JSON.stringify({{opened, received}}));
}})().catch(err=>{{ console.error(err); process.exit(1); }});
"""
    result = _run_node(script)
    assert result["opened"] is True
    assert result["received"]["session"]["session_id"] == "first"
    assert result["received"]["opts"]["skipComposerDraftSave"] is True
    assert result["received"]["opts"]["skipProfileResolve"] is True


def test_open_helper_reports_extension_veto_for_safe_new_chat_fallback():
    selector = _extract_function(SESSIONS_JS, "_profileSwitchFirstSession")
    opener = _extract_function(SESSIONS_JS, "_openFirstSessionForActiveProfile")
    script = f"""
{selector}
{opener}
global._allSessions = [{{session_id:'first', profile:'work'}}];
global._sessionVisibleSidebarIds = ['first'];
global.S = {{activeProfile:'work', activeProfileIsDefault:false}};
global._openSidebarSession = async function(){{ return false; }};
(async()=>{{
  const opened = await _openFirstSessionForActiveProfile();
  console.log(JSON.stringify({{opened}}));
}})().catch(err=>{{ console.error(err); process.exit(1); }});
"""
    assert _run_node(script) == {"opened": False}


def test_load_session_honors_skip_composer_draft_save_option():
    load_session = _extract_function(SESSIONS_JS, "loadSession")
    assert "!opts.skipComposerDraftSave" in load_session
    assert "_saveComposerDraftNow" in load_session


def test_profile_switch_uses_global_preference_after_list_refresh():
    switch = _extract_function(PANELS_JS, "switchToProfile")
    setting_idx = switch.index("window._openFirstSessionOnProfileSwitch===true")
    render_idx = switch.index("await renderSessionList();", setting_idx)
    open_idx = switch.index("await _openFirstSessionForActiveProfile", render_idx)
    assert setting_idx < render_idx < open_idx
    assert "await newSession(false" in switch[open_idx:]


def test_settings_ui_boot_and_i18n_are_wired():
    html = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    boot = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    i18n = (REPO_ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

    assert 'id="settingsOpenFirstSessionOnProfileSwitch"' in html
    assert "window._openFirstSessionOnProfileSwitch=!!s.open_first_session_on_profile_switch" in boot
    assert "payload.open_first_session_on_profile_switch" in PANELS_JS
    assert "settings_label_open_first_session_on_profile_switch" in i18n
    assert "settings_desc_open_first_session_on_profile_switch" in i18n
