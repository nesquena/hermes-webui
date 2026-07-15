"""Static assertions for the frontend half of Issue #6022.

The three-value worktree contract only holds if system-minted sessions
(boot-time auto-bind, onboarding) explicitly opt OUT — otherwise a config
``worktree: true`` default would leak a fresh worktree + branch on every page
load, with nothing to reap it (#6023).
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def test_new_session_forwards_explicit_worktree_and_omits_absent():
    src = read("static/sessions.js")
    # Explicit true/false forwarded verbatim; absent key stays absent so the
    # server can apply the config default.
    assert (
        "if(options&&Object.prototype.hasOwnProperty.call(options,'worktree')) "
        "reqBody.worktree=!!options.worktree;" in src
    )
    # The old shape (only-true is ever sent) must be gone.
    assert "if(options&&options.worktree) reqBody.worktree=true;" not in src


def test_boot_auto_bind_sends_explicit_worktree_false():
    src = read("static/boot.js")
    bind = src[src.index("async function _maybeBindFreshDefaultWorkspaceSession") :]
    bind = bind[: bind.index("\n}\n")]
    assert "worktree: false" in bind


def test_onboarding_session_sends_explicit_worktree_false():
    src = read("static/onboarding.js")
    finish = src[src.index("async function _finishOnboarding") :]
    finish = finish[: finish.index("\n}\n")]
    assert "worktree: false" in finish


def test_profile_switch_session_sends_explicit_worktree_false():
    src = read("static/panels.js")
    assert (
        "await newSession(false, {awaitWorkspaceLoad: workspaceVisible, worktree: false});"
        in src
    )


def test_workspace_bind_prompts_send_explicit_worktree_false():
    # promptWorkspacePath + switchToWorkspace both auto-mint a session from a
    # blank page; each must opt out of the config default explicitly.
    src = read("static/panels.js")
    assert (
        src.count(
            "body:JSON.stringify({workspace:ws,worktree:false})"
        )
        >= 2
    ), "panels.js blank-page session mints must send worktree:false"
    # No panels.js session/new call may omit the worktree key.
    assert "body:JSON.stringify({workspace:ws})" not in src


def test_file_and_folder_creation_send_explicit_worktree_false():
    src = read("static/ui.js")
    for fn in ("async function promptNewFile", "async function promptNewFolder"):
        block = src[src.index(fn) :]
        block = block[: block.index("\n}\n")]
        assert "worktree:false" in block, f"{fn} must opt out of the config default"
    assert "body:JSON.stringify({workspace:ws})" not in src


def test_terminal_auto_session_sends_explicit_worktree_false():
    src = read("static/commands.js")
    assert "await newSession(false, {worktree: false});" in src


def test_no_bare_session_new_posts_remain_in_static_js():
    # Belt-and-suspenders: no static file may POST /api/session/new with a
    # body that has a workspace but silently omits the worktree key on an
    # auto-mint path. All known auto-mint sites are asserted above; this
    # catches future regressions of the same shape.
    for name in ("panels.js", "ui.js"):
        src = read(f"static/{name}")
        assert "body:JSON.stringify({workspace:ws})" not in src, (
            f"static/{name}: auto-mint session/new must pass explicit worktree:false"
        )


def test_deliberate_new_chat_paths_do_not_pin_worktree():
    # Sidebar "New Chat" and command paths must NOT pass an explicit worktree
    # value — they inherit the server-side config default by design.
    boot = read("static/boot.js")
    for line_no in (
        i
        for i, line in enumerate(boot.splitlines(), 1)
        if "await newSession();await renderSessionList();closeMobileSidebar();" in line
    ):
        line = boot.splitlines()[line_no - 1]
        assert "worktree" not in line
