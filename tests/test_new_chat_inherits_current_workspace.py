"""A new chat inherits the CURRENT conversation's workspace, not a global pointer.

`newSession()` resolved the workspace of a new chat as:

    switchWs || S._profileDefaultWorkspace || S.session.workspace

`S._profileDefaultWorkspace` is profile-global and, on the server side,
`get_profile_default_workspace()` derives it from `last_workspace.txt`
(`api/workspace.py`), which EVERY conversation rewrites through
`set_last_workspace()` on `/api/chat/start` and `/api/session/update`.

With several conversations open on different workspaces, opening a new chat from
conversation A could therefore land on the workspace last touched by conversation
B. Expected order:

    switchWs || S.session.workspace || S._profileDefaultWorkspace

These tests extract the real expression from `static/sessions.js` and evaluate it
under Node with a simulated state, so they assert observable behavior rather than
the presence of a source string.
"""

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SESSIONS_JS = REPO / "static" / "sessions.js"

node = shutil.which("node")
node_test = pytest.mark.skipif(node is None, reason="node is required for this behavior test")


def _inherit_expression() -> str:
    """Extract the workspace-resolution expression from newSession()."""
    src = SESSIONS_JS.read_text(encoding="utf-8")
    match = re.search(r"const inheritWs=([^;]+);", src)
    assert match, "inheritWs expression not found in newSession()"
    return match.group(1)


def _eval_inherit(session_workspace, profile_default, switch_ws=None) -> dict:
    """Evaluate the extracted expression under Node with a simulated state."""
    expr = _inherit_expression()
    session_literal = (
        json.dumps({"workspace": session_workspace}) if session_workspace is not None else "null"
    )
    driver = f"""
    const S = {{
      session: {session_literal},
      _profileDefaultWorkspace: {json.dumps(profile_default)},
      _profileSwitchWorkspace: {json.dumps(switch_ws)},
    }};
    const switchWs = S._profileSwitchWorkspace;
    S._profileSwitchWorkspace = null;
    const inheritWs = {expr};
    console.log(JSON.stringify({{
      inheritWs: inheritWs,
      switchConsumed: S._profileSwitchWorkspace === null,
      profileDefaultPreserved: S._profileDefaultWorkspace,
    }}));
    """
    out = subprocess.run(
        [str(node), "-e", driver], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


@node_test
def test_new_chat_from_a_conversation_inherits_that_conversation_workspace():
    """A new chat opened from a conversation stays in that conversation's workspace."""
    res = _eval_inherit(
        session_workspace="/srv/projects/alpha",
        profile_default="/srv/projects/beta",
    )
    assert res["inheritWs"] == "/srv/projects/alpha", (
        "a new chat must inherit the current conversation's workspace rather than "
        f"the profile-global pointer (got: {res['inheritWs']})"
    )


@node_test
def test_blank_page_still_falls_back_to_profile_default():
    """With no conversation loaded, the profile default remains the fallback (#804/#5169)."""
    res = _eval_inherit(session_workspace=None, profile_default="/srv/projects/beta")
    assert res["inheritWs"] == "/srv/projects/beta"


@node_test
def test_profile_switch_workspace_still_wins_and_is_consumed():
    """The one-shot profile-switch flag keeps absolute priority and is consumed (#823)."""
    res = _eval_inherit(
        session_workspace="/srv/projects/alpha",
        profile_default="/srv/projects/beta",
        switch_ws="/srv/projects/switched",
    )
    assert res["inheritWs"] == "/srv/projects/switched"
    assert res["switchConsumed"] is True
    assert res["profileDefaultPreserved"] == "/srv/projects/beta"
