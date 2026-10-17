"""Tests for issue #8055: profile display_name reaches the chip/titlebar/assistant name.

A renamed profile (``profile.yaml`` -> ``display_name``) already showed its
display name in the Profiles panel and the composer dropdown, but the composer
chip, the titlebar label and the assistant name in the transcript still
rendered the canonical id — so the same profile read differently depending on
where you looked.

Fix: ``/api/profiles``, ``/api/profile/active`` and ``/api/profile/switch``
now carry ``display_name``, and the client renders the chip / titlebar /
assistant name from it (falling back to the canonical id when unset). The
canonical id stays the identity for switching and every API call.
"""

from pathlib import Path

import json
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
SESSIONS_JS = (REPO_ROOT / "static" / "sessions.js").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
PROFILES_PY = (REPO_ROOT / "api" / "profiles.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


# ── API surface ────────────────────────────────────────────────────────────


def test_profile_display_name_helper_reads_meta_and_never_raises(tmp_path, monkeypatch):
    """_profile_display_name returns the meta value, '' when unreadable."""
    import sys
    import types

    import api.profiles as p

    (tmp_path / "profile.yaml").write_text("display_name: Base Profile\n", encoding="utf-8")

    # The helper imports hermes_cli.profiles lazily; provide a stub module so
    # the test does not depend on the agent checkout being importable here.
    stub = types.ModuleType("hermes_cli.profiles")
    stub.read_profile_meta = lambda _dir: {"display_name": "Base Profile"}  # noqa: B010
    agent_stub = types.ModuleType("hermes_cli")
    agent_stub.profiles = stub  # noqa: B010
    monkeypatch.setitem(sys.modules, "hermes_cli", agent_stub)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", stub)

    assert p._profile_display_name(tmp_path) == "Base Profile"

    # Unreadable meta must degrade to '' rather than 500 the whole payload.
    def _boom(_dir):
        raise OSError("nope")

    stub.read_profile_meta = _boom  # noqa: B010
    assert p._profile_display_name(tmp_path) == ""


def test_profile_rows_carry_display_name(monkeypatch):
    """Every list_profiles_api row shape must include the display_name key."""
    import api.profiles as p

    rows = p.list_profiles_api()
    assert rows, "expected at least one profile row"
    for row in rows:
        assert "display_name" in row
        assert isinstance(row["display_name"], str)


def test_active_profile_endpoint_returns_display_name():
    assert '"display_name": profiles_api._profile_display_name(' in ROUTES_PY


def test_switch_profile_response_returns_display_name(monkeypatch, tmp_path):
    """The switch response carries the newly active profile's display name."""
    import api.profiles as p

    profile_dir = tmp_path / "work"
    profile_dir.mkdir()

    monkeypatch.setattr(p, "list_profiles_api", lambda: [
        {"name": "work", "display_name": "Client A", "is_default": False, "path": str(profile_dir)},
    ])
    monkeypatch.setattr(p, "get_active_profile_name", lambda: "work")
    monkeypatch.setattr(p, "_is_root_profile", lambda _n: False)
    monkeypatch.setattr(p, "_resolve_named_profile_home", lambda _n: profile_dir)
    monkeypatch.setattr("api.config.invalidate_models_cache", lambda **kw: None)

    result = p.switch_profile("work", process_wide=False)

    assert result["active"] == "work"
    assert result["display_name"] == "Client A"


# ── Client rendering ───────────────────────────────────────────────────────


def test_assistant_display_name_prefers_profile_display_name():
    """The transcript's assistant name uses the display name when present."""
    idx = UI_JS.find("function assistantDisplayName(){")
    assert idx != -1
    body = UI_JS[idx: idx + 600]
    assert "S.activeProfileDisplayName" in body
    # Canonical id remains the fallback, so unset display names are unchanged.
    assert "S.activeProfile.charAt(0).toUpperCase()" in body


def test_profile_chip_label_helper_prefers_display_name():
    idx = UI_JS.find("function profileChipDisplayLabel(){")
    assert idx != -1
    body = UI_JS[idx: idx + 300]
    assert "S.activeProfileDisplayName" in body
    assert "S.activeProfile||'default'" in body


def test_no_profile_chip_written_from_canonical_id():
    """No surface may write S.activeProfile straight into the chip/titlebar."""
    for name, src in (("boot.js", BOOT_JS), ("ui.js", UI_JS), ("panels.js", PANELS_JS)):
        assert "profileChipLabel" in src, name
    for src in (BOOT_JS, UI_JS):
        assert "profileLabel.textContent=S.activeProfile||'default'" not in src
        assert "_profileLabel.textContent=S.activeProfile||'default'" not in src
        assert "titleLabel.textContent=S.activeProfile||'default'" not in src
        assert "_titleLabel.textContent=S.activeProfile||'default'" not in src


def test_switch_paths_cache_display_name_on_session_state():
    """Both switch entry points read display_name off the API response."""
    assert "S.activeProfileDisplayName = data.display_name" in PANELS_JS
    assert "S.activeProfileDisplayName=data.display_name" in SESSIONS_JS


def test_boot_caches_display_name_from_active_profile():
    assert "S.activeProfileDisplayName = p.display_name" in BOOT_JS


def test_profile_rows_include_display_name_in_fast_and_slow_paths():
    """Both the fast row builder and the upstream fallback carry the field."""
    assert "'display_name': _profile_display_name(home)," in PROFILES_PY
    assert "'display_name': str(getattr(p, 'display_name', '') or '')," in PROFILES_PY


def test_isolated_constructed_fallback_row_carries_display_name(monkeypatch, tmp_path):
    """The isolated no-matching-upstream-row / error fallback in
    list_profiles_api() must carry display_name too, resolved from the pinned
    isolated home (#8055 round-2).

    The fast path, the matched-isolated path and the ordinary upstream path
    all already emit the key; this test forces the constructed fallback branch
    (hermes_cli.profiles.list_profiles absent) and pins the key there as well.
    """
    import sys
    import types

    import api.profiles as p

    home = tmp_path / "profiles" / "work"
    home.mkdir(parents=True)
    (home / "profile.yaml").write_text("display_name: Client A\n", encoding="utf-8")

    stub = types.ModuleType("hermes_cli.profiles")
    stub.read_profile_meta = lambda _dir: {"display_name": "Client A"}  # noqa: B010
    agent_stub = types.ModuleType("hermes_cli")
    agent_stub.profiles = stub  # noqa: B010
    monkeypatch.setitem(sys.modules, "hermes_cli", agent_stub)
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", stub)
    # Isolated mode with NO upstream list_profiles -> the constructed fallback row.
    monkeypatch.setattr(p, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(p, "_INITIAL_HERMES_HOME", str(home))
    monkeypatch.setattr(p, "_get_profile_skills_stats", lambda _h: (0, 0))

    rows = p.list_profiles_api()

    assert [r["name"] for r in rows] == ["work"]
    row = rows[0]
    assert "display_name" in row, "isolated fallback row must carry display_name"
    assert isinstance(row["display_name"], str)
    assert row["display_name"] == "Client A"


def _render_profile_dropdown_body(src: str) -> str:
    """Return the full source of renderProfileDropdown()."""
    start = src.find("function renderProfileDropdown(")
    assert start != -1, "renderProfileDropdown not found in panels.js"
    i = src.find("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start : j + 1]
    raise AssertionError("could not find end of renderProfileDropdown()")


def test_render_profile_dropdown_titlebar_write_uses_presentation_helper():
    """renderProfileDropdown()'s trailing titlebar write must use the same
    active-profile label resolver (display name), not the canonical id — the
    mutation observer recorded `Client A -> work` when the dropdown opened and
    the phone DOM kept `work` (#8055 round-2)."""
    body = _render_profile_dropdown_body(PANELS_JS)
    idx = body.find("const tbl = $('titlebarProfileLabel')")
    assert idx != -1, "titlebar label sync not found in renderProfileDropdown"
    tail = body[idx:]
    assert "tbl.textContent = profileChipDisplayLabel()" in tail, (
        "renderProfileDropdown must write the display-name-aware label, "
        "not the canonical id"
    )
    assert "tbl.textContent = active" not in tail, (
        "renderProfileDropdown must NOT write the canonical id to the titlebar"
    )


_SWITCH_ROLLBACK_DRIVER = r"""
const fs = require('fs');
const uiSrc = fs.readFileSync(process.argv[2], 'utf8');
const panelsSrc = fs.readFileSync(process.argv[3], 'utf8');
function extractFunc(src, name) {
  const re = new RegExp('(?:async\\s+)?function\\s+' + name + '\\s*\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') depth--;
    i++;
  }
  return src.slice(start, i);
}
function makeEl() {
  return {textContent: '', disabled: false, classList: {add(){}, remove(){}, toggle(){}, contains(){return false;}}, style: {}};
}
const els = {};
global.$ = (id) => { if (!els[id]) els[id] = makeEl(); return els[id]; };
global.S = {activeProfile: 'work', activeProfileIsDefault: false, activeProfileDisplayName: 'Client A', session: null, messages: [], _pendingSessionToolsets: null};
global.t = (k) => k;
global.showToast = () => {};
global.applyBotName = () => {};
global.syncAppTitlebar = () => {};
global.syncTopbar = () => {};
global.startGatewaySSE = () => {};
global.newSession = async () => {};
global.renderSessionList = async () => {};
global.renderSessionListFromCache = () => {};
global.loadDir = () => {};
global.clearWorkspaceTreeSkeleton = () => {};
global.showSessionListSkeleton = () => {};
global.bumpWorkspaceTreeGen = () => {};
global.closeSessionActionMenu = () => {};
global._invalidateSessionListRenders = () => {};
global._setProfileSwitchListEmbargo = () => {};
global._maybeReplayChatTodosRailSync = () => {};
global._profileSwitchPanelLoad = async () => {};
global._refreshProfileSwitchBackground = () => {};
global._openProfileSwitchSessionBrowser = () => {};
global.animateNextSessionListRefresh = () => {};
global._clearPersistedModelState = () => {};
global._profileMatchesActiveProfile = () => true;
global._sessionListSkeletonActive = false;
var _profileSwitchGeneration = 0;
var _workspacePanelMode = 'closed';
eval(extractFunc(uiSrc, 'profileChipDisplayLabel'));
eval(extractFunc(panelsSrc, 'switchToProfile'));
global.api = (url) => {
  if (url === '/api/profile/switch') return Promise.reject(new Error('rejected'));
  return Promise.resolve({});
};
(async () => {
  const returned = await switchToProfile('vops');
  console.log(JSON.stringify({
    returned,
    activeProfile: S.activeProfile,
    activeProfileDisplayName: S.activeProfileDisplayName,
    activeProfileIsDefault: S.activeProfileIsDefault,
    chipLabel: $('profileChipLabel').textContent,
    titlebarLabel: $('titlebarProfileLabel').textContent,
  }));
})().catch(e => { console.error(e); process.exit(1); });
"""


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_failed_switch_rolls_back_previous_identity_name_pair(tmp_path):
    """Behavioral: a rejected /api/profile/switch must restore the previous
    identity/name pair — S.activeProfileDisplayName is cleared before the POST
    and must come back on failure so the chip/titlebar/assistant keep the
    friendly name (`Client A`), not the canonical id (`work`) (#8055 round-2)."""
    driver = tmp_path / "switch_rollback_driver.js"
    driver.write_text(_SWITCH_ROLLBACK_DRIVER, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(driver), str(REPO_ROOT / "static" / "ui.js"), str(REPO_ROOT / "static" / "panels.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"node driver failed:\nSTDOUT={result.stdout}\nSTDERR={result.stderr}")
    got = json.loads(result.stdout)

    assert got["returned"] is False
    # The server rejected the POST: the previous profile is still active.
    assert got["activeProfile"] == "work"
    # The cleared display name must be restored with the previous identity.
    assert got["activeProfileDisplayName"] == "Client A", (
        "failed switch must restore the previous display name, got: "
        + repr(got["activeProfileDisplayName"])
    )
    assert got["activeProfileIsDefault"] is False
    # The labels resolve through the presentation helper -> friendly name.
    assert got["chipLabel"] == "Client A", "chip label must show the restored display name"
    assert got["titlebarLabel"] == "Client A", "titlebar label must show the restored display name"
