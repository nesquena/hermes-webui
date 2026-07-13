"""Behavioral regression for #4877 fresh blank boot workspace binding."""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOT_JS = ROOT / "static" / "boot.js"
NODE = shutil.which("node")

node_test = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _extract_async_function(source: str, name: str) -> str:
    marker = f"async function {name}("
    start = source.find(marker)
    if start < 0:
        marker = f"function {name}("
        start = source.find(marker)
    assert start >= 0, f"{name}() function must exist"
    brace = source.find("{", source.find(")", start))
    assert brace > start, f"{name}() function body must start"
    depth = 0
    in_string = None
    escaped = False
    in_line_comment = False
    in_block_comment = False
    for idx in range(brace, len(source)):
        ch = source[idx]
        nxt = source[idx + 1] if idx + 1 < len(source) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
            continue
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            continue
        if ch in ("'", '"', "`"):
            in_string = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : idx + 1]
    raise AssertionError(f"could not extract {name}()")


def _run_node(script: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", script],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _helper_driver(panel_mode: str, default_workspace: str | None, *, reject: bool = False) -> dict:
    helper_dep = _extract_async_function(BOOT_JS.read_text(encoding="utf-8"), "_prefillHasDraftText")
    helper = _extract_async_function(BOOT_JS.read_text(encoding="utf-8"), "_maybeBindFreshDefaultWorkspaceSession")
    default_workspace_repr = json.dumps(default_workspace)
    script = textwrap.dedent(
        f"""
        let calls = [];
        let shouldReject = {str(reject).lower()};
        let S = {{
          session: null,
          _profileDefaultWorkspace: {default_workspace_repr},
        }};
        var _workspacePanelMode = {json.dumps(panel_mode)};
        function newSession(a, b) {{
          calls.push({{arg0:a,arg1:b}});
          return shouldReject ? Promise.reject(new Error('bind-failed')) : Promise.resolve({{}});
        }}
        {helper_dep}
        {helper}
        _maybeBindFreshDefaultWorkspaceSession().then((bound)=>{{
          process.stdout.write(JSON.stringify({{bound,calls}}));
        }}).catch(err=>{{
          console.error(err && err.stack || err);
          process.exit(1);
        }});
        """
    )
    return _run_node(script)


@node_test
def test_blank_boot_open_panel_auto_binds_default_workspace_session():
    result = _helper_driver("browse", "/default-workspace")

    assert result["bound"] is True
    # worktree:false is explicit and load-bearing (#6022): the boot auto-bind
    # must opt out so a config-level worktree default can't mint a worktree
    # from merely opening the page.
    assert result["calls"] == [{"arg0": False, "arg1": {"awaitWorkspaceLoad": True, "worktree": False}}]


@node_test
def test_blank_boot_closed_panel_does_not_create_session():
    result = _helper_driver("closed", "/default-workspace")

    assert result["bound"] is False
    assert result["calls"] == []


@node_test
def test_blank_boot_without_default_workspace_does_not_create_session():
    result = _helper_driver("browse", None)

    assert result["bound"] is False
    assert result["calls"] == []


@node_test
def test_blank_boot_bind_failure_falls_back_cleanly():
    result = _helper_driver("browse", "/default-workspace", reject=True)

    assert result["bound"] is False
    assert len(result["calls"]) == 1


def test_no_saved_session_branch_restores_panel_pref_before_bind_attempt():
    src = BOOT_JS.read_text(encoding="utf-8")
    marker = "// no saved session - show empty state, wait for user to hit +"
    marker_idx = src.find(marker)
    assert marker_idx >= 0, "no-saved-session path not found"
    start = marker_idx
    sync_idx = src.find("syncWorkspacePanelState();", start)
    assert sync_idx > start, "no-saved-session path must still call syncWorkspacePanelState()"
    segment = src[start:sync_idx]
    fresh_pref = "if(_freshPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';"
    pref_idx = segment.find(fresh_pref)
    assert pref_idx >= 0, "no-saved-session path must restore panel preference"
    bind_call = "await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);"
    bind_idx = segment.find(bind_call)
    assert bind_idx >= 0, "no-saved-session path must attempt to bind after pref restore"
    assert pref_idx < bind_idx, "panel preference restoration must happen before bind attempt"


def test_ephemeral_blank_session_branch_restores_panel_pref_before_bind_attempt():
    src = BOOT_JS.read_text(encoding="utf-8")
    marker = "if(S.session && (S.session.message_count||0) === 0 && !_restoredInFlight && !_restoredHasDraft){"
    marker_idx = src.find(marker)
    assert marker_idx >= 0, "ephemeral blank-session path not found"
    return_idx = src.find("return;", marker_idx)
    assert return_idx > marker_idx, "ephemeral blank-session path must still return early"
    segment = src[marker_idx:return_idx]
    eph_pref = "if(_ephPanelPref&&!_isCompactWorkspaceViewport()) _workspacePanelMode='browse';"
    pref_idx = segment.find(eph_pref)
    assert pref_idx >= 0, "ephemeral blank-session path must restore panel preference"
    bind_call = "await _maybeBindFreshDefaultWorkspaceSession(prefillIntent);"
    bind_idx = segment.find(bind_call)
    assert bind_idx >= 0, "ephemeral blank-session path must attempt to bind before returning"
    assert pref_idx < bind_idx, "ephemeral panel preference restoration must happen before bind attempt"
