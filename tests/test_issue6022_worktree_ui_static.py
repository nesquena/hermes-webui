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
