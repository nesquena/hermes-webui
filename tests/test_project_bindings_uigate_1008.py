"""Focused regressions for the PR #6836 UX re-gate review of 2026-10-08T23:39:33Z.

That review (id 5464123333, submitted against head ``2a90c935``) closed the UX
review SHIP-WITH-UX-FIXES with three asks plus one note. One test per ask, in
the reviewer's order:

A [should fix] a hidden effort binding was still applied: the dialog cannot show
  or clear a stored ``reasoning_effort``, yet the project's + (new chat) still
  forwarded it into ``newSession``, which POSTs /api/reasoning and changes the
  PROFILE-wide effort for that model family while the chip reads "Default". The
  forward is now gone from ``_projectBindingsForNewSession`` (the binding itself
  is still stored by /api/projects/bind and returns with per-session effort,
  #7881).
B the "Type a path…" prompt hard-coded ``D:\\projects\\…`` on every host; the
  placeholder is now derived from a path the dialog already shows.
C touch targets: the workspace remove button and the Default pill rendered 22px
  square; both are >= 32px on narrow (phone) viewports.

D (note only, not required) is not asserted here.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BS = chr(92)          # a single backslash, spelled out so no escaping layer can eat it
ELLIPSIS = chr(8230)  # the U+2026 glyph


def _read_static(name: str) -> str:
    return (REPO_ROOT / "static" / name).read_text(encoding="utf-8")


def _read_sessions_js() -> str:
    return _read_static("sessions.js")


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


def _project_bindings_fn() -> str:
    return _slice(
        _read_sessions_js(),
        "function _projectBindingsForNewSession(project){",
        "function _attachProjectQuickCreateButton(chip, project){",
    )


def _placeholder_fn() -> str:
    return _slice(
        _read_sessions_js(),
        "function _wsPathPlaceholderFor(path){",
        "function _showProjectBindingsDialog(proj){",
    )


def _run_node(tmp_path: Path, name: str, script: str) -> str:
    if shutil.which("node") is None:
        pytest.skip("node is required for the frontend behavior probe")
    script_path = tmp_path / name
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


# ---------------------------------------------------------------------------
# A — a stored reasoning effort must not reach newSession (profile-wide apply)
# ---------------------------------------------------------------------------

_BINDINGS_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
globalThis.S = { activeProfile: 'default', session: null };
globalThis._profileMatchesActiveProfile = function () { return true; };
__FN__
const bound = _projectBindingsForNewSession({
  project_id: 'p1',
  profile: 'default',
  workspaces: ['D:/alpha'],
  default_workspace: 'D:/alpha',
  model: 'alpha-model',
  model_provider: 'alpha-prov',
  reasoning_effort: 'high',
});
assert(!('reasoning_effort' in bound),
  'a stored reasoning_effort must not be forwarded into newSession');
assert(bound.workspace === 'D:/alpha', 'the workspace binding still forwards');
assert(bound.model === 'alpha-model', 'the model binding still forwards');
assert(bound.model_provider === 'alpha-prov', 'the provider binding still forwards');
// An unbound project stays a bare {} (legacy behavior).
const bare = _projectBindingsForNewSession({ project_id: 'p2', profile: 'default', reasoning_effort: 'low' });
assert(Object.keys(bare).length === 0, 'an unbound project must yield no bindings');
// A foreign profile's project still yields nothing.
globalThis._profileMatchesActiveProfile = function () { return false; };
const foreign = _projectBindingsForNewSession({ profile: 'other', workspace: 'D:/x', reasoning_effort: 'high' });
assert(Object.keys(foreign).length === 0, 'a foreign profile project must yield no bindings');
console.log('ok');
"""


def test_a_stored_reasoning_effort_is_not_forwarded_into_new_session(tmp_path):
    """UX re-gate 2026-10-08T23:39:33Z, item A.

    The per-project dialog neither shows nor can clear a stored effort, so
    forwarding it made a plain "+" new chat silently rewrite the profile-wide
    effort for that model family (POST /api/reasoning) while the effort chip
    read "Default".
    """
    src = _read_sessions_js()
    fn = _project_bindings_fn()
    # The comment above the function explains the omission, so assert on the
    # code, not the word: no assignment of an effort binding may survive.
    assert "o.reasoning_effort" not in fn, (
        "_projectBindingsForNewSession must not forward a stored reasoning_effort "
        "until per-session effort exists (#7881)"
    )
    # The newSession merge site must not reintroduce it either.
    assert "_pb.reasoning_effort" not in src
    script = _BINDINGS_PROBE.replace("__FN__", fn)
    assert _run_node(tmp_path, "bindings_probe.js", script).strip() == "ok"


def test_the_dormant_effort_apply_branch_is_documented():
    """The /api/reasoning apply branch must stay, but be marked dormant: it is
    unreachable now that nothing forwards the option, and it is the landing site
    for per-session effort (#7881)."""
    src = _read_sessions_js()
    start = src.index("// Project-bound reasoning effort: apply after the session exists")
    end = src.index("if(boundEffort&&typeof api==='function'){", start)
    assert "DORMANT since the UX re-gate 2026-10-08T23:39:33Z (item A)" in src[start:end], (
        "the /api/reasoning apply branch must be marked dormant now that nothing "
        "forwards reasoning_effort"
    )


# ---------------------------------------------------------------------------
# B — the add-workspace prompt placeholder is no longer Windows-only
# ---------------------------------------------------------------------------


def test_the_workspace_path_placeholder_is_not_windows_only():
    """UX re-gate 2026-10-08T23:39:33Z, item B."""
    src = _read_sessions_js()
    assert "'D:" + BS + BS + "projects" not in src, (
        "the add-workspace prompt must not hard-code a Windows drive"
    )
    assert "_wsPathPlaceholderFor(" in src
    fn = _placeholder_fn()
    assert "pb_enter_ws_path" in fn, "an unknown path style must fall back to the localized hint"


_PLACEHOLDER_PROBE = """
function assert(cond, msg) { if (!cond) throw new Error(msg); }
globalThis.t = function (k) { return k; };
__FN__
assert(_wsPathPlaceholderFor('D:' + __BS__ + 'projects' + __BS__ + 'hermes-webui') ===
  'D:' + __BS__ + 'projects' + __BS__ + __ELL__,
  'a Windows path must yield its parent directory');
assert(_wsPathPlaceholderFor('/home/u/work') === '/home/u/' + __ELL__,
  'a POSIX path must yield its parent directory');
assert(_wsPathPlaceholderFor('/home/u/work/') === '/home/u/work/' + __ELL__,
  'a trailing separator stays part of the parent');
assert(_wsPathPlaceholderFor('') === 'pb_enter_ws_path', 'no path: localized hint');
assert(_wsPathPlaceholderFor('relative-only') === 'pb_enter_ws_path',
  'a path without a separator has no parent to hide behind');
console.log('ok');
"""


def test_the_workspace_path_placeholder_derives_from_the_shown_path(tmp_path):
    fn = _placeholder_fn()
    script = (
        _PLACEHOLDER_PROBE.replace("__FN__", fn)
        .replace("__BS__", repr(BS))
        .replace("__ELL__", repr(ELLIPSIS))
    )
    assert _run_node(tmp_path, "placeholder_probe.js", script).strip() == "ok"


# ---------------------------------------------------------------------------
# D — the docs must describe the dormant effort binding (greptile P2)
# ---------------------------------------------------------------------------


def test_docs_describe_the_dormant_effort_binding():
    """greptile P2 (ARCHITECTURE.md:803, 2026-10-09T00:51:28Z): the docs still
    told readers that ``_projectBindingsForNewSession`` forwards
    ``reasoning_effort`` and that a bound session changes the profile-wide
    preference. Both passages now describe the dormant binding."""
    arch = (REPO_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "`model_provider` and `reasoning_effort` as `newSession` options" not in arch
    assert "deliberately NOT `reasoning_effort`" in arch
    assert "**stored but dormant**" in arch
    assert "is applied through the **profile-wide** reasoning-effort preference" not in readme
    assert "is dormant" in readme


# ---------------------------------------------------------------------------
# C — 32px touch targets on narrow viewports
# ---------------------------------------------------------------------------


def test_the_bindings_row_controls_reach_32px_on_narrow_viewports():
    """UX re-gate 2026-10-08T23:39:33Z, item C: the remove button and the Default
    pill were 22px square, below the 32px minimum on a phone."""
    css = _read_static("style.css")
    # Locate the touch-target media block that targets the bindings row controls.
    marker = "@media (max-width:640px){"
    idx = css.index(marker, css.index(".project-bindings-ws-row .ws-row-remove:hover"))
    window = css[idx : idx + 600]
    assert ".project-bindings-ws-row .ws-row-remove{width:32px;height:32px;}" in window, (
        "the workspace remove button must be 32px on narrow viewports"
    )
    assert ".project-bindings-ws-row .ws-row-default{min-height:32px;" in window, (
        "the Default pill must reach 32px on narrow viewports"
    )
    # The desktop size stays untouched (the phone block is scoped, not global).
    assert ".project-bindings-ws-row .ws-row-remove{flex:0 0 auto;width:22px;height:22px;" in css
